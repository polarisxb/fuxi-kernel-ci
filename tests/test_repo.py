"""Check that this repository keeps pins and patch queues instead of upstream copies."""
import importlib.util
from pathlib import Path
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


class RepositoryLayoutTests(unittest.TestCase):
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
