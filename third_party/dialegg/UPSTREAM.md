# DialEgg (Cedar snapshot) — provenance

This directory is a source snapshot of the Cedar/CIRCT version of DialEgg. It is
built by Cedar's toolchain scripts into the `egg-opt` binary. No Git history
was imported.

## Original project

| Field | Value |
| --- | --- |
| Name | DialEgg — MLIR + equality saturation with egglog |
| URL | <https://github.com/AzizZayed/DialEgg> |
| Original author | Abd-El-Aziz Zayed (with Christophe Dubach, McGill University) |
| License | Apache License 2.0 — see [`LICENSE.txt`](LICENSE.txt) (unchanged) |
| Paper | A. Zayed and C. Dubach, *DialEgg: Dialect-Agnostic MLIR Optimizer using Equality Saturation with Egglog*, CGO 2025 |
| Last upstream commit contained in the snapshot | `6cddd7b9dfb4af61fe5fbd3aca9d9dc92d9dfcd9` ("Stablehlo benchmarks (#5)", 2025-07-17) |

## Snapshot revision

| Field | Value |
| --- | --- |
| Imported revision | `cc8fdafd07e0faae3545b48ca04b90a9a9c37256` (2026-06-18, "fix(eggify/reconstruct): register opaque-op region captures as users ...") |
| Commits after upstream merge-base | 80 |
| Uncommitted changes at import time | none; this revision was built into the `cedar-toolchain` 2.8.0 reference wheel |

## Cedar-specific changes contained in the snapshot

Relative to upstream `6cddd7b`, the snapshot adds CIRCT support used by Cedar:

* CIRCT dialect linkage in `CMakeLists.txt` (links every CIRCT dialect so
  `egg-opt` can parse `hw`/`comb`/`seq`/`llhd`/... MLIR).
* `--eggify-only` (`src/EggifyOnlyPass.cpp`) and
  `--reconstruct-from-extraction` (`src/ReconstructFromExtractionPass.cpp`):
  the decoupled MLIR → egglog → external extraction → MLIR flow
  (see [`DECOUPLED_EXTRACTION.md`](DECOUPLED_EXTRACTION.md)).
* Variadic-operand (`OpVec`) encoding, `let`-binding parsing, opaque-op region
  capture handling and other fixes in `src/Egglog.cpp`.
* Hierarchical emission (`--hierarchical`, module-level e-nodes).
* LLHD lowering passes used before Verilog export:
  `src/LowerLoopCarryProcessesPass.cpp`, `src/LowerFreeLLHDSignalsPass.cpp`,
  `src/LowerInterProcessLLHDSignalsPass.cpp`, with lit tests under `test/`.
* CIRCT round-trip tests and examples (`test/circt`, `test/replacement`,
  `test/anywidth_int`, `test/vec_of_rule`, `circt-test/`).

## Changes made while importing into Cedar

The import is reproducible with
`python3 toolchain/scripts/import_third_party.py dialegg <source-checkout> cc8fdafd07e0faae3545b48ca04b90a9a9c37256`.

Removed from the snapshot:

* `reports/` — development diaries (contained developer filesystem paths).
* `check_llvm_progress.sh`, `setup_dialegg.sh`, `run_dialegg.sh` — host-specific
  helper scripts with hard-coded developer paths; not used by the build.
* `test/circt/sm/` — test inputs that are not redistributable and are not part
  of any lit test or of the `egg-opt` build.
* `test/lower_loop_carry/comb_process_with_module_clock.mlir` — replaced by
  the synthetic mixed-process regression in Cedar's `tests/test_open_flow.py`.
* `test/reconstruct_opaque_region_capture/` — replaced by the synthetic
  opaque-region capture regression in Cedar's `tests/test_open_flow.py`.
* `test/circt/fir/` — the external FIR example, generated compiler outputs,
  logs and helper scripts. Replaced by an independent synthetic selector
  datapath regression in Cedar's `tests/test_open_flow.py`.

Functional changes to `egg-opt`:

* Hierarchical opacity is based on external declarations or explicit user
  configuration, rather than built-in library/design symbol patterns.
  `hw.module.extern` declarations now supply module-level interface nodes so
  instances can reference them. Internal modules can be kept opaque with
  `--opaque-module=<symbol>` or the unit attribute `cedar.blackbox`.
  Invalid opacity configuration fails with a diagnostic. These controls require
  `--hierarchical`; ordinary flat emission is unchanged.
  `--list-hw-modules` provides a JSON inventory, and `--opt-module=<symbol>`
  selects one internal body for matching emission/reconstruction, including
  private modules. Reconstruction cleanup stays within that selected body.
  The optional Cedar workflow uses these controls for independent-module
  optimization (`tests/test_hierarchical_flow.py`).
  The importer first discards the obsolete name policy, then applies `toolchain/patches/dialegg-opaque-modules.patch` to
  preserve these fixes across snapshot refreshes. Synthetic regressions live in
  `tests/test_hierarchical_opacity.py`.

Other edits:

* `CMakeLists.txt`: the `CIRCT_SOURCE_DIR` / `CIRCT_BUILD_DIR` cache defaults
  pointed to a developer machine; they are now empty and must be passed
  explicitly (Cedar's build scripts always pass them).
* Seven demo/test shell scripts (`circt-test/*.sh`, `test/*/run*.sh`):
  hard-coded `export PATH=<developer path>`
  lines replaced by `DIALEGG_TOOLS_PATH`.
* `test/vec_of_rule/{README,COST_TUNING_SUMMARY}.md`: developer path replaced by
  `<dialegg>/`.

Files carrying third-party notices (e.g. `test/anywidth_int/blake2s_G.v`,
BSD-2-Clause, Assured AB) are retained with their notices.

## External build dependencies

`egg-opt` is built against the CIRCT/LLVM/MLIR revision pinned in
[`toolchain/sources.lock.json`](../../toolchain/sources.lock.json); at run time
it invokes the pinned `egglog` (`saulshanabrook/egg-smol` revision) binary.

## Updating this snapshot

1. Check out the new source revision locally (read-only).
2. Re-run the import script above with the new revision; review the diff
   (`git diff third_party/dialegg`) for credentials, internal material, large
   generated files and absolute paths.
3. Update this file and `toolchain/sources.lock.json` (`dialegg.import_revision`).
4. Rebuild the toolchain and run `tests/` plus the toolchain smoke test.
