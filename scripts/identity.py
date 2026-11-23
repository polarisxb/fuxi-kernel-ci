"""Derive build identities and public artifact names from ci-layout.json."""
import argparse
import json
from pathlib import Path
import re


LAYOUT_PATH = Path(__file__).resolve().parents[1] / "ci-layout.json"


def derive(layout):
    values = dict(layout["identity"])
    upstreams = layout["upstreams"]
    for name, upstream in upstreams.items():
        commit = upstream["commit"]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError(f"Invalid commit for {name}")
        values[name + "_commit"] = commit
        values[name + "_commit8"] = commit[:8]

    release = layout["kernel_release"]
    version = upstreams["susfs"]["version"]
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(f"Unexpected SUSFS version: {version}")
    values.update(kernel_release=release, kernel_version=release.split("-", 1)[0],
                  susfs_version=version, susfs_short=version[1:], susfs_compact=version[1:].replace(".", ""))
    required_suffix = "-g{common_commit:.12}-suki{driver_version}-susfs{susfs_compact}-{release_suffix}"
    if not release.endswith(required_suffix.format_map(values)):
        raise ValueError("kernel_release is inconsistent with the locked commits and versions")
    apk = upstreams["manager"]["apk"]
    values["manager_git_count"] = upstreams["manager"]["git_count"]
    values["manager_tag"] = upstreams["manager"]["tag"]
    values.update(apk_name=apk["name"], apk_sha256=apk["sha256"])
    for name, template in layout["names"].items():
        value = template.format_map(values)
        if name != "kernel_string" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", value):
            raise ValueError(f"Unsafe filename for {name}: {value!r}")
        values[name] = value
    return values


def from_repository():
    return derive(json.loads(LAYOUT_PATH.read_bytes()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", required=True)
    parser.parse_args()
    print(json.dumps(from_repository(), indent=2))
