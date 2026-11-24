"""Check that this repository keeps pins and patch queues instead of upstream copies."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from xml.etree import ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Q = load("repo_patch_queue", ROOT / "scripts/patch_queue.py")
S = load("repo_source_client", ROOT / "scripts/source.py")
I = load("repo_identity", ROOT / "scripts/identity.py")


class RepositoryLayoutTests(unittest.TestCase):
    def test_fixed_driver_and_module_inputs_match_the_identity(self):
        identity = I.from_repository()
        patch, = (ROOT / "patches/driver").glob("0002-*.patch")
        makefile = patch.read_text(encoding="utf-8")
        for name, key in (("KSU_VERSION", "driver_version"), ("KSU_VERSION_FULL", "driver_version_full")):
            self.assertEqual(re.findall(rf"^\+{name} := (\S+)$", makefile, re.M), [str(identity[key])])
        module = ROOT / "module/susfs4ksu"
        installer = (module / "customize.sh").read_text(encoding="utf-8")
        for var, key in (("ksuver", "driver_version"), ("SUSFS_VERSION_RAW", "susfs_version")):
            self.assertEqual(re.findall(rf'^\[ "\${var}" = "([^"]+)" \]', installer, re.M),
                             [str(identity[key])])
        props = dict(line.split("=", 1) for line in (module / "module.prop").read_text(encoding="utf-8").splitlines()
                     if "=" in line)
        for field in ("module_release", "module_local"):
            self.assertIn(identity[field], props["version"].split("-"))

    def test_scripts_use_locked_versions(self):
        identity = I.from_repository()
        keys = ("driver_version", "manager_version_code", "susfs_short", "kernel_version",
                "common_commit8", "driver_commit8", "susfs_commit8", "manager_commit8", "manager_git_count")
        terms = {str(identity[key]) for key in keys} | {Q.load_layout(ROOT)["upstreams"]["manager"]["tag"]}
        literals = re.compile("|".join(re.escape(term) for term in sorted(terms)) +
                              r"|(?<![a-zA-Z0-9])" + re.escape(identity["release_suffix"]) + r"(?![a-zA-Z0-9])")
        files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z",
                                         "--", "*.py", "*.sh", "*.yml", "*.cjs"], cwd=ROOT)
        violations = []
        for name in files.decode().split("\0"):
            # These two independent golden fixtures preserve historical output and SCM formatting.
            if not name or name.startswith("module/") or name in {"tests/test_identity.py", "tests/test_patch_queue.py"}:
                continue
            for n, line in enumerate((ROOT / name).read_text(encoding="utf-8").splitlines(), start=1):
                if literals.search(line):
                    violations.append(f"{name}:{n}")
        self.assertEqual(violations, [])

    def test_backup_urls_are_distinct_https_alternatives(self):
        layout = json.loads((ROOT / "ci-layout.json").read_bytes())
        driver = layout["upstreams"]["driver"]
        self.assertTrue(driver["backup_urls"])
        for bad in (["http://example.com/backup.git"], [driver["url"]], ["https://example.com/b.git"] * 2,
                    "https://example.com/b.git"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as temp:
                driver["backup_urls"] = bad
                (Path(temp) / "ci-layout.json").write_text(json.dumps(layout), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "backup_urls"):
                    Q.load_layout(temp)

    def test_every_queue_is_complete_documented_and_windows_safe(self):
        layout = Q.load_layout(ROOT)
        for queue in Q.QUEUES:
            with self.subTest(queue=queue):
                patches = Q.read_series(Q.queue_directory(ROOT, layout, queue))
                self.assertTrue(patches, f"{queue} queue is empty")
                for path in patches:
                    data = path.read_bytes()
                    self.assertNotIn(b"\r", data, f"{path.name} has CR line endings")
                    header, diff = Q.split_patch(data.decode("utf-8"))
                    Q.check_header(header, path.name)
                    for name in Q.patch_paths(diff):
                        S.windows_path(name)

    def test_manifest_pins_agree_with_the_upstream_lock(self):
        upstreams = Q.load_layout(ROOT)["upstreams"]
        manifest = ET.parse(ROOT / "configs/manifest.locked.xml").getroot()
        revisions = {node.get("path", node.get("name")): node.get("revision") for node in manifest.findall("project")}
        self.assertEqual(revisions["common"], upstreams["common"]["commit"])
        self.assertEqual(revisions["prebuilts/ndk-r23"], upstreams["ndk"]["commit"])
        release = Q.load_layout(ROOT)["kernel_release"]
        self.assertIn("-g" + upstreams["common"]["commit"][:12], release)

    def test_upstream_trees_are_not_vendored(self):
        self.assertFalse((ROOT / "src").exists(),
                         "Upstream sources come from ci-layout.json pins; edit patches/ with scripts/source.py")


if __name__ == "__main__":
    unittest.main()
