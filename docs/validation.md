# Release validation (v0.1.0)

Manually reviewed summary of the checks run for this release. Raw logs,
profiles and run directories are private to the test machine and are not
part of the repository.

## Environment

* Linux x86-64 (RHEL 8 family host); 144 cores shared with other users.
* Agents: Claude Code 2.1.319 (model `claude-sonnet-5-5`), Codex CLI 0.160.0
  (configured model `gpt-6.1-sol`).
* EDA: Synopsys Design Compiler V-2023.12-SP5, VCS V-2023.12-SP2-1,
  PrimeTime W-2024.09-SP5 (also provides `vcd2saif`); Cadence Innovus 23.11,
  Conformal LEC 24.1.
* Technology: ASAP7 7.5-track RVT cells, TT corner.

## Toolchain builds

| Check | Result |
| --- | --- |
| Container build (`toolchain/build/Dockerfile`, manylinux_2_28, JOBS=32), from scratch | pass, 23 min first build (LLVM 868 s, CIRCT 210 s); final rebuild 17 min (LLVM 657 s, CIRCT 196 s, egglog 38 s, DialEgg 22 s, extraction-gym 11 s) |
| Native build from the release source bundle, **without network access** (`unshare -rn`; JOBS=24, host GCC 13, cmake 4.1, cargo 1.87) | pass, 31 min under concurrent load |
| Release source bundle regenerated from the release commit (`make_source_bundle.py`) | dependency and vendored-crate tree hashes identical to the bundle used for the offline native build; Cedar snapshot = release commit |
| ABI policy (`check_abi.py`) on both builds | pass: only manylinux-allowlisted libraries, GLIBC <= 2.28, no RUNPATH |
| Toolchain smoke test (RTL -> e-graph -> Verilog -> circt-lec + Z3) on both builds | pass (equivalence proved) |
| `cedar-toolchain` wheel (`check_wheel.py`): files, licenses, manifest hashes vs. binaries and lockfile, platform tag, content scan | pass |
| `cedar-workflow` wheel and sdist: path allowlist, content scan | pass |

## Tests (`pytest`)

| Environment | Result |
| --- | --- |
| repository checkout (editable install, Python 3.13), container-built toolchain prefix | 114 passed |
| repository checkout, toolchain prefix built natively from the source bundle | passed (see build section) |
| fresh venv from the final wheels (Python 3.11, toolchain from the `cedar-toolchain` wheel), tests from the sdist | 113 passed, 1 skipped (example templates need the checkout) |
| fresh venv in the manylinux_2_28 container (AlmaLinux 8, glibc 2.28, Python 3.9), final wheels | 113 passed, 1 skipped |

Toolchain-dependent tests run the real binaries: the weighted cost model must
flip the real extractor's choice, the extractor must be deterministic on an
e-graph that exposed nondeterminism before, and circt-lec + Z3 must prove and
refute equivalence. Engine tests stub only the commercial EDA stages, or
replace the EDA tools by stand-ins that emit the real tools' message formats
(`tests/fake_eda.py`); process-cleanup tests spawn real child processes. The
compiler toolchain and its wheel were not rebuilt for the review fixes: no
toolchain input changed after the final toolchain build.

## Agent hosts

| Check | Result |
| --- | --- |
| Claude Code plugin validation (`claude plugin validate`) | pass |
| Claude Code: `--plugin-dir` and marketplace install (`claude plugin marketplace add` + `install`) | skills loaded / installed |
| Codex: marketplace install (`codex plugin marketplace add` + `codex plugin add`) | installed, skills used by `codex exec` |
| Guided setup (`cedar-setup`) in Claude Code: tool + technology profiles via `cedar setup write-profile`, design spec, `cedar doctor`, `cedar flow-check` | pass; flow-check (synthesis, P&R, activity, power, self-equivalence) 100 s |
| Guided setup in Codex: profiles via `write-profile`, design spec, `cedar doctor` | pass |

