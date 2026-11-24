#!/usr/bin/env python3
"""Shared helpers for the pinned upstreams and per-component patch queues.

ci-layout.json pins every upstream; patches/<queue>/series lists the local
changes applied in order on top of them. bootstrap.py applies the queues to
full upstream trees on Linux CI, and source.py applies them to a few files on
Windows. Both use `git apply` without fuzz, so a patch either applies exactly
or stops the assembly with its name.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess


QUEUES = ("common", "driver", "manager")
SERIES = "series"
PATCH_NAME = re.compile(r"^[0-9]{4}-[a-z0-9][a-z0-9.-]*\.patch$")
REQUIRED_HEADERS = ("Subject:", "Upstream-Status:", "Drop-when:")
GIT = ("git", "-c", "core.autocrlf=false", "-c", "core.eol=lf", "-c", "core.safecrlf=false")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def load_layout(root):
    layout = json.loads((Path(root) / "ci-layout.json").read_text(encoding="utf-8"))
    upstreams = layout.get("upstreams", {})
    for name in ("common", "susfs", "driver", "manager", "ndk"):
        commit = upstreams.get(name, {}).get("commit", "")
        require(re.fullmatch(r"[0-9a-f]{40}", commit), f"ci-layout.json must pin {name} to a full commit")
        backups = upstreams[name].get("backup_urls", [])
        require(isinstance(backups, list) and len(set(backups)) == len(backups)
                and all(isinstance(url, str) and url.startswith("https://") and url != upstreams[name].get("url")
                        for url in backups),
                f"ci-layout.json {name} backup_urls must be distinct HTTPS URLs other than its url")
    require(set(layout.get("patch_queues", {})) == set(QUEUES), "ci-layout.json patch_queues is incomplete")
    return layout


def queue_directory(root, layout, queue):
    require(queue in QUEUES, f"Unknown patch queue: {queue}")
    relative = PurePosixPath(layout["patch_queues"][queue])
    require(not relative.is_absolute() and ".." not in relative.parts, "Unsafe patch queue path")
    return Path(root) / relative


def read_series(directory):
    """Return the queue's patch files in order; every patch must be listed once."""
    directory = Path(directory)
    names = []
    for line in (directory / SERIES).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        require(PATCH_NAME.fullmatch(line), f"Invalid patch name in {directory.name}/series: {line!r}")
        require(line not in names, f"Patch listed twice in {directory.name}/series: {line}")
        require((directory / line).is_file() and not (directory / line).is_symlink(),
                f"Listed patch is missing: {directory.name}/{line}")
        names.append(line)
    present = {path.name for path in directory.glob("*.patch")}
    unlisted = sorted(present - set(names))
    require(not unlisted, f"Patches missing from {directory.name}/series: {unlisted}")
    return [directory / name for name in names]


def split_patch(text):
    """Split a patch file into its description header and the git diff."""
    match = re.search(r"(?m)^diff --git ", text)
    require(match, "Patch contains no git diff")
    return text[:match.start()], text[match.start():]


def check_header(header, name):
    for key in REQUIRED_HEADERS:
        require(re.search(rf"(?m)^{re.escape(key)} \S", header), f"{name}: header lacks '{key}'")


def safe_relative(value):
    path = PurePosixPath(value)
    require(value and "\\" not in value and ":" not in value and not path.is_absolute()
            and ".." not in path.parts and ".git" not in path.parts and str(path) == value,
            f"Unsafe patch path: {value!r}")
    return value


def patch_paths(diff):
    """Return every path a git diff creates, modifies or deletes."""
    paths = []
    for old, new in re.findall(r"(?m)^diff --git a/(\S+) b/(\S+)$", diff):
        for value in (old, new):
            if safe_relative(value) not in paths:
                paths.append(value)
    require(paths, "Patch touches no files")
    return paths


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git_apply(root, patch, label, include=None):
    """Apply one patch exactly inside root; never search an enclosing repository."""
    root = Path(root)
    command = [*GIT, "apply", "--whitespace=nowarn"]
    command += [f"--include={path}" for path in include or ()]
    command.append("-")
    environment = dict(os.environ, GIT_CEILING_DIRECTORIES=str(root.resolve().parent))
    result = subprocess.run(command, cwd=root, input=patch, capture_output=True, env=environment)
    if result.returncode:
        raise ValueError(f"{label} does not apply: " + result.stderr.decode(errors="replace").strip())


def apply_series(root, directory, include=None, upto=None):
    """Apply the queue (optionally only its first <upto> patches) and return records."""
    selected = set(include) if include is not None else None
    records = []
    for path in read_series(directory)[:upto]:
        data = path.read_bytes()
        header, diff = split_patch(data.decode("utf-8"))
        check_header(header, path.name)
        touched = patch_paths(diff)
        records.append({"name": path.name, "sha256": sha256_bytes(data)})
        if selected is not None and not selected.intersection(touched):
            continue
        git_apply(root, data, path.name, include=sorted(selected) if selected is not None else None)
    return records


def install_susfs(common, susfs_root, info):
    """Copy the official SUSFS sources into common and apply its GKI patch."""
    common, susfs_root = Path(common), Path(susfs_root)
    for target, source in sorted(info["kernel_files"].items()):
        destination = common / safe_relative(target)
        require(not destination.exists() and not destination.is_symlink(),
                f"SUSFS file would replace an upstream kernel file: {target}")
        origin = susfs_root / safe_relative(source)
        require(origin.is_file() and not origin.is_symlink(), f"Pinned SUSFS file missing: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination)
    patch = susfs_root / safe_relative(info["kernel_patch"])
    require(patch.is_file() and not patch.is_symlink(), "Pinned SUSFS kernel patch missing")
    git_apply(common, patch.read_bytes(), "Official SUSFS kernel patch")
    return {"kernel_patch_sha256": sha256_bytes(patch.read_bytes()),
            "kernel_files": sorted(info["kernel_files"])}


def kernel_version(makefile_text):
    values = {}
    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL", "EXTRAVERSION"):
        match = re.search(rf"(?m)^{key} = ?(\S*)$", makefile_text)
        require(match, f"Kernel Makefile lacks {key}")
        values[key] = match.group(1)
    return "{VERSION}.{PATCHLEVEL}.{SUBLEVEL}{EXTRAVERSION}".format(**values)


def scmversion(makefile_text, release, common_commit):
    """The .scmversion suffix that makes the kernel report the locked release."""
    version = kernel_version(makefile_text)
    require(release.startswith(version + "-"),
            f"kernel_release {release!r} does not start with this tree's version {version}")
    require(f"-g{common_commit[:12]}" in release, "kernel_release does not name the pinned common commit")
    return release[len(version):] + "\n"


def write_scmversion(common, release, common_commit):
    common = Path(common)
    target = common / ".scmversion"
    require(not target.exists() and not target.is_symlink(), "Upstream tree already has .scmversion")
    target.write_text(scmversion((common / "Makefile").read_text(), release, common_commit),
                      encoding="utf-8", newline="\n")
