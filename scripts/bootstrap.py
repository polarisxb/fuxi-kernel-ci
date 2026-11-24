#!/usr/bin/env python3
"""Assemble pinned upstreams, inputs and the patch queues into a fresh CI workspace."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from xml.etree import ElementTree as ET


# Loading source-manifest helpers must not add files to the committed inputs.
sys.dont_write_bytecode = True


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


Q = load_helper("patch_queue")
REPO = Path(__file__).resolve().parents[1]
LAYOUT = Q.load_layout(REPO)
UPSTREAMS = LAYOUT["upstreams"]
COMMON_SHA = UPSTREAMS["common"]["commit"]
MANAGER_SHA = UPSTREAMS["manager"]["commit"]
MANAGER_URL = UPSTREAMS["manager"]["url"]
MANAGER_TAG = UPSTREAMS["manager"]["tag"]
MANAGER_TAG_SHA = UPSTREAMS["manager"]["tag_commit"]
MANAGER_GIT_COUNT = UPSTREAMS["manager"]["git_count"]
NDK_SHA = UPSTREAMS["ndk"]["commit"]
NDK_URL = UPSTREAMS["ndk"]["url"]
PRIVATE_NAMES = {"signing_key.pem", "adbkey", "id_rsa", "id_ed25519"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def relative(value, allow_dot=False):
    require(isinstance(value, str), f"Path must be a string: {value!r}")
    path = PurePosixPath(value)
    require(isinstance(value, str) and value and "\\" not in value and ":" not in value
            and "\n" not in value and "\r" not in value and not path.is_absolute()
            and ".." not in path.parts and str(path) == value
            and (allow_dot or value != "."), f"Unsafe relative path: {value!r}")
    return path


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(args, cwd=None, env=None):
    result = subprocess.run(list(map(str, args)), cwd=cwd, env=env, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed ({result.returncode}): "
                           + result.stderr.decode(errors="replace")[-3000:])
    return result.stdout.decode().strip()


@contextmanager
def cache_lock(cache, name):
    import fcntl  # Linux CI only; a lazy import keeps the helpers testable on Windows.

    cache.mkdir(parents=True, exist_ok=True)
    with (cache / (name + ".lock")).open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def fetch_input(lock, cache):
    require(re.fullmatch(r"[0-9a-f]{64}", lock.get("sha256", "")), "Input SHA256 missing")
    require(lock.get("url", "").startswith("https://"), "Input URL must use HTTPS")
    target = cache / "archives" / (lock["sha256"] + ".tar.gz")
    with cache_lock(cache, "inputs"):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and sha256(target) == lock["sha256"]:
            return target
        temporary = target.with_suffix(".part")
        request = urllib.request.Request(lock["url"], headers={"User-Agent": "fuxi-kernel-ci"})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        require(sha256(temporary) == lock["sha256"], "Downloaded input SHA256 mismatch")
        temporary.replace(target)
    return target


def extract_inputs(archive, destination):
    with tarfile.open(archive, "r:gz") as source:
        for member in source.getmembers():
            name = member.name.rstrip("/")
            path = relative(name)
            require(path.parts[0] in {"inputs", "downloads", "tools"},
                    f"Unexpected input entry: {name}")
            require(member.isdir() or member.isfile() or member.islnk(),
                    f"Unsupported input node: {name}")
            require(path.name.casefold() not in PRIVATE_NAMES and path.suffix.casefold() != ".pem",
                    f"Private input must not be in public archive: {name}")
            if member.islnk():
                link = relative(member.linkname)
                require(link.parts[0] in {"inputs", "downloads", "tools"},
                        f"Unsafe input hardlink: {name}")
        source.extractall(destination, filter="data")


def manifest_projects(path):
    root = ET.parse(path).getroot()
    remotes = {node.get("name"): node.get("fetch") for node in root.findall("remote")}
    default = root.find("default").get("remote")
    projects = []
    for node in root.findall("project"):
        name = node.get("name")
        local = str(relative(node.get("path", name)))
        revision = node.get("revision", "")
        require(re.fullmatch(r"[0-9a-f]{40}", revision), f"Unpinned project: {name}")
        base = remotes[node.get("remote", default)]
        require(base.startswith("https://"), f"Non-HTTPS upstream: {name}")
        projects.append({"name": name, "path": local, "revision": revision,
                         "url": base.rstrip("/") + "/" + name,
                         "links": [{"src": str(relative(link.get("src"), allow_dot=True)),
                                    "dest": str(relative(link.get("dest")))}
                                   for link in node.findall("linkfile")]})
    return projects


def git_cache(url, revision, cache, full_history=False, mirror=None):
    name = hashlib.sha256((url + "\0" + revision + str(full_history)).encode()).hexdigest()
    target = cache / "git" / name
    with cache_lock(cache, name):
        target.parent.mkdir(parents=True, exist_ok=True)
        if not (target / "HEAD").is_file():
            run(["git", "init", "--bare", target])
            if mirror is not None:
                require((mirror / ".git").exists(), f"Local Git mirror missing: {mirror}")
                run(["git", "-C", target, "remote", "add", "origin", mirror.resolve()])
            else:
                run(["git", "-C", target, "remote", "add", "origin", url])
        try:
            resolved = run(["git", "-C", target, "rev-parse", revision + "^{commit}"])
        except RuntimeError:
            resolved = ""
        if resolved != revision:
            print(f"Fetch {url} at {revision[:12]}", flush=True)
            if full_history:
                run(["git", "-C", target, "fetch", "--tags", "origin",
                     "+refs/heads/*:refs/remotes/origin/*"])
                if run(["git", "-C", target, "rev-parse", "--is-shallow-repository"]) == "true":
                    run(["git", "-C", target, "fetch", "--unshallow", "origin"])
            else:
                args = ["git", "-C", target, "fetch", "--depth=1"]
                run([*args, "origin", revision])
        require(run(["git", "-C", target, "rev-parse", revision + "^{commit}"]) == revision,
                f"Wrong cached Git revision: {url}")
        run(["git", "-C", target, "update-ref", "refs/heads/pinned", revision])
    return target


def checkout(cache_repo, revision, destination, sparse=None):
    require(not destination.exists(), f"Checkout destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", "--shared", "--no-checkout", cache_repo, destination])
    run(["git", "-C", destination, "config", "core.autocrlf", "false"])
    if sparse:
        run(["git", "-C", destination, "sparse-checkout", "set", "--cone", *sparse])
    run(["git", "-C", destination, "checkout", "--detach", revision])
    require(run(["git", "-C", destination, "rev-parse", "HEAD"]) == revision,
            f"Checkout does not match {revision}")


def copy_overlay(source, destination):
    copied = []
    for path in sorted(source.rglob("*")):
        name = str(path.relative_to(source)).replace(os.sep, "/")
        relative(name)
        require(not path.is_symlink(), f"Declare source links in source-links.json: {name}")
        if path.is_file():
            require(path.name.casefold() not in PRIVATE_NAMES, f"Private source file: {name}")
            target = destination / name
            require(not target.is_symlink(), f"Overlay would follow target link: {name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            require(target.parent.resolve().is_relative_to(destination.resolve()),
                    f"Overlay target leaves workspace: {name}")
            shutil.copy2(path, target)
            copied.append(name)
    return copied


def restore_links(root, declarations):
    for name, value in declarations.items():
        path = root / str(relative(name))
        require(not PurePosixPath(value).is_absolute() and "\\" not in value
                and "\n" not in value and "\r" not in value, f"Unsafe source link: {name}")
        target = (path.parent / value).resolve()
        require(target.is_relative_to(root.resolve()) and target.exists(),
                f"Source link points outside/missing workspace: {name} -> {value}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        else:
            require(not path.exists(), f"Link would replace a source file: {name}")
        path.symlink_to(value, target_is_directory=target.is_dir())


def capture_patches(workspace, builtin_archive):
    common = workspace / "kernel/common"
    patches = workspace / "patches"
    patches.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="capture-", dir=workspace) as temporary:
        temp = Path(temporary)
        env = dict(os.environ, GIT_INDEX_FILE=str(temp / "common.index"))
        run(["git", "read-tree", "HEAD"], cwd=common, env=env)
        run(["git", "add", "-f", "-A", "."], cwd=common, env=env)
        result = subprocess.run(["git", "diff", "--cached", "--binary", "HEAD"],
                                cwd=common, env=env, capture_output=True, check=True)
        require(b"diff --git a/drivers/kernelsu b/drivers/kernelsu" in result.stdout,
                "Captured source patch lacks the required driver link")
        (patches / "common-final.diff").write_bytes(result.stdout)
        tree = temp / "pristine"
        extract_single_tree(builtin_archive, tree)
        run(["git", "init", "-q"], cwd=tree)
        run(["git", "add", "-f", "-A", "."], cwd=tree)
        baseline = run(["git", "write-tree"], cwd=tree)
        for path in tree.iterdir():
            if path.name == ".git":
                continue
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        shutil.copytree(workspace / "kernel/KernelSU", tree, dirs_exist_ok=True, symlinks=True)
        run(["git", "add", "-f", "-A", "."], cwd=tree)
        result = subprocess.run(["git", "diff", "--cached", "--binary", baseline],
                                cwd=tree, capture_output=True, check=True)
        (patches / "sukisu-final.diff").write_bytes(result.stdout)
        run(["git", "apply", "--reverse", "--check", patches / "sukisu-final.diff"], cwd=tree)


def ci_source_info():
    try:
        actual = run(["git", "-C", REPO, "rev-parse", "HEAD"])
        dirty = bool(run(["git", "-C", REPO, "status", "--porcelain", "--untracked-files=all"]))
    except RuntimeError:
        actual, dirty = None, True
    requested = os.environ.get("CI_SOURCE_SHA") or os.environ.get("GITHUB_SHA")
    if requested:
        require(re.fullmatch(r"[0-9a-f]{40}", requested) and requested == actual,
                "CI source SHA must match this repository's actual checkout")
    if requested or os.environ.get("GITHUB_ACTIONS") == "true":
        require(actual is not None and not dirty, "CI repository inputs must be a clean Git checkout")
    files = {str(path.relative_to(REPO)).replace(os.sep, "/"): sha256(path)
             for name in ("patches", "module") for path in sorted((REPO / name).rglob("*"))
             if path.is_file() and not path.is_symlink()}
    return {"source_sha": actual, "source_ref": os.environ.get("CI_SOURCE_REF", os.environ.get("GITHUB_REF", "local")),
            "source_files": files, "dirty": dirty, "local_validation_only": actual is None or dirty}


def extract_single_tree(archive, destination):
    """Extract an upstream source archive whose content sits in one top directory."""
    require(not destination.exists() and not destination.is_symlink(),
            f"Archive destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="extract-", dir=destination.parent) as temporary:
        with tarfile.open(archive) as source:
            source.extractall(temporary, filter="data")
        children = list(Path(temporary).iterdir())
        require(len(children) == 1 and children[0].is_dir() and not children[0].is_symlink(),
                f"Unexpected source archive layout: {archive.name}")
        children[0].rename(destination)


def install_susfs(workspace, common, cached):
    """Fetch SUSFS at its pin and apply the official GKI kernel patch to common."""
    info = UPSTREAMS["susfs"]
    source = workspace / "upstream/susfs4ksu"
    checkout(cached, info["commit"], source, sparse=["kernel_patches"])
    record = Q.install_susfs(common, source, info)
    return {"url": info["url"], "commit": info["commit"], "version": info["version"], **record}


def archive_upstreams(workspace, cache, mirror_root, identity):
    directory = workspace / "upstream/archives"
    directory.mkdir(parents=True)
    caches = {}
    for name, archive_key, mirror_name in (("driver", "builtin_archive", "KernelSU"),
                                           ("susfs", "susfs_archive", "susfs4ksu"),
                                           ("manager", "manager_archive", "KernelSU")):
        upstream = UPSTREAMS[name]
        revision = upstream["commit"]
        mirror = mirror_root / mirror_name if mirror_root else None
        cached = git_cache(upstream["url"], revision, cache, mirror=mirror)
        run(["git", "-C", cached, "archive", "--format=tar.gz", f"--prefix={name}-{revision}/",
             "--output", directory / identity[archive_key], revision])
        caches[name] = cached
    return caches["susfs"]


def verify_driver_archive(workspace, driver):
    # The legacy archive lives in the immutable input release. Compare its unpacked
    # bytes and modes; gzip headers and top-level archive names are not source inputs.
    lock = json.loads((REPO / "reference/inputs.lock.json").read_bytes())
    archives = [path for path in lock["files"] if re.fullmatch(r"downloads/builtin-[0-9a-f]+\.tar\.gz", path)]
    require(len(archives) == 1, "Expected one legacy driver archive in the locked input release")
    verifier = load_delivery_verifier()
    with tempfile.TemporaryDirectory(dir=workspace) as temporary:
        old = Path(temporary) / "legacy"
        extract_single_tree(workspace / archives[0], old)
        expected, actual = verifier.tree_manifest(old), verifier.tree_manifest(driver)
        if actual != expected:
            changed = sorted(name for name in expected.keys() | actual.keys()
                             if expected.get(name) != actual.get(name))
            raise ValueError(f"Git driver archive differs from the legacy baseline: {changed[:12]}")
    print(f"Git driver tree matches the legacy archive ({len(actual)} entries)", flush=True)


def load_delivery_verifier():
    spec = importlib.util.spec_from_file_location("bootstrap_delivery", REPO / "workspace/scripts/verify-delivery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_workspace_identity(workspace, projects):
    identity = load_helper("identity").derive(LAYOUT)
    makefile = workspace / "kernel/common/Makefile"
    version = Q.kernel_version(makefile.read_text(encoding="utf-8"))
    require(version == identity["kernel_version"], f"Unexpected kernel version in Makefile: {version}")
    identity["actual_kernel_version"] = version
    locked = [{"path": project["path"], "sha": project["revision"],
               "shared_readonly": project["path"] != "common"} for project in projects]
    configs = workspace / "configs"
    for name, record in (("build-identity.json", identity), ("locked-workspace-inputs.json", locked)):
        (configs / name).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")


def prepare_kernel(workspace, cache, mirror_root=None):
    projects = manifest_projects(REPO / "configs/manifest.locked.xml")
    pinned = [project["revision"] for project in projects if project["path"] == "common"]
    require(pinned == [COMMON_SHA], "configs/manifest.locked.xml and ci-layout.json pin different kernel/common")
    def fetch(project):
        mirror = mirror_root / project["path"] if mirror_root else None
        return project, git_cache(project["url"], project["revision"], cache, mirror=mirror)
    with ThreadPoolExecutor(max_workers=4) as pool:
        caches = list(pool.map(fetch, projects))
    kernel = workspace / "kernel"
    kernel.mkdir()
    for project, cached in caches:
        sparse = ["clang-r450784e"] if project["path"] == "prebuilts/clang/host/linux-x86" else None
        checkout(cached, project["revision"], kernel / project["path"], sparse)
    for project in projects:
        for link in project["links"]:
            path = kernel / link["dest"]
            target = kernel / project["path"] / link["src"]
            require(target.exists(), f"Pinned manifest link source missing: {target}")
            path.parent.mkdir(parents=True, exist_ok=True)
            require(not path.exists() and not path.is_symlink(), f"Manifest link collision: {path}")
            path.symlink_to(os.path.relpath(target, path.parent), target_is_directory=target.is_dir())
    common = kernel / "common"
    identity = load_helper("identity").derive(LAYOUT)
    susfs_cache = archive_upstreams(workspace, cache, mirror_root, identity)
    susfs = install_susfs(workspace, common, susfs_cache)
    Q.write_scmversion(common, LAYOUT["kernel_release"], COMMON_SHA)
    queues = {"common": Q.apply_series(common, Q.queue_directory(REPO, LAYOUT, "common"))}
    builtin_archive = workspace / "upstream/archives" / identity["builtin_archive"]
    extract_single_tree(builtin_archive, kernel / "KernelSU")
    verify_driver_archive(workspace, kernel / "KernelSU")
    queues["driver"] = Q.apply_series(kernel / "KernelSU", Q.queue_directory(REPO, LAYOUT, "driver"))
    links = json.loads((REPO / "configs/source-links.json").read_text())["kernel_links"]
    restore_links(workspace, links)
    for source, target in ((REPO / "configs", workspace / "configs"),
                           (REPO / "workspace/scripts", workspace / "scripts"),
                           (REPO / "workspace/tests", workspace / "tests"),
                           (REPO / "module", workspace / "module")):
        copy_overlay(source, target)
    write_workspace_identity(workspace, projects)
    (workspace / "configs/ci-source.json").write_text(json.dumps(ci_source_info(), indent=2) + "\n")
    (workspace / "reports").mkdir()
    capture_patches(workspace, builtin_archive)
    return {"kind": "kernel", "projects": [{k: v for k, v in p.items() if k != "links"}
                                             for p in projects],
            "susfs": susfs, "driver": {"commit": UPSTREAMS["driver"]["commit"],
                                       "archive": builtin_archive.relative_to(workspace).as_posix()},
            "patch_queues": queues, "kernel_release": LAYOUT["kernel_release"],
            "source_epoch": LAYOUT["source_epoch"], "source_timestamp": LAYOUT["source_timestamp"]}


def manager_tag_info(source, restore_missing=False):
    require(run(["git", "-C", source, "rev-parse", "HEAD"]) == MANAGER_SHA,
            "Native upstream checkout changed")
    count = int(run(["git", "-C", source, "rev-list", "--count", "HEAD"]))
    require(count == MANAGER_GIT_COUNT,
            "Native build requires the original full upstream Git history")
    try:
        historical = run(["git", "-C", source, "rev-parse", MANAGER_TAG_SHA + "^{commit}"])
    except RuntimeError as error:
        raise ValueError("Pinned historical Manager tag commit is missing") from error
    require(historical == MANAGER_TAG_SHA, "Historical Manager tag commit changed")
    try:
        run(["git", "-C", source, "merge-base", "--is-ancestor", historical, "HEAD"])
    except RuntimeError as error:
        raise ValueError("Historical Manager tag commit is not an ancestor of the pinned HEAD") from error
    distance = int(run(["git", "-C", source, "rev-list", "--count", historical + "..HEAD"]))
    require(distance == 1, "Historical Manager tag must be exactly one commit behind the pinned HEAD")
    ref = "refs/tags/" + MANAGER_TAG
    present = ref in run(["git", "-C", source, "for-each-ref", "--format=%(refname)", ref]).splitlines()
    if present:
        require(run(["git", "-C", source, "rev-parse", ref + "^{commit}"]) == historical,
                "Existing historical Manager tag points to a different commit")
    else:
        require(restore_missing, "Pinned historical Manager tag missing from the build checkout")
        # Upstream removed this historical tag. Restore only this validated ref
        # in the owned build checkout; never rewrite an existing tag or remote.
        run(["git", "-C", source, "tag", MANAGER_TAG, historical])
    return {"name": MANAGER_TAG, "commit": historical, "distance": distance, "restored": not present}


def prepare_native(workspace, cache, mirror_root=None):
    identity = load_helper("identity").derive(LAYOUT)
    ci_source = ci_source_info()
    require(not ci_source["local_validation_only"],
            "Native builds require a clean committed development checkout")
    manager_mirror = mirror_root / "KernelSU" if mirror_root else None
    source_cache = git_cache(MANAGER_URL, MANAGER_SHA, cache, full_history=True, mirror=manager_mirror)
    source = workspace / "source"
    checkout(source_cache, MANAGER_SHA, source)
    historical_tag = manager_tag_info(source, restore_missing=True)
    queue = Q.apply_series(source, Q.queue_directory(REPO, LAYOUT, "manager"))
    count = int(run(["git", "rev-list", "--count", "HEAD"], cwd=source))
    describe = run(["git", "describe", "--tags", "--always"], cwd=source)
    require(count == MANAGER_GIT_COUNT and describe == "v" + identity["manager_version_name"]
            and 40000 - 2815 + count == identity["manager_version_code"],
            f"Original Manager build identity changed: {count}, {describe}")
    ndk_mirror = mirror_root / "prebuilts/ndk-r23" if mirror_root else None
    ndk_cache = git_cache(NDK_URL, NDK_SHA, cache, mirror=ndk_mirror)
    ndk = workspace / "ndk"
    checkout(ndk_cache, NDK_SHA, ndk)
    inputs = native_inputs(workspace)
    require(inputs["ci_source"] == ci_source, "CI repository inputs changed during native assembly")
    return {"kind": "native", "manager_sha": MANAGER_SHA, "git_count": count,
            "git_describe": describe, "version_code": 40000 - 2815 + count,
            "historical_tag": historical_tag, "patch_queue": queue,
            "ndk_sha": NDK_SHA, "android_api": 26, "inputs": inputs}


def native_inputs(workspace):
    identity = load_helper("identity").derive(LAYOUT)
    ci_source = ci_source_info()
    require(not ci_source["local_validation_only"],
            "Native builds require a clean committed development checkout")
    source = workspace / "source"
    historical_tag = manager_tag_info(source)
    historical_tag.pop("restored")
    count = int(run(["git", "-C", source, "rev-list", "--count", "HEAD"]))
    describe = run(["git", "-C", source, "describe", "--tags", "--always"])
    require(count == MANAGER_GIT_COUNT and describe == "v" + identity["manager_version_name"]
            and 40000 - 2815 + count == identity["manager_version_code"],
            "Native build requires the original full upstream Git history")
    require(run(["git", "-C", workspace / "ndk", "rev-parse", "HEAD"]) == NDK_SHA
            and not run(["git", "-C", workspace / "ndk", "status", "--porcelain", "--untracked-files=all"]),
            "Pinned NDK checkout changed")
    # Share the delivery verifier's file/mode/link records instead of inventing
    # a second source snapshot format. Mutable Git internals are excluded.
    helper = load_delivery_verifier()
    return {"ci_source": ci_source, "native_source": helper.tree_manifest(source),
            "manager_sha": MANAGER_SHA, "git_count": count, "git_describe": describe,
            "historical_tag": historical_tag, "version_code": 40000 - 2815 + count,
            "ndk_sha": NDK_SHA, "android_api": 26}


def verify_native_inputs(workspace, before_build=False):
    report = json.loads((workspace / "bootstrap.json").read_text())
    require(report.get("kind") == "native" and report.get("passed") is True
            and isinstance(report.get("inputs"), dict), "Native assembly input snapshot missing")
    current = native_inputs(workspace)
    require(current == report["inputs"], "Native build inputs changed since workspace assembly")
    tag = report.get("historical_tag", {})
    require({key: value for key, value in tag.items() if key != "restored"} == current["historical_tag"]
            and isinstance(tag.get("restored"), bool), "Native historical tag restoration record missing or changed")
    record = workspace / "native-build-inputs.json"
    if before_build:
        require(not record.exists(), "Native build input snapshot already exists")
        record.write_text(json.dumps({"before": current}, indent=2) + "\n")
        return {"before": current}
    require(record.is_file(), "Native input snapshot before Cargo build missing")
    before = json.loads(record.read_text()).get("before")
    require(before == current, "Native build inputs changed during Cargo compilation")
    result = {"before": before, "after": current}
    record.write_text(json.dumps(result, indent=2) + "\n")
    return result


def verify_native(workspace, target_dir, dist):
    identity = load_helper("identity").derive(LAYOUT)
    inputs = verify_native_inputs(workspace)
    artifact = target_dir / "aarch64-linux-android/release/ksud"
    require(artifact.is_file(), "Native build output missing")
    header = artifact.read_bytes()[:64]
    require(len(header) == 64 and header[:6] == b"\x7fELF\x02\x01"
            and struct.unpack_from("<HH", header, 16) == (3, 183),
            "Native output must be AArch64 little-endian ELF64 PIE")
    segments = run(["readelf", "-lW", artifact])
    dynamic = run(["readelf", "-dW", artifact])
    require("/system/bin/linker64" in segments, "Wrong Android interpreter")
    needed = re.findall(r"\(NEEDED\).*?\[([^]]+)\]", dynamic)
    require(set(needed) <= {"liblog.so", "libdl.so", "libm.so", "libc.so"}
            and "libc.so" in needed, f"Unexpected native runtime dependencies: {needed}")
    require(identity["manager_version_name"].encode() in artifact.read_bytes(), "Original version string missing")
    bindings = list((target_dir / "aarch64-linux-android/release/build").glob("ksud-*/out/bindings.rs"))
    require(len(bindings) == 1, "Actual bindgen output missing or ambiguous")
    build_output = (bindings[0].parents[1] / "output").read_text().splitlines()
    require(f"cargo:rustc-env=VERSION_CODE={identity['manager_version_code']}" in build_output
            and f"cargo:rustc-env=VERSION_NAME={identity['manager_version_name']}" in build_output,
            "Actual Cargo build did not retain the original Manager version identity")
    expected = json.loads((REPO / "reference/manager-arm64-uapi.json").read_text())
    require(len(expected) == 144, "Expected ARM64 UAPI comparison must cover 144 values")
    template = (REPO / "reference/arm64-uapi-probe.rs.in").read_text()
    template, count = re.subn(r'include!\("[^"\n]+"\)',
                             lambda _: "include!(" + json.dumps(str(bindings[0])) + ")", template, count=1)
    require(count == 1, "UAPI probe does not contain its bindgen include marker")
    probe = workspace / "uapi-layout.rs"; probe.write_text(template)
    obj = workspace / "uapi-layout.o"
    run(["rustc", "+1.91.1", "--edition=2024", "--crate-type=lib", "--target",
         "aarch64-linux-android", "--emit=obj", "-O", probe, "-o", obj])
    vector = workspace / "uapi-values.bin"
    objcopy = workspace / "ndk/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-objcopy"
    run([objcopy, "--dump-section", ".rodata.KSU_UAPI_VALUES=" + str(vector), obj])
    raw = vector.read_bytes()
    require(len(raw) == 8 * len(expected), "Generated ARM64 UAPI vector has wrong size")
    actual = dict(zip(expected, struct.unpack("<" + "Q" * len(expected), raw)))
    mismatches = {key: {"expected": expected[key], "actual": actual[key]}
                  for key in expected if expected[key] != actual[key]}
    require(not mismatches, f"ARM64 Rust/C UAPI mismatch: {mismatches}")
    require(actual["constant.KERNEL_SU_UAPI_VERSION"] == 2
            and actual["constant.KSU_APP_PROFILE_VER"] == 4
            and actual["sizeof.app_profile"] == 784, "Native UAPI/Profile changed")
    source_sha = inputs["before"]["ci_source"]["source_sha"]
    rustc = run(["rustc", "+1.91.1", "--version"])
    require(rustc.startswith("rustc 1.91.1 "), "Native verifier requires Rust 1.91.1")
    # The UAPI compilation must not mutate the inputs either.
    require(native_inputs(workspace) == inputs["after"], "Native inputs changed during verification")
    dist.mkdir(parents=True, exist_ok=True)
    require(not any(dist.iterdir()), f"Refusing non-empty native output: {dist}")
    final = dist / "libksud.so"; shutil.copy2(artifact, final)
    info = {"schema_version": 1, "component": "ksud", "source_sha": source_sha,
            "upstream_sha": MANAGER_SHA, "repository": os.environ.get("GITHUB_REPOSITORY", "polarisxb/fuxi-kernel-ci"),
            "source_ref": os.environ.get("CI_SOURCE_REF", os.environ.get("GITHUB_REF", "")),
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "version_code": inputs["after"]["version_code"], "version_name": identity["manager_version_name"], "android_api": 26,
            "rustc": rustc, "ndk_revision": "23.1.7779620", "ndk_sha": NDK_SHA,
            "source_inputs": inputs,
            "historical_tag": json.loads((workspace / "bootstrap.json").read_text())["historical_tag"],
            "uapi_version": 2, "profile_version": 4, "profile_bytes": 784,
            "uapi_compared_values": len(actual), "uapi_all_equal": True,
            "artifact_sha256": sha256(final), "artifact_bytes": final.stat().st_size,
            "elf_architecture": "AArch64", "interpreter": "/system/bin/linker64", "needed": needed,
            "actual_build": True, "official_signed_apk": False, "device_operations": False}
    info["artifacts"] = {final.name: {"sha256": sha256(final), "bytes": final.stat().st_size}}
    note = (f"Local patched SukiSU {identity['manager_version_code']} ARM64 executable. Not an official signed APK.\n"
            "Manager runs extracted nativeLibraryDir/libksud.so; changing only /data/adb/ksud is insufficient.\n"
            "Check version, APK, privileges and SELinux labels; back up current files before deployment.\n"
            "Updating or reinstalling Manager re-extracts the original library.\n")
    archive = dist / identity["native_zip"]
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.write(final, final.name)
        # The ZIP cannot embed its own digest. Internal metadata binds the
        # executable; the external record binds both completed payloads.
        bundle.writestr("native-build-info.json", json.dumps(info, indent=2) + "\n")
        bundle.writestr("README.txt", note)
    with zipfile.ZipFile(archive) as bundle:
        require(bundle.testzip() is None and bundle.read(final.name) == final.read_bytes(),
                "Native ZIP differs from the verified executable")
    info["artifacts"][archive.name] = {"sha256": sha256(archive), "bytes": archive.stat().st_size}
    (dist / "native-build-info.json").write_text(json.dumps(info, indent=2) + "\n")
    (dist / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {path.name}\n"
                                             for path in sorted(dist.iterdir()) if path.is_file()))
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--inputs-cache", type=Path)
    parser.add_argument("--mirror-root", type=Path, help="Read-only local Git mirrors for validation")
    parser.add_argument("--native-only", action="store_true")
    parser.add_argument("--verify-native", action="store_true")
    parser.add_argument("--check-native-inputs", action="store_true", help="Record source inputs immediately before Cargo")
    parser.add_argument("--target-dir", type=Path)
    parser.add_argument("--dist", type=Path)
    args = parser.parse_args()
    require(os.name == "posix", "Build workspace assembly runs on Linux CI; Windows edits patches/ with scripts/source.py")
    workspace = args.workspace.resolve()
    require(not (args.check_native_inputs and args.verify_native), "Choose one native verification action")
    if args.check_native_inputs:
        inputs = verify_native_inputs(workspace, before_build=True)
        print(json.dumps({"passed": True, "source_sha": inputs["before"]["ci_source"]["source_sha"]}))
        return
    if args.verify_native:
        require(args.target_dir is not None and args.dist is not None, "Native verification needs target/dist")
        result = verify_native(workspace, args.target_dir.resolve(), args.dist.resolve())
        print(json.dumps({key: value for key, value in result.items() if key != "source_inputs"}, indent=2))
        return
    require(workspace != REPO and not any(workspace.is_relative_to(REPO / name)
                                         for name in ("patches", "configs", "workspace", "reference")),
            "Workspace must not overwrite repository inputs")
    workspace.mkdir(parents=True, exist_ok=True)
    require(not any(workspace.iterdir()), f"Refusing a non-empty workspace: {workspace}")
    cache = (args.inputs_cache or Path(os.environ.get("FUXI_INPUTS_CACHE", REPO / ".cache/inputs"))).resolve()
    cache.mkdir(parents=True, exist_ok=True)
    if args.native_only:
        report = prepare_native(workspace, cache, args.mirror_root)
    else:
        lock = json.loads((REPO / "reference/inputs.lock.json").read_text())
        archive = fetch_input(lock, cache)
        extract_inputs(archive, workspace)
        report = prepare_kernel(workspace, cache, args.mirror_root)
        report["input_archive_sha256"] = sha256(archive)
    report.update(passed=True, workspace=str(workspace), repo=str(REPO))
    (workspace / "bootstrap.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: value for key, value in report.items() if key != "inputs"}, indent=2))


if __name__ == "__main__":
    main()