## Fast full-flow test (quick preset, `examples/dist_mac`, 1.0 ns clock)

Each run: baseline DC + Innovus + SDF gate-level activity + PrimeTime(SAIF),
agent direct rewrite, agent rule, Z3 admission, equality saturation, weighted
extraction, Conformal check of every candidate against the original RTL, full
PPA of every verified candidate, frontier report. Profiles and run
directories were outside the repository; profiles were created through the
guided setup.

**Release runs** (final package; fresh environment from the final wheels;
plugins loaded from a clean export of the release commit; the run budget,
measurement gates and process-group cleanup active):

| Host | Run wall clock | Agent session | Result |
| --- | --- | --- | --- |
| Claude Code (`--plugin-dir`) | 5 min 03 s | 5 min 23 s | completed, contract met, iteration successful; extracted candidate Conformal PASS: area -38.3%, power -30.3%, delay +2.6%, timing met; the agent's rule proved at 1/8/16 bits |
| Codex (installed plugin) | 5 min 34 s | 6 min 33 s | completed, contract met; identical extracted result; agent rule proved at 1/8/16 bits |
| Unattended (`cedar run start --agent-cmd "claude -p ..." --run-timeout-s 1500`) | 5 min 26 s | — | completed, contract met; both agent tasks accepted after a zero exit on the first attempt; agent rule proved at 1/8/16 bits |

Every stage of these runs recorded its gate evidence: DC/PrimeTime without
error messages, Innovus with only the profile-accepted `IMPTR-2101` (in
`power_route`), the post-route SPEF loaded with 100% of internal nets
annotated, SAIF 100% of nets, and a completed SDF annotation of `Top.DUT`.

Runs of the same flow after each review round, all completed with the
contract met (run wall clock Claude / Codex): 5 min 06 s / 5 min 37 s after
the vector-cardinality fix, 5 min 00 s / 5 min 37 s after the nested-operand
fix, 4 min 58 s / 5 min 56 s after the measurement gates, 5 min 18 s /
6 min 13 s after the run budget; in two of them an
agent's otherwise correct rule was rejected for `(let ?x ...)`, which led to
the prompt fix. Earlier release-candidate runs: 4 min 57 s / 5 min 19 s
(third review), 6 min 58 s / 5 min 59 s (second review), and a negative check
in which a deliberately wrong direct rewrite got Conformal exit 16, was
excluded and never measured.

All these runs measured the same baseline (893.7 um2, 0.661 ns, 229.6 uW) and
the same extracted candidate (551.5 um2, 0.678 ns, 160.1 uW). Phase times of
a Codex run: baseline ~100 s, direct-candidate verification + synthesis
preview ~40 s, e-graph iteration incl. verification and parallel full-PPA
measurement of both candidates ~130 s; agent tasks 33 s and 42 s. Before the
gate fixes the same flow measured 5 min 06 s (Claude) and 6 min 26 s (Codex);
one Claude run took 10 min 02 s while a 24-job native LLVM build and another
user's Innovus job shared the machine (P&R 240-259 s instead of ~76 s) — the
<= 10 min target assumes a reasonably idle host and available licenses.

**Earlier runs during development** (same flow, earlier package revisions):
Claude Code with an editable install (5 min 04 s), Codex with seed rules
disabled so that only the agent's own rule drove saturation (6 min 54 s;
extracted candidate Conformal PASS), Claude Code from wheels (completed; slow
session because the agent restarted after a tool timeout — this led to
`cedar run wait`), and a negative check where a deliberately wrong direct
rewrite failed Conformal (15 non-equivalent points), was excluded and never
measured.

Measured per-stage times for one candidate on this machine: DC 12-17 s,
Innovus 72-111 s, activity simulation 5-6 s, PrimeTime 9-11 s, Conformal
1-30 s (license checkout dominates the first call). The quick test meets the
initial <= 10 min target; most of the time is P&R.

