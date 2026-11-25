"""Prepare and publish a draft upstream-pin PR from a completed watch report."""
import argparse
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess


LOCK = Path("ci-layout.json")
REPORTS = Path("docs/upstream-reports")


def output(*args):
    return subprocess.check_output(args, text=True, encoding="utf-8").strip()


def pins(layout):
    return {name: info["commit"] for name, info in layout["upstreams"].items()}


def update_lock(original, updates):
    expected = json.loads(original)
    # Preserve the lock's hand-formatted arrays and all bytes outside the commit lines.
    result = original
    for name, sha in updates.items():
        info = expected["upstreams"][name]
        if "branch" not in info or not re.fullmatch(r"[0-9a-f]{40}", sha) or sha == info["commit"]:
            raise ValueError(f"Invalid proposed revision for {name}")
        section = rb'(?m)^    "' + re.escape(name.encode()) + rb'": \{\n(?:      [^\n]*\n)*?'
        prefix = b"(" + section.removeprefix(b"(?m)") + rb'      "commit": ")'
        pattern = prefix + re.escape(info["commit"].encode()) + rb'"'
        result, count = re.subn(pattern, lambda m: m[1] + sha.encode() + b'"', result, flags=re.M)
        if count != 1:
            raise ValueError(f"Expected one commit line for {name}")
        info["commit"] = sha
    if json.loads(result) != expected:
        raise ValueError("Unexpected change outside the proposed upstream pins")
    return result


def prepare(folder):
    updates = json.loads((folder / "updates.json").read_bytes())
    if not updates:
        return
    report, = (folder / "reports").glob("*.md")
    day = date.fromisoformat(report.stem).isoformat()
    if report.name != day + ".md":
        raise ValueError("Unexpected report filename")
    LOCK.write_bytes(update_lock(LOCK.read_bytes(), updates))
    stat = output("git", "diff", "--numstat", "--", str(LOCK))
    if stat != f"{len(updates)}\t{len(updates)}\t{LOCK}":
        raise ValueError(f"Unexpected lock diff: {stat}")
    REPORTS.mkdir(parents=True, exist_ok=True)
    with (REPORTS / report.name).open("xb") as dest:
        dest.write(report.read_bytes())
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8", newline="\n") as step:
        step.write(f"day={day}\n")


def matching_pr(repo, base, desired):
    for page in json.loads(output("gh", "api", "--paginate", "--slurp",
                                  f"repos/{repo}/pulls?state=open&per_page=100")):
        for pr in page:
            head = pr["head"]
            if (pr["base"]["ref"] != base or not head["ref"].startswith("bot/upstream-")
                    or (head.get("repo") or {}).get("full_name") != repo):
                continue
            # Read the PR head as data. Never check it out or run its scripts.
            lock = output("gh", "api", "-H", "Accept: application/vnd.github.raw+json",
                          f"repos/{repo}/contents/ci-layout.json?ref={head['sha']}")
            if pins(json.loads(lock)) == desired:
                return pr["html_url"]
    return None


def publish(day):
    day = date.fromisoformat(day).isoformat()
    repo = os.environ["GITHUB_REPOSITORY"]
    base = os.environ["GITHUB_REF_NAME"]
    existing = matching_pr(repo, base, pins(json.loads(LOCK.read_bytes())))
    if existing:
        print(f"Matching open PR: {existing}")
        return
    branch = "bot/upstream-" + day
    report = REPORTS / (day + ".md")
    subprocess.run(["git", "switch", "-c", branch], check=True)
    subprocess.run(["git", "add", "--", str(LOCK), str(report)], check=True)
    subprocess.run(["git", "-c", "user.name=github-actions[bot]", "-c",
                    "user.email=41898282+github-actions[bot]@users.noreply.github.com",
                    "commit", "-m", f"Review upstream changes detected on {day}"], check=True)
    # Limit the credential helper to this push and require the remote branch to be new.
    ref = f"refs/heads/{branch}"
    subprocess.run(["git", "-c", "credential.helper=", "-c",
                    "credential.https://github.com.helper=!gh auth git-credential",
                    "push", f"--force-with-lease={ref}:", "origin", f"HEAD:{ref}"], check=True)
    subprocess.run(["gh", "pr", "create", "--repo", repo, "--base", base, "--head", branch,
                    "--draft", "--title", f"Review upstream changes for {day}", "--body-file", str(report)], check=True)
    subprocess.run(["gh", "workflow", "run", "build.yml", "--repo", repo, "--ref", branch], check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument("folder", type=Path)
    commands.add_parser("publish").add_argument("day")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.folder)
    else:
        publish(args.day)
