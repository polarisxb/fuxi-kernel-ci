"""Check that the imported baseline can be reproduced by an actual Git clone."""
import json
from pathlib import Path
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositorySourceTests(unittest.TestCase):
    def test_imported_source_files_are_tracked_and_present(self):
        snapshot = json.loads((ROOT / "reference/source-snapshot.json").read_text())
        tracked = set(subprocess.check_output(
            ["git", "-C", str(ROOT), "ls-files", "-z"], text=True).split("\0"))
        missing_git = sorted(set(snapshot) - tracked)
        missing_disk = sorted(name for name in snapshot if not (ROOT / name).is_file())
        self.assertFalse(missing_git, f"Imported sources omitted by Git: {missing_git}")
        self.assertFalse(missing_disk, f"Imported sources missing from checkout: {missing_disk}")


if __name__ == "__main__":
    unittest.main()
