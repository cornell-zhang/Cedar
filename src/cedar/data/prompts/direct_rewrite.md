# Cedar task {task_id}: direct RTL rewrite candidates for `{top}` (round {round})

You are the optimization agent in a Cedar run. Produce {n_variants} functionally
equivalent RTL rewrite(s) of the design below that improve the stated objective.

## Inputs
- Original RTL (read these files; do not modify them): {rtl_files}
- Top module: `{top}` — the module name and every port (name, direction, width) must stay identical.
- Clock / timing goal: {clock_desc}
- {objective}
- Baseline measurement ({measurement_mode}): {baseline_ppa}
- E-graph operator histogram of the design: {op_histogram}

## Previous rounds
{round_context}

## Constraints
- Cycle behaviour must be identical: same registers per output path, same reset values,
  same latency. {latency_rule}
- Every candidate is checked with `{verification_mode}` against the original RTL and
  measured with the full flow; failing candidates are discarded.
- Synthesizable Verilog-2001/SystemVerilog only; no vendor primitives, no `initial` blocks in RTL.

## Outputs (write exactly these files)
Each Verilog file must be self-contained: it replaces *all* original RTL files, so
include every module the top needs.
{output_list}
- `{notes_path}`: JSON object `{{"variants": [{{"file": "<file name>", "change": "<one sentence>", "expected_effect": "<area/delay/power reasoning>"}}]}}`

When the files are written, submit with:
`cedar task submit --run {run_dir} {task_id} --agent <claude|codex> --model <model id>`
