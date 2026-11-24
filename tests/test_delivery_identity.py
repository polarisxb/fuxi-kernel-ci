"""Check identity loading without needing a Linux build workspace."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = load("delivery_identity_verifier", ROOT / "workspace/scripts/verify-delivery.py")
I = load("delivery_identity_generator", ROOT / "scripts/identity.py")


class DeliveryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.identity = I.from_repository()

    def fixture(self):
        for name in V.required_files(self.identity):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("fixture " + name).encode())
        self.identity["apk_sha256"] = V.sha256(self.root / self.identity["apk_name"])
        (self.root / "configs/build-identity.json").write_bytes(json.dumps(self.identity).encode())
        V.write_inventory(self.root, {
            "common_sha": self.identity["common_commit"],
            "builtin_sha": self.identity["driver_commit"],
            "susfs_sha": self.identity["susfs_commit"],
            "manager_version": self.identity["manager_version_code"],
            "driver_version": self.identity["driver_version"],
            "susfs_version": self.identity["susfs_version"],
            "uapi_version": 2, "profile_version": 4,
        })

    def test_repository_import_keeps_source_manifest_helpers_available(self):
        (self.root / "sample").write_bytes(b"source")
        self.assertEqual(V.tree_manifest(self.root)["sample"]["sha256"],
                         V.sha256(self.root / "sample"))
        with self.assertRaises(FileNotFoundError):
            V.read_identity(self.root)

    def test_candidate_identity_controls_required_names(self):
        self.identity["kernel_zip"] = "alternate-test-payload.zip"
        self.fixture()
        V.verify_inventory(self.root)
        (self.root / self.identity["kernel_zip"]).unlink()
        with self.assertRaisesRegex(ValueError, "Missing required"):
            V.verify_inventory(self.root)

    def test_identity_bytes_are_covered_by_inventory(self):
        self.fixture()
        V.verify_inventory(self.root)
        identity = self.root / "configs/build-identity.json"
        identity.write_bytes(identity.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "hash/size"):
            V.verify_inventory(self.root)

    def test_identity_filename_cannot_escape_the_package(self):
        self.fixture()
        self.identity["builtin_archive"] = "../outside.tar.gz"
        (self.root / "configs/build-identity.json").write_bytes(json.dumps(self.identity).encode())
        with self.assertRaisesRegex(ValueError, "identity filename"):
            V.read_identity(self.root)
