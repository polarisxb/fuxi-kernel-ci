#!/usr/bin/env python3
"""Run the real UAPI verifier against isolated correct and faulty headers."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SOURCE = Path(os.environ.get('PIPELINE_SOURCE_ROOT', Path(__file__).resolve().parents[1]))


class UapiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='fuxi-uapi-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'scripts').mkdir()
        shutil.copy2(SOURCE / 'scripts/verify-uapi.py', self.root / 'scripts/verify-uapi.py')
        shutil.copytree(SOURCE / 'configs', self.root / 'configs')
        headers = self.root / 'kernel/KernelSU/kernel/include/uapi'
        shutil.copytree(SOURCE / 'kernel/KernelSU/kernel/include/uapi', headers)
        (self.root / 'kernel/prebuilts').symlink_to((SOURCE / 'kernel/prebuilts').resolve(), target_is_directory=True)
        self.headers = headers

    def run_verifier(self, optimized):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONOPTIMIZE', None)
        if optimized:
            env['PYTHONOPTIMIZE'] = '1'
        result = subprocess.run(['python3', str(self.root / 'scripts/verify-uapi.py')],
                                text=True, capture_output=True, timeout=45, env=env)
        return result

    def test_matching_headers_pass_in_both_modes(self):
        for optimized in (False, True):
            with self.subTest(optimized=optimized):
                result = self.run_verifier(optimized)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads((self.root / 'reports/uapi/complete-uapi-alignment.json').read_text())
                self.assertTrue(report['arm64_values_all_equal'])
                identity = json.loads((self.root / 'configs/build-identity.json').read_bytes())
                self.assertEqual(report['manager_sha'], identity['manager_commit'])
                self.assertEqual(report['kernel_sha'], identity['driver_commit'])

    def test_missing_build_identity_rejected_in_both_modes(self):
        (self.root / 'configs/build-identity.json').unlink()
        for optimized in (False, True):
            with self.subTest(optimized=optimized):
                result = self.run_verifier(optimized)
                self.assertNotEqual(result.returncode, 0, 'Missing build identity was accepted')
                self.assertIn('build-identity.json', result.stderr)

    def test_wrong_kernel_magic_rejected_in_both_modes(self):
        header = self.headers / 'supercall.h'
        old = header.read_text()
        self.assertIn('0xDEADBEEF', old)
        header.write_text(old.replace('0xDEADBEEF', '0xDEADBEF0'))
        for optimized in (False, True):
            with self.subTest(optimized=optimized):
                result = self.run_verifier(optimized)
                self.assertNotEqual(result.returncode, 0, 'Mismatched UAPI was accepted')

    def test_changed_manager_header_rejected_in_both_modes(self):
        header = self.root / 'configs/manager40900-uapi/supercall.h'
        header.write_text(header.read_text() + '\n/* unexpected input */\n')
        for optimized in (False, True):
            with self.subTest(optimized=optimized):
                result = self.run_verifier(optimized)
                self.assertNotEqual(result.returncode, 0, 'Modified pinned Manager header was accepted')

    def test_layout_difference_rejected_in_both_modes(self):
        header = self.headers / 'supercall.h'
        old = header.read_text()
        marker = 'struct ksu_get_info_cmd {'
        self.assertIn(marker, old)
        header.write_text(old.replace(marker, marker + '\n    unsigned long long unexpected_field;'))
        for optimized in (False, True):
            with self.subTest(optimized=optimized):
                result = self.run_verifier(optimized)
                self.assertNotEqual(result.returncode, 0, 'Changed wire layout was accepted')

    def test_compiler_failure_rejected_in_both_modes(self):
        script = self.root / 'scripts/verify-uapi.py'
        source = script.read_text()
        source = source.replace("CLANG=str(ROOT/'kernel/prebuilts/clang/host/linux-x86/clang-r450784e/bin/clang')", "CLANG='/bin/false'")
        self.assertNotEqual(source, script.read_text())
        script.write_text(source)
        for optimized in (False, True):
            with self.subTest(optimized=optimized):
                result = self.run_verifier(optimized)
                self.assertNotEqual(result.returncode, 0, 'Failed compiler was accepted')


if __name__ == '__main__':
    unittest.main(verbosity=2)
