# Building the toolchain and the Python distributions

All toolchain builds consume `toolchain/sources.lock.json`:

| Component | Source | Revision |
| --- | --- | --- |
| CIRCT | https://github.com/llvm/circt | `fa297ef296a906f5024c4be002951175f7ccdb3b` |
| LLVM/MLIR | https://github.com/llvm/llvm-project (CIRCT submodule) | `52dfcab327fe959074563603b6ebaaed314e9677` |
| slang (CIRCT FetchContent `v10.0`) | https://github.com/MikePopoloski/slang | `ace09c5d7c9f4e28eed654d2f353c6dc792ebf67` |
| fmt (slang FetchContent `12.1.0`) | https://github.com/fmtlib/fmt | `407c905e45ad75fc29bf0f9bb7c5c2fd3475976f` |
| egglog (egg-smol `cost-action` fork) | https://github.com/saulshanabrook/egg-smol | `b6e1c96ed7335366e90056ea0a24ef425dfbb8fb` |
| DialEgg (Cedar fork) | `third_party/dialegg` | imported from `cc8fdafd…` |
| extraction-gym (Cedar fork) | `third_party/extraction-gym` | imported from `47d50494…` |

The egglog fork matters: DialEgg's passes and Cedar's templates require it;
mainline egglog >= 0.4 is not compatible. No patches are applied to CIRCT,
LLVM or egglog. `python3 toolchain/scripts/lock.py check` validates the lockfile.

## Native build

Prerequisites: git, python >= 3.8, cmake >= 3.28, ninja >= 1.11, GCC >= 12 (or clang >= 16),
clang (for DialEgg), lld, python3, Rust (cargo; rustup recommended so egglog's
`rust-toolchain.toml` can select 1.74). Resources: ~45 GB disk for the build
tree, ~2 GB RAM per compile job and ~8 GB per concurrent LLVM link.

```bash
CEDAR_BUILD_ROOT=/scratch/cedar-build CEDAR_PREFIX=/opt/cedar-toolchain \
JOBS=16 LINK_JOBS=2 toolchain/build/build-native.sh
export CEDAR_TOOLCHAIN_PREFIX=/opt/cedar-toolchain
cedar doctor
```

Stages: `fetch llvm circt egglog dialegg gym install check` (pass stage names to
run a subset). `check` runs the manylinux ABI check and the toolchain smoke
test (RTL -> e-graph with a distributivity rule -> Verilog -> `circt-lec`).
The prefix contains `bin/`, `share/cedar/` (templates), `licenses/` and
`cedar-toolchain-manifest.json`.

Measured on a 144-core x86-64 host (container build, `JOBS=32 LINK_JOBS=4`):
fetch 67 s, LLVM/MLIR 868 s, CIRCT 210 s, egglog 43 s, DialEgg 25 s,
extraction-gym 13 s; total 23 min including image setup.

## Container build

```bash
docker build -f toolchain/build/Dockerfile --build-arg JOBS=16 --build-arg LINK_JOBS=2 \
    -t cedar-toolchain-build:0.1.0 .
```

The image is based on `quay.io/pypa/manylinux_2_28_x86_64` (AlmaLinux 8,
glibc 2.28) and runs the same `build-native.sh`. LLVM is built with zlib, zstd
and libxml2 disabled and binaries link libstdc++/libgcc statically, so the
outputs depend only on the manylinux allowlisted system libraries.

## Wheels

```bash
toolchain/wheel/build_wheel.sh                 # container build + package
toolchain/wheel/build_wheel.sh --image TAG     # package an existing build image
toolchain/wheel/build_wheel.sh --prefix DIR    # package a native install prefix
python -m build                                # cedar-workflow wheel + sdist
python toolchain/scripts/check_wheel.py dist/*.whl dist/*.tar.gz [--private-patterns FILE]
```

The toolchain wheel is tagged `py3-none-manylinux_2_28_x86_64`. `check_wheel.py`
verifies required binaries/templates/licenses, manifest hashes against the
packaged binaries and against the current lockfile, the WHEEL tag, the ABI
policy, and scans every file for private-data patterns; for the workflow
distributions it enforces an allowlist of paths. Packaging uses explicit
inclusion lists (`package-data`, `MANIFEST.in`), so local files can never be
picked up implicitly.

## Release source bundle

```bash
python3 toolchain/scripts/make_source_bundle.py --out /scratch/bundle
```

produces `cedar-src-<version>.tar.gz` with the committed Cedar tree, CIRCT with
its LLVM submodule, slang, fmt and egglog at the pinned revisions, and
`cargo vendor` crate sources for egglog and extraction-gym (with
`.cargo/config.toml` pointing at them), plus `BUNDLE.json` (lockfile, per-tree
hashes). Build from an unpacked bundle without access to any source host:

```bash
cd cedar-src-0.1.0
CEDAR_SOURCES_DIR=$PWD/sources CARGO_OFFLINE=1 CEDAR_BUILD_ROOT=/scratch/b \
    cedar/toolchain/build/build-native.sh
```

This guarantees *source availability*. A fully offline build additionally
requires the build tools themselves (compilers, cmake, ninja, lld, Rust), which
are not part of the bundle. Bundles are large (~2.9 GB unpacked) and are kept
outside Git.

## Updating the DialEgg / extraction-gym snapshots

See `third_party/*/UPSTREAM.md` and `toolchain/scripts/import_third_party.py`.
