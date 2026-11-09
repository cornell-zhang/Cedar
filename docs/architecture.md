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

The separate low-level hierarchical exporter supports explicit opaque module
boundaries; see [hierarchical opacity](hierarchical-opacity.md) for its CLI
options and MLIR annotation.

## Rule admission (`cedar.rules.certify`)

Rules are quarantined text. First the *entire executable content* is
validated: exactly one top-level `rule` (premises only `(= ?var term)`, in
either orientation, or an exact guard `(= N (vec-length ?v))`; actions only
`let` bindings and exactly one `union`; keywords `:ruleset rules` and `:name`)
or one `rewrite`; any other top-level command, action (`set`, `delete`,
`panic`, `extract`, a second `union`, ...), keyword (`:when`, `:subsume`) or
nested command is rejected, and so is every type other than a signless
integer (`(I1|I4|I8|I16|I32|I64)`, `(Int N)` or a type variable) -- signed,
unsigned, floating, index, aggregate, opaque and computed (`type-of`) types --
in data and attribute positions alike: the proof model is exact only for
signless bit vectors and never assumes a width for another type (the verifier
also refuses such a type independently). Reversed equalities and guards are normalized
to that orientation. The validated rule is then alpha-renamed: every
pattern variable becomes a fresh `?cvN` and every `let` name a fresh
`cedar_let_N`, so distinct egglog symbols can never be merged by the proof
model (which desugars rewrites with an internal `?lhs`, treats bare `let` names
as variables and strips `?` for Z3 names). Only the canonical serialization of
this renamed tree is proved and written into e-graph programs, so comments,
trailing text or name tricks cannot smuggle behaviour past the proof. For each bit width present in the target design
the rule's single type variable is replaced by that concrete type, which both
*guards* the rule (it can only match e-nodes of that width) and fixes the proof
width. `cedar.rules.z3verify` (the paper's Z3 bit-vector verifier, used as a
library) proves `premises => lhs == rhs` at exactly that width. Additional
fail-closed checks: one rule/one union, no stateful operators, every matched
operand vector must have exactly one `(= N (vec-length ?v))` guard and the
verifier models it as exactly N elements (N = 0 included; never sized from the
indices read), every `vec-get` index must be a literal in `[0, N)`, vector
shapes the model does not represent (`vec-push` onto a matched vector,
`comb_concat` over a matched non-empty vector) are unsupported. Every variable
is classified by its egglog sort, inferred from the declared constructor
signatures (never from where it is nested), and every `Op`-sorted data
operand -- including operands inside `vec-of`/`vec-push` literals, attribute
or type arguments and the right-hand side -- must get its width from the match
premises alone, with the proof using that same width; unknown constructors
and unclassifiable variables are rejected. Widths above `max_width` (256)
are unsupported, and so is any width above the proof model's limit (2^20)
whatever `max_width` a caller passes: the verifier models every width exactly
or refuses (it never clamps to a smaller width), and cached certificates are
not consulted beyond these limits. Certificates are keyed by the SHA-256 of the rule text, the
width and a hash of the verifier code, the admission-semantics version and
the Z3 version; any change forces a new proof. Within a run, instances are
applied only through the recorded admission inventory: each instance file must
hash to its certified text, carry a certificate from the current verifier
(otherwise it is re-proved from the recorded rule source) and apply to a width
of the design; edited, missing or unrecorded instance files stop the run.

## EDA adapters (`cedar.eda`)

`ToolEnv` runs each named adapter through `bash -c` with the profile's setup
script, required environment-variable names, niceness and a timeout, capturing
a log; results are `ok | failed | timeout | unavailable`. All external commands
(EDA tools, compiled simulators, the compiler toolchain, unattended agents) go
through `cedar.proc.run`: own process group, timeout capped by the run's
overall budget, the whole group terminated and reaped on timeout and any
leftover descendants terminated after a normal exit.
`flow.py` generates DC, Innovus, VCS, vcd2saif, PrimeTime and Conformal scripts
from the technology profile and the shared SDC, parses reports into typed
`Metric`s (`cedar.metrics`). Completion markers are necessary but never
sufficient: each generated script announces its steps, and a stage is `ok` only
if the tool reported no error in any step (Innovus: except IDs the technology
profile accepts for non-critical steps), its outputs exist with the expected
content, and its annotation evidence holds -- SPEF loaded with annotated nets
and >= 95% internal-net coverage, SAIF >= 90% of nets, a completed SDF
annotation of the intended instance. That evidence is kept in
`StageResult.evidence`. See `docs/configuration.md` ("Measurement gates").

## Agents

The engine never calls a model. Task prompts (`src/cedar/data/prompts`) carry
the objective, units, constraints, evidence and exact output paths. Hosts load
the same `skills/` through their plugin manifests (`.claude-plugin/`,
`.codex-plugin/`). Task submissions record the agent host and model; optional
per-stage model names from the design spec are recorded in provenance.
