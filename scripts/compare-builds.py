"""Compare phase 1b outputs from gh run download, or unpacked kernel/ and native/ directories.

The kernel build is bit-reproducible, so every kernel payload must match byte for
byte. libksud.so is not: two builds from identical native inputs emit the same
symbols and constants in a different order, and so with different padding. It
therefore passes when its recorded inputs, toolchain and build mode match; its
differing hashes are reported as not_reproducible.
"""
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
# How libksud.so was produced, besides the run's own commit and ref.
NATIVE_INPUT_FIELDS = ("upstream_sha", "rustc", "ndk_revision", "ndk_sha", "android_api",
                       "elf_architecture", "interpreter", "needed", "uapi_version",
                       "profile_version", "profile_bytes", "actual_build",
                       "official_signed_apk", "device_operations", "schema_version")
NON_REPRODUCIBLE = {"libksud.so"}


def native_inputs(info, path):
    inputs = info["source_inputs"]
    require(inputs["before"] == inputs["after"], f"Native inputs changed during the build in {path}")
    recorded = {key: value for key, value in inputs["after"].items() if key != "ci_source"}
    recorded.update({key: info[key] for key in NATIVE_INPUT_FIELDS})
    return hashlib.sha256(json.dumps(recorded, sort_keys=True).encode()).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(stream):
    return hashlib.file_digest(stream, "sha256").hexdigest()


def snapshot(root, expected_commit=None):
    snapshot = {}
    unstable = {}
    components = []
    for folder, fields in (("kernel", KERNEL_FIELDS), ("native", NATIVE_FIELDS)):
        prefix = f"fuxi-{folder}-"
        directories = [p for p in root.iterdir() if p.is_dir() and
                       (p.name == folder or re.fullmatch(prefix + r"[0-9a-f]{40}", p.name))]
        require(len(directories) == 1, f"Expected exactly one {folder} directory in {root}, found {len(directories)}")
        directory, = directories
        path = directory / (folder + "-build-info.json")
        info = json.loads(path.read_bytes())
        require(directory.name in (folder, prefix + info["source_sha"]), f"Artifact directory/commit mismatch: {path}")
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
            snapshot["native/inputs"] = native_inputs(info, path)
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
                (unstable if folder == "native" and name in NON_REPRODUCIBLE else snapshot)[prefix] = record["sha256"]
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
                    key, mode = f"{prefix}/{entry.filename}", entry.external_attr >> 16
                    if folder == "native" and entry.filename in NON_REPRODUCIBLE:
                        unstable[key] = sha
                        snapshot[key + ":mode"] = mode
                    else:
                        snapshot[key] = (sha, mode)
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
    return {"repository": repository, "source_sha": commit, "run_id": run_id}, snapshot, unstable


def compare(baseline, candidate, baseline_sha=None, candidate_sha=None):
    old_run, old, old_unstable = snapshot(baseline, baseline_sha)
    new_run, new, new_unstable = snapshot(candidate, candidate_sha)
    require(old_run["repository"] == new_run["repository"], "Runs belong to different repositories")
    require(old_unstable.keys() == new_unstable.keys(), "Runs contain different non-reproducible payloads")
    differences = {key: {"baseline": old.get(key), "candidate": new.get(key)}
                   for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)}
    not_reproducible = {key: {"baseline": old_unstable[key], "candidate": new_unstable[key]}
                        for key in sorted(old_unstable) if old_unstable[key] != new_unstable[key]}
    return {"baseline": old_run, "candidate": new_run, "passed": not differences,
            "differences": differences, "not_reproducible": not_reproducible}


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
