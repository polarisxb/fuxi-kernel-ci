#!/usr/bin/env python3
"""Read pinned sources and edit this repository's patch queues on Windows.

Nothing here checks out a full Linux tree. A "view" fetches only the files it
needs at the commits pinned in ci-layout.json, applies the official SUSFS
kernel patch (for common) and then the queue in patches/<queue>/series.

  read     print a file as CI will build it (--upstream for the pristine file)
  edit     put the files of one patch into .work/edit/<queue>/ for editing
  new      start a new patch at the end of a queue for the given files
  refresh  write the edited files back into the patch and check later patches
  discard  drop the current edit session
  status   show the current edit session
"""
import argparse
import base64
import importlib.util
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("patch_queue", Path(__file__).resolve().with_name("patch_queue.py"))
Q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(Q)

ALIASES = {"common": "common", "driver": "driver", "kernelsu": "driver",
           "manager": "manager", "sukisu": "manager", "module": "module"}
NEW_HEADER = ("Subject: {subject}\nUpstream-Status: Pending\n"
              "Drop-when: TODO: the upstream change that makes this patch unnecessary\n\n"
              "TODO: why this change is needed.\n")


def layout():
    return Q.load_layout(ROOT)


def logical_path(value):
    """Map component/path, following the Linux links CI creates, to (queue, path)."""
    value = value.replace("\\", "/")
    parts = PurePosixPath(value).parts
    if (len(parts) < 2 or value.startswith("/") or ":" in value
            or any(part in {"..", ".git"} for part in parts)):
        raise ValueError("Use a relative component/path without parent traversal")
    queue = ALIASES.get(parts[0].lower())
    if queue is None:
        raise ValueError("Component must be common, driver (KernelSU), manager (SukiSU) or module (SUSFS module)")
    path = "/".join(parts[1:])
    if queue == "common" and (path == "drivers/kernelsu" or path.startswith("drivers/kernelsu/")):
        queue, path = "driver", "kernel" + path[len("drivers/kernelsu"):]
    if queue == "driver" and (path == "uapi" or path.startswith("uapi/")):
        path = "kernel/include/uapi" + path[len("uapi"):]
    if queue == "manager":
        links = json.loads((ROOT / "configs/source-links.json").read_text(encoding="utf-8"))
        for prefix, target in sorted(links.get("manager_links", {}).items(), key=lambda p: -len(p[0])):
            if path == prefix or path.startswith(prefix + "/"):
                path = posixpath.normpath(posixpath.join(posixpath.dirname(prefix), target) + path[len(prefix):])
                break
    if path.startswith("../") or path in {".", ".."}:
        raise ValueError("Declared source link escapes its component")
    return queue, Q.safe_relative(path)


def raw_urls(name, path):
    """Pinned raw-file URLs for one upstream, then its backups; only known hosts are contacted."""
    info = layout()["upstreams"][name]
    sha, encoded = info["commit"], quote(path, safe="/")
    if name == "common":
        return [(f"https://raw.githubusercontent.com/aosp-mirror/kernel_common/{sha}/{encoded}", False),
                (f"https://android.googlesource.com/kernel/common/+/{sha}/{encoded}?format=TEXT", True)]
    urls = []
    for url in (info["url"], *info.get("backup_urls", ())):
        match = re.fullmatch(r"https://(github\.com|gitlab\.com)/([\w.-]+)/([\w.-]+?)(?:\.git)?", url)
        if not match:
            raise ValueError(f"No raw file endpoint for {url}")
        host, owner, repo = match.groups()
        urls.append((f"https://raw.githubusercontent.com/{owner}/{repo}/{sha}/{encoded}" if host == "github.com"
                     else f"https://gitlab.com/{owner}/{repo}/-/raw/{sha}/{encoded}", False))
    return urls