## Loop regression (reduced paper preset, unattended `--agent-cmd "claude -p ..."`)

`paper` preset with `max_iterations: 4`, `min_iterations: 3`,
`min_cost_configs: 2`, `min_rule_combos: 2`, `direct_rounds: 2`, objective
`paper-power` (area 0.3, delay 0.2, power 0.5): completed in 24 min with the
execution contract met (4/4 verified iterations, 4 distinct numerical cost
configurations, 2 rule combinations), two direct-rewrite rounds, an agent
rule/cost-multiplier selection task per iteration, an agent stop decision,
attribution coverage of 98.8% area / 99.7% power / 98% critical-path delay,
and a four-point verified frontier (baseline, two direct rewrites, one
extracted candidate). The agent's proposed rule exposed a certifier bug
(vector element reads in rule actions were not counted toward the arity guard);
after the fix the rule is proved at 1 and 8 bits and is UNKNOWN (Z3 timeout,
not admitted) at 16 bits — see `tests/test_certify.py`.

## Independent code review

An independent review of the package before the final runs found, among
others, two certifier soundness holes (rewrite forms bypassing the vector-arity
guard; operand widths only fixed by the right-hand side) and several fail-open
paths (pending stop decisions skipped on resume, incomplete measurements
counted as accepted, defines/include dirs missing from verification). All were
fixed with regression tests before the final runs. Separately, run-to-run
differences in extraction were traced to nondeterministic hashing in
extraction-gym and fixed (see `third_party/extraction-gym/UPSTREAM.md`); the
toolchain was rebuilt from scratch afterwards (container build: LLVM 657 s,
CIRCT 196 s).

## Second review: admission and measurement gates

A second independent review reproduced four further gate failures, all fixed
with regression tests before the release runs above:

* a rule file could carry extra egglog commands or actions (a top-level
  `union` of two different constants, or a `set` action next to the proved
  union) that the certifier ignored; the whole file is now validated and only
  the canonical re-serialization of the single validated rule reaches egglog
  (regression executes the real egglog binary);
* an iteration whose candidate failed measurement still counted as successful
  for the execution contract; one predicate (verified and completely,
  comparably measured) now drives budgets, diversity, stopping and the report;
* resuming a run did not detect a changed user SDC or included header; runs now
  fingerprint all behaviour/constraint inputs and recompute the constraints id;
* a Conformal process failure with a PASS-shaped report (or a stale report from
  an earlier attempt) could be accepted; passes now require a successful
  process and fresh reports, and every EDA stage clears old outputs first.

## Third review: proof symbols, instance integrity, frontier gate

* Distinct egglog symbols could collide in the proof model (a user `?lhs` in a
  `rewrite` vs. the desugaring's internal `?lhs`; a bare `let` name vs. a
  `?`-variable of the same spelling; `?a` vs. `??a`), admitting false rules.
  Rules are now alpha-renamed to collision-free identifiers before both proof
  and execution; real-egglog regressions cover all three cases.
* Persisted rule-instance files were applied without re-checking their
  certificates; instances are now used only through the recorded admission
  inventory (exact certified hash, current verifier identity or re-proof from
  the recorded source, design width); edited, missing or unrecorded files stop
  the run.
* The published frontier did not use the candidate-validity predicate; changed
  RTL or incomplete/failed measurements can no longer appear on the frontier
  or influence direct-round convergence and agent feedback.

## Fourth review: vector cardinality and premise shapes

* Admission checked a vector's length guard against every index in the rule,
  but the proof model sized a matched operand vector from the indices its
  parser kept (one element when none were kept), and the parser silently
  dropped premises that were not written `(= ?var term)`. The earlier claim
  here that ignored premises "can refute a true rule (never admit a false
  one)" was wrong: two false rules were PROVED and admitted --
  `mul(1, ?v) -> add(0, ?v)` with `(vec-length ?v) = 0` (the empty vector
  was modeled as one operand), and the same rewrite with length 2 and a
  reversed read `(= (vec-get ?v 1) ?last)` (the read was dropped, so the
  vector was modeled as one operand).
