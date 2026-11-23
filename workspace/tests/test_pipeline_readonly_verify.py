#!/usr/bin/env python3
"""Delivery verification must not change its own exact inventory on import."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = load('readonly_delivery_fixture', ROOT / 'scripts/verify-delivery.py')
P = load('readonly_provenance_fixture', ROOT / 'scripts/build_provenance.py')


class ReadonlyVerificationTests(unittest.TestCase):
    def test_repeated_delivery_validation_without_bytecode_environment(self):
        with tempfile.TemporaryDirectory(prefix='delivery-readonly-test-') as temporary:
            root = Path(temporary) / 'release'
            root.mkdir()
            for name in V.REQUIRED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture ' + name)
            for name in ('verify-delivery.py', 'build_provenance.py', *P.BUILD_SCRIPTS):
                shutil.copy2(ROOT / 'scripts' / name, root / 'scripts' / name)
            (root / 'configs').mkdir()
            (root / 'source-diffs/integrated-source-manifest.json').write_text('{}')
            (root / 'tools/ksu_susfs').write_bytes(b'\x7fELF\x02\x01' + b'\0' * 12 + struct.pack('<H', 183))
            record = {
                'schema_version': 1, 'status': 'complete', 'build_returncode': 0,
                'attempt': 'isolated-fixture',
                'inputs': {'sources': {}, 'configs': {},
                           'build_scripts': {name: P.file_record(root / 'scripts' / name) for name in P.BUILD_SCRIPTS},
                           'patches': {name: P.file_record(root / 'source-diffs' / published)
                                       for name, published in zip(P.PATCHES, ('common.diff', 'SukiSU.diff'))}},
                'outputs': {name: P.file_record(root / name) for name in
                            set(P.OUTPUTS) | {'matching-gki-modules/zram.ko', 'matching-gki-modules/zsmalloc.ko'}},
            }
            (root / 'reports/build-provenance.json').write_text(json.dumps(record))
            identity = {'common_sha': V.COMMON_SHA, 'builtin_sha': V.BUILTIN_SHA,
                        'susfs_sha': V.SUSFS_SHA, 'driver_version': 40900, 'manager_version': 40900,
                        'uapi_version': 2, 'profile_version': 4, 'susfs_version': 'v2.3.0'}
            V.write_inventory(root, identity)
            before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in root.rglob('*') if p.is_file()}
            # Exercise the real orchestration, inventory, provenance dynamic
            # imports and file hashing. Only expensive binary/source-replay
            # validators are stubbed; their byte-level correctness is separate.
            driver = r'''
import hashlib, json, runpy, sys
from pathlib import Path
root = Path(sys.argv[1])
namespace = runpy.run_path(str(root / 'scripts/verify-delivery.py'))
globals_ = namespace['verify_delivery'].__globals__
globals_['APK_SHA'] = hashlib.sha256((root / namespace['APK_NAME']).read_bytes()).hexdigest()
globals_['verify_image'] = lambda root: '5.15.194-android13-8-gf4321180a397-suki40900-susfs230-r2'
for name in ('verify_boot', 'verify_zip', 'verify_modules', 'verify_replay'):
    globals_[name] = lambda root: True
result = namespace['verify_delivery'](root)
print(json.dumps(result))
'''
            environment = dict(os.environ)
            environment.pop('PYTHONDONTWRITEBYTECODE', None)
            environment.pop('PYTHONPYCACHEPREFIX', None)
            for attempt in (1, 2):
                with self.subTest(attempt=attempt):
                    result = subprocess.run(['python3', '-c', driver, str(root)], env=environment,
                                            capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(json.loads(result.stdout)['passed'])
                    after = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in root.rglob('*') if p.is_file()}
                    self.assertEqual(after, before, 'Verification changed its immutable file set/content')
                    self.assertFalse(list(root.rglob('__pycache__')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