def fetch(name, path):
    """Return the pinned upstream file, or None when the commit does not have it."""
    for url, encoded_body in raw_urls(name, path):
        try:
            with urlopen(Request(url, headers={"User-Agent": "fuxi-kernel-ci-source"}), timeout=60) as response:
                data = response.read(32 * 1024 * 1024 + 1)
        except HTTPError as error:
            if error.code == 404:
                continue
            raise
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("Requested source exceeds 32 MiB")
        return base64.b64decode(data, validate=True) if encoded_body else data
    return None


def windows_path(path):
    for part in PurePosixPath(path).parts:
        if (not part or part.rstrip(" .") != part or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part, re.I)):
            raise ValueError(f"Path cannot be edited safely on Windows: {part!r}; use read instead")
    return path


def unique_case(paths):
    seen = {}
    for path in paths:
        other = seen.setdefault(path.casefold(), path)
        if other != path:
            raise ValueError(f"Windows case collision: {other!r} and {path!r}")


def queue_patches(queue):
    return Q.read_series(Q.queue_directory(ROOT, layout(), queue))


def git(cwd, *args, data=None):
    result = subprocess.run([*Q.GIT, "-c", "user.name=fuxi-source", "-c", "user.email=source@localhost",
                             "-c", "commit.gpgsign=false", *map(str, args)],
                            cwd=cwd, input=data, capture_output=True)
    if result.returncode:
        raise ValueError(f"git {args[0]} failed: " + result.stderr.decode(errors="replace").strip())
    return result.stdout


def build_view(queue, paths, folder, upto=None, upstream=False):
    """Write the requested paths of the queue's result into folder (a new git root)."""
    folder.mkdir(parents=True, exist_ok=True)
    git(folder, "init", "-q")
    for path in paths:
        data = fetch(queue, path)
        if data is not None and queue == "module" and not upstream:
            # The same rules bootstrap.py applies before the module queue.
            info = layout()["upstreams"]["module"]
            if Q.module_excluded(path, info):
                data = None
            elif path in info["crlf_to_lf"]:
                data = Q.crlf_to_lf(data, path)
        if data is not None:
            (folder / path).parent.mkdir(parents=True, exist_ok=True)
            (folder / path).write_bytes(data)
    if upstream:
        return
    if queue == "common":
        info = layout()["upstreams"]["susfs"]
        for target, source in info["kernel_files"].items():
            if target in paths:
                data = fetch("susfs", source)
                if data is None:
                    raise ValueError(f"Pinned SUSFS file missing: {source}")
                (folder / target).parent.mkdir(parents=True, exist_ok=True)
                (folder / target).write_bytes(data)
        susfs_patch = fetch("susfs", info["kernel_patch"])
        if susfs_patch is None:
            raise ValueError("Pinned SUSFS kernel patch missing")
        if set(paths) & set(Q.patch_paths(Q.split_patch(susfs_patch.decode("utf-8"))[1])):
            Q.git_apply(folder, susfs_patch, "Official SUSFS kernel patch", include=paths)
    Q.apply_series(folder, Q.queue_directory(ROOT, layout(), queue), include=paths, upto=upto)


def susfs_paths():
    info = layout()["upstreams"]["susfs"]
    patch = fetch("susfs", info["kernel_patch"])
    if patch is None:
        raise ValueError("Pinned SUSFS kernel patch missing")
    return set(info["kernel_files"]) | set(Q.patch_paths(Q.split_patch(patch.decode("utf-8"))[1]))


def read(value, *, upstream=False):
    queue, path = logical_path(value)
    try:
        windows_path(path)
    except ValueError:
        # Windows cannot create this name, so it is returned without a view. That
        # is only the built file when no patch, including SUSFS, changes it.
        touched = {name for patch in queue_patches(queue)
                   for name in Q.patch_paths(Q.split_patch(patch.read_text(encoding="utf-8"))[1])}
        if not upstream and (path in touched or (queue == "common" and path in susfs_paths())):
            raise
        data = fetch(queue, path)
        if data is None:
            raise ValueError(f"{value} does not exist upstream")
        return data
    with tempfile.TemporaryDirectory(prefix="fuxi-view-") as temporary:
        folder = Path(temporary) / "view"
        build_view(queue, [path], folder, upstream=upstream)
        target = folder / path
        if not target.is_file():
            raise ValueError(f"{value} does not exist in the {'upstream' if upstream else 'patched'} {queue} tree")
        return target.read_bytes()


