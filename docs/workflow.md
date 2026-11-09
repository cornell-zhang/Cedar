# Workflow

A Cedar run is a resumable state machine stored in `<work-dir>/<run-id>/run.json`.
Python performs every deterministic step; the active agent (Claude Code or
Codex) performs only *tasks*. Start with

```bash
cedar run start --design design.yaml --work-dir <dir> [--preset quick|paper] \
    [--profile P] [--technology T] --agent <claude|codex> --model <model>
```

The command stops with exit code 3 whenever a task is pending and prints the
task file. After writing the outputs the agent runs
`cedar task submit --run <run> <task-id> --agent ... --model ...` (outputs are
validated: Verilog must parse and keep the original port list, rule files must
contain exactly one rule, JSON must parse) and `cedar run continue --run <run>`.
`cedar run status --run <run>` shows the phase, pending task, candidates and
timings; `cedar run wait --run <run>` blocks until a task is pending (exit 3),
the run finished (0) or failed (1). A run reads the original RTL files; at creation it fingerprints every input
that can change behaviour or constraints (RTL and every transitively
`` `include``d header, user SDC, activity and checking testbenches and their
includes, Tcl hooks, the tool setup script; size+mtime of library/LEF/RC files)
and resuming refuses if any of them, the design/tool/technology specs, or the
recomputed constraints id changed. A run directory is locked while a process
advances it. An optional unattended adapter (`--agent-cmd "claude -p ..."` or
`"codex exec ..."`) executes tasks in batch mode instead; its output files are
considered only if the command exits 0 within its time limit (stale outputs of
an earlier attempt are deleted first). A timeout or nonzero exit counts as a
failed attempt; two failed attempts fail the run.

### Run budget

Every run has an overall wall-clock budget, `search.run_timeout_s` (preset
default: quick 3600 s, paper 172800 s; `cedar run start --run-timeout-s N`
overrides it). It is stored in `run.json` at creation and counts from then,
across every `cedar run continue` and including the agent's work. Every
subprocess Cedar starts (EDA tools, simulators, the compiler toolchain,
unattended agents) runs in its own process group with its timeout capped by
the remaining budget; on a timeout the whole group is terminated and reaped,
and after a normal exit any leftover descendants are terminated too. In-process
Z3 work -- rule admission, re-certification and the `circt-lec` miter solve --
gets at most the remaining budget as its solver timeout (never 0, which Z3 would
treat as unlimited), checks the budget before every instance, and a proof cut
short by the budget yields no certificate or verdict. `cedar run continue` installs
the persisted budget before anything else: an expired run fails without
starting any process, and the resume checks (input fingerprints and tool
identity probes, which run the setup script and the tools' version queries) are
capped by the remaining budget. `cedar task submit` does the same: output
validation (the frontend parses submitted Verilog) runs under the persisted
deadline, outputs are accepted only if the budget has not expired by the end
of validation, and a validation that runs into the budget fails the run. When the
budget expires the run ends as `failed` with `expired` recorded: it cannot be
continued (not even with `--retry`), task submissions are refused, nothing done
under the expired budget earns successful-iteration credit, and a report
written as the budget ran out is marked `failed` with an incomplete contract.
`cedar run wait --timeout` only bounds the waiting command itself.

## Phases

| Phase | What happens | Gate / output |
| --- | --- | --- |
| preflight | resolve toolchain; check every stage required by the measurement mode and the verification backend; record the original port list | `missing_capability` stops the run |
| baseline | measure the original RTL (DC -> Innovus -> SDF gate-level activity -> SAIF -> PrimeTime) | incomplete baseline fails the run; a missed clock is `infeasible_timing_goal` (never relaxed) |
| analysis | eggify the design without rules: widths, operator histogram, cost-model scales | |
| direct_task | **agent**: direct RTL rewrites for the stated objective (ports and latency unchanged); later rounds receive every earlier variant's verification result, measured PPA and objective score | task outputs validated |
| direct_eval | verify each variant against the original (fail-closed) and get post-synthesis deltas; if more rounds are allowed, measure the round's verified variants and continue until `direct_min_rounds` are done and a round no longer improves the best weighted objective score | failed variants excluded |
| rules_task | **agent**: egglog rules generalizing the verified rewrites; the prompt contains the original RTL, verified diffs, PPA deltas and verification evidence | |
| rules_admit | quarantine -> parse -> specialize per design width -> Z3 proof -> certificate | only `PROVED` instances are usable |
| probe | each certified instance alone on the design: egglog match count and new e-nodes | non-firing rules are not selected |
| iterate | select rules + cost configuration -> equality saturation -> weighted extraction -> CIRCT reconstruction (agent fallback task on failure) -> re-parse -> verification -> full-PPA measurement (in parallel with pending direct candidates) -> frontier update -> history record | |
| attribution | netlist-to-e-node attribution of each measured extracted candidate | coverage + calibration used by later iterations |
| report | `report.json`: frontier, all candidates, exclusions, rule admission, execution contract, timings, toolchain identity | |

Candidate correctness is always checked against the **original** RTL with the
configured backend (`conformal`, `simulation` with a self-checking testbench,
or `circt-lec` for combinational designs). `failed` excludes the candidate;
`unavailable`/`error` stops the run. Activity testbenches are never accepted as
correctness evidence.

## Presets and the execution contract

| Setting | quick (smoke) | paper |
| --- | --- | --- |
| direct variants per round | 1 | 3 |
| direct rounds (min / max) | 1 / 1 | 3 / 5 (objective-based convergence) |
| rules requested | 1 | 2 |
| iterations (min / max) | 1 / 1 | 10 / 60 |
| distinct numerical cost configurations (min) | 1 | 3 |
| distinct rule combinations (min) | 1 | 3 |
| egglog schedule | 4 iterations | saturate (timeout 600 s) |
| rule/cost selection | deterministic (history ranking + complementary coverage) | agent task each iteration |
| stop decision | budget | agent task after `stall_window` = 5 unchanged frontiers |

`quick` keeps every correctness gate and every measurement stage; it only
bounds the search. An iteration counts as *successful* (for budgets, diversity,
stagnation and the contract) only if its candidate passed verification **and**
was completely and comparably measured (all stages ok, every required metric
valid, same constraints/measurement mode/stages as the baseline). The same
predicate gates frontier insertion and all search feedback (direct-round
convergence, agent prompts): a candidate whose RTL changed after verification
or whose measurement is incomplete never appears on the published frontier. A run whose
iterations all failed measurement ends `incomplete`, with per-iteration reasons
in `report.json` (`iteration_outcomes`). Every EDA stage deletes the outputs of
earlier attempts before it runs, and an equivalence result is `passed` only if
the tool process succeeded and wrote fresh evidence. The report's `execution_contract` states whether the
preset's minimums were met (`met`) or not (`incomplete`, with reasons); a run
that ends at `max_iterations` without meeting them is reported as incomplete.

## Outputs (`<run>/`)

| Path | Contents |
| --- | --- |
| `run.json` | full state: provenance (design/tool/technology snapshots with hashes, toolchain identity, agent host/model, stage models), tasks, candidates, rules, iterations, timings, log |
| `report.json` | final summary |
| `config-snapshot/` | non-secret configuration snapshots |
| `inputs/` | copy of the original RTL, original MLIR |
| `tasks/` | task prompts (and agent logs in unattended mode) |
| `candidates/direct`, `candidates/extracted` | candidate RTL |
| `rules/quarantine`, `rules/instances` | proposed rules, certified width instances |
| `egraph/` | e-graphs, egglog logs, extraction terms, reconstructed MLIR |
| `verify/`, `measure/` | equivalence and EDA work directories with logs and reports |

Certificates, the cross-design rule history and validation records live in
`$CEDAR_STATE_HOME`; user rules placed in `$CEDAR_STATE_HOME/rule-pool/*.egg` are
added to the pool (and certified like every other rule).

## Special cases from the paper's experiments

* **Hierarchical designs.** The default flow flattens private submodules
  (`--hw-flatten-modules`) before eggification. The toolchain also ships the
  hierarchical `Module` datatype (`module.egg`, `egg-opt --hierarchical`) used by
  the paper's design accumulator study, whose dedicated driver ran DC-only leaf
  evaluation with whole-design power measured separately. That driver is not part
  of this release; DC-only evaluation corresponds to
  `measurement.mode: synthesis-only` with an area/delay objective.
* **Multiple clocks / non-`clk` clock ports** are expressed through the design
  spec (`clocks[].port`, user `sdc`); the generated SDC binds the declared port.
* **Frequency search (shmoo)** from the paper's setup is intentionally not part
  of the release: the timing goal is an explicit user input.
