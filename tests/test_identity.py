"""Freeze the public names used before the version-lock migration."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/identity.py"
spec = importlib.util.spec_from_file_location("build_identity", SCRIPT)
identity = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity)


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.layout = json.loads((ROOT / "ci-layout.json").read_bytes())

    def test_original_public_names(self):
        expected = {
            "kernel_zip": "Fuxi_5.15.194_SukiSU_40900_SUSFS_2.3.0_r2_AnyKernel3.zip",
            "module_zip": "SUSFS_R28_Fuxi230_40900_local2.zip",
            "native_zip": "Fuxi_SukiSU_40900_Zygisk_StatusFix.zip",
            "kernel_string": "Fuxi GKI 5.15.194 - SukiSU 40900 + SUSFS 2.3.0 r2",
            "builtin_archive": "builtin-e2912817.tar.gz",
            "susfs_archive": "susfs-bca0d233.tar.gz",
            "manager_archive": "manager-904c60d1.tar.gz",
        }
        fields = identity.derive(self.layout)
        self.assertEqual({key: fields[key] for key in expected}, expected)
        output = subprocess.check_output([sys.executable, "-B", SCRIPT, "--json"], cwd=ROOT.parent)
        self.assertEqual(json.loads(output), fields)

    def test_kernel_release_must_match_its_inputs(self):
        original = self.layout["kernel_release"]
        for part in ("40900", "230", "r2", "f4321180a397"):
            with self.subTest(part=part), self.assertRaisesRegex(ValueError, "kernel_release"):
                self.layout["kernel_release"] = original.replace(part, "wrong")
                identity.derive(self.layout)

    def test_names_follow_the_lock(self):
        self.layout["identity"]["release_suffix"] = "next"
        self.layout["kernel_release"] = self.layout["kernel_release"].rsplit("-", 1)[0] + "-next"
        self.assertIn("_next_", identity.derive(self.layout)["kernel_zip"])

    def test_archive_and_payload_names_stay_inside_their_directory(self):
        for name in ("../escape.zip", "C:escape.zip", "bad\\name.zip", "bad/name.zip", "."):
            self.layout["names"]["module_zip"] = name
            with self.subTest(name=name), self.assertRaises(ValueError):
                identity.derive(self.layout)
