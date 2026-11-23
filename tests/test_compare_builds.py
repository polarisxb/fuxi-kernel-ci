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


def fixture(root, commit, *, image=b"kernel image", source=b"common", module=b"module"):
    origin = {"repository": "fixture/repo", "source_sha": commit, "run_id": commit[:4]}
    kernel = dict(origin, component="kernel", driver_version=1, manager_version=1,
                  susfs_version="v1", kernel_release="fixture", image_sha256=sha(image),
                  public_certificate_sha256=sha(b"cert"), source_identity={
                      "common.diff": sha(source), "SukiSU.diff": sha(b"driver"),
                      "integrated-source-manifest.json": sha(b"manifest")})
    native = dict(origin, component="ksud", version_code=1, version_name="fixture",
                  uapi_compared_values=2, uapi_all_equal=True)
    lib = b"native executable"
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

    def test_distinguishes_source_image_and_zip_entry_changes(self):
        fixture(self.new, "b" * 40, source=b"new patch", image=b"new image", module=b"new module")
        result = self.compare()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(set(json.loads(result.stdout)["differences"]), {
            "source/common.diff", "kernel/image_sha256", "kernel/kernel.zip/Image",
            "kernel/module.zip/module.prop"})

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
