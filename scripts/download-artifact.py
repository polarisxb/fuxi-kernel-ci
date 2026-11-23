#!/usr/bin/env python3
"""Download a public, completed CI release and verify its payload/source SHA."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


DEFAULT_REPO = "polarisxb/fuxi-kernel-ci"
DEFAULT_ASSET = "Fuxi_5.15.194_SukiSU_40900_SUSFS_2.3.0_r2_AnyKernel3.zip"
API = "https://api.github.com"


def safe_name(name):
    if (not isinstance(name, str) or not name or name in {".", ".."}
            or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
            or name.rstrip(" .") != name
            or re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name, re.I)):
        raise ValueError(f"Unsafe asset filename: {name!r}")
    return name


def validate_repo(repo):
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo)
            or repo.split("/", 1)[1] in {".", ".."}):
        raise ValueError("Repository must be owner/name")
    return repo


def request(url, *, api=False):
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or not (parsed.hostname in {"github.com", "api.github.com"}
                    or (parsed.hostname or "").endswith(".githubusercontent.com"))):
        raise ValueError("Release URL is not a GitHub HTTPS endpoint")
    headers = {"User-Agent": "fuxi-kernel-ci-agent", "Accept": "application/vnd.github+json"}
    if api:
        if parsed.hostname != "api.github.com":
            raise ValueError("API credentials are restricted to api.github.com")
        headers["X-GitHub-Api-Version"] = "2022-11-28"
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = "Bearer " + token
    return Request(url, headers=headers)


def read_small(url, *, api=False):
    with urlopen(request(url, api=api), timeout=60) as response:
        data = response.read(2 * 1024 * 1024 + 1)
    if len(data) > 2 * 1024 * 1024:
        raise ValueError("Release metadata exceeds 2 MiB")
    return data


def asset(release, name):
    matches = [item for item in release.get("assets", []) if item.get("name") == name]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one release asset {name!r}")
    item = matches[0]
    safe_name(item["name"])
    if item.get("state", "uploaded") != "uploaded":
        raise ValueError(f"Release asset is not uploaded: {name}")
    request(item["browser_download_url"])
    return item


def checksums(data):
    records = {}
    for line in data.decode("utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(.+)", line)
        if not match:
            raise ValueError("Malformed SHA256SUMS entry")
        name = safe_name(match.group(2))
        if name in records:
            raise ValueError(f"Duplicate checksum entry: {name}")
        records[name] = match.group(1).lower()
    return records


def source_commit(info):
    values = [info[key] for key in ("source_commit", "source_sha", "commit") if key in info]
    if (not values or not all(isinstance(value, str) for value in values)
            or len(set(values)) != 1 or not re.fullmatch(r"[0-9a-f]{40}", values[0])):
        raise ValueError("Build metadata does not identify one complete source commit")
    return values[0]


def download(repo=DEFAULT_REPO, output=Path("downloads"), name=DEFAULT_ASSET,
             tag=None, commit=None, force=False):
    validate_repo(repo)
    safe_name(name)
    if commit is not None and not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ValueError("Commit constraint must be 7–40 lowercase hex characters")
    endpoint = "tags/" + quote(tag, safe="") if tag else "latest"
    release = json.loads(read_small(f"{API}/repos/{repo}/releases/{endpoint}", api=True))
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("Only completed published CI releases can be downloaded")
    if tag and release.get("tag_name") != tag:
        raise ValueError("GitHub response does not match the requested immutable tag")
    selected = asset(release, name)
    metadata_asset = asset(release, "build-info.json")
    checksum_asset = asset(release, "SHA256SUMS")
    sums = checksums(read_small(checksum_asset["browser_download_url"]))
    metadata = read_small(metadata_asset["browser_download_url"])
    if sums.get("build-info.json") != hashlib.sha256(metadata).hexdigest():
        raise ValueError("Build metadata SHA256 mismatch")
    info = json.loads(metadata)
    sha = source_commit(info)
    if info.get("repository") != repo:
        raise ValueError("Build metadata belongs to another repository")
    if commit and not sha.startswith(commit):
        raise ValueError(f"Selected release does not match requested commit {commit}")
    if name not in sums:
        raise ValueError("Selected asset has no SHA256 entry")
    declared = info.get("artifacts", {}).get(name)
    if not isinstance(declared, dict) or declared.get("sha256") != sums[name]:
        raise ValueError("Selected asset is not bound to the build metadata")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    target = output / name
    receipt = output / (name + ".download.json")
    if not force and (target.exists() or receipt.exists()):
        raise FileExistsError("Download already exists; choose a new output or use --force")
    temporary = None
    receipt_temporary = None
    try:
        digest = hashlib.sha256()
        size = 0
        with tempfile.NamedTemporaryFile(prefix=".fuxi-download-", dir=output, delete=False) as file:
            temporary = Path(file.name)
            with urlopen(request(selected["browser_download_url"]), timeout=120) as response:
                while chunk := response.read(1024 * 1024):
                    file.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
        if digest.hexdigest() != sums[name]:
            raise ValueError("Downloaded ZIP SHA256 mismatch")
        if size != declared.get("bytes") or size != selected.get("size"):
            raise ValueError("Downloaded ZIP length mismatch")
        details = {"repository": repo, "tag": release["tag_name"], "source_commit": sha,
                   "release_url": release["html_url"],
                   "download_url": selected["browser_download_url"],
                   "filename": name, "bytes": size, "sha256": digest.hexdigest()}
        with tempfile.NamedTemporaryFile(prefix=".fuxi-receipt-", dir=output, delete=False) as file:
            file.write((json.dumps(details, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            receipt_temporary = Path(file.name)
        temporary.replace(target)
        receipt_temporary.replace(receipt)
        return details
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if receipt_temporary is not None:
            receipt_temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--output", type=Path, default=Path("downloads"))
    parser.add_argument("--asset", default=DEFAULT_ASSET)
    parser.add_argument("--tag", help="Choose an immutable published CI release tag")
    parser.add_argument("--commit", help="Require the selected release to match this source SHA prefix")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = download(args.repo, args.output, args.asset, args.tag, args.commit, args.force)
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"Download stopped: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
