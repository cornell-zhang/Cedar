#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the Cedar compiler toolchain from pinned sources.
#
# This one script is used by every build path:
#   * native builds on a Linux x86-64 host (this file, run directly),
#   * the container build (toolchain/build/Dockerfile runs it inside
#     manylinux_2_28), and therefore
#   * the wheel build (toolchain/wheel/build_wheel.sh packages the container
#     output).
# All revisions come from toolchain/sources.lock.json. DialEgg and
# extraction-gym are built from the checked-in third_party/ sources.
#
# Usage:
#   toolchain/build/build-native.sh [STAGE ...]
# Stages (default: all, in order):
#   fetch llvm circt egglog dialegg gym install check
#
# Environment:
#   CEDAR_BUILD_ROOT   scratch dir for sources and build trees
#                      (default: ./build-toolchain; needs ~45 GB free)
#   CEDAR_PREFIX       install prefix (default: $CEDAR_BUILD_ROOT/install)
#   JOBS               compile parallelism (default: min(nproc, 16))
#   LINK_JOBS          concurrent LLVM links (default: 2; each may need ~8 GB RAM)
#   CEDAR_SOURCES_DIR  use pre-fetched sources (e.g. from a source bundle)
#                      instead of fetching from the network
#   CARGO_OFFLINE=1    build Rust crates from vendored sources (source bundle)
#   CC_LLVM/CXX_LLVM   compilers for LLVM/CIRCT (default: gcc/g++)
#   CC_DIALEGG/CXX_DIALEGG compilers for DialEgg (default: clang/clang++)
#   CEDAR_MARCH        -march baseline (default from lock: x86-64-v2)
#
# Requirements: git, python3 >= 3.8, cmake >= 3.28, ninja >= 1.11, a C++17/20 compiler
# (GCC >= 12 or clang >= 16), lld, python3, rustup/cargo.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
eval "$(python3 "$REPO/toolchain/scripts/lock.py" shell)"

BUILD_ROOT="${CEDAR_BUILD_ROOT:-$PWD/build-toolchain}"
mkdir -p "$BUILD_ROOT"
BUILD_ROOT="$(cd "$BUILD_ROOT" && pwd)"
PREFIX="${CEDAR_PREFIX:-$BUILD_ROOT/install}"
SRC="${CEDAR_SOURCES_DIR:-$BUILD_ROOT/src}"
BLD="$BUILD_ROOT/build"
NPROC="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
JOBS="${JOBS:-$(( NPROC < 16 ? NPROC : 16 ))}"
LINK_JOBS="${LINK_JOBS:-2}"
MARCH="${CEDAR_MARCH:-$LOCK_CPU_BASELINE}"
CC_LLVM="${CC_LLVM:-gcc}"
CXX_LLVM="${CXX_LLVM:-g++}"
CC_DIALEGG="${CC_DIALEGG:-clang}"
CXX_DIALEGG="${CXX_DIALEGG:-clang++}"
STATIC_RT="-static-libstdc++ -static-libgcc"

