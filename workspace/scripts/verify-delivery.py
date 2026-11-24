#!/usr/bin/env python3
"""Verify the immutable delivery against its recorded build identity."""
import argparse
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

# Local helper imports must not mutate an exact, immutable delivery inventory.
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
# Source-manifest users import this module before a workspace is assembled.
IDENTITY_PATH = ROOT / 'configs/build-identity.json'
IDENTITY = json.loads(IDENTITY_PATH.read_text()) if IDENTITY_PATH.is_file() else {}
COMMON_SHA = IDENTITY.get('common_commit')
BUILTIN_SHA = IDENTITY.get('driver_commit')
SUSFS_SHA = IDENTITY.get('susfs_commit')
APK_NAME = IDENTITY.get('apk_name')
APK_SHA = IDENTITY.get('apk_sha256')
ZIP_NAME = IDENTITY.get('kernel_zip')
CORE_SHA = 'f7a51cfe2e093016a34a76e16de6c9885c2d1992c3482fcabc668680e1f0f6bb'
BASE_REQUIRED = {
    'Image', 'vmlinux', 'boot.img', '.config', 'System.map', 'Module.symvers',
    'kernel.release', 'manifest.locked.xml', 'README.zh-CN.md', 'configs/build-identity.json',
    'matching-gki-modules/zram.ko', 'matching-gki-modules/zsmalloc.ko',
    'public-certificates/signing_key.x509', 'tools/ksu_susfs',
    'source/common-base.tar.gz',
    'source-diffs/common.diff', 'source-diffs/SukiSU.diff',
    'source-diffs/integrated-source-manifest.json',
    'reports/offline/verification.json', 'reports/offline/built-module-checks.json',
    'reports/uapi/complete-uapi-alignment.json', 'reports/stock-boot-header.bin',
    'reports/anykernel-template.json', 'reports/baseline-Module.symvers',
    'verification-tools/avbtool', 'verification-tools/llvm-objcopy',
    'lib64/libc++.so.1',
    'reports/build-provenance.json', 'scripts/build_provenance.py',
    'scripts/preflight.py', 'scripts/check-config.py',
    'scripts/verify-delivery.py', 'scripts/collect-delivery.py',
    'scripts/run-build.sh', 'scripts/verify-build.py', 'scripts/verify-uapi.py',
} | {'reports/uapi/manager-headers/' + name for name in
     ('app_profile.h', 'feature.h', 'selinux.h', 'sulog.h', 'supercall.h')}
SUSFS_OPTIONS = {
    'CONFIG_KSU_SUSFS', 'CONFIG_KSU_SUSFS_SUS_PATH', 'CONFIG_KSU_SUSFS_SUS_MOUNT',
    'CONFIG_KSU_SUSFS_SUS_KSTAT', 'CONFIG_KSU_SUSFS_SPOOF_UNAME',
    'CONFIG_KSU_SUSFS_ENABLE_LOG', 'CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS',
    'CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG', 'CONFIG_KSU_SUSFS_OPEN_REDIRECT',
    'CONFIG_KSU_SUSFS_SUS_MAP',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_identity(root):
    identity = json.loads((root / 'configs/build-identity.json').read_text())
    require(isinstance(identity, dict), 'Invalid build identity')
    # Required filenames must stay inside their named package directories.
    for key in ('apk_name', 'kernel_zip', 'builtin_archive', 'susfs_archive', 'manager_archive'):
        value = identity.get(key, '')
        require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', value),
                f'Invalid build identity filename: {key}')
    return identity


def required_files(identity):
    return BASE_REQUIRED | {identity['apk_name'], identity['kernel_zip']} | {
        'source/' + identity[key] for key in ('builtin_archive', 'susfs_archive', 'manager_archive')
    }


REQUIRED = required_files(IDENTITY) if IDENTITY else BASE_REQUIRED


