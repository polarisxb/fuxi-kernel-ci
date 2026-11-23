#!/usr/bin/env python3
"""Bind one successful build to frozen local inputs and its published payloads.

The launcher holds .pipeline.lock. This guards accidental local changes; it is
not a signature scheme against an owner deliberately forging their own records.
"""
import argparse
from datetime import datetime, timezone
from functools import lru_cache
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import uuid

# Local helper imports must not mutate an exact, immutable delivery inventory.
sys.dont_write_bytecode = True

BUILD_SCRIPTS = ('run-build.sh', 'preflight.py', 'check-config.py', 'build_provenance.py')
PATCHES = ('common-final.diff', 'sukisu-final.diff')
OUTPUTS = {
    'Image': 'arch/arm64/boot/Image',
    'vmlinux': 'vmlinux',
    '.config': '.config',
    'System.map': 'System.map',
    'Module.symvers': 'Module.symvers',
    'kernel.release': 'include/config/kernel.release',
    'public-certificates/signing_key.x509': 'certs/signing_key.x509',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


@lru_cache(maxsize=1)
def verifier():
    spec = importlib.util.spec_from_file_location('provenance_delivery_helpers',
                                                Path(__file__).with_name('verify-delivery.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.next')
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')
    os.replace(temporary, path)


def file_record(path):
    require(path.is_file() and not path.is_symlink(), f'Missing regular input/output: {path}')
    return {'sha256': verifier().sha256(path), 'bytes': path.stat().st_size}


def shared_inputs(root):
    records = {}
    entries = json.loads((root / 'configs/locked-workspace-inputs.json').read_text())
    for entry in entries:
        path = root / 'kernel' / entry['path']
        sha = subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        require(sha == entry['sha'], f'Locked revision changed: {entry["path"]}')
        if entry.get('shared_readonly'):
            dirty = subprocess.check_output(['git', '-C', str(path), 'status', '--porcelain',
                                             '--untracked-files=all'], text=True)
            require(not dirty, f'Shared dependency has uncommitted changes: {entry["path"]}')
        records[entry['path']] = sha
    tools = {}
    for name, expected in json.loads((root / 'configs/tool-input-sha256.json').read_text()).items():
        actual = verifier().sha256(root / 'kernel' / name)
        require(actual == expected, f'Pinned tool content changed: {name}')
        tools[name] = actual
    return {'revisions': records, 'tools': tools}


def input_manifest(root):
    return {
        'sources': {name: verifier().tree_manifest(root / 'kernel' / name)
                    for name in ('common', 'KernelSU')},
        'configs': verifier().tree_manifest(root / 'configs'),
        'build_scripts': {name: file_record(root / 'scripts' / name) for name in BUILD_SCRIPTS},
        'patches': {name: file_record(root / 'patches' / name) for name in PATCHES},
        'shared_inputs': shared_inputs(root),
    }


def output_manifest(out):
    records = {name: file_record(out / path) for name, path in OUTPUTS.items()}
    release = (out / OUTPUTS['kernel.release']).read_text().strip()
    modules = out.parent / 'staging/lib/modules' / release / 'kernel'
    paths = sorted(modules.rglob('*.ko'))
    require(len(paths) == 2 and {p.name for p in paths} == {'zram.ko', 'zsmalloc.ko'},
            'Build provenance requires exactly the two matching GKI modules')
    for path in paths:
        records['matching-gki-modules/' + path.name] = file_record(path)
    return records


def state_paths(root):
    return (root / 'reports/.build-freeze.json', root / 'reports/build-attempt.json',
            root / 'out/sukisu/build-provenance.json')


def freeze_paths(root):
    """Do not follow shared links or include mutable Git/cache internals."""
    paths = []
    for relative in ('kernel/common', 'kernel/KernelSU', 'configs', 'scripts', 'patches'):
        base = root / relative
        require(base.is_dir() and not base.is_symlink(), f'Expected owned input directory: {base}')
        for current, directories, files in os.walk(base, followlinks=False):
            directories[:] = [name for name in directories if name not in {'.git', '__pycache__'}
                              and not (Path(current) / name).is_symlink()]
            paths.append(Path(current))
            paths.extend(Path(current) / name for name in files
                         if not (Path(current) / name).is_symlink() and name != '.git')
    return paths


def restore_permissions(root):
    journal, _, _ = state_paths(root)
    if not journal.exists():
        return
    records = json.loads(journal.read_text())['permissions']
    for name, mode in records.items():
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and '..' not in relative.parts,
                'Unsafe permission journal path')
        path = root / name
        require(not path.is_symlink(), f'Frozen input replaced by symlink: {name}')
        if path.exists():
            path.chmod(mode)
    journal.unlink()


def begin(root, recipe):
    journal, attempt, completed = state_paths(root)
    # Called under the launcher lock; any remaining journal belongs to a dead
    # previous launcher. Recover its modes before taking a new snapshot.
    restore_permissions(root)
    completed.unlink(missing_ok=True)
    record = {'schema_version': 1, 'status': 'running', 'attempt': uuid.uuid4().hex,
              'started_at': datetime.now(timezone.utc).isoformat(), 'recipe': recipe}
    atomic_json(attempt, record)
    permissions = {str(path.relative_to(root)): stat.S_IMODE(path.stat().st_mode)
                   for path in freeze_paths(root)}
    atomic_json(journal, {'attempt': record['attempt'], 'permissions': permissions})
    try:
        for name, mode in permissions.items():
            (root / name).chmod(mode & ~0o222)
        record['inputs'] = input_manifest(root)
        atomic_json(attempt, record)
    except BaseException:
        abort(root)
        raise
    return record


def finish(root, out):
    journal, attempt, completed = state_paths(root)
    require(journal.is_file() and attempt.is_file(), 'No active frozen build attempt')
    record = json.loads(attempt.read_text())
    require(record.get('status') == 'running' and 'inputs' in record, 'Invalid active build attempt')
    permissions = json.loads(journal.read_text())
    require(permissions['attempt'] == record['attempt'], 'Freeze journal belongs to another build')
    require(all(not ((root / name).stat().st_mode & 0o222)
                for name in permissions['permissions']), 'Build input was made writable')
    require(input_manifest(root) == record['inputs'], 'Build inputs changed during compilation')
    record.update({'outputs': output_manifest(out), 'status': 'complete', 'build_returncode': 0,
                   'completed_at': datetime.now(timezone.utc).isoformat()})
    atomic_json(completed, record)
    atomic_json(attempt, {'schema_version': 1, 'status': 'complete', 'attempt': record['attempt']})
    return record


def abort(root):
    _, attempt, completed = state_paths(root)
    # An EXIT trap after successful finish must not erase the valid record.
    running = None
    if attempt.exists():
        running = json.loads(attempt.read_text())
    if running and running.get('status') == 'running':
        completed.unlink(missing_ok=True)
        atomic_json(attempt, {'schema_version': 1, 'status': 'failed',
                              'attempt': running.get('attempt')})
    restore_permissions(root)


def verify_record(record):
    require(record.get('schema_version') == 1 and record.get('status') == 'complete'
            and record.get('build_returncode') == 0 and bool(record.get('attempt')),
            'Missing or unsuccessful build provenance')
    require(set(record.get('outputs', {})) == set(OUTPUTS) |
            {'matching-gki-modules/zram.ko', 'matching-gki-modules/zsmalloc.ko'},
            'Incomplete build output provenance')
    return record


def verify_workspace(root, out):
    journal, _, completed = state_paths(root)
    require(not journal.exists(), 'Cannot verify an active or interrupted frozen build')
    require(completed.is_file(), 'Missing successful build provenance; rebuild through run-build.sh')
    record = verify_record(json.loads(completed.read_text()))
    require(input_manifest(root) == record['inputs'], 'Inputs differ from the successful build; rebuild required')
    require(output_manifest(out) == record['outputs'], 'Outputs differ from the successful build')
    return record


def verify_delivered(root):
    record = verify_record(json.loads((root / 'reports/build-provenance.json').read_text()))
    expected_sources = json.loads((root / 'source-diffs/integrated-source-manifest.json').read_text())
    require(expected_sources == record['inputs']['sources'], 'Delivered sources differ from build provenance')
    require(verifier().tree_manifest(root / 'configs') == record['inputs']['configs'],
            'Delivered configuration inputs differ from build provenance')
    for name in BUILD_SCRIPTS:
        require(file_record(root / 'scripts' / name) == record['inputs']['build_scripts'][name],
                'Delivered build script differs from build provenance: ' + name)
    for name, published in zip(PATCHES, ('common.diff', 'SukiSU.diff')):
        require(file_record(root / 'source-diffs' / published) == record['inputs']['patches'][name],
                'Delivered source patch differs from build provenance: ' + name)
    for name, expected in record['outputs'].items():
        require(file_record(root / name) == expected, 'Delivered payload differs from build provenance: ' + name)
    return True


def run_build(root, command):
    recipe = {'argv': command, 'cwd': str(Path.cwd()),
              'environment': {key: os.environ[key] for key in
                              ('BUILD_CONFIG', 'GKI_BUILD_CONFIG_FRAGMENT', 'LTO', 'OUT_DIR',
                               'KMI_SYMBOL_LIST_STRICT_MODE', 'KBUILD_BUILD_TIMESTAMP',
                               'SOURCE_DATE_EPOCH', 'KBUILD_BUILD_USER', 'KBUILD_BUILD_HOST',
                               'KBUILD_BUILD_VERSION') if key in os.environ}}
    begin(root, recipe)
    try:
        preflight = subprocess.run([sys.executable, str(root / "scripts/preflight.py")])
        if preflight.returncode:
            return preflight.returncode
        result = subprocess.run(command)
        if result.returncode != 0:
            return result.returncode if result.returncode > 0 else 128 - result.returncode
        finish(root, root / 'out/sukisu/common')
        return 0
    finally:
        abort(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('run', 'abort', 'verify'))
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.action == 'run':
        command = args.command[1:] if args.command[:1] == ['--'] else args.command
        require(bool(command), 'Missing build command')
        return run_build(root, command)
    if args.action == 'abort':
        abort(root)
    else:
        verify_workspace(root, root / 'out/sukisu/common')
        print('Build input/output provenance verified.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
