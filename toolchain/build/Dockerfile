# SPDX-License-Identifier: Apache-2.0
#
# Containerized build of the Cedar toolchain on the manylinux_2_28 baseline
# (AlmaLinux 8, glibc 2.28). It runs the same toolchain/build/build-native.sh
# as a native build, with every revision taken from toolchain/sources.lock.json
# and DialEgg / extraction-gym taken from the checked-in third_party/ sources.
#
# Build from the repository root (the root .dockerignore limits the context):
#   docker build -f toolchain/build/Dockerfile -t cedar-toolchain-build .
# Output: /cedar-out/{bin,share/cedar,licenses,cedar-toolchain-manifest.json}
# toolchain/wheel/build_wheel.sh copies that tree into the wheel.

ARG BASE_IMAGE=quay.io/pypa/manylinux_2_28_x86_64
FROM ${BASE_IMAGE}

ARG JOBS=16
ARG LINK_JOBS=2
ARG RUST_DEFAULT=1.83.0

RUN dnf -y install git make patch which xz curl clang lld binutils \
    && dnf clean all

# Rust via rustup (egglog's rust-toolchain.toml selects 1.74.0 automatically).
ENV RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/cargo
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --default-toolchain ${RUST_DEFAULT} --profile minimal --no-modify-path \
    && /opt/cargo/bin/rustup toolchain install 1.74.0 --profile minimal

# cmake >= 3.28 and ninja >= 1.11 (the base image's packages are too old).
RUN /opt/python/cp311-cp311/bin/pip install --no-cache-dir cmake==3.31.2 ninja==1.11.1.1 \
    && ln -sf /opt/python/cp311-cp311/bin/cmake /usr/local/bin/cmake \
    && ln -sf /opt/python/cp311-cp311/bin/ninja /usr/local/bin/ninja \
    && ln -sf /opt/python/cp311-cp311/bin/python3 /usr/local/bin/python3

ENV PATH=/opt/cargo/bin:/usr/local/bin:${PATH} \
    CEDAR_BUILD_ROOT=/build CEDAR_PREFIX=/cedar-out
RUN gcc --version | head -1 && clang --version | head -1 && cmake --version | head -1 \
    && ninja --version && cargo --version && python3 --version

# Stage 1: pinned public sources + LLVM/MLIR + CIRCT + egglog. Only the lockfile
# and build scripts are copied first so third_party edits do not invalidate
# the long LLVM layer.
COPY toolchain/sources.lock.json /cedar/toolchain/sources.lock.json
COPY toolchain/scripts/lock.py /cedar/toolchain/scripts/lock.py
COPY toolchain/build/build-native.sh /cedar/toolchain/build/build-native.sh
RUN /cedar/toolchain/build/build-native.sh fetch
RUN JOBS=${JOBS} LINK_JOBS=${LINK_JOBS} /cedar/toolchain/build/build-native.sh llvm
RUN JOBS=${JOBS} LINK_JOBS=${LINK_JOBS} /cedar/toolchain/build/build-native.sh circt
RUN JOBS=${JOBS} /cedar/toolchain/build/build-native.sh egglog

# Stage 2: checked-in DialEgg and extraction-gym sources.
COPY third_party /cedar/third_party
RUN JOBS=${JOBS} /cedar/toolchain/build/build-native.sh dialegg gym

# Stage 3: templates, notices, install tree, ABI + smoke checks.
COPY toolchain/scripts /cedar/toolchain/scripts
COPY toolchain/templates /cedar/toolchain/templates
COPY LICENSE THIRD_PARTY_NOTICES.md /cedar/
RUN /cedar/toolchain/build/build-native.sh install check \
    && ls -lh /cedar-out/bin /cedar-out/share/cedar /cedar-out/licenses

CMD ["bash", "-c", "cp -r /cedar-out/. /out/ && echo 'toolchain staged in /out'"]
