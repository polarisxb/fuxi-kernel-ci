<!-- AUTONOMY DIRECTIVE — DO NOT REMOVE -->
Execute clear, authorized work to completion. Do not repeatedly ask permission
for routine implementation, validation, commits or pushes already requested.
Use native subagents for bounded independent work when it improves throughput.
<!-- END AUTONOMY DIRECTIVE -->

# Fuxi kernel development contract

This repository is designed for Windows development and Linux compilation in
GitHub Actions. Do not require a local Ubuntu setup for reading or editing code.

- Upstream code is never committed. `ci-layout.json` (`upstreams`) pins
  kernel/common, SUSFS, the SukiSU builtin driver, the SukiSU Manager and the NDK;
  `configs/manifest.locked.xml` must agree with it.
- Local changes live in `patches/common`, `patches/driver` and `patches/manager`,
  applied in `series` order after the official SUSFS kernel patch. One patch per
  logical change, each with `Subject:`, `Upstream-Status:` and `Drop-when:`.
  Edit them only through `scripts/source.py edit|new|refresh`; never reintroduce
  vendored upstream trees or full-file overlays. Submit real upstream bug fixes and
  drop the patch once upstream has it.
- The Manager CI build keeps the original Git history so versionCode 40900 is real,
  not fabricated.
- The SUSFS module is also a pinned upstream (`upstreams.module`, sidex15 R28) plus
  `patches/module`; CI assembles it and packages it as local3. Its `ksu_susfs` helper
  is compiled by CI from the pinned SUSFS commit and added when packaging; never
  commit a prebuilt helper or a copy of the module.
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

