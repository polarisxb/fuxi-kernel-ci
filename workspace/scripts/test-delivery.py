#!/usr/bin/env python3
"""Fault-injection tests for delivery gates; never builds a kernel or uses adb."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = load('delivery_verify_tests', 'verify-delivery.py')
C = load('delivery_collect_tests', 'collect-delivery.py')


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='fuxi-delivery-test-')
        self.root = Path(self.temporary.name)
        self.identity = {'common_sha': V.COMMON_SHA, 'builtin_sha': V.BUILTIN_SHA,
                         'susfs_sha': V.SUSFS_SHA, 'manager_version': 40900,
                         'driver_version': 40900, 'uapi_version': 2,
                         'profile_version': 4, 'susfs_version': 'v2.3.0'}

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self):
        package = self.root / 'candidate'
        package.mkdir()
        for relative in V.REQUIRED:
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('fixture for ' + relative + '\n')
        self.apk_patch = patch.object(V, 'APK_SHA', V.sha256(package / V.APK_NAME))
        self.apk_patch.start()
        self.addCleanup(self.apk_patch.stop)
        V.write_inventory(package, self.identity)
        return package

    def test_complete_inventory(self):
        package = self.fixture()
        _, count = V.verify_inventory(package)
        self.assertEqual(count, len(V.REQUIRED) + 2)

    def test_missing_essential_files_rejected_even_with_fresh_manifest(self):
        package = self.fixture()
        for name in [V.APK_NAME, 'matching-gki-modules/zram.ko',
                     'public-certificates/signing_key.x509',
                     'source/builtin-e2912817.tar.gz', 'tools/ksu_susfs']:
            path = package / name
            data = path.read_bytes()
            path.unlink()
            V.write_inventory(package, self.identity)
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'Missing required'):
                V.verify_inventory(package)
            path.write_bytes(data)
        V.write_inventory(package, self.identity)
        V.verify_inventory(package)

    def test_checksum_file_missing_is_rejected(self):
        package = self.fixture()
        (package / 'SHA256SUMS').unlink()
        with self.assertRaisesRegex(ValueError, 'inventory/checksums'):
            V.verify_inventory(package)

    def test_extra_or_stale_file_rejected(self):
        package = self.fixture()
        (package / 'old-success-report.json').write_text('{"passed":true}')
        with self.assertRaisesRegex(ValueError, 'file set'):
            V.verify_inventory(package)

    def test_mutated_payload_rejected(self):
        package = self.fixture()
        (package / 'Image').write_bytes(b'old image mixed into new delivery')
        with self.assertRaisesRegex(ValueError, 'hash/size'):
            V.verify_inventory(package)

    def test_wrong_driver_identity_rejected(self):
        package = self.fixture()
        V.write_inventory(package, dict(self.identity, driver_version=37974))
        with self.assertRaisesRegex(ValueError, 'release identity'):
            V.verify_inventory(package)

    def test_duplicate_module_rejected_after_inventory_rebuilt(self):
        package = self.fixture()
        duplicate = package / 'matching-gki-modules/duplicate/zram.ko'
        duplicate.parent.mkdir()
        duplicate.write_bytes((package / 'matching-gki-modules/zram.ko').read_bytes())
        V.write_inventory(package, self.identity)
        with self.assertRaisesRegex(ValueError, 'module file set'):
            V.verify_inventory(package)

    def test_duplicate_checksum_entries_rejected(self):
        package = self.fixture()
        sums = package / 'SHA256SUMS'
        sums.write_text(sums.read_text() + sums.read_text().splitlines()[0] + '\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate checksum'):
            V.verify_inventory(package)

    def test_private_key_and_symlink_rejected(self):
        package = self.fixture()
        key = package / 'private.pem'
        key.write_text('test')
        with self.assertRaisesRegex(ValueError, 'signing material'):
            V.write_inventory(package, self.identity)
        key.unlink()
        disguised = package / 'secret.txt'
        disguised.write_text('-----BEGIN ' + 'PRIVATE KEY-----\n')
        with self.assertRaisesRegex(ValueError, 'signing material'):
            V.write_inventory(package, self.identity)
        disguised.unlink()
        (package / 'outside').symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            V.write_inventory(package, self.identity)

    def test_preflight_missing_input_keeps_previous_release(self):
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'Image').write_text('old, complete release')
        with self.assertRaisesRegex(ValueError, 'Missing/empty packaging inputs'):
            C.preflight(self.root, {'missing': self.root / 'missing.apk'})
        self.assertEqual((artifacts / 'Image').read_text(), 'old, complete release')
        self.assertFalse((self.root / 'releases').exists())

    def test_shared_pipeline_lock(self):
        with C.pipeline_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, 'already owns'):
                with C.pipeline_lock(self.root):
                    self.fail('Second collector entered a locked pipeline')

    def publication_fixture(self):
        releases = self.root / 'releases'
        releases.mkdir()
        old = releases / 'release-old'
        old.mkdir()
        (old / 'Image').write_text('original')
        (self.root / 'artifacts').symlink_to('releases/release-old', target_is_directory=True)
        pending = releases / '.pending-new'
        pending.mkdir()
        (pending / 'Image').write_text('new')
        return old, pending

    def test_atomic_publish_keeps_previous_release(self):
        old, pending = self.publication_fixture()
        result = C.publish(self.root, pending, 'release-new')
        self.assertEqual((self.root / 'artifacts').resolve(), result)
        self.assertEqual((old / 'Image').read_text(), 'original')
        self.assertEqual((self.root / 'artifacts/Image').read_text(), 'new')

    def test_pointer_failure_keeps_previous_release_visible(self):
        old, pending = self.publication_fixture()
        replace = C.os.replace

        def fail_pointer(source, destination):
            if destination == self.root / 'artifacts':
                raise OSError('Injected pointer publication failure')
            return replace(source, destination)

        with patch.object(C.os, 'replace', side_effect=fail_pointer):
            with self.assertRaisesRegex(OSError, 'Injected'):
                C.publish(self.root, pending, 'release-new')
        self.assertEqual((self.root / 'artifacts').resolve(), old)
        self.assertEqual((self.root / 'artifacts/Image').read_text(), 'original')
        self.assertFalse(list(self.root.glob('.artifacts-next-*')))

    def test_unrelated_existing_directory_cannot_be_overwritten(self):
        releases = self.root / 'releases'
        releases.mkdir()
        pending = releases / '.pending-new'
        pending.mkdir()
        artifacts = self.root / 'artifacts'
        artifacts.mkdir()
        (artifacts / 'Image').write_text('independent delivery')
        with self.assertRaisesRegex(ValueError, 'nonempty artifacts'):
            C.publish(self.root, pending, 'release-new')
        self.assertEqual((artifacts / 'Image').read_text(), 'independent delivery')


if __name__ == '__main__':
    unittest.main(verbosity=2)
