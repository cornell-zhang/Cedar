# Cedar

Cedar optimizes the area, delay and power of RTL designs by combining an AI
coding agent with formally checked compiler machinery:

1. the agent proposes **direct RTL rewrites** and generalizes the verified ones
   into **egglog rewrite rules**;
2. every rule is **proved with Z3 at each exact bit width** it is applied to
   before it may enter an e-graph;
3. **equality saturation** (CIRCT + DialEgg + egglog) explores equivalent
   implementations and a **weighted per-e-node PPA cost model** drives
   extraction (extraction-gym);
4. every candidate is **checked for equivalence** against the original RTL
   (Cadence Conformal, a self-checking simulation, or open-source `circt-lec`)
   and measured with **your EDA flow** (Synopsys DC, Cadence Innovus, Synopsys
   VCS + PrimeTime with SAIF activity), and a verified Pareto frontier is reported.

The agent can be **Claude Code** or **Codex**: both load the same skills from
this repository, and the same Python package does all deterministic work
(compilation, proofs, verification, measurement, run state).

> Cedar does not include or redistribute commercial EDA tools or technology
> libraries. You configure your own through private profiles.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/cedar/` | `cedar-workflow` Python package and `cedar` CLI |
| `skills/` | shared agent skills: `cedar`, `cedar-setup`, `cedar-doctor` |
| `.claude-plugin/`, `.codex-plugin/`, `.agents/plugins/` | Claude Code and Codex plugin/marketplace manifests |
| `third_party/dialegg`, `third_party/extraction-gym` | Cedar versions of DialEgg and extraction-gym (provenance in each `UPSTREAM.md`) |
| `toolchain/` | pinned sources (`sources.lock.json`), native/container build, `cedar-toolchain` wheel, source bundle, templates |
| `examples/` | small redistributable designs |
| `tests/` | regression tests |
| `docs/` | setup, architecture, workflow, build and troubleshooting guides |

## Install

Requirements: Linux x86-64 (glibc >= 2.28), Python >= 3.9.

```bash
python -m venv ~/venvs/cedar && . ~/venvs/cedar/bin/activate
pip install cedar_toolchain-0.1.0-py3-none-manylinux_2_28_x86_64.whl   # prebuilt compiler toolchain
pip install cedar_workflow-0.1.0-py3-none-any.whl                     # or: pip install -e <this checkout>
cedar doctor
```

The wheels are built from this repository (`docs/building.md`): the toolchain
wheel by `toolchain/wheel/build_wheel.sh`, the workflow wheel by
`python -m build`. They are not published on PyPI for v0.1. You can also build
the toolchain natively (`toolchain/build/build-native.sh`) and select it with
`CEDAR_TOOLCHAIN_PREFIX=<prefix>`; Cedar never falls back to tools found on `PATH`.

## Try it without commercial tools

```bash
cedar example open --output-dir /tmp/cedar-open
```

This runs RTL -> MLIR -> e-graph (with Z3-certified seed rules) -> area- and
delay-weighted extraction -> Verilog, and proves each result equivalent to the
input with `circt-lec` + Z3.

## Use with an agent

**Claude Code** (from a checkout, for development):

```bash
claude --plugin-dir /path/to/Cedar
# or install through the bundled marketplace:
claude plugin marketplace add /path/to/Cedar && claude plugin install cedar@cedar
```

**Codex**:

```bash
codex plugin marketplace add /path/to/Cedar && codex plugin add cedar@cedar
```

Then ask the agent to "set up Cedar" (`cedar-setup` skill: guided tool and
technology profiles, design spec with an explicit timing goal) and to
"optimize <design> with Cedar" (`cedar` skill). The agent runs
`cedar run start ...`, performs the tasks the run hands it (RTL rewrites, rule
proposals, ...), submits them with `cedar task submit`, and reports the
verified frontier from `report.json`.

## Full PPA optimization

1. Create private profiles (outside any repository) with the `cedar-setup` skill or
   by hand (`docs/configuration.md`):
   * a **tool profile** — executables and environment setup for DC, Innovus, VCS,
     vcd2saif, PrimeTime and Conformal;
   * a **technology profile** — corners (.db, .lib, Verilog cell models, RC file),
     LEF, P&R parameters and optional Tcl hooks.
2. Validate: `cedar doctor --profile <p> --technology <t>` and once
   `cedar flow-check --design <design.yaml> --output-dir <scratch>`.
3. Create a design spec with an explicit timing goal:
   `cedar design init --top ... --rtl ... --period-ns 1.0 --verification conformal ...`.
4. Optimize: `cedar run start --design design.yaml --work-dir <scratch>`
   (`--preset quick` for a bounded smoke run, `--preset paper` for the paper's
   >=10-iteration exploration).

Measured quantities and stages are explicit in every report: area and delay
from DC post-synthesis reports, power from PrimeTime on the post-route netlist
with SPEF parasitics and SAIF activity from SDF-annotated gate-level
simulation. Missing measurements are reported as unavailable, never as zero.
Cedar does not search for a clock frequency: the timing goal is always yours.

## Documentation

* [`docs/setup.md`](docs/setup.md) — installation, host plugins, guided setup
* [`docs/configuration.md`](docs/configuration.md) — profile and design schemas
* [`docs/workflow.md`](docs/workflow.md) — phases, agent tasks, gates, presets, outputs
* [`docs/architecture.md`](docs/architecture.md) — components and data flow
* [`docs/cost-model.md`](docs/cost-model.md) — weighted objective, per-e-node costs, attribution
* [`docs/building.md`](docs/building.md) — native, container and wheel builds; source bundle
* [`docs/paper-to-code.md`](docs/paper-to-code.md) — how each method claim maps to code and tests
* [`docs/troubleshooting.md`](docs/troubleshooting.md)

Archived experimental evidence for the paper lives in the separate artifact
repository [`iccad26-cedar-artifacts`](https://github.com/zzzDavid/iccad26-cedar-artifacts)
(access may be restricted until it is published); this repository contains the
implementation only.

## License

Apache License 2.0 (see `LICENSE`). Third-party components and their licenses
are listed in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).
