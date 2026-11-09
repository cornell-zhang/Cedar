# Cedar task {task_id}: choose rules and extraction cost multipliers (iteration {iteration})

Choose a rule combination and optional per-constructor cost multipliers for the next
equality-saturation + extraction iteration.

- {objective}
- Per-e-node cost model: cost(n) = w_area*area(n)/S_area + w_delay*delay(n)/S_delay + w_power*power(n)/S_power
  with weights for this iteration {weights}; multipliers in [0.05, 20] scale individual constructors.
- Applicable admitted rules (probe: egglog matches / new e-nodes on this design, history evidence):
{rule_table}
- Already tried (rules, cost config): {tried}
- Current verified frontier: {frontier}
- Recent iterations: {recent}

Write `{output_json}` as
`{{"rules": ["<rule name>", ...], "cost_multipliers": {{"comb_mul": 1.0}}, "reason": "<short>"}}`.
Prefer complementary rules (different operator kinds). Combination effects are only known
for the whole combination. Then submit:
`cedar task submit --run {run_dir} {task_id} --agent <claude|codex> --model <model id>`
