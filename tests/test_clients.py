#!/usr/bin/env python3
"""Exercise Windows clients with mocked GitHub transport and real hashes."""
import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


D = load("fuxi_download_client", ROOT / "scripts/download-artifact.py")
S = load("fuxi_source_client", ROOT / "scripts/source.py")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / "downloads"
        self.sha = "a" * 40
        self.payload = b"verified-flashable-test-zip"
        self.info = {"repository": D.DEFAULT_REPO, "source_commit": self.sha,
                     "source_sha": self.sha,
                     "artifacts": {D.DEFAULT_ASSET: {"sha256": hashlib.sha256(self.payload).hexdigest(),
                                                     "bytes": len(self.payload)}}}
        self.release = {"tag_name": "ci-aaaaaaaaaaaa-1-1", "html_url": "https://github.com/polarisxb/fuxi-kernel-ci/releases/tag/ci-aaaaaaaaaaaa-1-1",
                        "draft": False, "prerelease": False, "assets": []}
        self.responses = {}
        self.rebuild_metadata()

    def rebuild_metadata(self):
        info = json.dumps(self.info).encode()
        sums = (hashlib.sha256(self.payload).hexdigest() + "  " + D.DEFAULT_ASSET + "\n" +
                hashlib.sha256(info).hexdigest() + "  build-info.json\n").encode()
        self.release["assets"] = []
        for name, data in ((D.DEFAULT_ASSET, self.payload), ("build-info.json", info), ("SHA256SUMS", sums)):
            url = "https://github.com/polarisxb/fuxi-kernel-ci/releases/download/ci-aaaaaaaaaaaa-1-1/" + name
            self.release["assets"].append({"name": name, "browser_download_url": url, "size": len(data), "state": "uploaded"})
            self.responses[url] = data

    def transport(self, request, timeout):
        if request.full_url.startswith(D.API):
            return io.BytesIO(json.dumps(self.release).encode())
        self.assertNotIn("Authorization", request.headers, "API token must not go to public asset downloads")
        return io.BytesIO(self.responses[request.full_url])

    def run_download(self, **kwargs):
        with patch.object(D, "urlopen", self.transport):
            return D.download(output=self.output, **kwargs)

    def test_verified_payload_and_source_receipt(self):
        with patch.dict("os.environ", {"GH_TOKEN": "not-a-real-token"}):
            result = self.run_download(commit=self.sha[:12])
        self.assertEqual((self.output / D.DEFAULT_ASSET).read_bytes(), self.payload)
        self.assertEqual(result["source_commit"], self.sha)
        receipt = json.loads((self.output / (D.DEFAULT_ASSET + ".download.json")).read_text())
        self.assertEqual(receipt["sha256"], hashlib.sha256(self.payload).hexdigest())
        self.assertIn("releases/download/ci-", receipt["download_url"])

    def test_corrupt_zip_is_not_published(self):
        url = self.release["assets"][0]["browser_download_url"]
        self.responses[url] = b"corrupted"
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            self.run_download()
        self.assertFalse((self.output / D.DEFAULT_ASSET).exists())
        self.assertFalse(list(self.output.glob(".fuxi-download-*")))

    def test_wrong_source_commit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "requested commit"):
            self.run_download(commit="b" * 12)
        self.assertFalse(self.output.exists())

    def test_conflicting_source_metadata_is_rejected(self):
        self.info["source_sha"] = "b" * 40
        self.rebuild_metadata()
        with self.assertRaisesRegex(ValueError, "one complete source commit"):
            self.run_download()

    def test_metadata_tampering_is_rejected(self):
        self.responses[self.release["assets"][1]["browser_download_url"]] = b"{}"
        with self.assertRaisesRegex(ValueError, "metadata SHA256 mismatch"):
            self.run_download()

    def test_missing_binding_is_rejected(self):
        self.info["artifacts"] = {}
        self.rebuild_metadata()
        with self.assertRaisesRegex(ValueError, "bound to the build"):
            self.run_download()

    def test_ambiguous_asset_is_rejected(self):
        self.release["assets"].append(self.release["assets"][0])
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.run_download()

    def test_draft_and_prerelease_are_rejected(self):
        for key in ("draft", "prerelease"):
            with self.subTest(key=key):
                self.release[key] = True
                with self.assertRaisesRegex(ValueError, "completed published"):
                    self.run_download()
                self.release[key] = False

    def test_length_mismatch_is_rejected(self):
        self.release["assets"][0]["size"] += 1
        with self.assertRaisesRegex(ValueError, "length mismatch"):
            self.run_download()
        self.assertFalse((self.output / D.DEFAULT_ASSET).exists())

    def test_existing_download_is_preserved(self):
        self.output.mkdir()
        (self.output / D.DEFAULT_ASSET).write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            self.run_download()
        self.assertEqual((self.output / D.DEFAULT_ASSET).read_bytes(), b"existing")

    def test_unsafe_asset_names_and_urls_are_rejected(self):
        for name in ("../boot.zip", "C:boot.zip", "AUX.zip", "boot.zip.", "boot\\image.zip"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.run_download(name=name)
        self.release["assets"][0]["browser_download_url"] = "http://localhost/private"
        with self.assertRaisesRegex(ValueError, "GitHub HTTPS"):
            self.run_download()

    def test_duplicate_checksums_are_rejected(self):
        line = "a" * 64 + "  boot.zip\n"
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            D.checksums((line + line).encode())

    def test_wrong_tag_and_repository_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "requested immutable tag"):
            self.run_download(tag="different-tag")
        for name in ("../repo", "owner/..", "owner/repo/extra"):
            with self.subTest(repo=name), self.assertRaises(ValueError):
                D.validate_repo(name)

    def test_force_replaces_receipt_link_without_writing_its_target(self):
        self.output.mkdir()
        outside = Path(self.temp.name) / "unrelated.json"
        outside.write_text("keep me")
        (self.output / (D.DEFAULT_ASSET + ".download.json")).symlink_to(outside)
        self.run_download(force=True)
        self.assertEqual(outside.read_text(), "keep me")
        self.assertFalse((self.output / (D.DEFAULT_ASSET + ".download.json")).is_symlink())


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "configs").mkdir()
        for component in ("common", "KernelSU", "SukiSU"):
            (self.root / "src" / component).mkdir(parents=True)
        (self.root / "ci-layout.json").write_text(json.dumps({"kernel_common_sha": "a" * 40, "builtin_sha": "b" * 40, "manager_sha": "c" * 40}))
        (self.root / "configs/source-links.json").write_text(json.dumps({"manager_links": {"kernel/include/uapi": "../../uapi", "manager/app/src/main/cpp/uapi": "../../../../../uapi"}}))
        patched = patch.object(S, "ROOT", self.root)
        patched.start()
        self.addCleanup(patched.stop)

    def test_local_overlay_is_read_without_network(self):
        p = self.root / "src/common/fs/namei.c"
        p.parent.mkdir()
        p.write_bytes(b"edited-source")
        with patch.object(S, "fetch", side_effect=AssertionError("Unexpected network")):
            self.assertEqual(S.read("common/fs/namei.c"), b"edited-source")

    def test_declared_links_are_resolved_without_windows_symlinks(self):
        self.assertEqual(S.logical_path("common/drivers/kernelsu/supercall.c"), ("KernelSU", "kernel/supercall.c"))
        self.assertEqual(S.logical_path("KernelSU/uapi/feature.h"), ("KernelSU", "kernel/include/uapi/feature.h"))
        self.assertEqual(S.logical_path("SukiSU/kernel/include/uapi/feature.h"), ("SukiSU", "uapi/feature.h"))
        self.assertEqual(S.logical_path("manager/manager/app/src/main/cpp/uapi/feature.h"), ("SukiSU", "uapi/feature.h"))

    def test_import_downloads_and_creates_editable_source(self):
        with patch.object(S, "fetch", return_value=b"upstream-source") as fetch:
            p = S.materialize("common/fs/new.c")
        self.assertEqual(p.read_bytes(), b"upstream-source")
        fetch.assert_called_once_with("common", "fs/new.c")

    def test_existing_source_is_not_overwritten_by_default(self):
        p = self.root / "src/common/known.c"
        p.write_bytes(b"my-edits")
        with self.assertRaises(FileExistsError):
            S.materialize("common/known.c")
        self.assertEqual(p.read_bytes(), b"my-edits")

    def test_windows_reserved_paths_can_be_read_but_not_imported(self):
        with patch.object(S, "fetch", return_value=b"upstream AUX source"):
            self.assertEqual(S.read("common/include/soc/arc/aux.h"), b"upstream AUX source")
            with self.assertRaisesRegex(ValueError, "Windows"):
                S.materialize("common/include/soc/arc/aux.h")

    def test_case_collision_is_rejected_even_on_linux(self):
        p = self.root / "src/common/fs/Example.c"
        p.parent.mkdir()
        p.write_bytes(b"existing")
        with self.assertRaisesRegex(ValueError, "case collision"):
            S.materialize("common/fs/example.c")

    def test_traversal_and_symbolic_destination_are_rejected(self):
        for name in ("common/../../outside", "/common/fs/x.c", "common/.git/config", "C:/common/fs/x.c"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                S.materialize(name)
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "src/common/link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            S.materialize("common/link/x.c")

    def test_google_fallback_decodes_the_pinned_file(self):
        calls = []
        def transport(request, timeout):
            calls.append(request.full_url)
            if "raw.githubusercontent.com" in request.full_url:
                raise HTTPError(request.full_url, 404, "missing", {}, None)
            return io.BytesIO(base64.b64encode(b"known kernel source"))
        with patch.object(S, "urlopen", transport):
            self.assertEqual(S.fetch("common", "include/linux/test.h"), b"known kernel source")
        self.assertTrue(all("a" * 40 in url for url in calls))

    def test_component_directory_cannot_redirect_imports(self):
        base = self.root / "src/common"
        base.rmdir()
        outside = self.root / "outside"
        outside.mkdir()
        base.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes"):
            S.materialize("common/new.c")

    def test_source_root_cannot_redirect_imports(self):
        source_root = self.root / "src"
        source_root.rename(self.root / "saved-src")
        outside = self.root / "outside"
        (outside / "common").mkdir(parents=True)
        source_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "escapes"):
            S.materialize("common/new.c")


class ReleaseCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        text = (ROOT / ".github/workflows/build.yml").read_text()
        match = re.search(r"python3 - <<'PY'\n(.*?)\n\s+PY\n", text, re.S)
        self.assertIsNotNone(match)
        self.script = textwrap.dedent(match[1])
        self.sha = "a" * 40
        self.env = dict(os.environ, GITHUB_SHA=self.sha, GITHUB_REPOSITORY=D.DEFAULT_REPO,
                        GITHUB_REF="refs/heads/main", GITHUB_RUN_ID="1")
        for component, names, metadata_name in (
            ("kernel", (D.DEFAULT_ASSET, "SUSFS_R28_Fuxi230_40900_local2.zip"), "kernel-build-info.json"),
            ("native", ("Fuxi_SukiSU_40900_Zygisk_StatusFix.zip", "libksud.so"), "build-info.json"),
        ):
            folder = self.root / "collected" / component
            folder.mkdir(parents=True)
            for name in names:
                (folder / name).write_bytes((component + name).encode())
            records = {name: {"bytes": (folder / name).stat().st_size,
                              "sha256": hashlib.sha256((folder / name).read_bytes()).hexdigest()} for name in names}
            (folder / metadata_name).write_text(json.dumps({"source_sha": self.sha, "artifacts": records}))
            lines = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n" for p in folder.iterdir()]
            (folder / "SHA256SUMS").write_text("".join(lines))

    def collect(self):
        return subprocess.run(["python3", "-c", self.script], cwd=self.root, env=self.env,
                              text=True, capture_output=True, timeout=20)

    def test_published_metadata_binds_all_verified_payloads(self):
        result = self.collect()
        self.assertEqual(result.returncode, 0, result.stderr)
        dist = self.root / "release-dist"
        info = json.loads((dist / "build-info.json").read_text())
        self.assertEqual(info["source_commit"], self.sha)
        self.assertEqual(info["repository"], D.DEFAULT_REPO)
        sums = D.checksums((dist / "SHA256SUMS").read_bytes())
        for name, digest in sums.items():
            self.assertEqual(hashlib.sha256((dist / name).read_bytes()).hexdigest(), digest)

    def test_payload_corruption_between_jobs_is_rejected(self):
        path = self.root / "collected/kernel" / D.DEFAULT_ASSET
        path.write_bytes(b"bad transport payload")
        result = self.collect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("payload checksum mismatch", result.stderr)
        self.assertFalse((self.root / "release-dist" / D.DEFAULT_ASSET).exists())

    def test_wrong_component_commit_is_rejected(self):
        path = self.root / "collected/native/build-info.json"
        path.write_text(json.dumps({"source_sha": "b" * 40}))
        result = self.collect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("wrong source commit", result.stderr)

    def test_metadata_payload_binding_cannot_be_missing_or_wrong(self):
        folder = self.root / "collected/kernel"
        path = folder / "kernel-build-info.json"
        info = json.loads(path.read_text())
        del info["artifacts"][D.DEFAULT_ASSET]
        path.write_text(json.dumps(info))
        records = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
                   for p in folder.iterdir() if p.name != "SHA256SUMS"]
        (folder / "SHA256SUMS").write_text("".join(records))
        result = self.collect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from build metadata", result.stderr)
        self.assertFalse((self.root / "release-dist").exists())

    def test_rehashed_corruption_still_rejects_original_build_binding(self):
        folder = self.root / "collected/kernel"
        (folder / D.DEFAULT_ASSET).write_bytes(b"corrupt but freshly rehashed")
        records = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n"
                   for p in folder.iterdir() if p.name != "SHA256SUMS"]
        (folder / "SHA256SUMS").write_text("".join(records))
        result = self.collect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differs from build metadata", result.stderr)
        self.assertFalse((self.root / "release-dist").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
