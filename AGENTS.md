<!-- AUTONOMY DIRECTIVE — DO NOT REMOVE -->
Execute clear, authorized work to completion. Do not repeatedly ask permission
for routine implementation, validation, commits or pushes already requested.
Use native subagents for bounded independent work when it improves throughput.
<!-- END AUTONOMY DIRECTIVE -->

# Fuxi kernel development contract

This repository is designed for Windows development and Linux compilation in
GitHub Actions. Do not require a local Ubuntu setup for reading or editing code.

- `src/common/` contains actual edited Linux files over a fixed upstream commit.
  CI combines them with the locked upstream, regenerates source diffs, and builds.
  To read/import another upstream file, use `scripts/source.py`; do not edit a
  generated `.work/` checkout or replace actual sources with stale patches.
- `src/KernelSU/` is the current builtin SukiSU driver used by the kernel.
- `src/SukiSU/` contains the matching Manager/userspace source. Its CI build keeps
  the original Git history so versionCode 40900 is real, not fabricated.
- `module/` contains the matching SUSFS local2 module source.
- `configs/`, `ci-layout.json` and `reference/inputs.lock.json` lock compatibility,
  source revisions, tools and public verification inputs. Change locks explicitly.
- Linux symlinks are recorded in `configs/source-links.json` and recreated in CI.
  Keep this checkout safe for ordinary Windows NTFS; never disable protectNTFS to
  force reserved names or case-conflicting Linux paths into the development tree.
- Preserve Full LTO, one LTO partition, two link threads, CFI, BTF, MODVERSIONS,
  module signature protection and strict KMI. Do not weaken checks to turn CI green.
- Keep all private signing keys in Actions Secrets and runner temporary files.
  Never commit them, include them in ZIPs, cache an entire out directory, or print
  their contents. Public certificates and hashes are verification material.
- Preserve source/input/output binding, stock-module ELF/CRC/import checks,
  signature checks, UAPI checks and clean source replay. A ZIP must correspond to
  the exact source commit recorded in its build metadata.
- GitHub Actions builds and publishes; CI never flashes a phone. Device writes
  require the user's separate explicit instruction and a fresh current-slot backup.
- Run checks appropriate to the change. Existing behavior tests must continue to
  pass. For refactoring, write a short plan before edits and prefer deletion/reuse.
- Report changed files, validation evidence, simplifications and remaining risks.

## Commit decision records

Every commit follows the Lore protocol: an intent line explaining why, optional
narrative context, and useful native Git trailers after a blank line. Use
`Constraint:`, `Rejected:`, `Confidence:`, `Scope-risk:`, `Directive:`, `Tested:`
and `Not-tested:` when they add information. Be honest about tests not performed.

