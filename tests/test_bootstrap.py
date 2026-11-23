#!/usr/bin/env python3
"""Check bootstrap trust boundaries using temporary archives and real local Git."""
import importlib.util
from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bootstrap", ROOT / "scripts/bootstrap.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fuxi-bootstrap-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, members):
        path = self.root / "input.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            for name, kind, content in members:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    data = content.encode(); info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
                elif kind == "hardlink":
                    info.type = tarfile.LNKTYPE; info.linkname = content
                    archive.addfile(info)
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE; info.linkname = content
                    archive.addfile(info)
                elif kind == "fifo":
                    info.type = tarfile.FIFOTYPE; archive.addfile(info)
        return path

    def test_safe_hardlinks_preserve_full_reference_paths(self):
        archive = self.archive([("inputs/stock/modules/a.ko", "file", "ELF data"),
                                ("inputs/stock/modules/b.ko", "hardlink", "inputs/stock/modules/a.ko")])
        output = self.root / "workspace"; output.mkdir()
        B.extract_inputs(archive, output)
        first = output / "inputs/stock/modules/a.ko"
        second = output / "inputs/stock/modules/b.ko"
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.stat().st_ino, second.stat().st_ino)

    def test_archive_escape_and_unsupported_nodes_are_rejected(self):
        for name, kind, value in [("../outside", "file", "bad"),
                                  ("inputs/a", "hardlink", "../outside"),
                                  ("inputs/a", "symlink", "/etc/passwd"),
                                  ("inputs/pipe", "fifo", ""),
                                  ("unexpected/file", "file", "bad"),
                                  ("inputs/signing_key.pem", "file", "secret"),
                                  ("inputs/SIGNING_KEY.PEM", "file", "secret")]:
            with self.subTest(name=name, kind=kind):
                archive = self.archive([(name, kind, value)])
                with self.assertRaises((ValueError, tarfile.FilterError)):
                    B.extract_inputs(archive, self.root / "out")
        self.assertFalse((self.root / "outside").exists())

    def test_overlay_edits_and_new_source_files_are_used(self):
        source = self.root / "src"; source.mkdir()
        (source / "namei.c").write_text("fixed source\n")
        (source / "new.c").write_text("new source\n")
        dest = self.root / "dest"; dest.mkdir()
        (dest / "namei.c").write_text("old source\n")
        self.assertEqual(B.copy_overlay(source, dest), ["namei.c", "new.c"])
        self.assertEqual((dest / "namei.c").read_text(), "fixed source\n")
        self.assertEqual((dest / "new.c").read_text(), "new source\n")

    def test_overlay_cannot_write_through_existing_link(self):
        source = self.root / "src"; source.mkdir()
        (source / "file.c").write_text("replace\n")
        dest = self.root / "dest"; dest.mkdir()
        outside = self.root / "outside"; outside.write_text("keep\n")
        (dest / "file.c").symlink_to(outside)
        with self.assertRaises(ValueError): B.copy_overlay(source, dest)
        self.assertEqual(outside.read_text(), "keep\n")

    def test_declared_links_must_resolve_inside_workspace(self):
        workspace = self.root / "work"; (workspace / "real").mkdir(parents=True)
        B.restore_links(workspace, {"uapi": "real"})
        self.assertEqual((workspace / "uapi").resolve(), workspace / "real")
        for value in ("../../outside", "/etc", "missing"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                B.restore_links(workspace, {"bad": value})

    def test_driver_archive_must_hold_exactly_one_source_tree(self):
        archive = self.archive([("SukiSU-Ultra-e291/kernel/Makefile", "file", "obj-y += ksu.o\n"),
                                ("SukiSU-Ultra-e291/uapi", "symlink", "./kernel/include/uapi")])
        destination = self.root / "kernel/KernelSU"
        B.extract_single_tree(archive, destination)
        self.assertEqual((destination / "kernel/Makefile").read_text(), "obj-y += ksu.o\n")
        self.assertEqual(os.readlink(destination / "uapi"), "./kernel/include/uapi")
        with self.assertRaisesRegex(ValueError, "already exists"):
            B.extract_single_tree(archive, destination)
        archive = self.archive([("first/a.c", "file", "a\n"), ("second/b.c", "file", "b\n")])
        with self.assertRaisesRegex(ValueError, "Unexpected source archive layout"):
            B.extract_single_tree(archive, self.root / "other")
        self.assertFalse((self.root / "other").exists())

    def test_kernel_assembly_stops_when_the_manifest_and_lock_disagree(self):
        repo = self.root / "repo"
        (repo / "configs").mkdir(parents=True)
        (repo / "configs/manifest.locked.xml").write_text(
            '<manifest><remote name="a" fetch="https://example.com/"/><default remote="a"/>'
            '<project name="kernel/common" path="common" revision="' + "b" * 40 + '"/></manifest>')
        with mock.patch.object(B, "REPO", repo), mock.patch.object(B, "git_cache") as fetch:
            with self.assertRaisesRegex(ValueError, "pin different kernel/common"):
                B.prepare_kernel(self.root / "workspace", self.root / "cache")
        fetch.assert_not_called()

    def test_manifest_requires_exact_git_revision(self):
        path = self.root / "manifest.xml"
        path.write_text('<manifest><remote name="a" fetch="https://example.com/"/>'
                        '<default remote="a"/><project name="kernel/common" revision="main"/></manifest>')
        with self.assertRaises(ValueError): B.manifest_projects(path)
        path.write_text(path.read_text().replace('revision="main"', 'revision="' + "a" * 40 + '"'))
        self.assertEqual(B.manifest_projects(path)[0]["revision"], "a" * 40)

    @unittest.skipUnless(os.name == "posix", "git_cache serializes with fcntl locks on Linux CI")
    def test_real_git_checkout_is_pinned_without_modifying_mirror(self):
        mirror = self.root / "mirror"; mirror.mkdir()
        B.run(["git", "init", "-q", mirror])
        B.run(["git", "-C", mirror, "config", "user.email", "test@example.invalid"])
        B.run(["git", "-C", mirror, "config", "user.name", "test"])
        (mirror / "file").write_text("baseline\n")
        B.run(["git", "-C", mirror, "add", "file"])
        B.run(["git", "-C", mirror, "commit", "-qm", "baseline"])
        revision = B.run(["git", "-C", mirror, "rev-parse", "HEAD"])
        (mirror / "file").write_text("uncommitted mirror change\n")
        cache = B.git_cache("https://example.com/kernel", revision, self.root / "cache", mirror=mirror)
        dest = self.root / "checkout"
        B.checkout(cache, revision, dest)
        self.assertEqual((dest / "file").read_text(), "baseline\n")
        self.assertEqual((mirror / "file").read_text(), "uncommitted mirror change\n")
        self.assertEqual(B.run(["git", "-C", dest, "rev-parse", "HEAD"]), revision)

    def native_workspace(self):
        """Keep Git trust checks real; stub only external compiler/ELF tools."""
        repo = self.root / "repo"
        queue = repo / "patches/manager"
        queue.mkdir(parents=True)
        (queue / "series").write_bytes(b"0001-patch-the-fixture-uapi.patch\n")
        (queue / "0001-patch-the-fixture-uapi.patch").write_bytes(
            b"Subject: Patch the fixture UAPI\nUpstream-Status: Inappropriate [test fixture]\n"
            b"Drop-when: never\n\nApply the manager queue to the pinned checkout.\n---\n"
            b"diff --git a/uapi.h b/uapi.h\n--- a/uapi.h\n+++ b/uapi.h\n"
            b"@@ -1 +1 @@\n-original UAPI\n+patched UAPI\n")
        (repo / "module").mkdir()
        (repo / "module/module.prop").write_text("version=local2\n")
        (repo / "reference").mkdir()
        for name in ("manager-arm64-uapi.json", "arm64-uapi-probe.rs.in"):
            shutil.copy2(ROOT / "reference" / name, repo / "reference" / name)
        (repo / "workspace/scripts").mkdir(parents=True)
        shutil.copy2(ROOT / "workspace/scripts/verify-delivery.py", repo / "workspace/scripts/verify-delivery.py")
        work = self.root / "native"
        # Stands in for the pinned upstream Manager checkout; git_cache/checkout are stubbed.
        (work / "source").mkdir(parents=True)
        (work / "source/Cargo.lock").write_bytes(b"locked original dependencies\n")
        (work / "source/uapi.h").write_bytes(b"original UAPI\n")
        (work / "source/uapi-link").symlink_to("uapi.h")
        (work / "ndk").mkdir()
        (work / "ndk/tool").write_text("pinned tool\n")

        def commit_tree(path):
            B.run(["git", "init", "-q", path])
            B.run(["git", "-C", path, "config", "user.email", "test@example.invalid"])
            B.run(["git", "-C", path, "config", "user.name", "test"])
            B.run(["git", "-C", path, "add", "-A"])
            B.run(["git", "-C", path, "commit", "-qm", "Lock native test input identity"])
            return B.run(["git", "-C", path, "rev-parse", "HEAD"])

        ci_sha = commit_tree(repo)
        manager_tag_sha = commit_tree(work / "source")
        B.run(["git", "-C", work / "source", "commit", "--allow-empty", "-qm", "Keep native tip one commit after its historical tag"])
        manager_sha = B.run(["git", "-C", work / "source", "rev-parse", "HEAD"])
        ndk_sha = commit_tree(work / "ndk")
        expected = json.loads((repo / "reference/manager-arm64-uapi.json").read_text())
        self.probe_values = list(expected.values())
        self.native_git_count = 3715
        original_run = B.run

        def tool_run(args, cwd=None, env=None):
            args = list(map(str, args))
            if args[0] == "git" and cwd == work / "source":
                args = ["git", "-C", str(cwd), *args[1:]]
                cwd = None
            if args[:3] == ["git", "-C", str(work / "source")]:
                if args[3:] == ["rev-list", "--count", "HEAD"]:
                    return str(self.native_git_count)
                if args[3:] == ["describe", "--tags", "--always"]:
                    return "v4.2.0-1-g904c60d1"
            if args[0] == "readelf":
                return ("/system/bin/linker64" if args[1] == "-lW"
                        else "(NEEDED) Shared library: [libc.so]")
            if args[0] == "rustc":
                if "--version" in args:
                    return "rustc 1.91.1 (test fixture)"
                Path(args[-1]).write_bytes(b"object fixture")
                return ""
            if args[0].endswith("llvm-objcopy"):
                Path(args[2].split("=", 1)[1]).write_bytes(struct.pack("<144Q", *self.probe_values))
                return ""
            return original_run(args, cwd=cwd, env=env)

        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(B, "REPO", repo))
        patches.enter_context(mock.patch.object(B, "MANAGER_SHA", manager_sha))
        patches.enter_context(mock.patch.object(B, "MANAGER_TAG_SHA", manager_tag_sha))
        patches.enter_context(mock.patch.object(B, "NDK_SHA", ndk_sha))
        patches.enter_context(mock.patch.object(B, "run", side_effect=tool_run))
        patches.enter_context(mock.patch.dict(os.environ, {"CI_SOURCE_SHA": ci_sha, "GITHUB_ACTIONS": "true"}))
        with mock.patch.object(B, "git_cache"), mock.patch.object(B, "checkout"):
            report = B.prepare_native(work, self.root / "cache")
        report["passed"] = True
        (work / "bootstrap.json").write_text(json.dumps(report))
        B.verify_native_inputs(work, before_build=True)

        target = self.root / "target"
        build = target / "aarch64-linux-android/release/build/ksud-fixture"
        (build / "out").mkdir(parents=True)
        (build / "out/bindings.rs").write_text("// external compiler fixture\n")
        (build / "output").write_text("cargo:rustc-env=VERSION_CODE=40900\n"
                                      "cargo:rustc-env=VERSION_NAME=4.2.0-1-g904c60d1\n")
        header = bytearray(64)
        header[:6] = b"\x7fELF\x02\x01"
        struct.pack_into("<HH", header, 16, 3, 183)
        (target / "aarch64-linux-android/release/ksud").write_bytes(header + b"4.2.0-1-g904c60d1")
        return repo, work, target, self.root / "dist"

    def test_native_metadata_binds_completed_library_and_zip_without_circular_hash(self):
        repo, work, target, dist = self.native_workspace()
        original_lock = (work / "source/Cargo.lock").read_bytes()
        result = B.verify_native(work, target, dist)
        external = json.loads((dist / "native-build-info.json").read_text())
        self.assertEqual(result, external)
        self.assertEqual(external["source_sha"], B.run(["git", "-C", repo, "rev-parse", "HEAD"]))
        self.assertEqual(external["source_inputs"]["before"], external["source_inputs"]["after"])
        self.assertEqual(external["historical_tag"],
                         {"name": "v4.2.0", "commit": B.MANAGER_TAG_SHA, "distance": 1, "restored": True})
        fixture_patch = "patches/manager/0001-patch-the-fixture-uapi.patch"
        self.assertEqual(external["source_inputs"]["before"]["ci_source"]["source_files"][fixture_patch],
                         B.sha256(repo / fixture_patch))
        self.assertEqual((work / "source/uapi.h").read_bytes(), b"patched UAPI\n")
        self.assertEqual(external["source_inputs"]["before"]["native_source"]["uapi.h"]["sha256"],
                         B.sha256(work / "source/uapi.h"))
        self.assertEqual(external["source_inputs"]["before"]["native_source"]["uapi-link"],
                         {"type": "symlink", "target": "uapi.h"})
        names = {"libksud.so", "Fuxi_SukiSU_40900_Zygisk_StatusFix.zip"}
        self.assertEqual(set(external["artifacts"]), names)
        for name in names:
            path = dist / name
            self.assertEqual(external["artifacts"][name], {"sha256": B.sha256(path), "bytes": path.stat().st_size})
        with zipfile.ZipFile(dist / "Fuxi_SukiSU_40900_Zygisk_StatusFix.zip") as bundle:
            self.assertIsNone(bundle.testzip())
            self.assertEqual(set(bundle.namelist()), {"libksud.so", "native-build-info.json", "README.txt"})
            internal = json.loads(bundle.read("native-build-info.json"))
            self.assertEqual(set(internal["artifacts"]), {"libksud.so"})
            self.assertEqual(internal["artifacts"]["libksud.so"], external["artifacts"]["libksud.so"])
            self.assertEqual(bundle.read("libksud.so"), (dist / "libksud.so").read_bytes())
        checksums = {name: digest for digest, name in
                     (line.split("  ", 1) for line in (dist / "SHA256SUMS").read_text().splitlines())}
        self.assertEqual(set(checksums), names | {"native-build-info.json"})
        self.assertTrue(all(digest == B.sha256(dist / name) for name, digest in checksums.items()))
        self.assertEqual((work / "source/Cargo.lock").read_bytes(), original_lock)

    def test_native_rejects_spoofed_ci_commit_and_dirty_development_sources(self):
        repo, work, target, dist = self.native_workspace()
        with mock.patch.dict(os.environ, {"CI_SOURCE_SHA": "a" * 40}):
            with self.assertRaisesRegex(ValueError, "actual checkout"):
                B.verify_native(work, target, dist)
        (repo / "patches/manager/series").write_bytes(b"# uncommitted change\n0001-patch-the-fixture-uapi.patch\n")
        with self.assertRaisesRegex(ValueError, "clean Git checkout"):
            B.verify_native(work, target, dist)
        self.assertFalse(dist.exists())

    def test_native_rejects_clean_development_commit_changed_after_assembly(self):
        repo, work, target, dist = self.native_workspace()
        (repo / "patches/manager/series").write_bytes(b"# new committed queue\n0001-patch-the-fixture-uapi.patch\n")
        B.run(["git", "-C", repo, "add", "patches/manager/series"])
        B.run(["git", "-C", repo, "commit", "-qm", "Change test source identity"])
        actual = B.run(["git", "-C", repo, "rev-parse", "HEAD"])
        with mock.patch.dict(os.environ, {"CI_SOURCE_SHA": actual}):
            with self.assertRaisesRegex(ValueError, "changed since workspace assembly"):
                B.verify_native(work, target, dist)
        self.assertFalse(dist.exists())

    def test_native_rejects_assembled_lockfile_mode_link_and_added_file_changes(self):
        _, work, target, dist = self.native_workspace()
        lock = work / "source/Cargo.lock"
        original = lock.read_bytes()
        mode = lock.stat().st_mode
        mutations = [
            (lambda: lock.write_text("replacement dependencies\n"), lambda: lock.write_bytes(original)),
            (lambda: (work / "source/new.rs").write_text("new source\n"),
             lambda: (work / "source/new.rs").unlink()),
            (lambda: (work / "source/uapi-link").unlink(),
             lambda: (work / "source/uapi-link").symlink_to("uapi.h"))]
        if os.name == "posix":  # Windows has no executable bit to flip
            mutations.append((lambda: lock.chmod(mode | 0o100), lambda: lock.chmod(mode)))
        for mutation, restore in mutations:
            with self.subTest(mutation=mutation):
                mutation()
                with self.assertRaisesRegex(ValueError, "changed since workspace assembly"):
                    B.verify_native(work, target, dist)
                restore()
        self.assertFalse(dist.exists())

    def test_native_rejects_upstream_history_and_ndk_changes(self):
        _, work, target, dist = self.native_workspace()
        self.native_git_count = 1
        with self.assertRaisesRegex(ValueError, "original full upstream Git history"):
            B.verify_native(work, target, dist)
        self.native_git_count = 3715
        (work / "ndk/tool").write_text("modified tool\n")
        with self.assertRaisesRegex(ValueError, "Pinned NDK checkout changed"):
            B.verify_native(work, target, dist)
        self.assertFalse(dist.exists())

    def test_native_requires_untampered_snapshot_from_before_cargo(self):
        _, work, target, dist = self.native_workspace()
        record = work / "native-build-inputs.json"
        before = json.loads(record.read_text())
        before["before"]["native_source"].pop("Cargo.lock")
        record.write_text(json.dumps(before))
        with self.assertRaisesRegex(ValueError, "changed during Cargo compilation"):
            B.verify_native(work, target, dist)
        record.unlink()
        with self.assertRaisesRegex(ValueError, "before Cargo build missing"):
            B.verify_native(work, target, dist)
        self.assertFalse(dist.exists())

    def test_native_cargo_version_and_actual_arm64_uapi_must_match(self):
        _, work, target, dist = self.native_workspace()
        output = target / "aarch64-linux-android/release/build/ksud-fixture/output"
        original = output.read_text()
        output.write_text(original.replace("VERSION_CODE=40900", "VERSION_CODE=0"))
        with self.assertRaisesRegex(ValueError, "original Manager version identity"):
            B.verify_native(work, target, dist)
        output.write_text(original)
        self.probe_values[0] += 1
        with self.assertRaisesRegex(ValueError, "ARM64 Rust/C UAPI mismatch"):
            B.verify_native(work, target, dist)
        self.assertFalse(dist.exists())

    def manager_history(self, count=2):
        source = self.root / "manager-history"
        B.run(["git", "init", "-q", source])
        B.run(["git", "-C", source, "config", "user.email", "test@example.invalid"])
        B.run(["git", "-C", source, "config", "user.name", "test"])
        first = None
        for number in range(count):
            B.run(["git", "-C", source, "commit", "--allow-empty", "-qm", f"Preserve actual history step {number}"])
            if first is None:
                first = B.run(["git", "-C", source, "rev-parse", "HEAD"])
        head = B.run(["git", "-C", source, "rev-parse", "HEAD"])
        patches = ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(B, "MANAGER_SHA", head))
        patches.enter_context(mock.patch.object(B, "MANAGER_TAG_SHA", first))
        patches.enter_context(mock.patch.object(B, "MANAGER_GIT_COUNT", count))
        return source, first, head

    def test_absent_historical_tag_restores_the_real_graph_without_changing_history(self):
        source, first, head = self.manager_history()
        result = B.manager_tag_info(source, restore_missing=True)
        self.assertEqual(result, {"name": "v4.2.0", "commit": first, "distance": 1, "restored": True})
        self.assertEqual(B.run(["git", "-C", source, "rev-parse", "refs/tags/v4.2.0^{commit}"]), first)
        self.assertEqual(B.run(["git", "-C", source, "rev-parse", "HEAD"]), head)
        self.assertEqual(B.run(["git", "-C", source, "rev-list", "--count", "HEAD"]), "2")
        self.assertTrue(B.run(["git", "-C", source, "describe", "--tags", "--always"]).startswith("v4.2.0-1-g"))
        self.assertFalse(B.manager_tag_info(source)["restored"])

    def test_wrong_existing_historical_tag_is_never_overwritten(self):
        source, _, head = self.manager_history()
        B.run(["git", "-C", source, "tag", "v4.2.0", head])
        with self.assertRaisesRegex(ValueError, "points to a different commit"):
            B.manager_tag_info(source, restore_missing=True)
        self.assertEqual(B.run(["git", "-C", source, "rev-parse", "refs/tags/v4.2.0^{commit}"]), head)

    def test_historical_tag_with_missing_commit_or_nonancestor_stops(self):
        source, _, head = self.manager_history()
        with mock.patch.object(B, "MANAGER_TAG_SHA", "a" * 40):
            with self.assertRaisesRegex(ValueError, "tag commit is missing"):
                B.manager_tag_info(source, restore_missing=True)
        B.run(["git", "-C", source, "checkout", "--orphan", "unrelated"])
        B.run(["git", "-C", source, "commit", "--allow-empty", "-qm", "Keep unrelated ancestry separate"])
        unrelated = B.run(["git", "-C", source, "rev-parse", "HEAD"])
        B.run(["git", "-C", source, "checkout", "--detach", head])
        with mock.patch.object(B, "MANAGER_TAG_SHA", unrelated):
            with self.assertRaisesRegex(ValueError, "not an ancestor"):
                B.manager_tag_info(source, restore_missing=True)
        self.assertEqual(B.run(["git", "-C", source, "tag", "--list", "v4.2.0"]), "")

    def test_historical_tag_wrong_distance_or_history_count_stops(self):
        source, _, _ = self.manager_history(count=3)
        with self.assertRaisesRegex(ValueError, "exactly one commit behind"):
            B.manager_tag_info(source, restore_missing=True)
        with mock.patch.object(B, "MANAGER_GIT_COUNT", 3715):
            with self.assertRaisesRegex(ValueError, "original full upstream Git history"):
                B.manager_tag_info(source, restore_missing=True)
        self.assertEqual(B.run(["git", "-C", source, "tag", "--list", "v4.2.0"]), "")

    def test_missing_historical_tag_is_not_restored_after_preparation(self):
        _, work, target, dist = self.native_workspace()
        B.run(["git", "-C", work / "source", "tag", "-d", "v4.2.0"])
        with self.assertRaisesRegex(ValueError, "tag missing from the build checkout"):
            B.verify_native(work, target, dist)
        self.assertEqual(B.run(["git", "-C", work / "source", "tag", "--list", "v4.2.0"]), "")
        self.assertFalse(dist.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