* The proof now models every matched vector with exactly the length of its
  `(= N (vec-length ?v))` guard (including 0), never from the reads.
  Reversed equalities and guards are normalized in the canonical text that
  is both proved and executed; any other premise shape is rejected before
  proof. Each vector variable needs exactly one guard, every read must be an
  integer literal in `[0, N)`, and unmodeled shapes (`vec-push` onto a
  matched vector, `comb_concat` over a matched non-empty vector) fail closed.
* The certificate semantics version is now `vector-lengths-v3`, so stored
  certificates and recorded run instances from earlier verifiers are
  re-proved (or the run stops) instead of reused.
* Regressions (`tests/test_certify.py`) run both counterexamples and a
  length-3/two-read variant through real egglog (raw rule makes the false
  check pass; nothing admitted does), and prove and execute valid zero-length,
  two-element and reversed-premise distributivity rules.

## Fifth review: widths of nested data operands

* The operand-width check exempted every symbol nested beneath an `OpVec`
  argument or the first element of a `vec-of`/`vec-push`, so data operands
  inside vector literals were never width-checked. A false rule was PROVED at
  1 bit and admitted: `icmp ne(?a, ?b) : I1 -> 0 ^ a[0] ^ b[0]` with the
  extracts inside `vec-of` (false for 8-bit `a = 0, b = 2`); its certified
  instance equated unequal ground terms in real egglog.
* Variables are now classified by their egglog sort, inferred from the
  declared constructor signatures (circt.egg operations; the pinned base.egg
  types, attributes and attribute pairs; the builtin vector functions), never
  by nesting. Every `Op`-sorted operand, wherever it occurs, must get its width
  from the match premises alone, and the proof must model it at that width;
  operand vectors with elements need a premise-fixed element width. Unknown
  constructors, ambiguous sorts and unclassifiable variables are rejected.
* The certificate semantics version is now `operand-sorts-v4`; earlier
  certificates and recorded run instances are re-proved instead of reused.
* Regressions run the reviewer's rule and a premise-side variant (an
  unconstrained operand inside a matched vector literal) through real egglog
  (raw rule equates unequal terms; nothing admitted does); a valid rule with a
  premise-fixed operand nested in `vec-of` still proves and runs, and a test
  pins the declared sorts against the toolchain templates.

## Sixth review: measurement gates that failed open

Real-tool negative probes showed three stages accepting failed tool steps
because only the exit status and Cedar's own completion marker were checked:

* PrimeTime with a malformed or missing SPEF (`SPFP-100` / `PARA-001` in the
  background `read_parasitics` log, zero annotated nets) returned
  `power = 217.7 uW, measured`.
* DC with an SDC containing an invalid command (`CMD-005`) returned measured
  area, delay and slack.
* VCS with a malformed or missing SDF (`SDF annotation aborted`, reported only
  as a warning with zero errors) produced SAIF and an `ok` activity stage.

Every generated script now announces its steps and each stage is judged on the
tool's own messages and evidence (see `docs/configuration.md`, "Measurement
gates"): any DC/PrimeTime `Error:`/`Fatal:` message fails the stage; PrimeTime
must show the read_parasitics summary for the post-route SPEF with zero errors
and annotated nets, and >= 95% of internal pin-to-pin nets annotated; VCS must
show a completed, error-free SDF annotation of the post-route SDF on the
intended instance with annotated instances; Innovus fails on every error and on
skipped/failed SDC commands (`CTE-*`, which Innovus reports as warnings) unless
a technology explicitly accepts an ID for a non-critical step, and its netlist,
SPEF and SDF are content-checked. The baseline now also requires every stage
to be `ok`. The evidence is recorded with each stage result.

