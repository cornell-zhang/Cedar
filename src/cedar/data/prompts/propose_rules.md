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
- Variadic operands live in an `OpVec`: `(comb_add ?first ?ops ?t)`. Every matched vector
  `?ops` MUST have exactly one guard `(= N (vec-length ?ops))`, where N is the exact number
  of operands the rule is about (0 is allowed). The proof models exactly N elements, so
  unread elements still count; reads `(vec-get ?ops i)` must use literal indices i < N.
- Premises are `(= ?var term)` or a length guard only. To require two terms to be equal,
  bind both to one variable: `(= ?z (vec-get ?a 0)) (= ?z (vec-get ?b 0))`. Any other
  premise shape is rejected.
- Every operand must get its width from the premises: bind it through an operation of the
  rule's type (`(= ?x (comb_and ?a ?ops ?t))` fixes `?a`) or `vec-get` from such an operand
  vector. Operands seen only through `comb_extract`, `comb_icmp`, `comb_concat`,
  `comb_replicate` or the right-hand side have no fixed width and the rule is rejected.
- Build new vectors with `(vec-push (vec-empty) ?x)`; never match vector constructors in premises.
- Actions are `let` bindings and exactly one `union`. Name `let` bindings WITHOUT `?`
  (`(let s (comb_add ?b ... ?t))`, then use `s`): `?` names are reserved for pattern variables
  and a rule with `(let ?s ...)` is rejected.
- No stateful ops (`seq_firreg`, `llhd_*`). No timing/latency changes.
- Start each file with `;` comments stating the identity and why it is valid for all inputs.

## Outputs
{output_list}

Submit with: `cedar task submit --run {run_dir} {task_id} --agent <claude|codex> --model <model id>`
