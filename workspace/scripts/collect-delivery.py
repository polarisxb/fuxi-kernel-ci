#!/usr/bin/env python3
"""Build and atomically publish a complete offline 40900 + SUSFS candidate."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import uuid
import zipfile


# Local helper imports must not mutate an exact, immutable delivery inventory.
sys.dont_write_bytecode = True

def load_verifier():
    spec = importlib.util.spec_from_file_location(
        'delivery_verifier', Path(__file__).with_name('verify-delivery.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V = load_verifier()
_provenance_spec = importlib.util.spec_from_file_location('collection_build_provenance', Path(__file__).with_name('build_provenance.py'))
P = importlib.util.module_from_spec(_provenance_spec)
_provenance_spec.loader.exec_module(P)
ROOT = Path(__file__).resolve().parent.parent
STOCK_SHA = 'd973cf1324147d9411feea07ab070aec11fb2ec2c1bb5ef7a512b42bd44c8859'


@contextmanager
def pipeline_lock(root):
    (root / 'logs').mkdir(exist_ok=True)
    with (root / '.pipeline.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Build or collection already owns this pipeline') from error
        yield


def copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def dependencies(root):
    return {
        'stock': root / 'inputs/stock/images/boot.img',
        'template': root / 'inputs/anykernel-template.zip',
        'baseline_exports': root / 'inputs/baseline/Module.symvers',
        'baseline_cert': root / 'inputs/baseline/signing_key.x509',
        'apk': root / 'inputs/manager' / V.APK_NAME,
        'manager_archive': root / 'downloads/manager-904c60d1.tar.gz',
        'builtin_archive': root / 'downloads/builtin-e2912817f4e1.tar.gz',
        'susfs_archive': root / 'downloads/susfs-bca0d2333c1a.tar.gz',
        'avbtool': root / 'kernel/prebuilts/kernel-build-tools/linux-x86/bin/avbtool',
        'objcopy': root / 'kernel/prebuilts/clang/host/linux-x86/clang-r450784e/bin/llvm-objcopy',
        'objcopy_runtime': root / 'kernel/prebuilts/clang/host/linux-x86/clang-r450784e/lib64/libc++.so.1',
        'susfs_tool': root / 'tools/ksu_susfs',
        'common_patch': root / 'patches/common-final.diff',
        'ksu_patch': root / 'patches/sukisu-final.diff',
        'readme': root / 'scripts/README.template.md',
    }


def preflight(root, inputs):
    """Check every late dependency before changing any candidate or published file."""
    out = root / 'out/sukisu/common'
    required = list(inputs.values()) + [
        out / 'arch/arm64/boot/Image', out / 'vmlinux', out / '.config',
        out / 'System.map', out / 'Module.symvers', out / 'include/config/kernel.release',
        out / 'certs/signing_key.x509', root / 'configs/manifest.locked.xml',
        root / 'scripts/verify-build.py', root / 'scripts/verify-uapi.py',
        root / 'scripts/verify-delivery.py', root / 'scripts/run-build.sh',
    ]
    missing = [str(path) for path in required if not path.is_file() or path.stat().st_size == 0]
    V.require(not missing, 'Missing/empty packaging inputs:\n' + '\n'.join(missing))
    V.require(shutil.which('git') is not None and shutil.which('openssl') is not None,
              'git and openssl are required')
    V.require(V.sha256(inputs['apk']) == V.APK_SHA, 'Unexpected official Manager APK')
    V.require(V.sha256(inputs['stock']) == STOCK_SHA, 'Unexpected stock boot input')
    V.require(V.sha256(inputs['baseline_cert']) == V.sha256(out / 'certs/signing_key.x509'),
              'Build signing certificate differs from the locked baseline')
    head = V.run(['git', '-C', root / 'kernel/common', 'rev-parse', 'HEAD']).decode().strip()
    V.require(head == V.COMMON_SHA, 'Common source is not the locked baseline')
    release = (out / 'include/config/kernel.release').read_text().strip()
    staging = root / 'out/sukisu/staging/lib/modules' / release / 'kernel'
    modules = list(staging.rglob('*.ko'))
    V.require(len(modules) == 2 and {path.name for path in modules} == {'zram.ko', 'zsmalloc.ko'},
              f'Expected exactly one zram.ko and zsmalloc.ko: {[str(p) for p in modules]}')
    with zipfile.ZipFile(inputs['template']) as archive:
        names = archive.namelist()
        V.require(len(names) == len(set(names)) and archive.testzip() is None,
                  'Invalid or duplicate-entry AnyKernel template')
        V.require({'Image', 'anykernel.sh', 'tools/ak3-core.sh'} <= set(names),
                  'AnyKernel template is incomplete')
        V.require(hashlib.sha256(archive.read('tools/ak3-core.sh')).hexdigest() == V.CORE_SHA,
                  'AnyKernel core differs from the audited pin')
    for key in ('avbtool', 'objcopy', 'susfs_tool'):
        V.require(os.access(inputs[key], os.X_OK), f'Input tool is not executable: {key}')
    return out, modules


def package_anykernel(inputs, stage):
    records = {}
    with zipfile.ZipFile(inputs['template']) as source, zipfile.ZipFile(
            stage / V.ZIP_NAME, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for entry in source.infolist():
            data = source.read(entry)
            if entry.filename == 'Image':
                data = (stage / 'Image').read_bytes()
            elif entry.filename == 'anykernel.sh':
                text = re.sub(r'^kernel.string=.*$',
                              'kernel.string=Fuxi GKI 5.15.194 - SukiSU 40900 + SUSFS 2.3.0 r2',
                              data.decode(), flags=re.M)
                V.require('SLOT_SELECT=active;' in text and 'BLOCK=boot;' in text,
                          'Unsafe AnyKernel template')
                data = text.encode()
                (stage / 'anykernel.sh').write_bytes(data)
            else:
                records[entry.filename] = hashlib.sha256(data).hexdigest()
            target.writestr(entry, data)
    (stage / 'reports/anykernel-template.json').write_text(json.dumps(records, indent=2) + '\n')


def package_boot(inputs, stage):
    stock = inputs['stock'].read_bytes()
    header = bytearray(stock[:4096])
    V.require(header[:8] == b'ANDROID!' and struct.unpack_from('<I', header, 40)[0] == 4
              and struct.unpack_from('<I', header, 12)[0] == 0, 'Unexpected stock boot format')
    (stage / 'reports/stock-boot-header.bin').write_bytes(header)
    image = (stage / 'Image').read_bytes()
    struct.pack_into('<I', header, 8, len(image))
    payload = bytes(header) + image
    boot = stage / 'boot.img'
    boot.write_bytes(payload + b'\0' * ((-len(payload)) % 4096))
    info = V.run([inputs['avbtool'], 'info_image', '--image', inputs['stock']]).decode()
    (stage / 'reports/stock-boot-avb.txt').write_text(info)
    salt = re.search(r'^\s*Salt:\s*([0-9a-f]+)$', info, re.M)
    V.require(salt is not None, 'Stock boot AVB salt missing')
    command = [inputs['avbtool'], 'add_hash_footer', '--image', boot,
               '--partition_size', str(len(stock)), '--partition_name', 'boot',
               '--hash_algorithm', 'sha256', '--salt', salt.group(1),
               '--algorithm', 'NONE', '--flags', '0']
    for key, value in re.findall(r"^\s*Prop: (\S+) -> '(.*)'$", info, re.M):
        command += ['--prop', key + ':' + value]
    V.run(command)
    output = V.run([inputs['avbtool'], 'verify_image', '--image', boot])
    (stage / 'reports/boot-avb-verification.txt').write_bytes(output)


def package_sources(root, inputs, stage):
    source = stage / 'source'
    source.mkdir()
    V.run(['git', '-C', root / 'kernel/common', 'archive', '--format=tar.gz',
           '--output=' + str(source / 'common-base.tar.gz'), V.COMMON_SHA])
    for key, name in [('builtin_archive', 'builtin-e2912817.tar.gz'),
                      ('susfs_archive', 'susfs-bca0d233.tar.gz'),
                      ('manager_archive', 'manager-904c60d1.tar.gz')]:
        copy(inputs[key], source / name)
    copy(inputs['common_patch'], stage / 'source-diffs/common.diff')
    copy(inputs['ksu_patch'], stage / 'source-diffs/SukiSU.diff')
    records = {name: V.tree_manifest(root / 'kernel' / directory)
               for name, directory in [('common', 'common'), ('KernelSU', 'KernelSU')]}
    (stage / 'source-diffs/integrated-source-manifest.json').write_text(
        json.dumps(records, indent=2) + '\n')


def publish(root, stage, release_id):
    """Publish by an atomic pointer replacement; all previous releases remain intact."""
    releases = root / 'releases'
    V.require(stage.parent == releases and stage.name.startswith('.pending-'),
              'Candidate directory must be owned by this release pipeline')
    destination = releases / release_id
    V.require(not destination.exists(), f'Release already exists: {release_id}')
    published = root / 'artifacts'
    if published.exists() and not published.is_symlink():
        V.require(published.is_dir() and not any(published.iterdir()),
                  'Refusing to replace an existing nonempty artifacts directory')
        published.rmdir()
    if published.is_symlink():
        V.require(published.resolve().parent == releases.resolve(),
                  'Refusing to replace an unrelated artifacts symlink')
    os.replace(stage, destination)
    next_link = root / ('.artifacts-next-' + uuid.uuid4().hex)
    try:
        next_link.symlink_to(Path('releases') / release_id, target_is_directory=True)
        os.replace(next_link, published)
    finally:
        if next_link.is_symlink():
            next_link.unlink()
    return destination


def collect(root):
    inputs = dependencies(root)
    with pipeline_lock(root):
        out, modules = preflight(root, inputs)
        P.verify_workspace(root, out)
        attempt = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
        reports = root / 'reports' / ('delivery-' + attempt)
        reports.mkdir()
        verification_environment = dict(os.environ, FUXI_PIPELINE_LOCK_HELD='1')
        V.run([sys.executable, root / 'scripts/verify-build.py', '--out-dir', out,
               '--report-dir', reports / 'offline', '--expected-ksu', 'yes'],
              env=verification_environment)
        (reports / 'uapi.log').write_bytes(V.run([sys.executable, root / 'scripts/verify-uapi.py']))
        releases = root / 'releases'
        releases.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.pending-', dir=releases))
        try:
            for relative, name in [('arch/arm64/boot/Image', 'Image'), ('vmlinux', 'vmlinux'),
                                   ('.config', '.config'), ('System.map', 'System.map'),
                                   ('Module.symvers', 'Module.symvers'),
                                   ('include/config/kernel.release', 'kernel.release')]:
                copy(out / relative, stage / name)
            copy(root / 'configs/manifest.locked.xml', stage / 'manifest.locked.xml')
            copy(inputs['baseline_exports'], stage / 'reports/baseline-Module.symvers')
            copy(out / 'certs/signing_key.x509', stage / 'public-certificates/signing_key.x509')
            copy(inputs['apk'], stage / V.APK_NAME)
            copy(inputs['susfs_tool'], stage / 'tools/ksu_susfs')
            copy(inputs['avbtool'], stage / 'verification-tools/avbtool')
            copy(inputs['objcopy'], stage / 'verification-tools/llvm-objcopy')
            # llvm-objcopy resolves this dependency through $ORIGIN/../lib64.
            copy(inputs['objcopy_runtime'], stage / 'lib64/libc++.so.1')
            V.run([stage / 'verification-tools/llvm-objcopy', '--version'])
            for path in modules:
                copy(path, stage / 'matching-gki-modules' / path.name)
            copy(root / 'out/sukisu/build-provenance.json', stage / 'reports/build-provenance.json')
            for name in ('configs', 'scripts', 'patches', 'tests'):
                shutil.copytree(root / name, stage / name, ignore=shutil.ignore_patterns('__pycache__'))
            for path in sorted((root / 'reports').iterdir()):
                if path.name.startswith('delivery-') or path.name in {'offline', 'offline-recheck'}:
                    continue
                if path.is_file():
                    copy(path, stage / 'reports' / path.name)
                elif path.name == 'uapi':
                    shutil.copytree(path, stage / 'reports/uapi')
            shutil.copytree(reports / 'offline', stage / 'reports/offline')
            for path in sorted((root / 'logs').glob('*.log')):
                copy(path, stage / 'logs' / path.name)
            copy(inputs['readme'], stage / 'README.zh-CN.md')
            package_anykernel(inputs, stage)
            package_boot(inputs, stage)
            package_sources(root, inputs, stage)
            P.verify_workspace(root, out)
            identity = {'common_sha': V.COMMON_SHA, 'builtin_sha': V.BUILTIN_SHA,
                        'susfs_sha': V.SUSFS_SHA, 'manager_version': 40900, 'driver_version': 40900,
                        'uapi_version': 2, 'profile_version': 4, 'susfs_version': 'v2.3.0',
                        'created_at': datetime.now(timezone.utc).isoformat(),
                        'runtime_compatibility_verified': False, 'phone_operations': False}
            V.write_inventory(stage, identity)
            verification = V.verify_delivery(stage)
            (reports / 'verification.json').write_text(json.dumps(verification, indent=2) + '\n')
            copy(reports / 'verification.json', stage / 'reports/delivery-verification.json')
            V.write_inventory(stage, identity)
            V.verify_inventory(stage)
            destination = publish(root, stage, 'release-' + attempt)
        except BaseException:
            (reports / 'failed-candidate.txt').write_text(str(stage) + '\n')
            raise
    return {'passed': True, 'release': str(destination), 'published': str(root / 'artifacts'),
            'phone_operations': False, 'runtime_compatibility_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight', action='store_true', help='check inputs without generating a release')
    args = parser.parse_args()
    try:
        if args.preflight:
            preflight(ROOT, dependencies(ROOT))
            result = {'passed': True, 'preflight': True}
        else:
            result = collect(ROOT)
    except Exception as error:
        result = {'passed': False, 'error': f'{type(error).__name__}: {error}', 'phone_operations': False}
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