def run(args, **kwargs):
    result = subprocess.run([str(x) for x in args], capture_output=True, timeout=300, **kwargs)
    require(result.returncode == 0,
            f'{args[0]} failed ({result.returncode}): {result.stderr.decode(errors="replace")[:2000]}')
    return result.stdout


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value):
    path = PurePosixPath(value)
    require(value and not path.is_absolute() and '..' not in path.parts
            and str(path) == value and '\n' not in value and '\r' not in value,
            f'Unsafe relative path: {value!r}')
    return path


def files_in(root):
    result = {}
    for path in root.rglob('*'):
        relative = path.relative_to(root).as_posix()
        require(not path.is_symlink(), f'Delivery contains a symlink: {relative}')
        if path.is_file():
            safe_relative(relative)
            require(path.suffix.lower() not in {'.pem', '.key'},
                    f'Private signing material filename: {relative}')
            with path.open('rb') as stream:
                require(not re.search(br'-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----', stream.read(16384)),
                        f'Private signing material content: {relative}')
            result[relative] = path
    return result


def write_inventory(root, identity):
    records = {name: {'sha256': sha256(path), 'bytes': path.stat().st_size}
               for name, path in sorted(files_in(root).items())
               if name not in {'RELEASE.json', 'SHA256SUMS'}}
    release = dict(identity, schema_version=1, files=records)
    (root / 'RELEASE.json').write_text(json.dumps(release, indent=2) + '\n')
    paths = files_in(root)
    (root / 'SHA256SUMS').write_text(''.join(
        f'{sha256(paths[name])}  {name}\n' for name in sorted(paths) if name != 'SHA256SUMS'))


def verify_inventory(root):
    identity = read_identity(root)
    paths = files_in(root)
    missing = required_files(identity) - paths.keys()
    require(not missing, f'Missing required delivery files: {sorted(missing)}')
    require({'RELEASE.json', 'SHA256SUMS'} <= paths.keys(), 'Missing release inventory/checksums')
    release = json.loads(paths['RELEASE.json'].read_text())
    require(release.get('schema_version') == 1, 'Unsupported inventory schema')
    for key, expected in {'common_sha': identity['common_commit'], 'builtin_sha': identity['driver_commit'],
                          'susfs_sha': identity['susfs_commit'], 'manager_version': identity['manager_version_code'],
                          'driver_version': identity['driver_version'], 'uapi_version': 2,
                          'profile_version': 4, 'susfs_version': identity['susfs_version']}.items():
        require(release.get(key) == expected, f'Incorrect release identity: {key}')
    actual_names = paths.keys() - {'RELEASE.json', 'SHA256SUMS'}
    require(release['files'].keys() == actual_names, 'Inventory differs from actual file set')
    for name, record in release['files'].items():
        require(record == {'sha256': sha256(paths[name]), 'bytes': paths[name].stat().st_size},
                f'Inventory hash/size differs: {name}')
    sums = {}
    for line in paths['SHA256SUMS'].read_text().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        require(match is not None, 'Malformed checksum line')
        digest, name = match.groups()
        safe_relative(name)
        require(name not in sums, f'Duplicate checksum entry: {name}')
        sums[name] = digest
    require(sums.keys() == paths.keys() - {'SHA256SUMS'}, 'Checksum file set differs')
    for name, digest in sums.items():
        require(sha256(paths[name]) == digest, f'Checksum mismatch: {name}')
    require(sha256(paths[identity['apk_name']]) == identity['apk_sha256'], 'Manager APK is not the locked official APK')
    modules = {str(p.relative_to(root / 'matching-gki-modules'))
               for p in (root / 'matching-gki-modules').rglob('*') if p.is_file()}
    require(modules == {'zram.ko', 'zsmalloc.ko'}, f'Incorrect module file set: {modules}')
    return release, len(paths)


def exports(path):
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        require(len(fields) >= 4, f'Malformed symbol record in {path.name}')
        record = (fields[0], fields[2], fields[3], fields[4] if len(fields) > 4 else '')
        require(fields[1] not in result, f'Duplicate symbol {fields[1]}')
        result[fields[1]] = record
    require(bool(result), f'Empty exports: {path}')
    return result