Rechecked with the real tools (copies of the reviewer probes, outputs outside
the reviewer's evidence): the SDC probe fails synthesis (`constraints: CMD-005`;
area/delay/slack unavailable); the malformed/missing SPEF probes fail power
(`SPFP-100` / `PARA-001`, power unavailable); the malformed/missing SDF probes
fail activity (`SDF annotation aborted` / `SDF Error`). Innovus with the same bad
SDC fails in `init` (`CTE-25`), and without an accepted list fails on ASAP7's
`IMPTR-2101` (`power_route`). A positive measurement with valid inputs is
complete with unchanged values (area 893.696 um2, power 229.6 uW, SPEF 683 nets
/ 100% internal coverage, SDF 599/607 instances, SAIF 100%); harmless warnings
(UITE-489, PTE-003, SDFCOM_UHICD, IMPLF-45, ...) remain accepted. The ASAP7
technology profiles used for validation accept `IMPTR-2101` (M10 pitch,
reported by `sroute`; M10 is never routed).

Regressions (`tests/test_measurement_gates.py`) run the stage adapters and
`measure()` against stand-in tools that emit the real message formats and exit
0: constraint/compile errors in DC and PrimeTime, malformed and missing SPEF,
aborted and missing SDF, Innovus errors in critical steps, skipped SDC commands,
missing SPEF output and unaccepted IDs all fail their stage and leave the
metrics unavailable; with real engine runs a failed baseline stage stops the
run, and a failed candidate stage keeps the candidate out of acceptance, the
frontier and successful-iteration credit. Disabling any one gate makes these
tests fail.

## Seventh review: overall run budget and process cleanup

* There was no overall execution budget: presets bounded individual steps,
  phases ran without a deadline and `cedar run wait --timeout` only stopped
  polling. Runs now carry a persisted wall-clock budget
  (`search.run_timeout_s`, preset default quick 3600 s / paper 172800 s,
  `--run-timeout-s`), checked before and after every phase and enforced inside
  every subprocess (timeouts are capped by the remaining budget). Expiry is
  recorded in `run.json`, ends the run as `failed`, survives `continue --retry`,
  refuses task submissions, never earns successful-iteration credit and marks
  a report written as the budget ran out as failed with an incomplete contract.
  End to end, `cedar run start --run-timeout-s 60` on the example design
  stopped the running tools during the baseline and failed with
  `run budget of 60 s expired during baseline`.
* The unattended adapter's exit status was ignored and whatever files existed
  were submitted. Outputs are now considered only after a zero exit within the
  time limit; stale outputs are deleted before each attempt; a timeout or
  nonzero exit is a failed attempt (two fail the run).
* `_shell` (compiled simulators) and the unattended adapter used
  `subprocess.run`, which on timeout killed only the immediate process: a
  backgrounded child survived. All external commands (EDA tools, simulators,
  the compiler toolchain, toolchain probes, agents) now run through
  `cedar.proc.run`: own process group, group-wide SIGTERM/SIGKILL with reaping
  on timeout, and leftover descendants terminated after a normal exit. With the
  reviewer's command (`bash -c 'sleep 30 & ...; wait'`, 1 s limit) the stage
  returns `timeout` in 1.5 s and the child is gone.
* Regressions (`tests/test_budget_and_processes.py`) spawn real children
  through `_shell`, `ToolEnv.run`, the agent runner and the budget cap, and run
  the engine with an expired budget across resume, a budget expiring during a
  measurement subprocess, a report written as the budget expires, and an
  unattended agent that writes valid files but exits nonzero or hangs.

* Host runs on the release package showed agents losing otherwise correct
  rules to `(let ?x ...)` (rejected since the third review's alpha-renaming,
  but not stated in the rule prompt). The prompt and skill now state that
  `let` names carry no `?`; the Codex rule from that run proves at 1/8/16
  bits once renamed.

## Eighth review: non-signless types

