#!/usr/bin/env python3
"""Fault injection for source/output binding; no real builds or device operations."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pipeline_provenance_tests', ROOT / 'scripts/build_provenance.py')
P = importlib.util.module_from_spec(spec)
spec.loader.exec_module(P)


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='fuxi-provenance-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(lambda: P.abort(self.root))
        self.out = self.root / 'out/sukisu/common'
        for path in ('kernel/common', 'kernel/KernelSU', 'kernel/shared', 'configs', 'scripts', 'patches', 'reports'):
            (self.root / path).mkdir(parents=True)
        (self.root / 'kernel/common/test.c').write_text('original source\n')
        (self.root / 'kernel/KernelSU/driver.c').write_text('original driver\n')
        (self.root / 'kernel/shared/tool').write_text('locked tool\n')
        shared = self.root / 'kernel/shared'
        subprocess.run(['git', 'init', '-q', str(shared)], check=True)
        subprocess.run(['git', '-C', str(shared), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(shared), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                        'commit', '-qm', 'Retain deterministic shared input fixture'], check=True)
        sha = subprocess.check_output(['git', '-C', str(shared), 'rev-parse', 'HEAD'], text=True).strip()
        (self.root / 'configs/locked-workspace-inputs.json').write_text(json.dumps([
            {'path': 'shared', 'sha': sha, 'shared_readonly': True}]))
        (self.root / 'configs/tool-input-sha256.json').write_text(json.dumps({
            'shared/tool': P.verifier().sha256(shared / 'tool')}))
        for name in P.BUILD_SCRIPTS:
            (self.root / 'scripts' / name).write_text('build script fixture\n')
        (self.root / 'scripts/run-build.sh').chmod(0o755)
        for name in P.PATCHES:
            (self.root / 'patches' / name).write_text('source patch fixture\n')
        self.make_outputs()

    def make_outputs(self):
        for name, relative in P.OUTPUTS.items():
            path = self.out / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('test-release\n' if name == 'kernel.release' else 'output for ' + name)
        for name in ('zram.ko', 'zsmalloc.ko'):
            path = self.out.parent / 'staging/lib/modules/test-release/kernel' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('signed fixture ' + name)

    def complete(self):
        P.begin(self.root, {'argv': ['mock-build']})
        result = P.finish(self.root, self.out)
        P.abort(self.root)
        return result

    def test_missing_record_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Missing successful'):
            P.verify_workspace(self.root, self.out)

    def test_success_binds_inputs_and_outputs(self):
        record = self.complete()
        self.assertEqual(P.verify_workspace(self.root, self.out), record)
        self.assertNotIn('signing_key.pem', json.dumps(record))

    def test_new_attempt_invalidates_success_before_work(self):
        self.complete()
        P.begin(self.root, {'argv': ['next-build']})
        self.assertFalse(P.state_paths(self.root)[2].exists())
        with self.assertRaisesRegex(ValueError, 'active or interrupted'):
            P.verify_workspace(self.root, self.out)

    def test_frozen_inputs_unwritable_and_modes_restored_on_abort(self):
        files = P.freeze_paths(self.root)
        before = {f: stat.S_IMODE(f.stat().st_mode) for f in files}
        P.begin(self.root, {'argv': ['mock-build']})
        for f in files:
            self.assertFalse(f.stat().st_mode & 0o222, str(f))
        if os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                (self.root / 'kernel/common/test.c').write_text('forbidden edit')
        P.abort(self.root)
        self.assertEqual(before, {f: stat.S_IMODE(f.stat().st_mode) for f in files})
        self.assertFalse(P.state_paths(self.root)[2].exists())

    def test_input_changed_during_build_rejected(self):
        P.begin(self.root, {'argv': ['mock-build']})
        source = self.root / 'kernel/common/test.c'
        source.chmod(0o644)
        source.write_text('changed input\n')
        source.chmod(0o444)
        with self.assertRaisesRegex(ValueError, 'changed during'):
            P.finish(self.root, self.out)
        P.abort(self.root)
        self.assertFalse(P.state_paths(self.root)[2].exists())

    def test_source_changed_after_success_rejected(self):
        self.complete()
        (self.root / 'kernel/common/test.c').write_text('new source but old image\n')
        with self.assertRaisesRegex(ValueError, 'Inputs differ'):
            P.verify_workspace(self.root, self.out)

    def test_patch_changed_after_success_rejected(self):
        self.complete()
        (self.root / 'patches/common-final.diff').write_text('new patch\n')
        with self.assertRaisesRegex(ValueError, 'Inputs differ'):
            P.verify_workspace(self.root, self.out)

    def test_output_changed_after_success_rejected(self):
        self.complete()
        (self.out / 'vmlinux').write_text('replaced binary\n')
        with self.assertRaisesRegex(ValueError, 'Outputs differ'):
            P.verify_workspace(self.root, self.out)

    def test_dirty_shared_dependency_rejected_without_chmod(self):
        tool = self.root / 'kernel/shared/tool'
        mode = stat.S_IMODE(tool.stat().st_mode)
        tool.write_text('dirty shared input\n')
        with self.assertRaisesRegex(ValueError, 'uncommitted changes'):
            P.begin(self.root, {'argv': ['mock-build']})
        self.assertEqual(stat.S_IMODE(tool.stat().st_mode), mode)
        self.assertFalse(P.state_paths(self.root)[0].exists())
        self.assertFalse(P.state_paths(self.root)[2].exists())

    def test_failed_actual_command_restores_modes_and_cannot_publish(self):
        original = P.freeze_paths(self.root)
        before = {f: stat.S_IMODE(f.stat().st_mode) for f in original}
        # The test preflight is a zero-return mock. The underlying command is
        # really executed and fails; it may not produce a success record.
        (self.root / 'scripts/preflight.py').write_text('raise SystemExit(0)\n')
        result = P.run_build(self.root, ['/bin/sh', '-c', 'exit 7'])
        self.assertEqual(result, 7)
        self.assertEqual(before, {f: stat.S_IMODE(f.stat().st_mode) for f in original})
        self.assertFalse(P.state_paths(self.root)[2].exists())

    def test_successful_command_lifecycle_and_delivery_binding(self):
        (self.root / 'scripts/preflight.py').write_text('raise SystemExit(0)\n')
        self.assertEqual(P.run_build(self.root, ['/bin/sh', '-c', 'exit 0']), 0)
        record = P.verify_workspace(self.root, self.out)
        package = self.root / 'delivery'
        for name in ('configs', 'scripts'):
            shutil.copytree(self.root / name, package / name)
        (package / 'reports').mkdir()
        (package / 'source-diffs').mkdir()
        (package / 'reports/build-provenance.json').write_text(json.dumps(record))
        (package / 'source-diffs/integrated-source-manifest.json').write_text(json.dumps(record['inputs']['sources']))
        for name, published in zip(P.PATCHES, ('common.diff', 'SukiSU.diff')):
            shutil.copy2(self.root / 'patches' / name, package / 'source-diffs' / published)
        for name, relative in P.OUTPUTS.items():
            target = package / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.out / relative, target)
        for name in ('zram.ko', 'zsmalloc.ko'):
            target = package / 'matching-gki-modules' / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(self.out.parent / 'staging/lib/modules/test-release/kernel' / name, target)
        self.assertTrue(P.verify_delivered(package))
        for relative in ('vmlinux', 'configs/tool-input-sha256.json', 'scripts/run-build.sh', 'source-diffs/common.diff'):
            with self.subTest(tampered_delivery_file=relative):
                path = package / relative
                original = path.read_bytes()
                path.write_bytes(b'tampered delivery')
                with self.assertRaises(ValueError):
                    P.verify_delivered(package)
                path.write_bytes(original)
        (package / 'source-diffs/integrated-source-manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'sources differ'):
            P.verify_delivered(package)

    def test_failed_preflight_invalidates_previous_success(self):
        self.complete()
        (self.root / 'scripts/preflight.py').write_text('raise SystemExit(9)\n')
        result = P.run_build(self.root, ['/bin/sh', '-c', 'touch should-not-exist'])
        self.assertEqual(result, 9)
        self.assertFalse(P.state_paths(self.root)[2].exists())
        self.assertFalse((self.root / 'should-not-exist').exists())
        self.assertFalse(P.state_paths(self.root)[0].exists())

    def test_interrupted_journal_can_be_recovered(self):
        path = self.root / 'kernel/common/test.c'
        old_mode = stat.S_IMODE(path.stat().st_mode)
        P.begin(self.root, {'argv': ['interrupted-build']})
        P.begin(self.root, {'argv': ['replacement-build']})
        P.abort(self.root)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), old_mode)
        self.assertFalse(P.state_paths(self.root)[2].exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
