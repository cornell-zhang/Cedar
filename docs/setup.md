# Setup

## 1. Requirements

| Item | Requirement |
| --- | --- |
| OS / CPU | Linux x86-64, glibc >= 2.28 (RHEL/Alma/Rocky 8+, Ubuntu 20.04+); CPU with x86-64-v2 |
| Python | >= 3.9 (tested: 3.9 in manylinux_2_28, 3.11, 3.13) |
| Compiler toolchain | `cedar-toolchain` wheel, or a native/container build (`docs/building.md`) |
| Agent (optional) | Claude Code and/or Codex CLI, logged in |
| Full PPA (optional) | your licensed Synopsys DC, VCS (+ `vcd2saif`), PrimeTime; Cadence Innovus, Conformal LEC; a technology library (db/lib/Verilog models/LEF/RC) |

Without commercial tools you can still run the compiler/e-graph flow and the
open-source equivalence check (`cedar example open`).

## 2. Install the Python packages

```bash
python -m venv ~/venvs/cedar
. ~/venvs/cedar/bin/activate
pip install cedar_toolchain-0.1.0-py3-none-manylinux_2_28_x86_64.whl
pip install cedar_workflow-0.1.0-py3-none-any.whl     # or: pip install -e /path/to/Cedar
cedar doctor
```

`cedar doctor` reports the toolchain it selected. Selection is explicit, in
this order: `--toolchain-prefix`, `CEDAR_TOOLCHAIN_PREFIX`, `toolchain.prefix`
in the tool profile, the installed `cedar-toolchain` wheel. Binaries that merely
happen to be on `PATH` are never used.

## 3. Install the agent plugin

The repository root is a plugin for both hosts; the three skills live in
`skills/`. Install from a clean clone (the hosts copy or load the plugin
directory as-is).

Claude Code:

```bash
claude --plugin-dir /path/to/Cedar                       # session-only, for development
claude plugin marketplace add /path/to/Cedar             # or cornell-zhang/Cedar
claude plugin install cedar@cedar
```

Codex:

```bash
codex plugin marketplace add /path/to/Cedar              # or cornell-zhang/Cedar
codex plugin add cedar@cedar
```

Agents run `cedar` commands, so the environment that launches the agent must
have `cedar` on `PATH` (activate the venv first) and the Cedar environment
variables you use (`CEDAR_CONFIG_HOME`, `CEDAR_STATE_HOME`,
`CEDAR_TOOLCHAIN_PREFIX`). EDA tools need license access, so run the agent in
a mode that allows the commands to reach your license servers (for example
Codex with a sandbox policy that permits network access).

## 4. Guided configuration

Ask the agent: *"Set up Cedar for my EDA tools."* The `cedar-setup` skill:

1. shows the questions (`cedar setup questions`) and discovers tools on `PATH`
   (`cedar setup discover`);
2. writes your answers to a scratch YAML and saves them with
   `cedar setup write-profile --kind tools|technology --answers FILE`
   (validated against the schema; saved with mode 0600 under the config home);
3. runs `cedar doctor --profile <p> --technology <t>`;
4. creates a design spec with `cedar design init` — the timing goal (clock
   period or unit-qualified frequency) is always asked from you;
5. optionally runs `cedar flow-check` to validate the full flow end to end.

Locations (all configurable):

| What | Default | Override |
| --- | --- | --- |
| profiles | `~/.config/cedar/profiles/<name>.yaml`, `~/.config/cedar/technologies/<name>.yaml` | `CEDAR_CONFIG_HOME` |
| certificates, rule history, validation records, user rule pool | `~/.local/state/cedar/` | `CEDAR_STATE_HOME` |
| run outputs | `--work-dir` (required) | — |

Keep profiles, design specs that reference private libraries, and run
directories outside any source repository.

## 5. Capability levels

`cedar doctor` distinguishes, for each stage (synthesis, place_route,
simulation, saif, power, equivalence):

* **configured** — present and schema-valid in the tool profile;
* **available** — the executable resolves inside the profile's environment;
* **validated** — `cedar flow-check` passed for this exact tool and technology
  profile with the same resolved executables (`stale` once anything changes).

Runs check that every stage required by the measurement mode is available and
stop otherwise (`missing_capability`); they never downgrade a full-PPA run.
