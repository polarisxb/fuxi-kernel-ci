#!/usr/bin/env python3
"""CI export boundaries; full binary/source validators have existing regressions."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_export", ROOT / "scripts/export-dist.py")
E = importlib.util.module_from_spec(spec)
spec.loader.exec_module(E)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="ci-export-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.release = self.work / "artifacts"
        self.release.mkdir(parents=True)
        self.reports = self.work / "reports/validation"
        (self.reports / "offline").mkdir(parents=True)
        for name in ("pre-regressions.json", "post-regressions.json", "clean-delivery-1.json",
                     "clean-delivery-2.json", "offline/verification.json"):
            (self.reports / name).write_text(json.dumps({"passed": True}))
        (self.release / "public-certificates").mkdir()
        (self.release / "public-certificates/signing_key.x509").write_bytes(b"public fixture")
        (self.release / "Image").write_bytes(b"test Image")
        (self.release / "kernel.release").write_text("test-r2\n")
        with zipfile.ZipFile(self.release / E.KERNEL_ZIP, "w") as archive:
            archive.writestr("Image", b"test Image")
        self.inventory = {"files": {E.KERNEL_ZIP: {
            "sha256": digest(self.release / E.KERNEL_ZIP),
            "bytes": (self.release / E.KERNEL_ZIP).stat().st_size}}}
        module = self.work / "module/susfs4ksu"
        module.mkdir(parents=True)
        (module / "module.prop").write_text("id=susfs4ksu\n")
        (module / "service.sh").write_text("#!/bin/sh\nexit 0\n")
        (module / "service.sh").chmod(0o755)
        self.module = module
        (self.release / "configs").mkdir()
        (self.release / "reports").mkdir()
        self.refresh_source_attestation()
        real_spec = importlib.util.spec_from_file_location(
            "export_file_safety", ROOT / "workspace/scripts/verify-delivery.py")
        real = importlib.util.module_from_spec(real_spec)
        real_spec.loader.exec_module(real)
        class Verifier:
            def verify_inventory(inner, release):
                return self.inventory, 4
            files_in = staticmethod(real.files_in)
        self.verifier = patch.object(E, "load_verifier", return_value=Verifier())
        self.verifier.start()
        self.addCleanup(self.verifier.stop)
        self.environment = patch.dict(os.environ, {"CI_SOURCE_SHA": "a" * 40,
                                                   "CI_SOURCE_REF": "refs/heads/test"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def export(self):
        return E.export(self.work, self.root / "dist", self.reports)

    def refresh_source_attestation(self):
        path = self.release / "configs/ci-source.json"
        path.write_text(json.dumps({
            "source_sha": "a" * 40,
            "local_validation_only": False,
            "source_files": {"module/" + item.relative_to(self.work / "module").as_posix(): digest(item)
                             for item in (self.work / "module").rglob("*")
                             if item.is_file() and not item.is_symlink()},
        }))
        (self.release / "reports/build-provenance.json").write_text(json.dumps({
            "inputs": {"configs": {"ci-source.json": {
                "type": "file", "sha256": digest(path), "executable": False}}}}))

    def test_only_named_public_payloads_exported(self):
        result = self.export()
        dist = self.root / "dist"
        self.assertEqual(set(path.name for path in dist.iterdir()),
                         {E.KERNEL_ZIP, E.MODULE_ZIP, "kernel-build-info.json", "SHA256SUMS"})
        self.assertEqual(result["source_sha"], "a" * 40)
        with zipfile.ZipFile(dist / E.MODULE_ZIP) as archive:
            self.assertEqual(archive.read("service.sh"), (self.module / "service.sh").read_bytes())
            self.assertEqual((archive.getinfo("service.sh").external_attr >> 16) & 0o777, 0o755)
        for line in (dist / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ", 1)
            self.assertEqual(digest(dist / name), expected)

    def test_failed_report_rejected_before_output(self):
        (self.reports / "post-regressions.json").write_text(json.dumps({"passed": False}))
        with self.assertRaisesRegex(ValueError, "post_regressions"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_invalid_source_identity_rejected(self):
        os.environ["CI_SOURCE_SHA"] = "not-a-commit"
        with self.assertRaisesRegex(ValueError, "source SHA"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_zip_changed_after_validation_rejected(self):
        with (self.release / E.KERNEL_ZIP).open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaisesRegex(ValueError, "changed before export"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_private_filename_rejected_and_partial_dist_removed(self):
        (self.module / "signing_key.pem").write_text("private fixture")
        self.refresh_source_attestation()
        with self.assertRaisesRegex(ValueError, "private key filename"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_private_content_rejected_and_partial_dist_removed(self):
        (self.module / "hidden.bin").write_bytes(b"-----BEGIN " + b"PRIVATE KEY-----\nfixture\n")
        self.refresh_source_attestation()
        with self.assertRaisesRegex(ValueError, "private key material"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_broken_symlink_rejected(self):
        (self.module / "unexpected").symlink_to("missing")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_module_changed_after_attestation_rejected(self):
        (self.module / "service.sh").write_text("changed after successful test/build")
        with self.assertRaisesRegex(ValueError, "Module sources differ"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_source_attestation_tampered_after_build_rejected(self):
        path = self.release / "configs/ci-source.json"
        source = json.loads(path.read_text())
        source["extra"] = "changed"
        path.write_text(json.dumps(source))
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_source_attestation_for_another_commit_rejected(self):
        os.environ["CI_SOURCE_SHA"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "publication commit"):
            self.export()
        self.assertFalse((self.root / "dist").exists())

    def test_existing_dist_not_overwritten(self):
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "keep").write_text("original")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.export()
        self.assertEqual((dist / "keep").read_text(), "original")


if __name__ == "__main__":
    unittest.main(verbosity=2)