def work_root():
    work = ROOT / ".work"
    for path in (work, work / "edit"):
        if path.is_symlink():
            raise ValueError(f"{path.relative_to(ROOT)} must not be a symbolic link")
    return work / "edit"


def state_file():
    return work_root() / "session.json"


def load_session():
    if not state_file().is_file():
        raise ValueError("No edit session; start one with edit or new")
    return json.loads(state_file().read_text(encoding="utf-8"))


def remove_tree(path):
    """Also remove read-only Git objects, which Windows refuses to delete."""
    def retry(function, target, _error):
        Path(target).chmod(stat.S_IWRITE)
        function(target)
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=retry)


def start_session(queue, patch_name, paths, upto, mode):
    paths = sorted({windows_path(Q.safe_relative(path)) for path in paths})
    unique_case(paths)
    root = work_root()
    if state_file().exists():
        raise ValueError("An edit session is already open; refresh or discard it first")
    if root.exists():
        remove_tree(root)
    tree = root / queue
    build_view(queue, paths, tree, upto=upto)
    git(tree, "add", "-f", "-A")
    git(tree, "commit", "-q", "--allow-empty", "-m", "before " + patch_name)
    session = {"queue": queue, "patch": patch_name, "paths": paths, "mode": mode}
    state_file().write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    return tree, session


def resolve(patches, reference):
    matches = [path for path in patches if path.name == reference or path.name.startswith(reference)]
    if len(matches) != 1:
        raise ValueError(f"Patch reference {reference!r} matches {len(matches)} patches")
    return matches[0]


def edit(queue, reference, add=()):
    queue = ALIASES[queue.lower()]
    patches = queue_patches(queue)
    target = resolve(patches, reference)
    index = patches.index(target)
    diff = Q.split_patch(target.read_text(encoding="utf-8"))[1]
    tree, session = start_session(queue, target.name, [*Q.patch_paths(diff), *add], index, "edit")
    try:
        Q.git_apply(tree, target.read_bytes(), target.name, include=session["paths"])
    except ValueError:
        discard()
        raise
    return tree


def new(queue, slug, paths):
    queue = ALIASES[queue.lower()]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ValueError("Use lowercase letters, digits and dashes for the patch name")
    patches = queue_patches(queue)
    number = 1 + max((int(path.name[:4]) for path in patches), default=0)
    if not paths:
        raise ValueError("Name at least one file for the new patch")
    tree, _ = start_session(queue, f"{number:04d}-{slug}.patch", paths, len(patches), "new")
    return tree


def later_patches_apply(queue, index, extra_paths):
    """Replay the whole queue for every file a later patch touches."""
    patches = queue_patches(queue)
    paths = set(extra_paths)
    for path in patches[index + 1:]:
        paths.update(Q.patch_paths(Q.split_patch(path.read_text(encoding="utf-8"))[1]))
    with tempfile.TemporaryDirectory(prefix="fuxi-check-") as temporary:
        build_view(queue, sorted(paths), Path(temporary) / "check")


