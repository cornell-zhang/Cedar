# Cedar task {task_id}: continue or stop exploration?

The frontier has not changed for {stall} successful iterations (iteration {iteration} of at
most {max_iterations}; minimum requirements: {contract}).
- {objective}
- Frontier: {frontier}
- Recent iterations: {recent}

Write `{output_json}` as `{{"decision": "STOP" | "CONTINUE", "reason": "<short>"}}` and submit:
`cedar task submit --run {run_dir} {task_id} --agent <claude|codex> --model <model id>`
