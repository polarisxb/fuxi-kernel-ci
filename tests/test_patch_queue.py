#!/usr/bin/env python3
"""Check the patch-queue helpers shared by CI assembly and the Windows tool."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("patch_queue", ROOT / "scripts/patch_queue.py")
Q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(Q)


def write(path, text):
    """Write LF bytes; Path.write_text() would emit CRLF on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


def patch_text(subject, diff, status="Pending"):
    return (f"Subject: {subject}\nUpstream-Status: {status}\nDrop-when: never\n\n"
            f"Test patch.\n---\n{diff}")


def one_line_diff(path, old, new):
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1 +1 @@\n-{old}\n+{new}\n")


class PatchQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="fuxi-queue-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.queue = self.root / "queue"
        self.queue.mkdir()
        self.tree = self.root / "tree"
        self.tree.mkdir()

    def write_queue(self, patches, series=None):
        for name, text in patches.items():
            write(self.queue / name, text)
        lines = series if series is not None else list(patches)
        write(self.queue / "series", "# comment\n\n" + "".join(line + "\n" for line in lines))

    def test_series_applies_in_order_and_records_each_patch(self):
        write(self.tree / "a.c", "one\n")
        self.write_queue({"0001-first.patch": patch_text("first", one_line_diff("a.c", "one", "two")),
                          "0002-second.patch": patch_text("second", one_line_diff("a.c", "two", "three"))})
        records = Q.apply_series(self.tree, self.queue)
        self.assertEqual((self.tree / "a.c").read_bytes(), b"three\n")
        self.assertEqual([record["name"] for record in records], ["0001-first.patch", "0002-second.patch"])
        self.assertEqual(records[0]["sha256"], Q.sha256_bytes((self.queue / "0001-first.patch").read_bytes()))

    def test_check_series_advances_the_tree_and_leaves_upstream_changes_intact(self):
        write(self.tree / "a.c", "two\n")
        self.write_queue({"0001-upstream.patch": patch_text("upstream", one_line_diff("a.c", "one", "two")),
                          "0002-next.patch": patch_text("next", one_line_diff("a.c", "two", "three"))})
        self.assertEqual(Q.check_series(self.tree, self.queue), [
            {"name": "0001-upstream.patch", "status": "already-upstream"},
            {"name": "0002-next.patch", "status": "applies"}])
        self.assertEqual((self.tree / "a.c").read_bytes(), b"three\n")

    def test_check_series_continues_and_names_only_overlapping_earlier_conflicts(self):
        write(self.tree / "a", "new head\n")
        write(self.tree / "b", "other\n")
        self.write_queue({"0001-first.patch": patch_text("first", one_line_diff("a", "one", "two")),
                          "0002-second.patch": patch_text("second", one_line_diff("a", "two", "three")),
                          "0003-other.patch": patch_text("other", one_line_diff("b", "other", "changed")),
                          "0004-last.patch": patch_text("last", one_line_diff("a", "new head", "last"))})
        first, second, other, last = Q.check_series(self.tree, self.queue)
        self.assertEqual(first["status"], "conflict")
        self.assertIn("a", first["error"])
        self.assertNotIn("after_conflict_in", first)
        self.assertEqual(second["after_conflict_in"], [first["name"]])
        self.assertEqual(other, {"name": "0003-other.patch", "status": "applies"})
        self.assertEqual(last["after_conflict_in"], [first["name"], second["name"]])
        self.assertEqual((self.tree / "a").read_bytes(), b"last\n")

    def test_series_must_list_every_existing_patch_exactly_once(self):
        first = patch_text("first", one_line_diff("a.c", "one", "two"))
        for patches, series, message in (
                ({"0001-first.patch": first}, [], "missing from"),
                ({}, ["0001-first.patch"], "missing"),
                ({"0001-first.patch": first}, ["0001-first.patch", "0001-first.patch"], "twice"),
                ({"first.patch": first}, ["first.patch"], "Invalid patch name"),
                ({"0001-first.patch": first}, ["../0001-first.patch"], "Invalid patch name")):
            with self.subTest(series=series):
                for path in self.queue.iterdir():
                    path.unlink()
                self.write_queue(patches, series)
                with self.assertRaisesRegex(ValueError, message):
                    Q.read_series(self.queue)

    def test_patch_headers_are_required(self):
        write(self.tree / "a.c", "one\n")
        self.write_queue({"0001-first.patch": "Subject: x\n---\n" + one_line_diff("a.c", "one", "two")})
        with self.assertRaisesRegex(ValueError, "Upstream-Status"):
            Q.apply_series(self.tree, self.queue)
        self.assertEqual((self.tree / "a.c").read_bytes(), b"one\n")

    def test_a_patch_that_does_not_apply_stops_with_its_name(self):
        write(self.tree / "a.c", "different\n")
        self.write_queue({"0001-first.patch": patch_text("first", one_line_diff("a.c", "one", "two"))})
        with self.assertRaisesRegex(ValueError, "0001-first.patch does not apply"):
            Q.apply_series(self.tree, self.queue)

    def test_include_limits_application_to_requested_files(self):
        write(self.tree / "a.c", "one\n")
        diff = one_line_diff("a.c", "one", "two") + one_line_diff("b.c", "x", "y")
        self.write_queue({"0001-both.patch": patch_text("both", diff),
                          "0002-other.patch": patch_text("other", one_line_diff("b.c", "y", "z"))})
        Q.apply_series(self.tree, self.queue, include=["a.c"])
        self.assertEqual((self.tree / "a.c").read_bytes(), b"two\n")
        self.assertFalse((self.tree / "b.c").exists())

    def test_upto_applies_only_the_first_patches(self):
        write(self.tree / "a.c", "one\n")
        self.write_queue({"0001-first.patch": patch_text("first", one_line_diff("a.c", "one", "two")),
                          "0002-second.patch": patch_text("second", one_line_diff("a.c", "two", "three"))})
        Q.apply_series(self.tree, self.queue, upto=1)
        self.assertEqual((self.tree / "a.c").read_bytes(), b"two\n")

    def test_apply_never_resolves_paths_against_an_enclosing_repository(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        write(self.tree / "a.c", "one\n")
        Q.git_apply(self.tree, one_line_diff("a.c", "one", "two").encode(), "nested")
        self.assertEqual((self.tree / "a.c").read_bytes(), b"two\n")
        self.assertFalse((self.root / "a.c").exists())

    def test_unsafe_patch_paths_are_rejected(self):
        for path in ("../outside", "/etc/passwd", ".git/config", "a\\b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                Q.patch_paths(f"diff --git a/{path} b/{path}\n")

    def test_official_susfs_files_and_patch_are_installed(self):
        susfs = self.root / "susfs"
        write(susfs / "kernel_patches/fs/susfs.c", "susfs source\n")
        write(susfs / "kernel_patches/50.patch", one_line_diff("fs/namei.c", "upstream", "hooked"))
        write(self.tree / "fs/namei.c", "upstream\n")
        info = {"kernel_patch": "kernel_patches/50.patch", "kernel_files": {"fs/susfs.c": "kernel_patches/fs/susfs.c"}}
        record = Q.install_susfs(self.tree, susfs, info)
        self.assertEqual((self.tree / "fs/susfs.c").read_bytes(), b"susfs source\n")
        self.assertEqual((self.tree / "fs/namei.c").read_bytes(), b"hooked\n")
        self.assertEqual(record["kernel_files"], ["fs/susfs.c"])
        with self.assertRaisesRegex(ValueError, "replace an upstream"):
            Q.install_susfs(self.tree, susfs, info)

    def module_checkout(self):
        checkout = self.root / "module-checkout"
        files = {".git/HEAD": b"ref\n", ".github/workflows/build.yml": b"ci\n", "README.md": b"readme\n",
                 "tools/ksu_susfs_arm64": b"\x7fELF prebuilt", "tools/keep.txt": b"kept\n",
                 "sepolicy.rule": b"allow a b dir {search}\r\nallow c d dir {search}",
                 "webroot/index.html": b"<html>\r\n<body>\n</html>\r\n", "service.sh": b"#!/system/bin/sh\n"}
        for name, data in files.items():
            (checkout / name).parent.mkdir(parents=True, exist_ok=True)
            (checkout / name).write_bytes(data)
        info = {"exclude": [".github", "README.md", "tools/ksu_susfs_arm64"], "crlf_to_lf": ["sepolicy.rule"]}
        return checkout, info

    def test_module_tree_drops_excluded_paths_and_converts_listed_crlf_files(self):
        checkout, info = self.module_checkout()
        destination = self.root / "module"
        copied = Q.assemble_module(checkout, destination, info)
        self.assertEqual(copied, ["sepolicy.rule", "service.sh", "tools/keep.txt", "webroot/index.html"])
        self.assertEqual((destination / "sepolicy.rule").read_bytes(), b"allow a b dir {search}\nallow c d dir {search}")
        # Mixed endings are not listed, so the file is kept exactly as upstream has it.
        self.assertEqual((destination / "webroot/index.html").read_bytes(), b"<html>\r\n<body>\n</html>\r\n")
        self.assertFalse((destination / "README.md").exists() or (destination / ".github").exists())
        with self.assertRaisesRegex(ValueError, "already exists"):
            Q.assemble_module(checkout, destination, info)

    def test_module_rules_must_still_describe_the_pinned_tree(self):
        checkout, info = self.module_checkout()
        cases = [(dict(info, exclude=info["exclude"] + ["LICENSE"]), "match nothing"),
                 (dict(info, crlf_to_lf=["sepolicy.rule", "missing.json"]), "missing files"),
                 (dict(info, crlf_to_lf=["webroot/index.html"]), "no longer ends every line in CRLF")]
        for number, (rules, message) in enumerate(cases):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                Q.assemble_module(checkout, self.root / f"module-{number}", rules)

    def test_scmversion_comes_from_the_locked_release(self):
        makefile = "VERSION = 5\nPATCHLEVEL = 15\nSUBLEVEL = 194\nEXTRAVERSION =\n"
        commit = "f4321180a3973d19b626b3eef51871199e4e1fac"
        release = "5.15.194-android13-8-gf4321180a397-suki40900-susfs230-r2"
        self.assertEqual(Q.scmversion(makefile, release, commit), "-android13-8-gf4321180a397-suki40900-susfs230-r2\n")
        with self.assertRaisesRegex(ValueError, "does not start"):
            Q.scmversion(makefile.replace("194", "195"), release, commit)
        with self.assertRaisesRegex(ValueError, "pinned common commit"):
            Q.scmversion(makefile, release, "0" * 40)
        write(self.tree / "Makefile", makefile)
        Q.write_scmversion(self.tree, release, commit)
        self.assertEqual((self.tree / ".scmversion").read_bytes(), b"-android13-8-gf4321180a397-suki40900-susfs230-r2\n")
        with self.assertRaisesRegex(ValueError, "already has"):
            Q.write_scmversion(self.tree, release, commit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