def verify_image(root):
    identity = read_identity(root)
    image = (root / 'Image').read_bytes()
    require(image[56:60] == b'ARM\x64', 'Not an AArch64 Image')
    require(identity['driver_version_full'].encode() in image, 'Incorrect builtin release identity')
    require(identity['susfs_version'].encode() in image, 'SUSFS identity absent from Image')
    config = (root / '.config').read_bytes()
    start = image.find(b'IKCFG_ST')
    end = image.find(b'IKCFG_ED', start + 8)
    require(start >= 0 and end > start, 'Missing embedded Image configuration')
    require(gzip.decompress(image[start + 8:end]) == config, 'Image/config mismatch')
    required = SUSFS_OPTIONS | {'CONFIG_KSU', 'CONFIG_LTO_CLANG_FULL', 'CONFIG_CFI_CLANG',
                               'CONFIG_MODVERSIONS', 'CONFIG_MODULE_SIG_PROTECT'}
    options = dict(line.split('=', 1) for line in config.decode().splitlines()
                   if line.startswith('CONFIG_'))
    require(all(options.get(key) == 'y' for key in required), 'Required SUSFS/hardening feature disabled')
    require(options.get('CONFIG_LTO_CLANG_THIN', 'n') == 'n', 'ThinLTO must remain disabled')
    require(options.get('CONFIG_KPM', 'n') == 'n', 'Unexpected KPM feature')
    release = (root / 'kernel.release').read_text().strip()
    require(release == identity['kernel_release'],
            f'Incorrect kernel release {release}')
    with tempfile.TemporaryDirectory(prefix='delivery-image-') as temp:
        output = Path(temp) / 'Image'
        run([root / 'verification-tools/llvm-objcopy', '-O', 'binary', '-R', '.note',
             '-R', '.note.gnu.build-id', '-R', '.comment', '-S', root / 'vmlinux', output])
        require(sha256(output) == sha256(root / 'Image'), 'vmlinux does not regenerate the delivered Image')
    built = exports(root / 'Module.symvers')
    baseline = exports(root / 'reports/baseline-Module.symvers')
    require(all(built.get(name) == record for name, record in baseline.items()),
            'Stock baseline exports/CRCs/namespaces changed')
    offline = json.loads((root / 'reports/offline/verification.json').read_text())
    require(offline.get('passed') is True and not offline.get('failures'), 'Offline build validation failed')
    require(offline['checks']['image_and_elf']['result']['Image_sha256'] == sha256(root / 'Image'),
            'Offline report describes a different Image')
    uapi = json.loads((root / 'reports/uapi/complete-uapi-alignment.json').read_text())
    manager_headers = root / 'reports/uapi/manager-headers'
    hashes = {path.name: sha256(path) for path in manager_headers.glob('*.h')}
    require(hashes == identity['manager_uapi_sha256'] and uapi['manager_sha'] == identity['manager_commit'],
            'Delivered Manager headers differ from build identity')
    require(all(hashes.get(name) == digest for name, digest in uapi['header_sha256']['manager'].items()),
            'UAPI comparison used different Manager headers')
    count = uapi.get('compared_shared_ioctl_constants')
    require(type(count) is int and count > 0
            and uapi.get('compatibility_rule') == 'shared-interfaces-equal'
            and uapi.get('shared_interfaces_all_equal') is True
            and uapi.get('arm64_values_all_equal') is True
            and uapi.get('profile_version') == 4, 'Incomplete Manager UAPI validation')
    return release


def verify_boot(root):
    data = (root / 'boot.img').read_bytes()
    image = (root / 'Image').read_bytes()
    stock_header = (root / 'reports/stock-boot-header.bin').read_bytes()
    require(len(data) == 100663296 and data[:8] == b'ANDROID!', 'Incorrect boot image size/magic')
    require(struct.unpack_from('<I', data, 40)[0] == 4
            and struct.unpack_from('<I', data, 12)[0] == 0, 'Boot v4 / empty ramdisk required')
    require(struct.unpack_from('<I', data, 8)[0] == len(image), 'Boot kernel_size mismatch')
    require(data[4096:4096 + len(image)] == image, 'Boot payload differs from Image')
    require(len(stock_header) == 4096 and stock_header[:8] + stock_header[12:] == data[:8] + data[12:4096],
            'Unexpected stock boot header change')
    run([root / 'verification-tools/avbtool', 'verify_image', '--image', root / 'boot.img'])


