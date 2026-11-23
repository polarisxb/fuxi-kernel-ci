#!/usr/bin/env python3
"""Capture reproducible source changes, including the untracked driver link."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent.parent
COMMON = ROOT / 'kernel/common'


def run(args, cwd, **kwargs):
    return subprocess.check_output(args, cwd=cwd, **kwargs)


with tempfile.TemporaryDirectory(prefix='fuxi-source-capture-') as temporary:
    work = Path(temporary)
    environment = dict(os.environ, GIT_INDEX_FILE=str(work / 'common-index'))
    run(['git', 'read-tree', 'HEAD'], COMMON, env=environment)
    modified = run(['git', 'diff', '--name-only', '-z', 'HEAD'], COMMON).decode().split('\0')
    added = run(['git', 'ls-files', '--others', '--exclude-standard', '-z'], COMMON).decode().split('\0')
    required = ['drivers/kernelsu', '.scmversion']
    paths = sorted(set(filter(None, modified + added + required)))
    run(['git', 'add', '-f', '--', *paths], COMMON, env=environment)
    patch = run(['git', 'diff', '--cached', '--binary', 'HEAD'], COMMON, env=environment)
    if b'diff --git a/drivers/kernelsu b/drivers/kernelsu' not in patch:
        raise ValueError('Driver symlink is missing from the captured changes')
    (ROOT / 'patches/common-final.diff').write_bytes(patch)

    pristine = work / 'pristine'
    pristine.mkdir()
    with tarfile.open(ROOT / 'downloads/builtin-e2912817f4e1.tar.gz') as archive:
        archive.extractall(pristine, filter='data')
    tree = next(pristine.iterdir())
    run(['git', 'init', '-q'], tree)
    run(['git', 'add', '-f', '.'], tree)
    baseline = run(['git', 'write-tree'], tree, text=True).strip()
    for source in (ROOT / 'kernel/KernelSU').iterdir():
        target = tree / source.name
        if source.is_symlink():
            if target.is_symlink() and target.readlink() == source.readlink():
                continue
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            target.symlink_to(source.readlink(), target_is_directory=source.is_dir())
        elif source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True)
        else:
            shutil.copy2(source, target)
    run(['git', 'add', '-f', '-A'], tree)
    patch = run(['git', 'diff', '--cached', '--binary', baseline], tree)
    (ROOT / 'patches/sukisu-final.diff').write_bytes(patch)
    run(['git', 'apply', '--reverse', '--check', str(ROOT / 'patches/sukisu-final.diff')], tree)

manifest = {}
for source in (ROOT / 'kernel/KernelSU').rglob('*'):
    if source.is_file() and not source.is_symlink():
        manifest[str(source.relative_to(ROOT / 'kernel/KernelSU'))] = hashlib.sha256(source.read_bytes()).hexdigest()
(ROOT / 'reports/kernel-driver-source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(f'Captured complete patches and {len(manifest)} driver source hashes.')
