#!/usr/bin/env python3
"""Read or import pinned upstream source without checking out a Linux tree."""
import argparse
import base64
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = {"common": "common", "kernelsu": "KernelSU", "sukisu": "SukiSU", "manager": "SukiSU"}


def logical_path(value):
    value = value.replace("\\", "/")
    parts = PurePosixPath(value).parts
    if (len(parts) < 2 or value.startswith("/") or ":" in value
            or any(part in {"..", ".git"} for part in parts)):
        raise ValueError("Use a relative component/path without parent traversal")
    component = COMPONENTS.get(parts[0].lower())
    if component is None:
        raise ValueError("Component must be common, KernelSU or SukiSU")
    path = "/".join(parts[1:])
    links = json.loads((ROOT / "configs/source-links.json").read_text(encoding="utf-8"))
    if component == "common" and (path == "drivers/kernelsu" or path.startswith("drivers/kernelsu/")):
        component, path = "KernelSU", "kernel" + path[len("drivers/kernelsu"):]
    if component == "KernelSU" and (path == "uapi" or path.startswith("uapi/")):
        path = "kernel/include/uapi" + path[len("uapi"):]
    if component == "SukiSU":
        for prefix, target in sorted(links.get("manager_links", {}).items(), key=lambda p: -len(p[0])):
            if path == prefix or path.startswith(prefix + "/"):
                path = posixpath.normpath(posixpath.join(posixpath.dirname(prefix), target) + path[len(prefix):])
                break
    if path.startswith("../") or path in {".", ".."}:
        raise ValueError("Declared source link escapes its component")
    return component, path


def fetch(component, path):
    layout = json.loads((ROOT / "ci-layout.json").read_text(encoding="utf-8"))
    sha = layout[{"common": "kernel_common_sha", "KernelSU": "builtin_sha", "SukiSU": "manager_sha"}[component]]
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Source revision is not a complete pinned commit")
    encoded = quote(path, safe="/")
    if component == "common":
        urls = [(f"https://raw.githubusercontent.com/aosp-mirror/kernel_common/{sha}/{encoded}", False),
                (f"https://android.googlesource.com/kernel/common/+/{sha}/{encoded}?format=TEXT", True)]
    else:
        urls = [(f"https://raw.githubusercontent.com/SukiSU-Ultra/SukiSU-Ultra/{sha}/{encoded}", False)]
    for number, (url, encoded_body) in enumerate(urls):
        try:
            with urlopen(Request(url, headers={"User-Agent": "fuxi-kernel-ci-source"}), timeout=60) as response:
                data = response.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("Requested source exceeds 32 MiB")
            return base64.b64decode(data, validate=True) if encoded_body else data
        except HTTPError as error:
            if error.code != 404 or number == len(urls) - 1:
                raise
    raise ValueError("Source could not be fetched")


def windows_path(path):
    for part in PurePosixPath(path).parts:
        if (not part or part.rstrip(" .") != part or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)):
            raise ValueError(f"Path cannot be imported safely on Windows: {part!r}; use read instead")


def local_path(component, path, *, materialize=False):
    source_root = ROOT / "src"
    base = source_root / component
    target = base / path
    if (source_root.is_symlink() or base.is_symlink()
            or not base.resolve().is_relative_to(source_root.resolve())
            or not target.resolve().is_relative_to(base.resolve())):
        raise ValueError("Local source path escapes its component")
    if materialize:
        windows_path(path)
        current = base
        for part in PurePosixPath(path).parts:
            if current.is_dir():
                for entry in current.iterdir():
                    if entry.name.casefold() == part.casefold() and entry.name != part:
                        raise ValueError(f"Windows case collision with {entry.name!r}")
            current /= part
            if current.is_symlink():
                raise ValueError("Import destination traverses a local symbolic link")
    return target


def read(value, *, upstream=False):
    component, path = logical_path(value)
    local = local_path(component, path)
    return fetch(component, path) if upstream or not local.is_file() else local.read_bytes()


def materialize(value, *, force=False):
    component, path = logical_path(value)
    target = local_path(component, path, materialize=True)
    if target.exists() and not force:
        raise FileExistsError("Source already exists; edit it directly or use --force to restore upstream")
    data = fetch(component, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".fuxi-source-", dir=target.parent, delete=False) as stream:
            stream.write(data)
            temporary = Path(stream.name)
        temporary.replace(target)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    reader = commands.add_parser("read", help="Read local overlay first, otherwise the pinned upstream")
    reader.add_argument("path")
    reader.add_argument("--upstream", action="store_true")
    importer = commands.add_parser("import", aliases=["materialize"], help="Copy pinned upstream into an editable overlay file")
    importer.add_argument("path")
    importer.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "read":
            sys.stdout.buffer.write(read(args.path, upstream=args.upstream))
        else:
            print(materialize(args.path, force=args.force))
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"Source operation stopped: {error}\n")


if __name__ == "__main__":
    main()
