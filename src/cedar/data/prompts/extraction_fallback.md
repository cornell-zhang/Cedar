# Cedar task {task_id}: reconstruct Verilog from an extracted e-graph term

The CIRCT reconstruction of iteration {iteration} failed: {error}

Reconstruct an equivalent synthesizable Verilog module `{top}` (identical ports) that
implements the extracted term in `{extracted}` (egglog ops: `{ops_egg}`; original RTL:
{rtl_files}). Write it to `{output_v}`. The result is verified with `{verification_mode}`
against the original RTL like every other candidate.

Submit with: `cedar task submit --run {run_dir} {task_id} --agent <claude|codex> --model <model id>`