def verify_zip(root):
    identity = read_identity(root)
    expected = json.loads((root / 'reports/anykernel-template.json').read_text())
    with zipfile.ZipFile(root / identity['kernel_zip']) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'Duplicate AnyKernel ZIP entries')
        require(set(names) == set(expected) | {'Image', 'anykernel.sh'}, 'AnyKernel entry set changed')
        require(archive.testzip() is None, 'Corrupt AnyKernel ZIP')
        for name, digest in expected.items():
            require(hashlib.sha256(archive.read(name)).hexdigest() == digest,
                    f'Unexpected AnyKernel template change: {name}')
        require(hashlib.sha256(archive.read('tools/ak3-core.sh')).hexdigest() == CORE_SHA,
                'Unaudited AnyKernel core')
        require(archive.read('Image') == (root / 'Image').read_bytes(), 'AnyKernel Image mismatch')
        script = archive.read('anykernel.sh').decode()
        require(script.index('SLOT_SELECT=active;') < script.index('. tools/ak3-core.sh;'),
                'Slot selection must precede core initialization')
        require(re.findall(r'^device.name\d+=(.*)$', script, re.M) == ['fuxi']
                and 'BLOCK=boot;' in script and 'do.devicecheck=1' in script,
                'AnyKernel device or target changed')
        require(str(identity['driver_version']) in script and 'SUSFS ' + identity['susfs_short'] in script,
                'Incorrect AnyKernel release label')


def verify_modules(root):
    records = json.loads((root / 'reports/offline/built-module-checks.json').read_text())
    require(len(records) == 2, 'Expected exactly two verified modules')
    checked = {Path(row['module']).name: row for row in records}
    require(checked.keys() == {'zram.ko', 'zsmalloc.ko'}, 'Incorrect verified module names')
    cert = root / 'public-certificates/signing_key.x509'
    for name, row in checked.items():
        path = root / 'matching-gki-modules' / name
        require(row['sha256'] == sha256(path) and row['signature']['valid'] is True,
                f'Module is not the independently verified file: {name}')
        data = path.read_bytes()
        marker = b'~Module signature appended~\n'
        require(data.endswith(marker), f'Missing module signature: {name}')
        end = len(data) - len(marker)
        length = struct.unpack('>I', data[end - 4:end])[0]
        require(0 < length <= end - 12 and data[end - 10] == 2, f'Invalid signature header: {name}')
        with tempfile.TemporaryDirectory(prefix='delivery-signature-') as temp:
            temp = Path(temp)
            (temp / 'content').write_bytes(data[:end - 12 - length])
            (temp / 'signature').write_bytes(data[end - 12 - length:end - 12])
            run(['openssl', 'x509', '-inform', 'DER', '-in', cert, '-out', temp / 'cert.pem'])
            run(['openssl', 'cms', '-verify', '-binary', '-inform', 'DER', '-in', temp / 'signature',
                 '-content', temp / 'content', '-certfile', temp / 'cert.pem', '-nointern',
                 '-noverify', '-out', '/dev/null'])


def tree_manifest(root):
    records = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if '.git' in relative.parts or '__pycache__' in relative.parts:
            continue
        if path.is_symlink():
            # Python's safe tar extraction removes redundant './' components.
            # PurePosixPath preserves '..' traversal and the destination name.
            records[str(relative)] = {'type': 'symlink', 'target': str(PurePosixPath(os.readlink(path)))}
        elif path.is_file():
            records[str(relative)] = {'type': 'file', 'sha256': sha256(path),
                                      'executable': bool(path.stat().st_mode & stat.S_IXUSR)}
    return records


def extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        source.extractall(destination, filter='data')


