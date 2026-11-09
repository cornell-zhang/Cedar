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
| ABI policy (`check_abi.py`) on both builds | pass: only manylinux-allowlisted libraries, GLIBC <= 2.28, no RUNPATH |
| Toolchain smoke test (RTL -> e-graph -> Verilog -> circt-lec + Z3) on both builds | pass (equivalence proved) |
| `cedar-toolchain` wheel (`check_wheel.py`): files, licenses, manifest hashes vs. binaries and lockfile, platform tag, content scan | pass |
| `cedar-workflow` wheel and sdist: path allowlist, content scan | pass |

## Tests (`pytest`)

| Environment | Result |
| --- | --- |
| repository checkout (editable install, Python 3.13), container-built toolchain prefix | 50 passed |
| repository checkout, toolchain prefix built natively from the source bundle | passed (see build section) |
| fresh venv from the final wheels (Python 3.11, toolchain from the `cedar-toolchain` wheel), tests from the sdist | 49 passed, 1 skipped (example templates need the checkout) |
| fresh venv in the manylinux_2_28 container (AlmaLinux 8, glibc 2.28, Python 3.9), final wheels | 49 passed, 1 skipped; `cedar example open` passes |

Toolchain-dependent tests run the real binaries: the weighted cost model must
flip the real extractor's choice, the extractor must be deterministic on an
e-graph that exposed nondeterminism before, and circt-lec + Z3 must prove and
refute equivalence. Engine tests stub only the commercial EDA stages.

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

**Release-commit runs** (final wheels in a fresh environment; plugins loaded
from a clean export of the release commit):

| Host | Run wall clock | Agent session | Result |
| --- | --- | --- | --- |
| Claude Code (`--plugin-dir`) | 5 min 06 s | 5 min 22 s | completed, contract met; extracted candidate Conformal PASS: area -38.3%, power -30.3%, delay +2.6%, timing met |
| Codex (installed plugin) | 6 min 26 s | 7 min 09 s | completed, contract met; identical extracted result (deterministic extraction); the agent's rule was proved at 1 and 8 bits, hit the 60 s Z3 budget at 16 bits and was rejected there |

Both runs measured the same baseline (893.7 um2, 0.661 ns, 229.6 uW) and the
same extracted candidate (551.5 um2, 0.678 ns, 160.1 uW). Phase times of the
Claude run: baseline 100 s, direct-candidate verification + synthesis preview
40 s, e-graph iteration incl. verification and parallel full-PPA measurement of
both candidates 134 s, agent tasks 11 s and 15 s. A first Claude run of the
same commit took 10 min 02 s because P&R ran 240-259 s (instead of ~76 s)
while a 24-job native LLVM build and another user's Innovus job shared the
machine — the target assumes a reasonably idle host and licenses.

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

## Known limitations

* Linux x86-64 only; macOS, ARM and Windows are untested.
* Per-e-node features are estimates; attribution splits shared cells equally
  among the operator tokens of an RTL line (see `docs/cost-model.md`).
* Rules that relate several bit widths are rejected by the certifier in v0.1.
* `circt-lec` verification supports combinational designs only.
* The hierarchical design accumulator driver from the paper's experiments is not part of
  this release.