log() { printf '\n\033[1;36m[cedar-build %s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
stamp() { date +%s > "$BLD/.stage-$1.done"; }

mkdir -p "$BLD" "$PREFIX"

# fetch_git <dir> <url> <sha>: shallow fetch of exactly one commit.
fetch_git() {
    local dir="$1" url="$2" sha="$3"
    if [ -d "$dir/.git" ] && [ "$(git -C "$dir" rev-parse HEAD 2>/dev/null)" = "$sha" ]; then
        log "already at $sha: $dir"; return 0
    fi
    rm -rf "$dir"; mkdir -p "$dir"
    git -C "$dir" init -q
    git -C "$dir" remote add origin "$url"
    git -C "$dir" -c protocol.version=2 fetch -q --depth 1 origin "$sha"
    git -C "$dir" checkout -q FETCH_HEAD
    test "$(git -C "$dir" rev-parse HEAD)" = "$sha"
}

stage_fetch() {
    if [ -n "${CEDAR_SOURCES_DIR:-}" ]; then
        log "using pre-fetched sources in $SRC"
        for d in circt circt/llvm slang fmt egglog; do
            test -d "$SRC/$d" || { echo "missing $SRC/$d" >&2; exit 1; }
        done
        return 0
    fi
    log "fetching pinned sources into $SRC"
    mkdir -p "$SRC"
    fetch_git "$SRC/circt" "$LOCK_CIRCT_URL" "$LOCK_CIRCT_REVISION"
    # The llvm submodule is fetched directly at its pinned commit.
    fetch_git "$SRC/circt/llvm" "$LOCK_LLVM_URL" "$LOCK_LLVM_REVISION"
    fetch_git "$SRC/slang" "$LOCK_SLANG_URL" "$LOCK_SLANG_REVISION"
    fetch_git "$SRC/fmt" "$LOCK_FMT_URL" "$LOCK_FMT_REVISION"
    fetch_git "$SRC/egglog" "$LOCK_EGGLOG_URL" "$LOCK_EGGLOG_REVISION"
    stamp fetch
}

stage_llvm() {
    log "configuring + building LLVM/MLIR (jobs=$JOBS link_jobs=$LINK_JOBS)"
    cmake -S "$SRC/circt/llvm/llvm" -B "$BLD/llvm" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER="$CC_LLVM" -DCMAKE_CXX_COMPILER="$CXX_LLVM" \
        -DLLVM_ENABLE_PROJECTS=mlir \
        -DLLVM_TARGETS_TO_BUILD=host \
        -DLLVM_BUILD_EXAMPLES=OFF -DLLVM_BUILD_TESTS=OFF -DLLVM_INCLUDE_TESTS=OFF \
        -DLLVM_INCLUDE_BENCHMARKS=OFF -DLLVM_INCLUDE_DOCS=OFF \
        -DLLVM_ENABLE_ZLIB=OFF -DLLVM_ENABLE_ZSTD=OFF -DLLVM_ENABLE_LIBXML2=OFF \
        -DLLVM_ENABLE_ASSERTIONS=OFF \
        -DLLVM_PARALLEL_LINK_JOBS="$LINK_JOBS" \
        -DLLVM_USE_LINKER=lld \
        -DCMAKE_C_FLAGS="-march=$MARCH" -DCMAKE_CXX_FLAGS="-march=$MARCH"
    cmake --build "$BLD/llvm" -- -j"$JOBS"
    stamp llvm
}

stage_circt() {
    log "configuring + building CIRCT (circt-verilog, circt-opt, circt-lec)"
    cmake -S "$SRC/circt" -B "$BLD/circt" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER="$CC_LLVM" -DCMAKE_CXX_COMPILER="$CXX_LLVM" \
        -DCIRCT_SLANG_FRONTEND_ENABLED=ON -DCIRCT_SLANG_BUILD_FROM_SOURCE=ON \
        -DFETCHCONTENT_SOURCE_DIR_SLANG="$SRC/slang" \
        -DFETCHCONTENT_SOURCE_DIR_FMT="$SRC/fmt" \
        -DFETCHCONTENT_FULLY_DISCONNECTED=ON \
        -DCMAKE_DISABLE_FIND_PACKAGE_Boost=ON \
        -DCIRCT_BUILD_TESTS=OFF -DCIRCT_INCLUDE_TESTS=OFF \
        -DLLVM_DIR="$BLD/llvm/lib/cmake/llvm" -DMLIR_DIR="$BLD/llvm/lib/cmake/mlir" \
        -DLLVM_ENABLE_ASSERTIONS=OFF \
        -DLLVM_USE_LINKER=lld -DCMAKE_SKIP_RPATH=ON \
        -DCMAKE_C_FLAGS="-march=$MARCH" -DCMAKE_CXX_FLAGS="-march=$MARCH" \
        -DCMAKE_EXE_LINKER_FLAGS="$STATIC_RT"
    cmake --build "$BLD/circt" --target circt-verilog circt-opt circt-lec -- -j"$JOBS"
    stamp circt
}

cargo_flags() {
    if [ "${CARGO_OFFLINE:-0}" = "1" ]; then echo "--offline --locked"; else echo "--locked"; fi
}

stage_egglog() {
    log "building egglog (egg-smol fork)"
    # Build in a copy so the source tree (possibly from a bundle) stays pristine.
    rm -rf "$BLD/egglog-target"
    ( cd "$SRC/egglog" && CARGO_TARGET_DIR="$BLD/egglog-target" \
        RUSTFLAGS="-C target-cpu=$MARCH" cargo build --release $(cargo_flags) )
    stamp egglog
}

stage_dialegg() {
    log "configuring + building DialEgg egg-opt from third_party/dialegg"
    cmake -S "$REPO/$LOCK_DIALEGG_PATH" -B "$BLD/dialegg" -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER="$CC_DIALEGG" -DCMAKE_CXX_COMPILER="$CXX_DIALEGG" \
        -DLLVM_DIR="$BLD/llvm/lib/cmake/llvm" -DMLIR_DIR="$BLD/llvm/lib/cmake/mlir" \
        -DCIRCT_DIR="$BLD/circt/lib/cmake/circt" \
        -DCIRCT_SOURCE_DIR="$SRC/circt" -DCIRCT_BUILD_DIR="$BLD/circt" \
        -DCMAKE_CXX_FLAGS="-march=$MARCH" -DCMAKE_SKIP_RPATH=ON \
        -DCMAKE_EXE_LINKER_FLAGS="$STATIC_RT -fuse-ld=lld"
    cmake --build "$BLD/dialegg" --target egg-opt -- -j"$JOBS"
    stamp dialegg
}

stage_gym() {
    log "building extract_result from third_party/extraction-gym"
    ( cd "$REPO/$LOCK_EXTRACTION_GYM_PATH" && CARGO_TARGET_DIR="$BLD/gym-target" \
        RUSTFLAGS="-C target-cpu=$MARCH" cargo build --release --bin extract_result $(cargo_flags) )
    stamp gym
}

stage_install() {
    log "installing into $PREFIX"
    mkdir -p "$PREFIX/bin" "$PREFIX/share/cedar" "$PREFIX/licenses"
    install -m 755 "$BLD/circt/bin/circt-verilog" "$PREFIX/bin/circt-verilog"
    install -m 755 "$BLD/circt/bin/circt-opt" "$PREFIX/bin/circt-opt"
    install -m 755 "$BLD/circt/bin/circt-lec" "$PREFIX/bin/circt-lec"
    install -m 755 "$BLD/dialegg/egg-opt" "$PREFIX/bin/egg-opt" 2>/dev/null \
        || install -m 755 "$BLD/dialegg/bin/egg-opt" "$PREFIX/bin/egg-opt"
    install -m 755 "$BLD/egglog-target/release/egglog" "$PREFIX/bin/egglog"
    install -m 755 "$BLD/gym-target/release/extract_result" "$PREFIX/bin/extract_result"
    strip --strip-unneeded "$PREFIX"/bin/* || true
    install -m 644 "$REPO"/toolchain/templates/*.egg "$PREFIX/share/cedar/"
    # Licenses / notices for binary redistribution.
    install -m 644 "$SRC/circt/LICENSE" "$PREFIX/licenses/circt-LICENSE.txt"
    install -m 644 "$SRC/circt/llvm/llvm/LICENSE.TXT" "$PREFIX/licenses/llvm-LICENSE.txt"
    install -m 644 "$SRC/circt/llvm/mlir/LICENSE.TXT" "$PREFIX/licenses/mlir-LICENSE.txt"
    install -m 644 "$SRC/slang/LICENSE" "$PREFIX/licenses/slang-LICENSE.txt"
    install -m 644 "$SRC/fmt/LICENSE" "$PREFIX/licenses/fmt-LICENSE.txt" 2>/dev/null \
        || install -m 644 "$SRC/fmt/LICENSE.rst" "$PREFIX/licenses/fmt-LICENSE.txt"
    install -m 644 "$SRC/egglog/LICENSE" "$PREFIX/licenses/egglog-LICENSE.txt"
    install -m 644 "$REPO/third_party/dialegg/LICENSE.txt" "$PREFIX/licenses/dialegg-LICENSE.txt"
    install -m 644 "$REPO/third_party/extraction-gym/LICENSE" "$PREFIX/licenses/extraction-gym-LICENSE.txt"
    install -m 644 "$REPO/LICENSE" "$PREFIX/licenses/cedar-LICENSE.txt"
    install -m 644 "$REPO/THIRD_PARTY_NOTICES.md" "$PREFIX/licenses/THIRD_PARTY_NOTICES.md"
    python3 "$REPO/toolchain/scripts/write_manifest.py" "$PREFIX" \
        --rust-lock "egglog=$SRC/egglog/Cargo.lock" \
        --rust-lock "extraction_gym=$REPO/$LOCK_EXTRACTION_GYM_PATH/Cargo.lock"
    stamp install
}

stage_check() {
    log "ABI/portability and smoke checks"
    python3 "$REPO/toolchain/scripts/check_abi.py" "$PREFIX/bin"
    python3 "$REPO/toolchain/scripts/smoke_toolchain.py" --bin "$PREFIX/bin" --share "$PREFIX/share/cedar"
    stamp check
}

STAGES=("$@")
if [ ${#STAGES[@]} -eq 0 ]; then
    STAGES=(fetch llvm circt egglog dialegg gym install check)
fi
T0=$(date +%s)
for s in "${STAGES[@]}"; do
    t=$(date +%s)
    "stage_$s"
    log "stage $s finished in $(( $(date +%s) - t ))s"
done
log "done in $(( $(date +%s) - T0 ))s; prefix: $PREFIX"
