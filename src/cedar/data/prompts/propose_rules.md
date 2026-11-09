# Cedar task {task_id}: propose egglog rewrite rules for `{top}`

Propose {n_rules} general, semantics-preserving egglog rewrite rule(s) that would let
equality saturation reach the improvements found by direct rewriting (and similar ones
on other designs). Every rule is quarantined and must be **proved by Z3 at each exact bit
width it is applied to** before use; unprovable or unguarded rules are rejected.

## Evidence
- Original RTL: {rtl_files}
- Eggified design (egglog ops, read it): {ops_egg}
- Operator signatures (egglog template): {circt_egg}
- Bit widths present in the design: {widths}
- {objective}
- Baseline: {baseline_ppa}
- Verified direct rewrites (diff vs original, PPA deltas, verification evidence):
{direct_evidence}
- Existing admitted rules (avoid duplicates): {existing_rules}

## Rule format (mandatory)
- Exactly one `(rule (<premises>) (<actions>) :ruleset rules)` per file with exactly one `union`.
- Use one type variable for the operation width (e.g. `?t` in `(comb_add ?x ?ops ?t)`); Cedar
  instantiates it per concrete width and proves each instance.
- Variadic operands live in an `OpVec`: `(comb_add ?first ?ops ?t)`. If you read
  `(vec-get ?ops i)`, you MUST also add the guard `(= N (vec-length ?ops))` with
  N = (largest index read) + 1, otherwise the rule is rejected (it would drop operands).
- Build new vectors with `(vec-push (vec-empty) ?x)`; never match vector constructors in premises.
- No stateful ops (`seq_firreg`, `llhd_*`). No timing/latency changes.
- Start each file with `;` comments stating the identity and why it is valid for all inputs.

## Outputs
{output_list}

Submit with: `cedar task submit --run {run_dir} {task_id} --agent <claude|codex> --model <model id>`
