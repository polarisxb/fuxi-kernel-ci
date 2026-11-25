import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/compare-builds.py"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture(root, commit, *, image=b"kernel image", source=b"common", module=b"module",
            lib=b"native executable", native_tree=None, native_extra=None, signing=None):
    origin = {"repository": "fixture/repo", "source_sha": commit, "run_id": commit[:4]}
    if signing is not None:
        origin["signing"] = signing
    kernel = dict(origin, component="kernel", driver_version=1, manager_version=1,
                  susfs_version="v1", kernel_release="fixture", image_sha256=sha(image),
                  public_certificate_sha256=sha(b"cert"), source_identity={
                      "common.diff": sha(source), "SukiSU.diff": sha(b"driver"),
                      "integrated-source-manifest.json": sha(b"manifest")})
    tree = native_tree or {"Cargo.lock": {"type": "file", "sha256": sha(b"lock"), "executable": False}}
    inputs = {"ci_source": {"source_sha": commit, "source_ref": "refs/heads/" + commit[:4]},
              "native_source": tree, "manager_sha": "c" * 40, "git_count": 1, "version_code": 1}
    native = dict(origin, component="ksud", version_code=1, version_name="fixture",
                  uapi_compared_values=2, uapi_all_equal=True,
                  source_inputs={"before": inputs, "after": inputs},
                  upstream_sha="c" * 40, rustc="rustc 1.91.1 (fixture)", ndk_revision="23.1",
                  ndk_sha="d" * 40, android_api=26, elf_architecture="AArch64",
                  interpreter="/system/bin/linker64", needed=["libc.so"], uapi_version=2,
                  profile_version=4, profile_bytes=784, actual_build=True,
                  official_signed_apk=False, device_operations=False, schema_version=1)
    native.update(native_extra or {})
    lib_record = {"bytes": len(lib), "sha256": sha(lib)}
    native["artifacts"] = {"libksud.so": lib_record}
    archives = {"kernel": {"kernel.zip": {"Image": image}, "module.zip": {"module.prop": module}},
                "native": {"userspace.zip": {"libksud.so": lib, "README.txt": b"fixture",
                                             "native-build-info.json": json.dumps(native).encode()}}}
    for folder, info in (("kernel", kernel), ("native", native)):
        directory = root / folder
        directory.mkdir(parents=True, exist_ok=True)
        info.setdefault("artifacts", {})
        if folder == "native":
            (directory / "libksud.so").write_bytes(lib)
        for name, entries in archives[folder].items():
            path = directory / name
            with zipfile.ZipFile(path, "w") as archive:
                for filename, data in entries.items():
                    archive.writestr(filename, data)
            info["artifacts"][name] = {"bytes": path.stat().st_size, "sha256": sha(path.read_bytes())}
        (directory / (folder + "-build-info.json")).write_bytes(json.dumps(info).encode())


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.old = Path(temp.name) / "baseline"
        self.new = Path(temp.name) / "candidate"
        fixture(self.old, "a" * 40)
        fixture(self.new, "b" * 40)

    def compare(self):
        return subprocess.run([sys.executable, "-B", SCRIPT, self.old, self.new,
                               "--baseline-sha", "aaaaaaa", "--candidate-sha", "bbbbbbb"],
                              capture_output=True, text=True)

    def test_different_build_origins_can_produce_equivalent_payloads(self):
        result = self.compare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["differences"], {})
        self.assertEqual(json.loads(result.stdout)["not_reproducible"], {})

    def test_compare_old_release_metadata_with_explicit_modes(self):
        fixture(self.new, "b" * 40, signing="release")
        self.assertEqual(self.compare().returncode, 0)
        fixture(self.new, "b" * 40, signing="ephemeral")
        result = self.compare()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)["differences"]), {"kernel/signing", "native/signing"})

    def test_reordered_native_library_from_identical_inputs_is_reported_not_failed(self):
        fixture(self.new, "b" * 40, lib=b"native executable, constants reordered")
        result = self.compare()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["differences"], {})
        self.assertEqual(set(report["not_reproducible"]),
                         {"native/libksud.so", "native/userspace.zip/libksud.so"})

    def test_native_library_differences_fail_when_its_inputs_differ(self):
        for changed in ({"native_tree": {"Cargo.lock": {"type": "file", "sha256": sha(b"other lock"),
                                                        "executable": False}}},
                        {"native_extra": {"rustc": "rustc 1.92.0 (fixture)"}},
                        {"native_extra": {"official_signed_apk": True}}):
            with self.subTest(changed=changed):
                fixture(self.new, "b" * 40, lib=b"different library", **changed)
                result = self.compare()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("native/inputs", json.loads(result.stdout)["differences"])

    def test_native_inputs_must_be_unchanged_during_each_build(self):
        path = self.new / "native/native-build-info.json"
        record = json.loads(path.read_bytes())
        record["source_inputs"]["after"] = dict(record["source_inputs"]["after"], git_count=2)
        path.write_bytes(json.dumps(record).encode())
        self.assertEqual(self.compare().returncode, 2)

    def test_distinguishes_source_image_and_zip_entry_changes(self):
        fixture(self.new, "b" * 40, source=b"new patch", image=b"new image", module=b"new module")
        result = self.compare()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)["differences"]), {
            "source/common.diff", "kernel/image_sha256", "kernel/kernel.zip/Image",
            "kernel/module.zip/module.prop"})

    def test_gh_download_directories_must_be_unambiguous_and_match_the_commit(self):
        for root, sha in ((self.old, "a" * 40), (self.new, "b" * 40)):
            for folder in ("kernel", "native"):
                (root / folder).rename(root / f"fuxi-{folder}-{sha}")
        result = self.compare()
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.old / "kernel").mkdir()
        self.assertEqual(self.compare().returncode, 2)
        (self.old / "kernel").rmdir()
        (self.old / ("fuxi-kernel-" + "a" * 40)).rename(self.old / ("fuxi-kernel-" + "c" * 40))
        self.assertEqual(self.compare().returncode, 2)

    def test_incomplete_or_corrupt_artifacts_are_rejected(self):
        path = self.new / "kernel/kernel-build-info.json"
        original = path.read_bytes()
        record = json.loads(original)
        del record["source_identity"]["common.diff"]
        path.write_bytes(json.dumps(record).encode())
        self.assertEqual(self.compare().returncode, 2)
        path.write_bytes(original)
        with (self.new / "kernel/kernel.zip").open("ab") as stream:
            stream.write(b"modified")
        self.assertEqual(self.compare().returncode, 2)
