#!/usr/bin/env python3
"""Export the small, verified public CI payloads without copying private output."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import zipfile


KERNEL_ZIP = "Fuxi_5.15.194_SukiSU_40900_SUSFS_2.3.0_r2_AnyKernel3.zip"
MODULE_ZIP = "SUSFS_R28_Fuxi230_40900_local2.zip"


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


def package_module(workspace, destination):
    module = workspace / "module/susfs4ksu"
    entries = sorted(module.rglob("*"))
    require(not any(path.is_symlink() for path in entries), "Module source contains an unexpected symlink")
    paths = [path for path in entries if path.is_file()]
    require(paths and (module / "module.prop").is_file(), "SUSFS module source missing")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            require(not path.is_symlink(), "Module source contains an unexpected symlink")
            name = path.relative_to(module).as_posix()
            require(path.suffix.lower() not in {".pem", ".key"}, "Module contains a private key filename")
            data = path.read_bytes()
            require(b"PRIVATE KEY-----" not in data[:16384], "Module contains private key material")
            entry = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            mode = stat.S_IMODE(path.stat().st_mode)
            entry.external_attr = (stat.S_IFREG | mode) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data)
    with zipfile.ZipFile(destination) as archive:
        require(archive.testzip() is None, "Module ZIP CRC failed")
        require(set(archive.namelist()) == {path.relative_to(module).as_posix() for path in paths},
                "Module ZIP file list differs from its sources")
        for path in paths:
            require(archive.read(path.relative_to(module).as_posix()) == path.read_bytes(),
                    "Module ZIP differs from the tested source")


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
    source_files = source.get("source_files", {})
    prefix = "module/"
    expected_module = {name[len(prefix):]: digest for name, digest in source_files.items()
                       if name.startswith(prefix)}
    module = workspace / "module"
    current_module = {path.relative_to(module).as_posix(): sha256(path)
                      for path in module.rglob("*") if path.is_file() and not path.is_symlink()}
    require(expected_module and expected_module == current_module,
            "Module sources differ from the source inputs recorded before compilation")
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
            "source_ref": os.environ.get("CI_SOURCE_REF", os.environ.get("GITHUB_REF", "local")),
            "repository": os.environ.get("GITHUB_REPOSITORY", "polarisxb/fuxi-kernel-ci"),
            "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
            "kernel_release": (release_root / "kernel.release").read_text().strip(),
            "image_sha256": sha256(release_root / "Image"),
            "public_certificate_sha256": sha256(release_root / "public-certificates/signing_key.x509"),
            "driver_version": 40900,
            "manager_version": 40900,
            "susfs_version": "v2.3.0",
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
