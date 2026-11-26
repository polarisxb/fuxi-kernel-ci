"""Compare phase 1b outputs from gh run download, or unpacked kernel/ and native/ directories.

Release kernel payloads must match byte for byte. With a temporary signing key,
only the embedded certificate and GNU build ID may vary in the Image.
libksud.so is not bit-reproducible: builds from identical native inputs emit the same
symbols and constants in a different order, and so with different padding. It
therefore passes when its recorded inputs, toolchain and build mode match; its
differing hashes are reported as not_reproducible.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import mmap
from pathlib import Path
import re
import shutil
import sys
import tempfile
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
BUILD_ID_NOTE = bytes.fromhex("040000001400000003000000474e5500")


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


def signing_regions(image, certificate_hash, asset):
    certificate = None
    for match in re.finditer(b"\x30\x82", image):
        start = match.start()
        length = int.from_bytes(image[start + 2:start + 4], "big")
        end = start + 4 + length
        if 256 <= length <= 8192 and end <= len(image):
            if hashlib.sha256(image[start:end]).hexdigest() == certificate_hash:
                require(certificate is None, f"Duplicate embedded certificate in {asset}")
                certificate = (start, end)
    require(certificate is not None, f"Missing embedded certificate {certificate_hash} in {asset}")
    identifiers = {}
    last_end = 0
    for match in re.finditer(re.escape(BUILD_ID_NOTE), image):
        note, start = match.span()
        end = start + 20
        require(end <= len(image), f"Truncated GNU build ID in {asset}")
        require(note >= last_end, f"Overlapping GNU build ID notes in {asset}")
        require(certificate[1] <= note or end <= certificate[0], f"Overlapping certificate and build ID in {asset}")
        identifiers[note] = image[start:end].hex()
        last_end = end
    require(identifiers, f"Missing GNU build ID note in {asset}")
    return certificate, identifiers


@contextmanager
def masked_image(asset, metadata):
    # Map a temporary copy so a compressed Image cannot force a ZIP-sized heap allocation.
    with tempfile.TemporaryFile() as tmp:
        with zipfile.ZipFile(asset) as archive, archive.open("Image") as stream:
            shutil.copyfileobj(stream, tmp)
        require(tmp.tell() > 0, f"Empty Image in {asset}")
        tmp.flush()
        with mmap.mmap(tmp.fileno(), 0, access=mmap.ACCESS_COPY) as image:
            require(hashlib.sha256(image).hexdigest() == metadata["kernel/image_sha256"],
                    f"Kernel ZIP Image changed during comparison: {asset}")
            certificate, identifiers = signing_regions(image, metadata["kernel/public_certificate_sha256"], asset)
            start, end = certificate
            image[start:end] = bytes(end - start)
            for note in identifiers:
                start = note + len(BUILD_ID_NOTE)
                image[start:start + 20] = bytes(20)
            with memoryview(image) as data:
                yield data, certificate, identifiers


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
        # Before the signing split all runs used the release certificate.
        signing = info.get("signing", "release")
        require(signing in ("release", "ephemeral"), f"Unknown signing mode in {path}")
        snapshot[f"{folder}/signing"] = signing
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
                        image_asset = asset
                    if folder == "native" and entry.filename == "libksud.so":
                        require(sha == artifacts["libksud.so"]["sha256"], f"Native ZIP library mismatch in {asset}")
        if folder == "kernel":
            require(images == [info["image_sha256"]], f"Kernel ZIP Image differs from {path}")
    repository, commit, run_id = components[0]
    require(snapshot["kernel/signing"] == snapshot["native/signing"], f"Mixed signing modes in {root}")
    require(components[0] == components[1], f"Mixed component origins in {root}")
    require(re.fullmatch(r"[0-9a-f]{40}", commit), f"Invalid source SHA in {root}")
    if expected_commit:
        require(re.fullmatch(r"[0-9a-f]{7,40}", expected_commit) and commit.startswith(expected_commit),
                f"Unexpected source commit {commit} in {root}")
    return {"repository": repository, "source_sha": commit, "run_id": run_id}, snapshot, unstable, image_asset


def compare(baseline, candidate, baseline_sha=None, candidate_sha=None):
    old_run, old, old_unstable, old_asset = snapshot(baseline, baseline_sha)
    new_run, new, new_unstable, new_asset = snapshot(candidate, candidate_sha)
    require(old_run["repository"] == new_run["repository"], "Runs belong to different repositories")
    require(old_unstable.keys() == new_unstable.keys(), "Runs contain different non-reproducible payloads")
    differences = {key: {"baseline": old.get(key), "candidate": new.get(key)}
                   for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)}
    signing_only = {}
    for key in ("kernel/signing", "native/signing"):
        if key in differences:
            signing_only[key] = differences.pop(key)
    cert_key = "kernel/public_certificate_sha256"
    if cert_key in differences and "ephemeral" in (old["kernel/signing"], new["kernel/signing"]):
        with masked_image(old_asset, old) as (a, cert_a, ids_a), masked_image(new_asset, new) as (b, cert_b, ids_b):
            require(ids_a.keys() == ids_b.keys(), "GNU build ID note counts or offsets differ between Images")
            if cert_a == cert_b and a == b:
                signing_only[cert_key] = differences.pop(cert_key)
                image_hash = "kernel/image_sha256"
                if image_hash in differences:
                    signing_only[image_hash] = differences.pop(image_hash)
                image_key = f"kernel/{old_asset.name}/Image"
                if old_asset.name == new_asset.name and old[image_key][1] == new[image_key][1]:
                    if image_key in differences:
                        signing_only[image_key] = dict(differences.pop(image_key), build_id=[
                            {"note_offset": hex(note), "baseline": value, "candidate": ids_b[note]}
                            for note, value in ids_a.items() if value != ids_b[note]])
    not_reproducible = {key: {"baseline": old_unstable[key], "candidate": new_unstable[key]}
                        for key in sorted(old_unstable) if old_unstable[key] != new_unstable[key]}
    return {"baseline": old_run, "candidate": new_run, "passed": not differences,
            "differences": differences, "signing_only": signing_only, "not_reproducible": not_reproducible}


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
