"""Compare two unpacked phase 1b Actions runs, each containing kernel/ and native/."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile


SOURCE_FILES = {"common.diff", "SukiSU.diff", "integrated-source-manifest.json"}
KERNEL_FIELDS = ("kernel_release", "driver_version", "manager_version", "susfs_version",
                 "image_sha256", "public_certificate_sha256")
NATIVE_FIELDS = ("version_code", "version_name", "uapi_compared_values")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot(root, expected_commit=None):
    snapshot = {}
    components = []
    for folder, fields in (("kernel", KERNEL_FIELDS), ("native", NATIVE_FIELDS)):
        directory = root / folder
        path = directory / (folder + "-build-info.json")
        info = json.loads(path.read_bytes())
        components.append((info["repository"], info["source_sha"], info["run_id"]))
        require(info["component"] == {"kernel": "kernel", "native": "ksud"}[folder], f"Wrong component in {path}")
        for key in fields:
            value = info[key]
            require((type(value) is int and value > 0) or (isinstance(value, str) and value.strip()),
                    f"Invalid {key} in {path}")
            snapshot[f"{folder}/{key}"] = value
        if folder == "kernel":
            identity = info["source_identity"]
            require(identity.keys() == SOURCE_FILES, f"Missing or unexpected source digests in {path}")
            for name, sha in identity.items():
                require(re.fullmatch(r"[a-f0-9]{64}", sha), f"Invalid source digest for {name}")
                snapshot["source/" + name] = sha
        else:
            require(info["uapi_all_equal"] is True, f"Native UAPI comparison failed in {path}")
        artifacts = info["artifacts"]
        zips = [name for name in artifacts if name.endswith(".zip")]
        expected = set(zips) if folder == "kernel" else {*zips, "libksud.so"}
        require(len(zips) == (2 if folder == "kernel" else 1) and expected == artifacts.keys(),
                f"Incomplete artifact set in {path}")
        snapshot[folder + "/artifacts"] = sorted(expected)
        images = []
        for name, record in artifacts.items():
            require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name), f"Unsafe asset name {name!r}")
            asset = directory / name
            require(asset.is_file() and not asset.is_symlink(), f"Missing regular file {asset}")
            with asset.open("rb") as stream:
                require({"sha256": digest(stream), "bytes": asset.stat().st_size} == record,
                        f"Artifact differs from build metadata: {asset}")
            prefix = f"{folder}/{name}"
            if name not in zips:
                snapshot[prefix] = record["sha256"]
                continue
            with zipfile.ZipFile(asset) as archive:
                entries = archive.namelist()
                require(entries and len(entries) == len(set(entries)), f"Empty ZIP or duplicate entry in {asset}")
                if folder == "native":
                    require(set(entries) == {"libksud.so", "native-build-info.json", "README.txt"},
                            f"Unexpected native ZIP contents in {asset}")
                for entry in archive.infolist():
                    if folder == "native" and entry.filename == "native-build-info.json":
                        internal = json.loads(archive.read(entry))
                        expected_info = dict(info, artifacts={"libksud.so": artifacts["libksud.so"]})
                        require(internal == expected_info, f"Native ZIP metadata mismatch in {asset}")
                        continue
                    with archive.open(entry) as stream:
                        sha = digest(stream)
                    snapshot[f"{prefix}/{entry.filename}"] = (sha, entry.external_attr >> 16)
                    if folder == "kernel" and entry.filename == "Image":
                        images.append(sha)
                    if folder == "native" and entry.filename == "libksud.so":
                        require(sha == artifacts["libksud.so"]["sha256"], f"Native ZIP library mismatch in {asset}")
        if folder == "kernel":
            require(images == [info["image_sha256"]], f"Kernel ZIP Image differs from {path}")
    repository, commit, run_id = components[0]
    require(components[0] == components[1], f"Mixed component origins in {root}")
    require(re.fullmatch(r"[0-9a-f]{40}", commit), f"Invalid source SHA in {root}")
    if expected_commit:
        require(re.fullmatch(r"[0-9a-f]{7,40}", expected_commit) and commit.startswith(expected_commit),
                f"Unexpected source commit {commit} in {root}")
    return {"repository": repository, "source_sha": commit, "run_id": run_id}, snapshot


def compare(baseline, candidate, baseline_sha=None, candidate_sha=None):
    old_run, old = snapshot(baseline, baseline_sha)
    new_run, new = snapshot(candidate, candidate_sha)
    require(old_run["repository"] == new_run["repository"], "Runs belong to different repositories")
    differences = {key: {"baseline": old.get(key), "candidate": new.get(key)}
                   for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)}
    return {"baseline": old_run, "candidate": new_run, "passed": not differences, "differences": differences}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--baseline-sha")
    parser.add_argument("--candidate-sha")
    args = parser.parse_args()
    try:
        result = compare(args.baseline, args.candidate, args.baseline_sha, args.candidate_sha)
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(2, str(error) + "\n")
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["passed"] else 1)
