---
name: cedar
description: Optimize an RTL design's area/delay/power with Cedar (direct rewrites, Z3-proved egglog rules, equality saturation, weighted extraction, equivalence checking, measured PPA frontier). Use when the user asks to optimize, explore or improve PPA of a Verilog/SystemVerilog design with Cedar.
---

# Cedar optimization workflow (agent-driven)

You are the reasoning agent of a Cedar run. The `cedar` Python CLI does all
compilation, proofs, verification, measurement and bookkeeping; you only do the
creative tasks it hands you. Never edit run state (`run.json`), never fabricate
results, and never skip a gate.

## 0. Preconditions
- `cedar --version` must work (install: `pip install cedar-workflow cedar-toolchain`,
  or an editable checkout). If not, stop and tell the user.
- A design spec (`cedar/design@1` YAML) with an explicit timing goal is required.
  If the user has none, use the `cedar-setup` skill (`cedar design init ...`). Never
  choose the clock period yourself and never run a frequency sweep — ask the user.
- Run `cedar doctor --profile <tools> --technology <tech> --design <design.yaml>`.
  Every stage required by the measurement mode must be `available`; otherwise stop
  and report what is missing (do not downgrade the measurement silently).
- Outputs must go to a work directory OUTSIDE the source repository (ask the user
  or use their configured scratch directory).

## 1. Start
```
cedar run start --design <design.yaml> --work-dir <dir> \
    [--profile <tools>] [--technology <tech>] [--preset quick|paper] \
    --agent <claude|codex> --model <your model id>
```
The command runs deterministic phases and stops with exit code 3 when a task needs
you, printing the task file. `cedar run status --run <run>` shows progress.

Long-running commands: `cedar run start` and `cedar run continue` run EDA tools and can
take several minutes (the baseline alone is a full synthesis + P&R + power run). If
your shell tool has a short timeout, pass `--run-id <id>` so you know the run directory
(`<work-dir>/<id>`), run the command in the background with output to a log file, then
call `cedar run wait --run <run>`: it returns when a task is pending (exit 3), the run
finished (0) or failed (1), or after `--timeout` seconds (exit 4; just call it again).
`--timeout` only limits the waiting; the run itself is bounded by its budget
(`search.run_timeout_s`, or `--run-timeout-s` on `cedar run start`). An expired run
fails and cannot be continued; tell the user and suggest a new run with a larger budget.
Do not poll with `pgrep -f` (it matches your own shell command). Never start a second
run because a command was interrupted: resume with `cedar run continue --run <run>`
(a run directory is locked while it is advancing).

## 2. Task loop
Repeat until the run is `completed` or `incomplete` (or fails):
1. Read the task file printed by the CLI (`cedar task show --run <run>` reprints it).
2. Produce exactly the requested output files at the given absolute paths.
3. `cedar task submit --run <run> <task-id> --agent <claude|codex> --model <model>`.
   If REJECTED, read the reason, fix the outputs, and submit again.
4. `cedar run continue --run <run>`.

Task kinds and what matters:
- **direct_rewrite** — rewrite RTL to improve the stated weighted objective. Keep the
  module name and every port identical, keep cycle behaviour (same registers, reset
  values, latency; no pipelining/retiming unless the spec allows it). Explain each
  variant in `notes.json`. Candidates are checked against the original RTL
  (Conformal / self-checking simulation / circt-lec) and failing ones are discarded.
- **propose_rules** — write general egglog rules (one `rule` with one `union` per
  file) generalizing the verified direct rewrites. Use a single type variable for
  the width; give every matched operand vector exactly one guard
  `(= N (vec-length ?ops))` with N its exact operand count (0 allowed) and read
  only literal indices below N; write every premise as `(= ?var term)` or a
  length guard (to equate two terms, bind both to one shared variable; other
  premise shapes are rejected); every operand's width must follow from the
  premises (an operand seen only through `comb_extract`/`comb_icmp` or the
  right-hand side has no fixed width and is rejected); only `let` (names without
  `?`, e.g. `(let s ...)`; `(let ?s ...)` is rejected) and one
  `union` actions; no stateful ops. Cedar proves each rule with Z3 at
  every exact width it is applied to and rejects anything unproved.
- **select_rules** (paper preset) — choose a complementary rule combination and
  optional per-constructor cost multipliers in [0.05, 20] as JSON.
- **extraction_fallback** — reconstruct Verilog from the extracted e-graph term when
  CIRCT reconstruction failed; it is verified like any other candidate.
- **stop_decision** (paper preset) — decide STOP/CONTINUE from the frontier history.

## 3. Report
Read `<run>/report.json` and tell the user, precisely:
- the timing goal, objective weights, measurement stages and units;
- the verified frontier (baseline/direct/extracted points with area um2, delay ns,
  power uW) and which candidates were excluded and why;
- rule admission results (which agent rules were proved/rejected);
- the execution-contract status (`quick` is a bounded smoke mode, not the paper's
  exploration setting) and per-phase timings;
- anything unavailable or failed. Do not claim improvements that the report does
  not show.
