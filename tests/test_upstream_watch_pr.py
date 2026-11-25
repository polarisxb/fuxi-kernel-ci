"""Check the workflow contract and exercise pin preparation against a local Git index."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("watch_pr", ROOT / "scripts/upstream_watch_pr.py")
PR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PR)
DAY = "2026-01-05"


class WatchWorkflowTests(unittest.TestCase):
    def test_workflow_permissions_triggers_and_credentials(self):
        raw = (ROOT / ".github/workflows/upstream-watch.yml").read_text(encoding="utf-8")
        workflow = json.loads(raw)
        self.assertEqual(set(workflow["on"]), {"schedule", "workflow_dispatch"})
        self.assertEqual(workflow["on"]["schedule"], [{"cron": "17 4 * * 1"}])
        self.assertEqual(workflow["permissions"], {p: "write" for p in ("contents", "pull-requests", "actions")})
        self.assertNotIn("secrets.", raw.lower())
        self.assertNotIn("env", workflow)
        job, = workflow["jobs"].values()
        self.assertNotIn("env", job)
        steps = job["steps"]
        actions = [step for step in steps if "uses" in step]
        self.assertTrue(actions)
        for action in actions:
            self.assertRegex(action["uses"], r"^[^@]+@[0-9a-f]{40}$")
        checkout, = [a for a in actions if a["uses"].startswith("actions/checkout@")]
        self.assertIs(checkout["with"]["persist-credentials"], False)
        publish, = [step for step in steps if " publish " in step.get("run", "")]
        self.assertEqual(publish["env"]["GH_TOKEN"], "${{ github.token }}")
        for step in steps:
            if step != publish:
                self.assertNotRegex(json.dumps(step), r"GH_TOKEN|GITHUB_TOKEN|github\.token")

    def test_prepare_preserves_lock_bytes_and_checks_git_numstat(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / PR.LOCK
            before = (ROOT / PR.LOCK).read_bytes()
            lock.write_bytes(before)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "-c", "core.autocrlf=false", "add", "--", str(PR.LOCK)], cwd=root, check=True)
            folder = root / "input"
            reports = folder / "reports"
            reports.mkdir(parents=True)
            report = reports / (DAY + ".md")
            report.write_bytes("report ✓\n".encode())
            output = root / "output"
            updates = {"driver": "1" * 40, "manager": "2" * 40}
            (folder / "updates.json").write_bytes(json.dumps(updates).encode())
            env = dict(os.environ, GITHUB_OUTPUT=str(output))
            result = subprocess.run([os.sys.executable, "-B", SPEC.origin, "prepare", str(folder)],
                                    cwd=root, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = before
            for name, head in updates.items():
                old = PR.pins(json.loads(before))[name]
                expected = expected.replace(f'"commit": "{old}"'.encode(), f'"commit": "{head}"'.encode())
            self.assertEqual(lock.read_bytes(), expected)
            self.assertEqual(output.read_bytes(), f"day={DAY}\n".encode())
            self.assertEqual((root / PR.REPORTS / report.name).read_bytes(), report.read_bytes())

    def test_open_pr_deduplication_reads_the_same_repository_head(self):
        repo = "example/ci"
        head = {"ref": "bot/upstream-" + DAY, "sha": "a" * 40, "repo": {"full_name": repo}}
        pr = {"head": head, "base": {"ref": "main"}, "html_url": "https://example.test/pr/1"}
        fork = dict(pr, head=dict(head, repo={"full_name": "unrelated/fork"}))
        layout = {"upstreams": {"driver": {"commit": "1" * 40}, "module": {"commit": "2" * 40}}}
        desired = PR.pins(layout)
        for candidate, expected in ((desired, pr["html_url"]), (dict(desired, module="3" * 40), None)):
            with self.subTest(expected=expected), patch.object(PR, "output", side_effect=[
                    json.dumps([[fork], [pr]]), json.dumps(layout)]) as read:
                self.assertEqual(PR.matching_pr(repo, "main", candidate), expected)
                self.assertEqual(read.call_count, 2)
                self.assertTrue(read.call_args.args[-1].endswith("?ref=" + head["sha"]))

    def test_matching_pr_skips_push_creation_and_dispatch(self):
        with patch.dict(os.environ, GITHUB_REPOSITORY="example/ci", GITHUB_REF_NAME="main"), \
                patch.object(PR, "LOCK", ROOT / PR.LOCK), patch.object(PR, "matching_pr", return_value="existing"), \
                patch.object(PR.subprocess, "run") as run:
            PR.publish(DAY)
            run.assert_not_called()