* The certifier and the verifier recognized only `(I<N>)`/`(Int N)` widths; any
  other concrete type fell back to a one-bit proof. `hw_constant 0 : SInt 8 ->
  hw_constant 2 : SInt 8` (and the same with `UInt 8`) was PROVED at width 1
  and admitted, and its instance equated the two constants in real egglog.
* Admission now rejects every term built with a Type constructor other than a
  signless integer (`(I1|I4|I8|I16|I32|I64)`, `(Int N)`, N >= 1) or a type
  variable -- signed, unsigned, floating, index, aggregate, opaque and computed
  (`type-of`) types, in data and attribute positions -- before translation, and
  literal terms must have the sort of their slot. Independently, the verifier
  raises on such a type instead of assuming a width (`UnsupportedType`), so the
  rule is not admitted even with the admission check bypassed. The e-graph width
  scan and the cost model read `SInt`/`UInt` widths, and the analysis records
  any non-signless types of a design (no certified rule can match them). The
  certificate semantics version is now `signless-types-v5`.
* The reviewer's command now exits 1 with both rules REJECTED. Regressions run
  both false rules through real egglog (raw rule equates the constants; nothing
  admitted does), reject all 18 non-signless Type constructors and `(Int 0)` in
  data and attribute positions plus `type-of`, check the verifier alone, and
  keep valid signless rules (including `(Int 12)`) proving and running. The seed
  rules and the final host runs' agent rules still prove.

## Ninth review: in-process deadlines and tool identity

* The run budget bounded subprocesses but not in-process Z3 work: a proof
  continued for its full solver allowance (2.52 s with 0.1 s of budget left in
  the reviewer's probe; up to 60 s per instance in production) and admission
  went on to the next instance. Every rule proof and the circt-lec miter solve
  now get `min(solver allowance, remaining budget)` as their Z3 timeout
  (`proc.bounded_ms`: never 0, which Z3 treats as unlimited; it raises instead),
  admission and re-certification check the budget before every instance
  (including cached certificates), and a proof or solve that ends after the
  budget raises `DeadlineExpired` -- no certificate, no verdict -- so the run
  expires (sticky failed). The reviewer's probe now stops after 0.10 s.
  Regressions use real Z3 (a proof that needs over a minute is cut at the
  deadline; nothing starts after expiry; a run expiring during rule admission
  fails within seconds) and a real solver whose solve outlasts the budget for
  the miter.
* End-to-end validation compared only `command -v` paths, so a tool replaced at
  the same path stayed `validated`. Validation records and run provenance now
  store each tool's resolved path, real path, content hash and an
  adapter-specific version probe (bounded, inside the profile environment),
  plus the environment identity (setup-script hash, `PATH` after setup, hashes
  of the required variables). `cedar doctor` reports `stale` with the list of
  changes; records without an identity are stale; a run refuses to resume when a
  tool's path, content or environment changed. Regressions cover replacement at
  the same path, a wrapper whose target changes (detected by version), an
  edited setup script, a changed environment behind an unchanged setup script,
  a hanging version probe and a run resumed after a tool replacement.

## Known limitations

* Linux x86-64 only; macOS, ARM and Windows are untested.
* Per-e-node features are estimates; attribution splits shared cells equally
  among the operator tokens of an RTL line (see `docs/cost-model.md`).
* Rules that relate several bit widths are rejected by the certifier in v0.1.
* Premises must be `(= ?var term)` (either orientation) or an exact length
  guard `(= N (vec-length ?v))`. Premises equating two non-variable terms, e.g.
  `(= (vec-get ?a 0) (vec-get ?b 0))`, are rejected rather than used; bind a
  shared variable instead (`(= ?z (vec-get ?a 0)) (= ?z (vec-get ?b 0))`).
  Variable operand vectors must have one exact length guard; `comb_concat`
  over a matched non-empty vector is unsupported.
* `circt-lec` verification supports combinational designs only.
* The hierarchical design accumulator driver from the paper's experiments is not part of
  this release.
