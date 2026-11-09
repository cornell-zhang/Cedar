#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build the cedar-toolchain wheel.
#
#   toolchain/wheel/build_wheel.sh                  # container build (Docker), then package
#   toolchain/wheel/build_wheel.sh --prefix DIR     # package an existing install prefix
#                                                   # (e.g. from toolchain/build/build-native.sh)
#   toolchain/wheel/build_wheel.sh --image TAG      # package the output of an already built image
#
# Environment: JOBS, LINK_JOBS (container build parallelism), OUT_DIR (default
# toolchain/wheel/dist), PYTHON (default python3; a venv is created for the build),
# CEDAR_PRIVATE_PATTERNS (optional file of extra regexes for the content scan).
# The wheel is tagged py3-none-manylinux_2_28_x86_64 and re-checked with
# toolchain/scripts/check_abi.py after packaging.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PREFIX="" ; IMAGE=""
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) PREFIX="$2"; shift 2 ;;
        --image) IMAGE="$2"; shift 2 ;;
        *) echo "unknown argument $1" >&2; exit 2 ;;
    esac
done
OUT_DIR="${OUT_DIR:-$HERE/dist}"
PYTHON="${PYTHON:-python3}"
PKG="$HERE/cedar_toolchain"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/cedar-wheel-XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
log() { printf '\n\033[1;36m[cedar-wheel]\033[0m %s\n' "$*"; }

if [ -z "$PREFIX" ]; then
    if [ -z "$IMAGE" ]; then
        IMAGE="cedar-toolchain-build:$(python3 "$REPO/toolchain/scripts/lock.py" get toolchain_version)"
        log "building container image $IMAGE (LLVM/CIRCT: tens of minutes)"
        DOCKER_BUILDKIT=1 docker build -f "$REPO/toolchain/build/Dockerfile" \
            --build-arg JOBS="${JOBS:-16}" --build-arg LINK_JOBS="${LINK_JOBS:-2}" -t "$IMAGE" "$REPO"
    fi
    log "extracting /cedar-out from $IMAGE"
    CID=$(docker create "$IMAGE")
    docker cp "$CID":/cedar-out/. "$STAGE/"
    docker rm "$CID" >/dev/null
    PREFIX="$STAGE"
fi

log "staging $PREFIX into the package"
for d in _bin _share _licenses; do find "$PKG/$d" -mindepth 1 ! -name .gitkeep -delete; done
rm -f "$PKG/cedar-toolchain-manifest.json"
for t in $(python3 "$REPO/toolchain/scripts/lock.py" get outputs.bin | tr -d '[]",'); do
    install -m 755 "$PREFIX/bin/$t" "$PKG/_bin/$t"
done
install -m 644 "$PREFIX"/share/cedar/*.egg "$PKG/_share/"
install -m 644 "$PREFIX"/licenses/* "$PKG/_licenses/"
install -m 644 "$PREFIX/cedar-toolchain-manifest.json" "$PKG/cedar-toolchain-manifest.json"
python3 "$REPO/toolchain/scripts/check_abi.py" "$PKG/_bin"

log "building wheel"
"$PYTHON" -m venv "$STAGE/venv"
"$STAGE/venv/bin/pip" install -q --upgrade pip build "wheel>=0.40"
rm -rf "$HERE/build" "$HERE"/*.egg-info
mkdir -p "$OUT_DIR"
"$STAGE/venv/bin/python" -m build --wheel --outdir "$STAGE/raw" "$HERE"
RAW=$(ls "$STAGE"/raw/cedar_toolchain-*.whl)
"$STAGE/venv/bin/python" -m wheel tags --remove --python-tag py3 --abi-tag none \
    --platform-tag manylinux_2_28_x86_64 "$RAW" >/dev/null
WHEEL=$(ls "$STAGE"/raw/cedar_toolchain-*manylinux_2_28_x86_64.whl)
cp "$WHEEL" "$OUT_DIR/"
rm -rf "$HERE/build" "$HERE"/*.egg-info
for d in _bin _share _licenses; do find "$PKG/$d" -mindepth 1 ! -name .gitkeep -delete; done
rm -f "$PKG/cedar-toolchain-manifest.json"

log "checking the packaged wheel"
python3 "$REPO/toolchain/scripts/check_wheel.py" ${CEDAR_PRIVATE_PATTERNS:+--private-patterns "$CEDAR_PRIVATE_PATTERNS"} "$OUT_DIR/$(basename "$WHEEL")"
ls -lh "$OUT_DIR/$(basename "$WHEEL")"
