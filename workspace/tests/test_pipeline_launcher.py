#!/usr/bin/env python3
"""Validate safe locked-toolchain launch arguments without running a build."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(os.environ.get('PIPELINE_SOURCE_ROOT', Path(__file__).resolve().parents[1]))


class LauncherTests(unittest.TestCase):
    def dry_run(self, **settings):
        environment = dict(os.environ)
        environment.pop('LTO_PARTITIONS', None)
        environment.pop('LTO_THREADS', None)
        environment.update(settings)
        return subprocess.run(['bash', str(ROOT / 'scripts/run-build.sh'), '--dry-run'],
                              env=environment, capture_output=True, text=True, timeout=10)

    def test_defaults_match_successful_baseline_link(self):
        result = self.dry_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Full LTO: build/build.sh -j8', result.stdout)
        self.assertIn('--threads=2 --lto-partitions=1', result.stdout)

    def test_multiple_lto_partitions_rejected_before_build(self):
        for partitions in range(2, 9):
            with self.subTest(partitions=partitions):
                result = self.dry_run(LTO_PARTITIONS=str(partitions))
                self.assertEqual(result.returncode, 2)
                self.assertIn('LTO_PARTITIONS=1', result.stderr)
                self.assertNotIn('build/build.sh', result.stdout)

    def test_single_partition_and_bounded_linker_threads_accepted(self):
        for threads in range(1, 9):
            with self.subTest(threads=threads):
                result = self.dry_run(LTO_PARTITIONS='1', LTO_THREADS=str(threads))
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'--threads={threads} --lto-partitions=1', result.stdout)
                self.assertIn('-j8', result.stdout)

    def test_invalid_numeric_values_rejected(self):
        for variable in ('LTO_PARTITIONS', 'LTO_THREADS'):
            for value in ('0', '9', '-1', 'one', '1 2', '1;echo unexpected'):
                with self.subTest(variable=variable, value=value):
                    result = self.dry_run(**{variable: value})
                    self.assertEqual(result.returncode, 2)
                    self.assertNotIn('build/build.sh', result.stdout)


if __name__ == '__main__':
    unittest.main(verbosity=2)