def verify_replay(root):
    identity = read_identity(root)
    expected = json.loads((root / 'source-diffs/integrated-source-manifest.json').read_text())
    with tempfile.TemporaryDirectory(prefix='delivery-source-replay-') as temp:
        temp = Path(temp)
        common = temp / 'kernel/common'
        extract(root / 'source/common-base.tar.gz', common)
        unpack = temp / 'unpack'
        extract(root / 'source' / identity['builtin_archive'], unpack)
        children = list(unpack.iterdir())
        require(len(children) == 1 and children[0].is_dir(), 'Unexpected builtin archive layout')
        ksu = temp / 'kernel/KernelSU'
        children[0].rename(ksu)
        run(['git', 'apply', '--whitespace=nowarn', root / 'source-diffs/common.diff'], cwd=common)
        run(['git', 'apply', '--whitespace=nowarn', root / 'source-diffs/SukiSU.diff'], cwd=ksu)
        require((common / 'drivers/kernelsu').is_symlink()
                and os.readlink(common / 'drivers/kernelsu') == '../../KernelSU/kernel'
                and (common / 'drivers/kernelsu/Kconfig').is_file(), 'Driver link missing after replay')
        for name, directory in [('common', common), ('KernelSU', ksu)]:
            actual = tree_manifest(directory)
            require(actual == expected[name],
                    f'Replayed {name} differs: missing={sorted(expected[name].keys()-actual.keys())[:10]}, '
                    f'extra={sorted(actual.keys()-expected[name].keys())[:10]}, '
                    f'changed={[key for key in actual.keys() & expected[name].keys() if actual[key] != expected[name][key]][:10]}')
        require(re.search(r'^config\s+KSU_SUSFS\s*$', (ksu / 'kernel/Kconfig').read_text(), re.M)
                and 'susfs_init();' in (ksu / 'kernel/ksu.c').read_text()
                and 'void susfs_init(void)' in (common / 'fs/susfs.c').read_text()
                and '#define SUSFS_VERSION "' + identity['susfs_version'] + '"' in (common / 'include/linux/susfs.h').read_text(),
                'Replayed source does not implement the required SUSFS initialization')
    return True


def verify_delivery(root):
    root = root.resolve()
    identity = read_identity(root)
    release, count = verify_inventory(root)
    kernel_release = verify_image(root)
    verify_boot(root)
    verify_zip(root)
    verify_modules(root)
    tool = (root / 'tools/ksu_susfs').read_bytes()
    require(tool[:6] == b'\x7fELF\x02\x01' and struct.unpack_from('<H', tool, 18)[0] == 183,
            'SUSFS tool is not ELF64 AArch64')
    require(hashlib.sha256(tool).hexdigest() == identity['ksu_susfs']['sha256']
            and len(tool) == identity['ksu_susfs']['bytes'], 'SUSFS tool differs from the build identity')
    spec = importlib.util.spec_from_file_location('delivery_build_provenance', root / 'scripts/build_provenance.py')
    provenance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(provenance)
    provenance.verify_delivered(root)
    verify_replay(root)
    return {'passed': True, 'files': count, 'kernel_release': kernel_release,
            'driver_version': release['driver_version'], 'manager_version': identity['manager_version_code'],
            'susfs_version': identity['susfs_version'], 'complete_source_replay': True,
            'build_input_output_provenance_verified': True,
            'runtime_compatibility_verified': False, 'phone_operations': False,
            'limitations': ['No device was accessed. SUSFS runtime behavior requires an authorized boot test.',
                            'Export/CRC validation does not establish complete CFI or hardware compatibility.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--art-dir', type=Path,
                        default=Path(__file__).resolve().parent.parent / 'artifacts')
    parser.add_argument('--report', type=Path)
    args = parser.parse_args()
    try:
        result = verify_delivery(args.art_dir)
    except Exception as error:
        result = {'passed': False, 'error': f'{type(error).__name__}: {error}', 'phone_operations': False}
    if args.report:
        require(args.art_dir.resolve() not in args.report.resolve().parents,
                'Write verification reports outside immutable delivery')
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
