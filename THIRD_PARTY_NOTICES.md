# Third-party notices

Cedar is licensed under the Apache License 2.0 (see [`LICENSE`](LICENSE)).
Cedar's Python workflow, verification utilities, EDA harness scripts and egglog
templates are provided under Apache-2.0. Their SPDX license identifiers are retained.

The source repository and the `cedar-toolchain` binary distribution include or
are built from the following third-party components. Exact revisions are in
[`toolchain/sources.lock.json`](toolchain/sources.lock.json); binary
distributions carry each license file under `licenses/`.

| Component | Use | License | Source |
| --- | --- | --- | --- |
| DialEgg (Cedar fork) | `egg-opt` (source in `third_party/dialegg`) | Apache-2.0 | <https://github.com/AzizZayed/DialEgg>; provenance in `third_party/dialegg/UPSTREAM.md` |
| extraction-gym (Cedar fork) | `extract_result` (source in `third_party/extraction-gym`) | MIT, Copyright 2019 Max Willsey | <https://github.com/egraphs-good/extraction-gym>; provenance in `third_party/extraction-gym/UPSTREAM.md` |
| egglog (egg-smol fork, `cost-action`) | `egglog` binary; linked by nothing else | MIT, Copyright (c) 2022 Max Willsey | <https://github.com/saulshanabrook/egg-smol> |
| CIRCT | `circt-verilog`, `circt-opt`, `circt-lec`; libraries linked into `egg-opt` | Apache-2.0 WITH LLVM-exception | <https://github.com/llvm/circt> |
| LLVM / MLIR | linked into all C++ tools | Apache-2.0 WITH LLVM-exception | <https://github.com/llvm/llvm-project> |
| slang | SystemVerilog frontend linked into `circt-verilog` | MIT | <https://github.com/MikePopoloski/slang> |
| fmt | linked via slang | MIT | <https://github.com/fmtlib/fmt> |
| Rust crates | linked into `egglog` and `extract_result` | various permissive (see each `Cargo.lock`; the source bundle includes their license files) | crates.io, <https://github.com/egraphs-good/egraph-serialize> |

Test inputs retained inside `third_party/dialegg/test` keep their own notices
(for example `test/anywidth_int/blake2s_G.v`, BSD-2-Clause, Assured AB).

Cedar does **not** include or redistribute commercial EDA tools (Synopsys
Design Compiler, VCS, PrimeTime; Cadence Innovus, Conformal) or proprietary
technology libraries. Users supply them through private tool and technology
profiles.
