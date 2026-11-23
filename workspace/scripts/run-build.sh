#!/usr/bin/env bash
set -euo pipefail
TASK_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
lto_partitions=${LTO_PARTITIONS:-1}
lto_threads=${LTO_THREADS:-2}
if [[ ! "$lto_partitions" =~ ^[1-8]$ || ! "$lto_threads" =~ ^[1-8]$ ]]; then
  printf '%s\n' 'LTO_PARTITIONS and LTO_THREADS must be integers from 1 to 8.' >&2
  exit 2
fi
# The successful r1 link used one Full LTO partition. Clang r450784e with
# this kernel's CFI/BTF fails final linking with multiple partitions.
if [[ "$lto_partitions" != 1 ]]; then
  printf '%s\n' 'The locked Clang 14 CFI/BTF build requires LTO_PARTITIONS=1.' >&2
  exit 2
fi
case "${1:-}" in
  '') ;;
  --preflight) exec python3 "$TASK_ROOT/scripts/preflight.py" ;;
  --dry-run) printf 'Full LTO: build/build.sh -j8 "LD=ld.lld --threads=%s --lto-partitions=%s"\n' "$lto_threads" "$lto_partitions"; exit 0 ;;
  *) printf '%s\n' 'Usage: bash scripts/run-build.sh [--preflight|--dry-run]' >&2; exit 2 ;;
esac
if (( $# > 1 )); then exit 2; fi
mkdir -p "$TASK_ROOT/logs"
exec 9>"$TASK_ROOT/.pipeline.lock"
flock -n 9 || { printf '%s\n' 'Another build, verifier or delivery operation owns this pipeline.' >&2; exit 1; }
export TASK_ROOT BUILD_PHASE=sukisu
export PYTHONDONTWRITEBYTECODE=1
unset PYTHONOPTIMIZE
trap 'python3 "$TASK_ROOT/scripts/build_provenance.py" abort' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
export BUILD_CONFIG=common/build.config.gki.aarch64
export GKI_BUILD_CONFIG_FRAGMENT="$TASK_ROOT/configs/fuxi-build.config"
export LTO=full KMI_SYMBOL_LIST_STRICT_MODE=1
export OUT_DIR="$TASK_ROOT/out/sukisu"
export SIGNING_KEY_DIR="${SIGNING_KEY_DIR:-$TASK_ROOT/inputs/signing}"
# Inherited make variables can silently change trust, compilation or signing.
while IFS= read -r name; do
  case "$name" in KSU_*|KBUILD_*|KCFLAGS|KCPPFLAGS|CFLAGS_KERNEL|CFLAGS_MODULE|LDFLAGS*|KCONFIG*|MAKEFLAGS|MFLAGS|MAKEOVERRIDES|LLVM*|CC|LD|AR|NM|OBJCOPY|OBJDUMP|STRIP|SKIP_*|BUILD_NUMBER) unset "$name" ;; esac
done < <(compgen -e)
export KBUILD_BUILD_USER=build-user KBUILD_BUILD_HOST=build-host KBUILD_BUILD_VERSION=1
KBUILD_BUILD_TIMESTAMP=$(git -C "$TASK_ROOT/kernel/common" show -s --format=%cD HEAD)
SOURCE_DATE_EPOCH=$(git -C "$TASK_ROOT/kernel/common" show -s --format=%ct HEAD)
export KBUILD_BUILD_TIMESTAMP SOURCE_DATE_EPOCH
if [[ -f "$OUT_DIR/common/.config" ]]; then export SKIP_MRPROPER=1; fi
cd "$TASK_ROOT/kernel"
log="$TASK_ROOT/logs/sukisu-build-$(date -u +%Y%m%dT%H%M%SZ).log"
ln -sfn "$(basename "$log")" "$TASK_ROOT/logs/sukisu-build.log"
python3 "$TASK_ROOT/scripts/build_provenance.py" run -- /usr/bin/time -v build/build.sh -j8 "LD=ld.lld --threads=$lto_threads --lto-partitions=$lto_partitions" 2>&1 | tee "$log"
