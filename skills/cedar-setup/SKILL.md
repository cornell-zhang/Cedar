---
name: cedar-setup
description: Guided first-time Cedar setup - create and validate private tool profiles (DC, Innovus, VCS, PrimeTime, Conformal), technology profiles (libraries, LEF, RC, corners) and design specs with an explicit timing goal. Use when Cedar is not configured yet, a profile is missing or invalid, or the user wants to add a design.
---

# Cedar guided setup

Goal: produce validated, reusable, PRIVATE configuration through the same schema
the workflow uses. You gather answers conversationally; `cedar` validates and saves.

Rules
- Profiles contain machine-specific paths: keep them in the user's config home
  (`$CEDAR_CONFIG_HOME` or `~/.config/cedar`), never inside a source repository.
- Record environment-variable NAMES (e.g. `LM_LICENSE_FILE`), never their values,
  and never ask for passwords or license keys.
- The timing goal is the user's decision: ask for a clock period in ns (or a
  unit-qualified frequency such as "1.25 GHz"). Never search for a frequency.

## Steps
1. `cedar setup questions` — the question list and the three schemas.
2. `cedar setup discover` — candidate executables already on PATH. If the user
   loads tools via modules or a site script, ask for that script; it becomes
   `environment.setup_script`.
3. Ask the [tools] questions. Write the answers as a YAML document following the
   tool-profile schema to a private scratch file, then
   `cedar setup write-profile --kind tools --answers <file>` (add `--force` to replace).
   Show the user the saved profile and any validation errors; iterate until it saves.
4. Ask the [technology] questions (corners with .db/.lib/Verilog models/RC file,
   LEF, P&R site/fillers/layers, optional Tcl hooks) and save with
   `cedar setup write-profile --kind technology --answers <file>`.
5. `cedar doctor --profile <name> --technology <name>` — every stage should be
   configured and available. Explain anything that is not.
6. For each design: ask for RTL files, top, clock port, TIMING GOAL, resets,
   activity testbench (or generated stimulus), verification backend
   (`conformal`, `simulation` with a self-checking testbench, or `circt-lec` for
   combinational designs) and objective priorities (presets: balanced, area, delay,
   power, paper-power, area-delay, or explicit weights). Then
   `cedar design init --top ... --rtl ... --period-ns ... --verification ... \
   --tools <profile> --technology <tech> -o <private dir>/design.yaml`
   and show the resolved weights it prints. Multiple clocks need a user SDC.
7. Optional but recommended once per profile: `cedar flow-check --design <design.yaml>
   --output-dir <scratch>` runs synthesis, P&R, activity simulation, power and a
   self-equivalence check and records the profile as end-to-end validated.
   `cedar doctor` then reports `validated`; it reports `stale` when tools change.
