"""Exercise the watch with public transports mocked and real strict git apply."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import upstream_watch as W
from test_patch_queue import one_line_diff, patch_text

OLD, NEW = "a" * 40, "b" * 40


class WatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.layout = W.source.layout()
        for name, info in self.layout["upstreams"].items():
            info["commit"] = OLD
            queue = "common" if name == "susfs" else name
            if queue in self.layout["patch_queues"]:
                folder = self.root / self.layout["patch_queues"][queue]
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "series").write_bytes(b"")
        (self.root / "ci-layout.json").write_bytes(json.dumps(self.layout).encode())
        self.data = {}
        self.head = self.start(W, "branch_head", return_value=OLD)
        self.fetch = self.start(W.source, "fetch", side_effect=lambda n, p, commit=None: self.data.get((n, p, commit)))
        self.api = self.start(W, "public_json", return_value={"ahead_by": 3})

    def start(self, module, name, **kwargs):
        mocked = patch.object(module, name, **kwargs)
        self.addCleanup(mocked.stop)
        return mocked.start()

    def change(self, name):
        self.head.side_effect = lambda info: NEW if info == self.layout["upstreams"][name] else OLD

    def queue(self, name, *changes):
        folder = self.root / self.layout["patch_queues"][name]
        names = []
        for i, (path, before, after) in enumerate(changes, 1):
            filename = f"{i:04}-sample.patch"
            names.append(filename)
            (folder / filename).write_bytes(patch_text("test", one_line_diff(path, before, after)).encode())
        (folder / "series").write_bytes(("\n".join(names) + "\n").encode())

    def run_report(self):
        return W.report(self.root, "2026-01-01")

    def test_unchanged_heads_need_no_files_or_comparisons(self):
        report, updates = self.run_report()
        self.assertEqual(updates, {})
        self.assertEqual(report.count("No update (0 commits)."), 4)
        self.fetch.assert_not_called()
        self.api.assert_not_called()

    def test_manager_queue_applies_in_order(self):
        self.change("manager")
        self.queue("manager", ("a", "one", "two"), ("a", "two", "three"))
        self.data["manager", "a", NEW] = b"one\n"
        report, updates = self.run_report()
        self.assertEqual(updates, {"manager": NEW})
        self.assertEqual(report.count(".patch: applies"), 2)
        self.assertIn("Commits ahead: 3", report)

    def test_driver_reports_deleted_file_and_suggests_upstream_patch(self):
        self.change("driver")
        self.queue("driver", ("ksu.c", "one", "two"), ("kept.c", "one", "two"))
        self.data["driver", "kept.c", NEW] = b"two\n"
        self.api.side_effect = OSError("offline")
        report, updates = self.run_report()
        for phrase in ("Commits ahead: unknown", "0001-sample.patch: conflict", "Absent at head: ksu.c",
                       "0002-sample.patch: already-upstream", "- driver/0002-sample.patch"):
            self.assertIn(phrase, report)
        self.assertEqual(updates, {"driver": NEW})

    def test_module_reviews_stale_rules_and_keeps_checking(self):
        self.change("module")
        info = self.layout["upstreams"]["module"]
        self.start(W, "module_tree", return_value={"README.md": "100644", "sepolicy.rule": "100755"})
        self.queue("module", ("sepolicy.rule", "one", "two"))
        self.data["module", "sepolicy.rule", NEW] = b"one\n"
        self.data["module", "module.prop", NEW] = b"version=head\nversionCode=7\n"
        report, _ = self.run_report()
        for phrase in ("Review lock rule: sepolicy.rule", "crlf_to_lf file missing",
                       "exclude matches nothing: .github", "0001-sample.patch: applies",
                       "module.prop version: head", "module.prop versionCode: 7"):
            self.assertIn(phrase, report)
        self.assertNotIn("exclude matches nothing: README.md", report)
        for rule in info["exclude"]:
            self.assertNotIn(unittest.mock.call("module", rule, commit=NEW), self.fetch.call_args_list)

    def test_unavailable_module_tree_reports_unchecked_rules_and_replays_patches(self):
        self.change("module")
        self.start(W, "module_tree", side_effect=OSError("HTTP 403"))
        self.queue("module", ("file", "one", "two"))
        self.data["module", "file", NEW] = b"one\n"
        report, updates = self.run_report()
        self.assertIn("无法读取模块文件列表：HTTP 403；exclude 规则和文件权限没有检查", report)
        self.assertNotIn("exclude matches nothing", report)
        self.assertNotIn("Upstream Git mode:", report)
        self.assertIn("0001-sample.patch: applies", report)
        self.assertEqual(updates, {"module": NEW})

    def test_redirected_gbk_stdout_receives_utf8(self):
        markdown = "report ✓\n"
        updates = {"✓": NEW}
        with patch.object(W, "report", return_value=(markdown, updates)):
            for args in (["--dry-run"], ["--write", str(self.root / "out")]):
                with self.subTest(args=args), patch.object(sys, "argv", ["watch", *args]):
                    with io.TextIOWrapper(io.BytesIO(), encoding="gbk") as output, redirect_stdout(output):
                        W.main()
                        received = output.buffer.getvalue().decode("utf-8")
                    if args[0] == "--dry-run":
                        self.assertEqual(received, markdown)
                    else:
                        self.assertIn("✓", received)
                        self.assertEqual(json.loads(received), updates)

    def test_susfs_replays_official_patch_on_pinned_common_before_local_queue(self):
        self.change("susfs")
        info = self.layout["upstreams"]["susfs"]
        self.queue("common", ("file.c", "hooked", "local"))
        self.data["common", "file.c", None] = b"pinned\n"
        for path, remote in info["kernel_files"].items():
            self.data["susfs", remote, NEW] = b'#define SUSFS_VERSION "vTest"\n'
        self.data["susfs", info["kernel_patch"], NEW] = one_line_diff("file.c", "pinned", "hooked").encode()
        self.api.return_value = {"commits": [{}, {}]}
        report, _ = self.run_report()
        for phrase in ("Commits ahead: 2", "Official SUSFS installation: applies", "SUSFS_VERSION: vTest",
                       "0001-sample.patch: applies"):
            self.assertIn(phrase, report)
        self.data["common", "file.c", None] = b"diverged\n"
        report, _ = self.run_report()
        self.assertIn("Official SUSFS installation: conflict", report)
        self.assertIn("0001-sample.patch: conflict", report)

    def test_write_outputs_only_report_and_commit_map(self):
        self.change("manager")
        before = (self.root / "ci-layout.json").read_bytes()
        with patch.object(W.source, "ROOT", self.root), patch.object(sys, "argv", ["watch", "--write", str(self.root / "out")]):
            with io.TextIOWrapper(io.BytesIO(), encoding="gbk") as output, redirect_stdout(output):
                W.main()
                self.assertEqual(json.loads(output.buffer.getvalue()), {"manager": NEW})
        report, = (self.root / "out").iterdir()
        self.assertIn(b"## manager", report.read_bytes())
        self.assertEqual(before, (self.root / "ci-layout.json").read_bytes())


class TransportRetryTests(unittest.TestCase):
    INFO = {"url": "https://example.invalid/repo.git", "branch": "main"}

    def test_branch_head_retries_transport_failures_but_not_a_missing_branch(self):
        line = NEW + "\trefs/heads/main\n"
        answers = [subprocess.CalledProcessError(128, ["git"]), subprocess.TimeoutExpired(["git"], 90), line]
        with patch.object(W.subprocess, "check_output", side_effect=answers) as run, \
                patch.object(W.source, "sleep") as pause:
            self.assertEqual(W.branch_head(self.INFO), NEW)
        self.assertEqual(run.call_count, 3)
        self.assertEqual([call.args[0] for call in pause.call_args_list], list(W.source.RETRY_DELAYS))
        with patch.object(W.subprocess, "check_output", side_effect=subprocess.CalledProcessError(2, ["git"])) as run, \
                patch.object(W.source, "sleep") as pause:
            with self.assertRaises(subprocess.CalledProcessError):
                W.branch_head(self.INFO)
        self.assertEqual(run.call_count, 1)
        pause.assert_not_called()

    def test_public_json_uses_the_retrying_reader_with_a_size_limit(self):
        with patch.object(W.source, "read_url", return_value=b'{"ahead_by": 4}') as reader:
            self.assertEqual(W.public_json("https://api.github.com/x"), {"ahead_by": 4})
        self.assertEqual(reader.call_args.args, ("https://api.github.com/x", "fuxi-upstream-watch", W.JSON_LIMIT + 1))
        with patch.object(W.source, "read_url", return_value=b" " * (W.JSON_LIMIT + 1)):
            with self.assertRaisesRegex(ValueError, "exceeds"):
                W.public_json("https://api.github.com/x")
