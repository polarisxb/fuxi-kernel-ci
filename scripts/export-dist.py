#!/usr/bin/env python3
"""Export the small, verified public CI payloads without copying private output."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile


spec = importlib.util.spec_from_file_location("artifact_identity", Path(__file__).with_name("identity.py"))
identity_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(identity_module)
IDENTITY = identity_module.from_repository()


KERNEL_ZIP = IDENTITY["kernel_zip"]
MODULE_ZIP = IDENTITY["module_zip"]


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require(value, message):
    if not value:
        raise ValueError(message)


def load_verifier(workspace):
    spec = importlib.util.spec_from_file_location("ci_dist_verifier", workspace / "scripts/verify-delivery.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE_TOOL = "tools/ksu_susfs_arm64"
TOOL_DIGEST_PLACEHOLDER = b"@KSU_SUSFS_SHA256@"


def susfs_tool_record(workspace):
    tool = workspace / "upstream/susfs-tool/ksu_susfs"
    return {"sha256": sha256(tool), "bytes": tool.stat().st_size, "ndk_commit": IDENTITY["ndk_commit"]}


def module_contents(workspace):
    """Module files as shipped: the sources plus the CI-built helper, its digest filled in."""
    module = workspace / "module/susfs4ksu"
    entries = sorted(module.rglob("*"))
    require(not any(path.is_symlink() for path in entries), "Module source contains an unexpected symlink")
    paths = [path for path in entries if path.is_file()]
    require(paths and (module / "module.prop").is_file(), "SUSFS module source missing")
    tool = (workspace / "upstream/susfs-tool/ksu_susfs").read_bytes()
    digest = hashlib.sha256(tool).hexdigest().encode()
    contents = {}
    for path in paths:
        name = path.relative_to(module).as_posix()
        require(path.suffix.lower() not in {".pem", ".key"}, "Module contains a private key filename")
        data = path.read_bytes()
        require(b"PRIVATE KEY-----" not in data[:16384], "Module contains private key material")
        contents[name] = (data.replace(TOOL_DIGEST_PLACEHOLDER, digest), stat.S_IMODE(path.stat().st_mode))
    require(MODULE_TOOL not in contents, "The module source must not carry its own SUSFS helper")
    require(TOOL_DIGEST_PLACEHOLDER in (module / "customize.sh").read_bytes(),
            "customize.sh does not pin the CI-built SUSFS helper")
    contents[MODULE_TOOL] = (tool, 0o755)
    return contents


def package_module(workspace, destination):
    contents = module_contents(workspace)
    # PurePosixPath order keeps the source entries in the order of a sorted directory walk.
    names = sorted(contents, key=PurePosixPath)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in names:
            data, mode = contents[name]
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | mode) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    current = module_contents(workspace)
    with zipfile.ZipFile(destination) as archive:
        require(archive.testzip() is None, "Module ZIP CRC failed")
        require(archive.namelist() == sorted(current, key=PurePosixPath),
                "Module ZIP file list differs from its sources")
        for name in names:
            require(archive.read(name) == current[name][0], "Module ZIP differs from the tested source")


def verify_ci_source(workspace, release_root, source_sha):
    path = release_root / "configs/ci-source.json"
    source = json.loads(path.read_text())
    require(source.get("source_sha") == source_sha and not source.get("local_validation_only"),
            "Delivered CI source identity does not match this publication commit")
    provenance = json.loads((release_root / "reports/build-provenance.json").read_text())
    expected = {"type": "file", "sha256": sha256(path),
                "executable": bool(path.stat().st_mode & stat.S_IXUSR)}
    require(provenance["inputs"]["configs"].get("ci-source.json") == expected,
            "CI source identity is not bound to the successful compilation")
    identity_path = release_root / "configs/build-identity.json"
    manager_headers = workspace / "upstream/manager-uapi"
    header_hashes = {path.name: sha256(path) for path in manager_headers.glob("*.h")}
    expected_identity = dict(IDENTITY, actual_kernel_version=IDENTITY["kernel_version"],
                             manager_uapi_sha256=header_hashes, ksu_susfs=susfs_tool_record(workspace))
    require(header_hashes and json.loads(identity_path.read_bytes()) == expected_identity,
            "Delivered build identity differs from the repository lock")
    require(provenance["inputs"]["configs"].get("build-identity.json") == {
        "type": "file", "sha256": sha256(identity_path),
        "executable": bool(identity_path.stat().st_mode & stat.S_IXUSR)},
        "Build identity is not bound to the successful compilation")
    expected_module = source.get("module_files")
    module = workspace / "module"
    current_module = {path.relative_to(module).as_posix(): sha256(path)
                      for path in module.rglob("*") if path.is_file() and not path.is_symlink()}
    require(expected_module and expected_module == current_module,
            "Module sources differ from the tree bootstrap assembled before compilation")
    return source


def export(workspace, dist, reports):
    workspace = workspace.resolve()
    reports = reports.resolve()
    require(not dist.exists(), "Refusing to overwrite an existing dist directory")
    verification = {}
    for key, name in (("pre_regressions", "pre-regressions.json"),
                      ("post_regressions", "post-regressions.json"),
                      ("clean_delivery_1", "clean-delivery-1.json"),
                      ("clean_delivery_2", "clean-delivery-2.json")):
        record = json.loads((reports / name).read_text())
        require(record.get("passed") is True, "Required verification did not pass: " + key)
        verification[key] = True
    offline = json.loads((reports / "offline/verification.json").read_text())
    require(offline.get("passed") is True, "Offline build verification did not pass")
    verification["offline_build"] = True
    release_root = (workspace / "artifacts").resolve()
    V = load_verifier(workspace)
    release, _ = V.verify_inventory(release_root)
    # Every workspace/release file is scanned by the existing verifier. The
    # export copies only these named public payloads, never the whole out tree.
    expected = release["files"][KERNEL_ZIP]
    kernel = release_root / KERNEL_ZIP
    require(expected == {"sha256": sha256(kernel), "bytes": kernel.stat().st_size},
            "Verified kernel ZIP changed before export")
    source_sha = os.environ.get("CI_SOURCE_SHA", os.environ.get("GITHUB_SHA", ""))
    require(len(source_sha) == 40 and all(char in "0123456789abcdef" for char in source_sha),
            "A complete CI source SHA is required")
    verify_ci_source(workspace, release_root, source_sha)
    alignment = json.loads((release_root / "reports/uapi/complete-uapi-alignment.json").read_bytes())
    manager_only = alignment["manager_only_ioctl_constants"]
    require(isinstance(manager_only, list) and all(isinstance(name, str) for name in manager_only),
            "Invalid Manager-only ioctl list in the verified UAPI report")
    source_identity = {
        name: sha256(release_root / "source-diffs" / name)
        for name in ("common.diff", "SukiSU.diff", "integrated-source-manifest.json")
    }
    dist.mkdir(parents=True)
    try:
        shutil.copy2(kernel, dist / KERNEL_ZIP)
        package_module(workspace, dist / MODULE_ZIP)
        artifacts = {path.name: {"sha256": sha256(path), "bytes": path.stat().st_size}
                     for path in sorted(dist.iterdir())}
        metadata = {
            "schema_version": 1,
            "component": "kernel",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_sha": source_sha,
            "source_identity": source_identity,
            "source_ref": os.environ.get("CI_SOURCE_REF", os.environ.get("GITHUB_REF", "local")),
            "repository": os.environ.get("GITHUB_REPOSITORY", "polarisxb/fuxi-kernel-ci"),
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "kernel_release": (release_root / "kernel.release").read_text().strip(),
            "image_sha256": sha256(release_root / "Image"),
            "uapi": {"manager_only_ioctl_constants": manager_only},
            "ksu_susfs": dict(susfs_tool_record(workspace), susfs_commit=IDENTITY["susfs_commit"],
                              module_path=MODULE_TOOL),
            "public_certificate_sha256": sha256(release_root / "public-certificates/signing_key.x509"),
            "driver_version": IDENTITY["driver_version"],
            "manager_version": IDENTITY["manager_version_code"],
            "susfs_version": IDENTITY["susfs_version"],
            "protections": {"full_lto": True, "lto_partitions": 1, "lto_threads": 2,
                            "cfi": True, "btf": True, "modversions": True,
                            "module_sig_protect": True, "strict_kmi": True},
            "verification": verification,
            "artifacts": artifacts,
            "runtime_compatibility_verified": False,
            "phone_operations": False,
        }
        (dist / "kernel-build-info.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (dist / "SHA256SUMS").write_text("".join(
            f"{sha256(path)}  {path.name}\n" for path in sorted(dist.iterdir()) if path.name != "SHA256SUMS"))
        V.files_in(dist)
    except BaseException:
        shutil.rmtree(dist)
        raise
    return metadata


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--reports", required=True, type=Path)
    args = parser.parse_args()
    result = export(args.workspace, args.dist, args.reports)
    print(json.dumps({"passed": True, "dist": str(args.dist.resolve()), "source_sha": result["source_sha"]}))
