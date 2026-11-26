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
import shutil
import sys
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


def lf(path, text):
    """Write LF bytes; Path.write_text() would emit CRLF on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


def queue_patch(subject, diff):
    return (f"Subject: {subject}\nUpstream-Status: Pending\nDrop-when: never\n\n"
            f"Fixture patch.\n---\n{diff}")


def hunk(path, old, new, context=()):
    lines = "".join(f" {line}\n" for line in context)
    count = len(context) + 1
    return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -1,{count} +1,{count} @@\n{lines}-{old}\n+{new}\n")


class SourceTests(unittest.TestCase):
    """Patch-queue views and edit sessions against an in-memory pinned upstream."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        layout = {"upstreams": {
            "common": {"commit": "a" * 40},
            "susfs": {"url": "https://gitlab.com/simonpunk/susfs4ksu.git", "commit": "d" * 40,
                      "kernel_patch": "kernel_patches/50.patch",
                      "kernel_files": {"fs/susfs.c": "kernel_patches/fs/susfs.c"}},
            "driver": {"url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git", "commit": "b" * 40},
            "manager": {"url": "https://github.com/SukiSU-Ultra/SukiSU-Ultra.git", "commit": "c" * 40},
            "ndk": {"commit": "e" * 40},
            "module": {"url": "https://github.com/sidex15/susfs4ksu-module.git", "commit": "f" * 40,
                       "exclude": ["README.md", "tools/ksu_susfs_arm64"], "crlf_to_lf": ["sepolicy.rule"]}},
            "patch_queues": {"common": "patches/common", "driver": "patches/driver", "manager": "patches/manager",
                             "module": "patches/module"}}
        lf(self.root / "ci-layout.json", json.dumps(layout))
        lf(self.root / "configs/source-links.json", json.dumps({"manager_links": {
            "kernel/include/uapi": "../../uapi", "manager/app/src/main/cpp/uapi": "../../../../../uapi"}}))
        self.upstream = {
            ("common", "fs/namei.c"): b"upstream\n",
            ("common", "fs/stat.c"): b"stat\nline\n",
            ("common", "include/soc/arc/aux.h"): b"upstream AUX source\n",
            ("susfs", "kernel_patches/fs/susfs.c"): b"susfs source\n",
            ("susfs", "kernel_patches/50.patch"): hunk("fs/namei.c", "upstream", "hooked").encode(),
            ("driver", "kernel/ksu.c"): b"driver\n",
            ("module", "sepolicy.rule"): b"allow a b dir {search}\r\nallow c d dir {search}\r\n",
            ("module", "service.sh"): b"#!/system/bin/sh\nlog=1\n",
            ("module", "README.md"): b"upstream readme\n",
        }
        for queue in ("driver", "manager", "module"):
            lf(self.root / "patches" / queue / "series", "")
        self.common = self.root / "patches/common"
        lf(self.common / "0001-fix-namei.patch", queue_patch("fix namei", hunk("fs/namei.c", "hooked", "fixed")))
        lf(self.common / "0002-tune-stat.patch", queue_patch("tune stat", hunk("fs/stat.c", "line", "tuned", ["stat"])))
        lf(self.common / "series", "0001-fix-namei.patch\n0002-tune-stat.patch\n")
        root_patch = patch.object(S, "ROOT", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.fetch_patch = patch.object(S, "fetch", side_effect=lambda name, path: self.upstream.get((name, path)))
        self.fetch_patch.start()
        self.addCleanup(self.fetch_patch.stop)

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.common.iterdir()}

    def test_read_shows_upstream_susfs_and_queue_or_the_pristine_file(self):
        self.assertEqual(S.read("common/fs/namei.c"), b"fixed\n")
        self.assertEqual(S.read("common/fs/susfs.c"), b"susfs source\n")
        self.assertEqual(S.read("common/fs/namei.c", upstream=True), b"upstream\n")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            S.read("common/fs/missing.c")

    def test_module_view_applies_the_bootstrap_rules_before_its_queue(self):
        module = self.root / "patches/module"
        lf(module / "0001-quiet-the-log.patch", queue_patch("quiet the log", hunk("service.sh", "log=1", "log=0",
                                                                                 ["#!/system/bin/sh"])))
        lf(module / "series", "0001-quiet-the-log.patch\n")
        self.assertEqual(S.read("module/sepolicy.rule"), b"allow a b dir {search}\nallow c d dir {search}\n")
        self.assertEqual(S.read("module/sepolicy.rule", upstream=True),
                         b"allow a b dir {search}\r\nallow c d dir {search}\r\n")
        self.assertEqual(S.read("module/service.sh"), b"#!/system/bin/sh\nlog=0\n")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            S.read("module/README.md")
        self.assertEqual(S.read("module/README.md", upstream=True), b"upstream readme\n")
        self.upstream[("module", "sepolicy.rule")] = b"allow a b dir {search}\nallow c d dir {search}\r\n"
        with self.assertRaisesRegex(ValueError, "crlf_to_lf"):
            S.read("module/sepolicy.rule")

    def test_edit_then_refresh_without_changes_rewrites_the_same_bytes(self):
        S.edit("common", "0001")
        S.refresh()  # store the hand-written fixture in canonical refreshed form first
        before = self.snapshot()
        tree = S.edit("common", "0001")
        self.assertEqual((tree / "fs/namei.c").read_bytes(), b"fixed\n")
        S.refresh()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root / ".work/edit").exists())

    def test_refresh_keeps_the_header_and_records_the_edit(self):
        tree = S.edit("common", "0002-tune-stat.patch")
        lf(tree / "fs/stat.c", "stat\nretuned\n")
        patch_file = S.refresh()
        text = patch_file.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("Subject: tune stat\nUpstream-Status: Pending\nDrop-when: never\n\nFixture patch.\n---\ndiff --git"))
        self.assertEqual(text.count("\n---\n"), 1)
        self.assertIn("+retuned\n", text)
        self.assertEqual(S.read("common/fs/stat.c"), b"stat\nretuned\n")

    def test_refresh_restores_everything_when_a_later_patch_stops_applying(self):
        lf(self.common / "0002-tune-stat.patch", queue_patch("tune stat", hunk("fs/namei.c", "fixed", "tuned")))
        before = self.snapshot()
        tree = S.edit("common", "0001")
        lf(tree / "fs/namei.c", "fixed differently\n")
        with self.assertRaisesRegex(ValueError, "(?s)0002-tune-stat.patch does not apply.*nothing was written"):
            S.refresh()
        self.assertEqual(self.snapshot(), before)
        S.discard()

    def test_new_patch_is_appended_with_a_header_to_complete(self):
        tree = S.new("common", "retune-stat", ["fs/stat.c"])
        self.assertEqual((tree / "fs/stat.c").read_bytes(), b"stat\ntuned\n")
        lf(tree / "fs/stat.c", "stat\ntuned\nmore\n")
        patch_file = S.refresh()
        self.assertEqual(patch_file.name, "0003-retune-stat.patch")
        self.assertTrue((self.common / "series").read_text().endswith("0003-retune-stat.patch\n"))
        self.assertIn("Subject: retune stat\nUpstream-Status: Pending\nDrop-when: TODO", patch_file.read_text())
        self.assertEqual(S.read("common/fs/stat.c"), b"stat\ntuned\nmore\n")

    def test_only_one_session_and_unchanged_or_crlf_edits_are_refused(self):
        tree = S.edit("common", "0002")
        with self.assertRaisesRegex(ValueError, "already open"):
            S.edit("common", "0001")
        (tree / "fs/stat.c").write_bytes(b"stat\r\ntuned\r\n")
        with self.assertRaisesRegex(ValueError, "CR line endings"):
            S.refresh()
        S.discard()
        tree = S.new("common", "nothing", ["fs/stat.c"])
        with self.assertRaisesRegex(ValueError, "equal the state before"):
            S.refresh()
        S.discard()
        self.assertFalse((self.root / ".work/edit").exists())

    def test_files_outside_the_session_are_not_slipped_into_the_patch(self):
        before = self.snapshot()
        tree = S.edit("common", "0001")
        lf(tree / "fs/namei.c.orig", "editor backup\n")
        with self.assertRaisesRegex(ValueError, "outside this patch.*namei.c.orig"):
            S.refresh()
        self.assertEqual(self.snapshot(), before)
        (tree / "fs/namei.c.orig").unlink()
        S.refresh()

    def test_windows_reserved_paths_can_be_read_but_not_edited(self):
        self.assertEqual(S.read("common/include/soc/arc/aux.h"), b"upstream AUX source\n")
        with self.assertRaisesRegex(ValueError, "Windows"):
            S.new("common", "aux", ["include/soc/arc/aux.h"])
        with self.assertRaisesRegex(ValueError, "case collision"):
            S.new("common", "case", ["fs/Stat.c", "fs/stat.c"])

    def test_traversal_and_symbolic_work_directory_are_rejected(self):
        for name in ("common/../../outside", "/common/fs/x.c", "common/.git/config", "C:/common/fs/x.c"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                S.logical_path(name)
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / ".work").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            S.edit("common", "0001")
        self.assertEqual(list(outside.iterdir()), [])

    def test_declared_links_are_resolved_without_windows_symlinks(self):
        self.assertEqual(S.logical_path("common/drivers/kernelsu/supercall.c"), ("driver", "kernel/supercall.c"))
        self.assertEqual(S.logical_path("KernelSU/uapi/feature.h"), ("driver", "kernel/include/uapi/feature.h"))
        self.assertEqual(S.logical_path("SukiSU/kernel/include/uapi/feature.h"), ("manager", "uapi/feature.h"))
        self.assertEqual(S.logical_path("manager/manager/app/src/main/cpp/uapi/feature.h"), ("manager", "uapi/feature.h"))

    def test_fetch_uses_only_pinned_known_endpoints(self):
        self.fetch_patch.stop()
        calls = []
        def transport(request, timeout):
            calls.append(request.full_url)
            if "raw.githubusercontent.com" in request.full_url:
                raise HTTPError(request.full_url, 404, "missing", {}, None)
            if "googlesource" in request.full_url:
                return io.BytesIO(base64.b64encode(b"known kernel source"))
            return io.BytesIO(b"raw file")
        with patch.object(S, "urlopen", transport):
            self.assertEqual(S.fetch("common", "include/linux/test.h"), b"known kernel source")
            self.assertIsNone(S.fetch("driver", "kernel/missing.c"))
            self.assertEqual(S.fetch("susfs", "kernel_patches/fs/susfs.c"), b"raw file")
        self.assertTrue(all("a" * 40 in url for url in calls[:2]))
        self.assertIn("https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/" + "b" * 40 + "/kernel/missing.c", calls)
        self.assertIn("https://gitlab.com/simonpunk/susfs4ksu/-/raw/" + "d" * 40 + "/kernel_patches/fs/susfs.c", calls)

    def test_fetch_reads_a_backup_after_the_upstream_lost_the_file(self):
        self.fetch_patch.stop()
        layout = json.loads((self.root / "ci-layout.json").read_bytes())
        layout["upstreams"]["driver"]["backup_urls"] = ["https://github.com/backup/SukiSU-Ultra.git"]
        lf(self.root / "ci-layout.json", json.dumps(layout))
        calls = []
        def transport(request, timeout):
            calls.append(request.full_url)
            if "/backup/" not in request.full_url:
                raise HTTPError(request.full_url, 404, "missing", {}, None)
            return io.BytesIO(b"backup copy")
        with patch.object(S, "urlopen", transport):
            self.assertEqual(S.fetch("driver", "kernel/ksu.c"), b"backup copy")
        self.assertEqual(calls, [f"https://raw.githubusercontent.com/{owner}/SukiSU-Ultra/{'b' * 40}/kernel/ksu.c"
                                 for owner in ("SukiSU-Ultra", "backup")])


class ReleaseCollectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        text = (ROOT / ".github/workflows/build.yml").read_text()
        match = re.search(r"python3 - <<'PY'\n(.*?)\n\s+PY\n", text, re.S)
        self.assertIsNotNone(match)
        self.script = textwrap.dedent(match[1])
        (self.root / "scripts").mkdir()
        shutil.copyfile(ROOT / "scripts/identity.py", self.root / "scripts/identity.py")
        shutil.copyfile(ROOT / "ci-layout.json", self.root / "ci-layout.json")
        self.sha = "a" * 40
        self.env = dict(os.environ, GITHUB_SHA=self.sha, GITHUB_REPOSITORY=D.DEFAULT_REPO,
                        GITHUB_REF="refs/heads/main", GITHUB_RUN_ID="1")
        for component, names, metadata_name in (
            ("kernel", (D.DEFAULT_ASSET, D.IDENTITY["module_zip"]), "kernel-build-info.json"),
            ("native", (D.IDENTITY["native_zip"], "libksud.so"), "build-info.json"),
        ):
            folder = self.root / "collected" / component
            folder.mkdir(parents=True)
            for name in names:
                (folder / name).write_bytes((component + name).encode())
            records = {name: {"bytes": (folder / name).stat().st_size,
                              "sha256": hashlib.sha256((folder / name).read_bytes()).hexdigest()} for name in names}
            (folder / metadata_name).write_bytes(json.dumps({
                "source_sha": self.sha, "artifacts": records, "signing": "release"}).encode())
            lines = [hashlib.sha256(p.read_bytes()).hexdigest() + "  " + p.name + "\n" for p in folder.iterdir()]
            (folder / "SHA256SUMS").write_text("".join(lines))

    def collect(self):
        return subprocess.run([sys.executable, "-B", "-c", self.script], cwd=self.root, env=self.env,
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

    def test_only_explicit_release_metadata_from_both_jobs_can_be_published(self):
        for component, filename in (("kernel", "kernel-build-info.json"), ("native", "build-info.json")):
            folder = self.root / "collected" / component
            path = folder / filename
            original = path.read_bytes()
            sums = folder / "SHA256SUMS"
            original_sums = sums.read_bytes()
            for mode in ("ephemeral", None):
                with self.subTest(component=component, mode=mode):
                    info = json.loads(original)
                    if mode is None:
                        del info["signing"]
                    else:
                        info["signing"] = mode
                    payload = json.dumps(info).encode()
                    path.write_bytes(payload)
                    sums.write_bytes(original_sums.replace(hashlib.sha256(original).hexdigest().encode(),
                                                          hashlib.sha256(payload).hexdigest().encode()))
                    result = self.collect()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("release signing required", result.stderr)
                    self.assertFalse((self.root / "release-dist").exists())
            path.write_bytes(original)
            sums.write_bytes(original_sums)

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
