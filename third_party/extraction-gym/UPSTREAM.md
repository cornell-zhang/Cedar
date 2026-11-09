# extraction-gym (Cedar snapshot) — provenance

This directory is a source snapshot of the Cedar fork of extraction-gym. Cedar
builds only the `extract_result` binary from it. No Git history was imported.

## Original project

| Field | Value |
| --- | --- |
| Name | extraction-gym — benchmarks and algorithms for e-graph extraction |
| URL | <https://github.com/egraphs-good/extraction-gym> |
| License | MIT — `Copyright 2019 Max Willsey`, see [`LICENSE`](LICENSE) (unchanged) |
| Last upstream commit contained in the fork | `ca39f566452f4e5fe3d30c57354675f73f9e56a7` (2024-11-08, "Chidlren of enodes should be other enodes, not eclasses. Fixed #44 (#45)") |

## Immediate source (fork) and import revision

| Field | Value |
| --- | --- |
| Fork | `zzzDavid/extraction-gym` (public GitHub fork of the original) |
| Imported revision | `47d50494391a19b10d3668cf1fe2791f2fc11894` (2026-03-09, "Set default extraction timeout to 120s with faster-bottom-up fallback") |
| Commits in fork after upstream merge-base | 8 |
| Uncommitted changes in the fork at import time | none in source files (the only local checkout differed only by deleted `data/` benchmark files); this revision is the one built into the `cedar-toolchain` 2.8.0 reference wheel |

## Cedar-specific changes contained in the fork revision

* `src/extract_result.rs` (new `extract_result` binary, declared in
  `Cargo.toml`): reads a serialized e-graph JSON with `root_eclasses` and
  per-node `cost`, runs the `faster-greedy-dag` extractor (DAG-sharing aware)
  with a timeout and `faster-bottom-up` fallback, and prints the selected term
  as an S-expression with `(let __sN ...)` bindings for shared sub-terms. This
  is the format consumed by DialEgg's `--reconstruct-from-extraction`.
* `src/extract_result.rs.backup`: an earlier version of the same file, kept as
  in the fork; it is not compiled.

Cedar consumes **scalar per-node costs** from the JSON `cost` field. Cedar's
Python layer computes those costs from the weighted area/delay/power node
feature model (see `docs/cost-model.md`); the extractor itself is unchanged.

## Changes made while importing into Cedar

Reproducible with
`python3 toolchain/scripts/import_third_party.py extraction-gym <fork-checkout> 47d50494391a19b10d3668cf1fe2791f2fc11894`.

* Removed `data/` (≈246 MB of upstream benchmark e-graphs); it is not needed to
  build or run `extract_result` and remains available from the upstream
  repository. `test_data/` is retained.
* No source files were edited.

## Build dependencies

Rust crates are pinned by `Cargo.lock`. The `egraph-serialize` dependency is a
Git dependency on <https://github.com/egraphs-good/egraph-serialize> at
revision `951b829a434f4008c7b45ba4ac0da1037d2da90` (public). The release source
bundle (`toolchain/scripts/make_source_bundle.py`) vendors all crates.

## Updating this snapshot

Same procedure as `third_party/dialegg/UPSTREAM.md`, using the
`extraction-gym` component name and `extraction_gym.import_revision` in the
lockfile.
