# Architecture

```
            Claude Code  /  Codex                     (reasoning: rewrites, rules, choices)
                 |  skills/cedar*, task files, `cedar task submit`
                 v
  cedar CLI ---- cedar.workflow.engine (run.json state machine, task contracts)
                 |            |               |                 |
     cedar.config     cedar.rules       cedar.egraph       cedar.measure / cedar.verification
     (3 schemas,      (Z3 exact-width   (pipeline: CIRCT ->  (cedar.eda adapters: DC, Innovus,
      profiles)        certificates)     DialEgg/egglog ->    VCS, vcd2saif, PrimeTime, Conformal;
                                         extract_result ->    circt-lec + Z3)
                                         CIRCT; costmodel)
                 |
     cedar.objective / cedar.frontier / cedar.selection / cedar.attribution
                 |
     cedar.toolchain  ->  cedar-toolchain wheel or native prefix (explicit; never PATH)
```

## Packages

* **cedar-workflow** (`src/cedar`, pure Python; PyYAML, z3-solver): CLI,
  configuration, workflow, adapters, proofs, scoring. Package data: prompts,
  seed rules, the open example.
* **cedar-toolchain** (`toolchain/wheel`, manylinux_2_28 x86-64): `circt-verilog`,
  `circt-opt`, `circt-lec`, `egg-opt`, `egglog`, `extract_result`, the matching
  egglog templates, licenses and a manifest with source revisions and binary
  hashes. Runs record the toolchain identity (`manifest_match` checks the
  binaries against the manifest).

## Compiler flow (`cedar.egraph.pipeline`)

1. `circt-verilog --top=<top> <rtl...>` -> MLIR (`hw`/`comb`/`seq`), then
   `circt-opt --hw-flatten-modules`.
2. `egg-opt --eggify-only --egg-file=<program>`: MLIR -> egglog program built from
   `circt.egg` with certified rule instances and a bounded or saturating
   schedule; egglog runs and serializes the e-graph (`.ops.json`).
3. Root e-classes are populated in natural order; weighted costs are written
   into the JSON (`cedar.egraph.costmodel`).
4. `extract_result` (DAG-sharing greedy, with timeout fallback) -> S-expression.
5. `egg-opt --reconstruct-from-extraction`, Cedar's LLHD lowering passes,
   `circt-opt --lower-seq-to-sv --hw-memory-sim | --export-verilog` -> Verilog,
   which must re-parse with `circt-verilog`.

## Rule admission (`cedar.rules.certify`)

Rules are quarantined text. For each bit width present in the target design
the rule's single type variable is replaced by that concrete type, which both
*guards* the rule (it can only match e-nodes of that width) and fixes the proof
width. `cedar.rules.z3verify` (the paper's Z3 bit-vector verifier, used as a
library) proves `premises => lhs == rhs` at exactly that width. Additional
fail-closed checks: one rule/one union, no stateful operators, every variadic
operand vector read with `vec-get` must have an exact `vec-length` guard (the
verifier models a vector as exactly the elements read), every operand width
must be determined by the instance types, and widths above `max_width` (256)
are unsupported. Certificates are keyed by the SHA-256 of the rule text, the
width and a hash of the verifier code + Z3 version; any change forces a new
proof.

## EDA adapters (`cedar.eda`)

`ToolEnv` runs each named adapter through `bash -c` with the profile's setup
script, required environment-variable names, niceness and a process-group
timeout, capturing a log; results are `ok | failed | timeout | unavailable`.
`flow.py` generates DC, Innovus, VCS, vcd2saif, PrimeTime and Conformal scripts
from the technology profile and the shared SDC, parses reports into typed
`Metric`s (`cedar.metrics`), and checks completion markers. PrimeTime power is
accepted only if SAIF annotated at least 90% of nets.

## Agents

The engine never calls a model. Task prompts (`src/cedar/data/prompts`) carry
the objective, units, constraints, evidence and exact output paths. Hosts load
the same `skills/` through their plugin manifests (`.claude-plugin/`,
`.codex-plugin/`). Task submissions record the agent host and model; optional
per-stage model names from the design spec are recorded in provenance.
