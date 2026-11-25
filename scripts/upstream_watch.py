#!/usr/bin/env python3
"""Report how the local queues replay at current upstream branch heads."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

sys.dont_write_bytecode = True
import source

Q = source.Q


def branch_head(info):
    ref = "refs/heads/" + info["branch"]
    with tempfile.TemporaryDirectory() as cwd:
        output = subprocess.check_output(
            ["git", "-c", "credential.helper=", "-c", "http.extraHeader=", "ls-remote",
             "--exit-code", "--refs", info["url"], ref], cwd=cwd, text=True, timeout=90,
            env=dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never"))
    sha, actual = output.strip().split()
    if actual != ref or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"Unexpected ls-remote result for {ref}")
    return sha


def public_json(url):
    with urlopen(Request(url, headers={"User-Agent": "fuxi-upstream-watch"}), timeout=60) as response:
        return json.load(response)


def compare(info, head):
    url = urlsplit(info["url"])
    project = url.path.strip("/").removesuffix(".git")
    old = info["commit"]
    try:
        if url.hostname == "github.com":
            result = public_json(f"https://api.github.com/repos/{project}/compare/{old}...{head}")
            return result["ahead_by"]
        if url.hostname == "gitlab.com":
            result = public_json(f"https://gitlab.com/api/v4/projects/{quote(project, safe='')}"
                                 f"/repository/compare?from={old}&to={head}")
            if not result.get("compare_timeout"):
                return len(result["commits"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return "unknown"


def module_tree(info, head):
    project = urlsplit(info["url"]).path.strip("/").removesuffix(".git")
    result = public_json(f"https://api.github.com/repos/{project}/git/trees/{head}?recursive=1")
    if result["truncated"]:
        raise ValueError("Incomplete module tree from GitHub")
    return {entry["path"]: entry["mode"] for entry in result["tree"] if entry["type"] == "blob"}


def touched(data):
    return Q.patch_paths(Q.split_patch(data.decode("utf-8"))[1])


def put(tree, path, data):
    target = tree / source.windows_path(Q.safe_relative(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def inspect(name, info, head, tree, directory):
    files = {path for patch in Q.read_series(directory) for path in touched(patch.read_bytes())}
    notes, missing = [], set()
    modes = None
    component = name
    revision = head
    if name == "susfs":
        component, revision = "common", None  # kernel/common stays at its lock
        official = source.fetch(name, info["kernel_patch"], commit=head)
        if official is not None:
            files.update(touched(official))
        files.update(info["kernel_files"])
    if name == "module":
        files.update(info["crlf_to_lf"])
        files.add("module.prop")
        try:
            modes = module_tree(info, head)
        except (OSError, ValueError, KeyError, TypeError) as error:
            notes.append(f"无法读取模块文件列表：{error}；exclude 规则和文件权限没有检查")
        else:
            for rule in info["exclude"]:
                if not any(Q.module_excluded(path, {"exclude": [rule]}) for path in modes):
                    notes.append(f"Review lock rule: exclude matches nothing: {rule}")
    source.unique_case(files)
    for path in sorted(files):
        if name == "module" and Q.module_excluded(path, info):
            continue
        data = source.fetch(component, path, commit=revision)
        if name == "module" and path in info["crlf_to_lf"]:
            if data is None:
                notes.append(f"Review lock rule: crlf_to_lf file missing: {path}")
            else:
                try:
                    data = Q.crlf_to_lf(data, path)
                except ValueError as error:
                    notes.append(f"Review lock rule: {error}")
        if data is None:
            missing.add(path)
        else:
            target = put(tree, path, data)
            if modes is not None and modes.get(path) == "100755":
                target.chmod(0o755)
    if modes is not None:
        for patch in Q.read_series(directory):
            data = patch.read_bytes()
            if b"\nold mode " in data:
                for path in touched(data):
                    notes.append(f"Upstream Git mode: {path}: {modes.get(path, 'absent')}"
                                 " (git apply may only warn on a mode mismatch)")
    if name == "susfs":
        issues = []
        version = None
        for path, upstream_path in info["kernel_files"].items():
            data = source.fetch(name, upstream_path, commit=head)
            if data is None:
                issues.append(f"missing {upstream_path}")
                continue
            if (tree / path).exists():
                issues.append(f"would overwrite {path} in pinned common")
                continue
            put(tree, path, data)
            if path.endswith(("/susfs.h", "/susfs_def.h")):
                match = re.search(rb'(?m)^\s*#define\s+SUSFS_VERSION\s+"([\w.+-]+)"', data)
                if match:
                    version = match[1].decode()
        if official is None:
            issues.append(f"missing {info['kernel_patch']}")
        else:
            try:
                Q.git_apply(tree, official, "Official SUSFS patch")
            except ValueError as error:
                issues.extend(str(error).splitlines()[:5])
        notes.append("Official SUSFS installation: " + ("conflict\n" + "\n".join(issues) if issues else "applies"))
        notes.append("SUSFS_VERSION: " + (version or "unknown"))
        if issues:
            notes.append("Common queue results below use the partially prepared tree.")
    if name == "module":
        prop = tree / "module.prop"
        props = prop.read_bytes().decode() if prop.exists() else ""
        for field in ("version", "versionCode"):
            match = re.search(rf"(?m)^{field}=([^\r\n]+)", props)
            notes.append(f"module.prop {field}: " + (match[1] if match else "unknown"))
    records = Q.check_series(tree, directory)
    for record in records:
        if name in {"driver", "manager"} and record["status"] == "conflict":
            absent = missing.intersection(touched((directory / record["name"]).read_bytes()))
            if absent:
                record["missing"] = sorted(absent)
    return notes, records


def report(root, date):
    layout = Q.load_layout(root)
    lines = [f"# Upstream watch {date}"]
    updates, drops = {}, []
    for name, info in layout["upstreams"].items():
        if "branch" not in info:
            continue
        head = branch_head(info)
        old = info["commit"]
        lines.extend(["", f"## {name} ({info['branch']})", "", f"Pin: {old}", f"Head: {head}"])
        if head == old:
            lines.append("No update (0 commits).")
            continue
        updates[name] = head
        lines.append(f"Commits ahead: {compare(info, head)}")
        queue = "common" if name == "susfs" else name
        directory = Q.queue_directory(root, layout, queue)
        with tempfile.TemporaryDirectory(prefix="fuxi-upstream-") as temp:
            notes, records = inspect(name, info, head, Path(temp), directory)
        for note in notes:
            lines.extend(["", *("    " + line for line in note.splitlines())])
        lines.append("")
        for record in records:
            label = record["name"]
            lines.append(f"- {label}: {record['status']}")
            for line in record.get("error", "").splitlines()[:4]:
                lines.append("      " + line)
            if record.get("missing"):
                lines.append("      Absent at head: " + ", ".join(record["missing"]))
            if record.get("after_conflict_in"):
                lines.append("      after_conflict_in: " + ", ".join(record["after_conflict_in"]))
            if record["status"] == "already-upstream":
                drops.append(f"{queue}/{label}")
    lines.extend(["", "## Suggested patch removals", "", *["- " + p for p in drops or ["None"]],
                  "", "## Fields for human review", "", "- identity.driver_version",
                  "- kernel_release: suki and susfs components", "- identity.release_suffix",
                  "- identity.module_local", "- Module versionCode rule"])
    return "\n".join(lines) + "\n", updates


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Print the report (default)")
    mode.add_argument("--write", type=Path, metavar="DIR", help="Write the dated report and print update JSON")
    args = parser.parse_args()
    date = datetime.now(timezone.utc).date().isoformat()
    try:
        markdown, updates = report(source.ROOT, date)
        if args.write is None:
            sys.stdout.buffer.write(markdown.encode("utf-8"))
        else:
            args.write.mkdir(parents=True, exist_ok=True)
            (args.write / f"{date}.md").write_bytes(markdown.encode("utf-8"))
            sys.stdout.buffer.write((json.dumps(updates, ensure_ascii=False) + "\n").encode("utf-8"))
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Upstream watch stopped: {error}\n")


if __name__ == "__main__":
    main()