def refresh(force=False):
    session = load_session()
    queue, name = session["queue"], session["patch"]
    tree = work_root() / queue
    git(tree, "add", "-f", "-A")
    changed = git(tree, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD").decode("utf-8")
    stray = sorted(set(filter(None, changed.split("\0"))) - set(session["paths"]))
    if stray:
        git(tree, "reset", "-q")
        raise ValueError(f"Files outside this patch changed: {stray}; delete them or start with --add")
    # Pin every diff setting a user's global Git config could change.
    diff = git(tree, "diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames", "--full-index",
               "--no-relative", "--src-prefix=a/", "--dst-prefix=b/", "--diff-algorithm=myers",
               "--indent-heuristic", "HEAD").decode("utf-8")
    if not diff:
        raise ValueError("The edited files equal the state before this patch; discard instead")
    if "\r" in diff:
        raise ValueError("The edited files contain CR line endings; save them with LF only")
    for path in Q.patch_paths(diff):
        windows_path(path)
    directory = Q.queue_directory(ROOT, layout(), queue)
    patch_file = directory / name
    series = directory / Q.SERIES
    if session["mode"] == "edit":
        header = Q.split_patch(patch_file.read_text(encoding="utf-8"))[0].rstrip()
        header = header[:-3].rstrip() if header.endswith("\n---") else header
    else:
        header = NEW_HEADER.format(subject=name[5:-6].replace("-", " ")).rstrip()
    old_patch = patch_file.read_bytes() if patch_file.exists() else None
    old_series = series.read_bytes()
    patch_file.write_text(header + "\n---\n" + diff, encoding="utf-8", newline="\n")
    if session["mode"] == "new":
        with series.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(name + "\n")
    try:
        index = [path.name for path in queue_patches(queue)].index(name)
        later_patches_apply(queue, index, Q.patch_paths(diff))
    except ValueError as error:
        if not force:
            if old_patch is None:
                patch_file.unlink()
            else:
                patch_file.write_bytes(old_patch)
            series.write_bytes(old_series)
            raise ValueError(f"{error}; nothing was written (use --force to keep the edit anyway)")
        print(f"Kept despite: {error}", file=sys.stderr)
    remove_tree(work_root())
    return patch_file


def discard():
    root = work_root()
    if root.exists():
        remove_tree(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    reader = commands.add_parser("read", help="Print a file as CI builds it")
    reader.add_argument("path", help="component/path, e.g. common/fs/namei.c or driver/kernel/ksu.c")
    reader.add_argument("--upstream", action="store_true", help="Print the pristine pinned upstream file")
    editor = commands.add_parser("edit", help="Edit an existing patch")
    editor.add_argument("queue", type=str.lower, choices=sorted(ALIASES))
    editor.add_argument("patch", help="Patch file name or unique prefix such as 0003")
    editor.add_argument("--add", nargs="+", default=[], metavar="PATH", help="Also bring these files into the patch")
    creator = commands.add_parser("new", help="Start a new patch at the end of a queue")
    creator.add_argument("queue", type=str.lower, choices=sorted(ALIASES))
    creator.add_argument("name", help="Short dashed name, e.g. susfs-fix-statfs")
    creator.add_argument("paths", nargs="+", metavar="PATH")
    refresher = commands.add_parser("refresh", help="Write the edited files back into the patch")
    refresher.add_argument("--force", action="store_true", help="Keep the edit even if a later patch stops applying")
    commands.add_parser("discard", help="Drop the current edit session")
    commands.add_parser("status", help="Show the current edit session")
    args = parser.parse_args()
    try:
        if args.command == "read":
            sys.stdout.buffer.write(read(args.path, upstream=args.upstream))
        elif args.command in {"edit", "new"}:
            tree = (edit(args.queue, args.patch, args.add) if args.command == "edit"
                    else new(args.queue, args.name, args.paths))
            session = load_session()
            print(f"Editing {session['queue']}/{session['patch']} in {tree}:")
            for path in session["paths"]:
                print("  " + str(tree / path))
            print("Run `python scripts/source.py refresh` when done.")
        elif args.command == "refresh":
            print(refresh(force=args.force))
        elif args.command == "discard":
            discard()
        else:
            print(json.dumps(load_session(), indent=2) if state_file().exists() else "No edit session.")
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"Source operation stopped: {error}\n")


if __name__ == "__main__":
    main()
