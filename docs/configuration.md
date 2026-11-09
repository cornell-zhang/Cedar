# Configuration reference

Cedar uses three versioned YAML layers. Unknown keys are errors, units are
explicit, and relative paths resolve against the directory of the file that
contains them (never the shell's working directory). `${VAR}` references are
expanded at load time; provenance snapshots keep the unexpanded text, and only
environment-variable *names* are recorded. `cedar setup schema --kind
tools|technology|design` prints a JSON Schema for each layer; `cedar setup
questions` prints the field documentation.

## 1. Tool profile — `cedar/tool-profile@1`

```yaml
schema: cedar/tool-profile@1
name: my-site
environment:
  setup_script: /path/to/site-env.sh   # sourced (bash) before every tool call
  env_required: [LM_LICENSE_FILE]      # names only; checked, never recorded
execution:
  nice: 10
  threads: 4
tools:
  synthesis:   {adapter: synopsys-dc,        executable: dc_shell,  timeout_s: 1800}
  place_route: {adapter: cadence-innovus,    executable: innovus,   timeout_s: 3600}
  simulation:  {adapter: synopsys-vcs,       executable: vcs}
  saif:        {adapter: synopsys-vcd2saif,  executable: vcd2saif}
  power:       {adapter: synopsys-primetime, executable: pt_shell}
  equivalence: {adapter: cadence-conformal,  executable: lec}
toolchain:                             # optional explicit Cedar toolchain prefix
  prefix: /opt/cedar-toolchain-0.1.0
```

Each stage is a *named adapter* with declared inputs and outputs (see
`src/cedar/eda/flow.py`); arbitrary command strings are not accepted. Site
behaviour goes into the setup script and into the technology hooks.

## 2. Technology profile — `cedar/technology@1`

```yaml
schema: cedar/technology@1
name: mypdk-tt
units: {time: ps}                      # library time unit used in SDC and reports
default_corner: tt
corners:
  tt:
    db: ["${PDK}/db/*_TT.db"]
    liberty: ["${PDK}/lib/*_TT.lib"]
    sim_models: ["${PDK}/verilog/*_TT.v"]
    rc_tech: ${PDK}/qrc/qrcTechFile
lef:
  tech: ${PDK}/lef/tech.lef
  cells: [${PDK}/lef/cells.lef]
synthesis:
  compile_command: compile -map_effort high -area_effort high -no_design_rule -power_effort high
  max_transition: 50.0                 # library time units
  input_transition_max: 60.0
  input_transition_min: 20.0
  dont_use: []
  pre_compile_hook: hooks/pre_compile.tcl     # optional Tcl, sourced by DC
place_route:
  site: coreSite
  core_utilization: 0.6
  core_margin: 4.0                     # um
  filler_cells: [FILLER_CELL_A, FILLER_CELL_B]
  max_route_layer: 6
  route_top_layer: 5
  route_bottom_layer: 2
  post_init_hook: hooks/innovus_post_init.tcl
equivalence:
  extra_commands: []                   # Conformal dofile commands before 'set system mode lec'
```

## 3. Design spec — `cedar/design@1`

```yaml
schema: cedar/design@1
name: dist_mac
top: dist_mac
rtl: [rtl/dist_mac.v]
include_dirs: []
defines: {}
profiles: {tools: my-site, technology: mypdk-tt}   # names (config home) or paths
clocks:
  - {port: clk, period_ns: 1.0}        # or frequency: "1.25 GHz"; omit port for a virtual clock
resets:
  - {port: rst, active: high, synchronous: true}
# sdc: constraints.sdc                 # advanced; required for more than one clock
io_delay_fraction: 0.1
activity:                              # switching activity for power ONLY
  mode: testbench                      # or: generated (random stimulus)
  testbench: tb/tb_activity.v
  tb_top: Top
  dut_instance: DUT
verification:
  mode: conformal                      # conformal | simulation | circt-lec
  # testbench: tb/tb_check.v           # simulation mode: self-checking, prints pass_marker
objective:
  preset: balanced                     # balanced | area | delay | power | paper-power | area-delay
  # weights: {area: 0.3, delay: 0.2, power: 0.5}
  normalization: baseline
  require_timing_met: true
  weight_schedule: []                  # [{from_iteration: 5, weights: {...}}]
measurement:
  mode: full-ppa                       # or synthesis-only (area/delay objectives only)
  approximate_pruning: false
search:
  preset: quick                        # or paper; individual fields override the preset
agent:
  stage_models: {}                     # e.g. {proposal: <model>, selection: <model>}
```

Validation rules beyond the schema:

* at least one clock with exactly one of `period_ns` / `frequency`; the timing
  goal is never chosen automatically and an infeasible goal is reported
  (`infeasible_timing_goal`), not relaxed;
* more than one clock requires a user `sdc`;
* `verification.mode: simulation` requires a self-checking testbench that is
  different from the activity testbench;
* an objective preset or explicit weights is required; weights are finite,
  non-negative, and at least one is positive; a positive power weight requires
  `measurement.mode: full-ppa`.

## Constraint consistency

All stages (DC, Innovus, PrimeTime) read the same generated SDC (or your SDC
verbatim). Every run records a `constraints_id` hash of clocks, SDC, I/O delay,
technology profile, corner and measurement mode; frontiers only compare points
with the same `constraints_id`, measurement mode and per-metric stage.
