#!/usr/bin/env bash
set -euo pipefail
TASK_REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TASK_WORK=
TASK_DIST=
while (( $# )); do
  case "$1" in
    --workspace) TASK_WORK=$2; shift 2 ;;
    --dist) TASK_DIST=$2; shift 2 ;;
    *) printf '%s\n' 'Usage: build-ksud.sh --workspace WORK --dist DIST' >&2; exit 2 ;;
  esac
done
[[ -n "$TASK_WORK" && -n "$TASK_DIST" ]] || { printf '%s\n' 'workspace and dist are required' >&2; exit 2; }
TASK_SOURCE_SHA=$(git -C "$TASK_REPO" rev-parse HEAD)
[[ "$TASK_SOURCE_SHA" =~ ^[0-9a-f]{40}$ && "${CI_SOURCE_SHA:-${GITHUB_SHA:-$TASK_SOURCE_SHA}}" == "$TASK_SOURCE_SHA" ]] || {
  printf '%s\n' 'CI source SHA differs from the checked out development commit.' >&2; exit 1;
}
[[ -z "$(git -C "$TASK_REPO" status --porcelain --untracked-files=all)" ]] || {
  printf '%s\n' 'Native builds require a clean committed development checkout.' >&2; exit 1;
}
export CI_SOURCE_SHA="$TASK_SOURCE_SHA"
export CI_SOURCE_REF="${CI_SOURCE_REF:-${GITHUB_REF:-local}}"
TASK_WORK=$(realpath -m "$TASK_WORK")
TASK_DIST=$(realpath -m "$TASK_DIST")
export PYTHONDONTWRITEBYTECODE=1 CARGO_NET_GIT_FETCH_WITH_CLI=true
python3 "$TASK_REPO/scripts/bootstrap.py" --workspace "$TASK_WORK" --native-only
command -v rustup >/dev/null || { printf '%s\n' 'rustup is required on the CI runner' >&2; exit 1; }
rustup toolchain install 1.91.1 --profile minimal --target aarch64-linux-android
export ANDROID_NDK_HOME="$TASK_WORK/ndk"
export PATH="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin:$PATH"
export CARGO_TARGET_DIR="$TASK_WORK/target"
cd "$TASK_WORK/source"
source .github/scripts/setup-rust-build.sh aarch64-linux-android 26
python3 "$TASK_REPO/scripts/bootstrap.py" --workspace "$TASK_WORK" --check-native-inputs >/dev/null
cargo +1.91.1 build --locked --release --target aarch64-linux-android --manifest-path userspace/ksud/Cargo.toml
python3 "$TASK_REPO/scripts/bootstrap.py" --workspace "$TASK_WORK" --verify-native \
  --target-dir "$CARGO_TARGET_DIR" --dist "$TASK_DIST"
