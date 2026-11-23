#!/usr/bin/env python3
"""Host unit tests for actual VFS function bodies; no device or kernel execution."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path(os.environ.get('FUXI_TEST_SOURCE_ROOT', ROOT))
EVIDENCE = []


def function(source, name):
    match = re.search(r'(?m)^(?:static\s+)?(?:inline\s+)?[^\n;{}]*\b' +
                      re.escape(name) + r'\([^;{}]*\)\s*\{', source)
    if not match:
        raise ValueError('Missing function: ' + name)
    depth = 1
    end = match.end()
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[match.start():end]


def render(template):
    namei = (SOURCE_ROOT / 'kernel/common/fs/namei.c').read_text()
    proc = (SOURCE_ROOT / 'kernel/common/fs/proc/base.c').read_text()
    text = (Path(__file__).with_name(template)).read_text()
    for marker in re.findall(r'/\* @(\w+)@ \*/', text):
        if marker == 'NAMEIDATA':
            start = namei.index('struct nameidata {')
            end = namei.index('} __randomize_layout;', start) + len('} __randomize_layout;')
            definition = namei[start:end]
        else:
            definition = function(proc if marker == 'do_proc_readlink' else namei, marker)
        text = text.replace('/* @' + marker + '@ */', definition)
    return text


class CompiledHarness(unittest.TestCase):
    template = None

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='fuxi-vfs-unit-')
        directory = Path(cls.temp.name)
        source = directory / 'harness.c'
        source.write_text(render(cls.template))
        cls.binary = directory / 'harness'
        cmd = [os.environ.get('CC_FOR_TESTS', 'cc'), '-std=gnu11', '-g', '-O1',
               '-Wall', '-Wextra', '-Werror', '-Wno-unused-function',
               '-Wno-unused-parameter', '-Wno-sign-compare', '-fno-pie', '-no-pie',
               '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
               str(source), '-o', str(cls.binary)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode:
            cls.temp.cleanup()
            raise AssertionError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def check_case(self, *args):
        result = subprocess.run([str(self.binary), *map(str, args)], capture_output=True,
                                text=True, timeout=10,
                                preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
                                env=dict(os.environ, ASAN_OPTIONS='detect_leaks=1:abort_on_error=0'))
        EVIDENCE.append({'case': list(args), 'exit_code': result.returncode,
                         'stdout': result.stdout, 'stderr': result.stderr})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class NameidataTests(CompiledHarness):
    template = 'vfs_state_harness.c.in'

    def test_original_and_nested_lifetimes(self):
        for kind in ('normal', 'opath', 'tmpfile'):
            for case in ('success', 'retry-child', 'retry-stale', 'retry-both',
                         'permanent-child', 'permanent-stale', 'alloc-failure',
                         'open-failure', 'no-rule', 'unmarked', 'no-outer', 'root-preset'):
                with self.subTest(kind=kind, case=case):
                    self.check_case(kind, case)

    def test_rcu_walk_transitions_before_allocation(self):
        for kind in ('normal', 'opath', 'tmpfile'):
            for case in ('rcu', 'no-rule-rcu'):
                with self.subTest(kind=kind, case=case):
                    self.check_case(kind, case)

    def test_cached_lookup_does_not_allocate_redirect_filename(self):
        self.check_case('normal', 'cached')


class ReadlinkTests(CompiledHarness):
    template = 'vfs_readlink_harness.c.in'

    def test_copy_lengths_and_buffer_boundaries(self):
        for kind in ('proc', 'custom', 'cached', 'delayed'):
            for size in (0, 1, 6, 7, 8, 63):
                with self.subTest(kind=kind, size=size):
                    self.check_case(kind, 'success', size)

    def test_only_no_rule_falls_back(self):
        for kind in ('proc', 'custom', 'cached', 'delayed'):
            for case in ('helper-error', 'no-rule', 'unmarked', 'fault'):
                with self.subTest(kind=kind, case=case):
                    self.check_case(kind, case, 3)

    def test_proc_allocation_failure(self):
        self.check_case('proc', 'allocation', 63)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path, default=SOURCE_ROOT)
    parser.add_argument('--report', type=Path)
    args, remaining = parser.parse_known_args()
    SOURCE_ROOT = args.source_root.resolve()
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                               for cls in (NameidataTests, ReadlinkTests))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps({
            'passed': result.wasSuccessful(), 'unit_tests': result.testsRun,
            'cases': EVIDENCE, 'source_root': str(SOURCE_ROOT),
            'source_sha256': {str(p.relative_to(SOURCE_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (SOURCE_ROOT / 'kernel/common/fs/namei.c',
                                        SOURCE_ROOT / 'kernel/common/fs/proc/base.c')},
            'scope': 'Actual nameidata/lifecycle/open/readlink functions; simulated filesystem and helper boundaries.',
            'device_operations': False,
        }, indent=2) + '\n')
    raise SystemExit(0 if result.wasSuccessful() else 1)
