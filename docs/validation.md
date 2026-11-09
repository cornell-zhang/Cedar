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
| Container build (`toolchain/build/Dockerfile`, manylinux_2_28, `--no-cache`, JOBS=32) | pass, 23 min (LLVM 868 s, CIRCT 210 s, egglog 43 s, DialEgg 25 s, extraction-gym 13 s) |
| Native build from the release source bundle, **without network access** (JOBS=24, host GCC 13, cmake 4.1, cargo 1.87) | pass, 31 min under concurrent load |
| ABI policy (`check_abi.py`) on both builds | pass: only manylinux-allowlisted libraries, GLIBC <= 2.28, no RUNPATH |
| Toolchain smoke test (RTL -> e-graph -> Verilog -> circt-lec + Z3) on both builds | pass (equivalence proved) |
| `cedar-toolchain` wheel (`check_wheel.py`): files, licenses, manifest hashes vs. binaries and lockfile, platform tag, content scan | pass |
| `cedar-workflow` wheel and sdist: path allowlist, content scan | pass |

## Tests (`pytest`, 44 tests)

| Environment | Result |
| --- | --- |
| editable checkout, Python 3.13, container-built prefix | pass |
| editable checkout, native-built prefix | pass |
| fresh venv from the wheels (Python 3.11, toolchain from the `cedar-toolchain` wheel), tests from the sdist | pass |
| fresh venv in the manylinux_2_28 container (AlmaLinux 8, glibc 2.28, Python 3.9) | pass |

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
PPA of every verified candidate, frontier report.

| Run | Wall clock | Result |
| --- | --- | --- |
| Claude Code, editable install, `--plugin-dir` | 5 min 04 s (agent session 5 min 20 s) | completed; extracted candidate Conformal PASS: area -38.3%, power -30.3%, delay +2.6%, timing met; agent rule proved at 1/8/16 bits (did not match this design; seed rules applied) |
| Codex, installed plugin, seed rules disabled | 6 min 54 s (session 7 min 40 s) | completed; the agent's own rule proved at 1/8/16 bits and drove saturation; extracted candidate Conformal PASS: area -37.5%, power -22.5%, delay +24.5% |
| Claude Code, fresh wheel environment, clean plugin export | completed; contract met (session time inflated by an agent restart after a tool timeout and concurrent load; not a timing reference) |
| Negative check: deliberately wrong direct rewrite | Conformal FAIL (15 non-equivalent points); candidate excluded and never measured; run completed |

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

Measured per-stage times for one candidate on this machine: DC 12-17 s,
Innovus 72-111 s, activity simulation 5-6 s, PrimeTime 10-11 s, Conformal
~30 s. The quick test meets the initial <= 10 min target; most of the time is
P&R and the two agent tasks.

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

## Known limitations

* Linux x86-64 only; macOS, ARM and Windows are untested.
* Per-e-node features are estimates; attribution splits shared cells equally
  among the operator tokens of an RTL line (see `docs/cost-model.md`).
* Rules that relate several bit widths are rejected by the certifier in v0.1.
* `circt-lec` verification supports combinational designs only.
* The hierarchical design accumulator driver from the paper's experiments is not part of
  this release.
