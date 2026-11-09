# SPDX-License-Identifier: Apache-2.0


"""
Rule Verifier: Translate egglog rewrite rules to Z3 and prove correctness.

This tool parses egglog rewrite rules (the in-tree `(rule premises actions
:ruleset rules)` shape, plus the legacy `(rewrite L R :ruleset rules)`
desugar) and uses Z3's bitvector theory to formally verify that the
rewrite preserves semantics across a sweep of bitwidths.

Gate categories emitted to stdout:
    VALID                       — all sweep widths prove unsat-on-negation
    INVALID                     — Z3 found a counterexample
    UNKNOWN: <reason>           — Z3 timeout or a rule form the verifier
                                   chose not to model (NEVER "parse failure")
    REJECTED: banned-op <name>   — body contains an unverifiable op
    REJECTED: banned-phrase <s>  — leading comment contains a banned
                                   hand-wavey justification

Usage:
    python verify_rule.py <rule_file.egg>
    python verify_rule.py --all
    python verify_rule.py --all --no-phrase-lint

Based on the approach described in the ROVER paper:
"ROVER: RTL Optimization via Verified E-Graph Rewriting" (Coward et al., 2024)
"""

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import z3  # z3-solver is a declared dependency of cedar-workflow

# Cedar: this module is the historical verifier used as a library by
# cedar.rules.certify, which proves each rule instance at ONE exact width.
# The legacy multi-width sweep below is kept for the standalone CLI only;
# its results are never used as admission evidence.
PROJECT_ROOT = Path.cwd()

# --------------------------------------------------------------------------
# Banned-op / banned-phrase tables (criteria 6 & 7).
# --------------------------------------------------------------------------

BANNED_OPS: Tuple[str, ...] = (
    "seq.firreg",
    "llhd.sig",
    "llhd.drv",
    "llhd.prb",
)
# Egglog identifiers cannot contain `.`, so DialEgg's eggify pass emits
# the dialect-qualified op names in underscore form (e.g. `seq_firreg`
# rather than `seq.firreg`). The banned-op gate must catch both spellings:
# the underscore form is the dominant case for machine-generated rules,
# the dotted form survives for any hand-written or comment text that
# happens to mention the canonical MLIR name.

BANNED_PHRASES: Tuple[str, ...] = (
    "in this branch",
    "the chain guarantees",
    "given that",
    "assuming",
    "in this context",
)

# Default bitwidth sweep — implementation pins {1, 64} as the baseline.
DEFAULT_SWEEP: Tuple[int, ...] = (1, 64)

# Sweep ceiling — Z3 bitvector solving gets sluggish above ~64 bits when
# multiplication is involved. Annotations like (I256) would blow that up,
# so we cap the sweep instead of refusing the rule outright.
SWEEP_CAP: int = 1 << 20

# Per-rule solver budget. The paper (methodology.tex:124) commits to "a
# 60-second solver timeout per rule". This is enforced as a *total* Z3
# budget per rule, apportioned across the bitwidth sweep: each sweep width
# gets the remaining budget, and we subtract the elapsed wall time after
# each solve. A rule can therefore never consume more than this many ms of
# Z3 solver time end-to-end, regardless of sweep length.
DEFAULT_RULE_TIMEOUT_MS: int = 60000


# --------------------------------------------------------------------------
# S-expression tokenizer + parser.
# --------------------------------------------------------------------------


def _strip_line_comments(src: str) -> str:
    """Egglog uses `;` line comments. Quoted strings can contain `;`, so we
    walk the source one char at a time rather than regexing per-line."""
    out: List[str] = []
    in_str = False
    i = 0
    while i < len(src):
        ch = src[i]
        if in_str:
            out.append(ch)
            if ch == "\\" and i + 1 < len(src):
                out.append(src[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ";":
            # Drop until end of line.
            while i < len(src) and src[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|\(|\)|[^\s()]+')


def _tokenize(src: str) -> List[str]:
    return _TOKEN_RE.findall(src)


def _parse_tokens(tokens: List[str], i: int) -> Tuple[Any, int]:
    if i >= len(tokens):
        raise ValueError("Unexpected EOF while parsing s-expression")
    tok = tokens[i]
    if tok == "(":
        i += 1
        out: List[Any] = []
        while i < len(tokens) and tokens[i] != ")":
            elem, i = _parse_tokens(tokens, i)
            out.append(elem)
        if i >= len(tokens):
            raise ValueError("Unbalanced parentheses")
        return out, i + 1
    if tok == ")":
        raise ValueError("Unexpected `)`")
    # Atom.
    if tok.startswith('"') and tok.endswith('"'):
        return tok, i + 1  # keep the quotes; treat as a string literal atom
    # Integer?
    try:
        return int(tok), i + 1
    except ValueError:
        return tok, i + 1


def parse_top_forms(src: str) -> List[Any]:
    """Strip line comments, tokenize, parse a sequence of top-level forms."""
    clean = _strip_line_comments(src)
    tokens = _tokenize(clean)
    forms: List[Any] = []
    i = 0
    while i < len(tokens):
        form, i = _parse_tokens(tokens, i)
        forms.append(form)
    return forms


def sexp_to_str(sexp: Any) -> str:
    if isinstance(sexp, list):
        return "(" + " ".join(sexp_to_str(x) for x in sexp) + ")"
    return str(sexp)


# --------------------------------------------------------------------------
# Rule structure.
# --------------------------------------------------------------------------


class ParsedRule:
    """A normalized rule: list of premise equalities + a single union goal.

    Premises are pairs (var_name, expr) meaning `?var == expr`. Action
    `let` bindings are inlined into the premise list (treated identically
    to `(= ?var <expr>)` since egglog's let is just a name binding).
    """

    def __init__(
        self,
        name: str,
        description: str,
        leading_comment: str,
        body_text: str,
        premises: List[Tuple[str, Any]],
        lhs_var: str,
        rhs_expr: Any,
        annotated_widths: List[int],
        skip_reason: Optional[str] = None,
        vec_lengths: Optional[Dict[str, int]] = None,
    ):
        self.name = name
        self.description = description
        self.leading_comment = leading_comment
        self.body_text = body_text
        self.premises = premises
        self.lhs_var = lhs_var
        self.rhs_expr = rhs_expr
        self.annotated_widths = annotated_widths
        self.skip_reason = skip_reason
        # Exact length of every variable operand vector, from the rule's
        # (= N (vec-length ?v)) premises. The model uses exactly these lengths
        # (zero included); a variable vector without one is unsupported.
        self.vec_lengths: Dict[str, int] = dict(vec_lengths or {})


def _rewrite_bare_refs(expr: Any, bare_names: Dict[str, str]) -> Any:
    """Replace bare-symbol references to known `let`-bound names with the
    normalized `?`-prefixed form. Operates structurally; leaves all
    other atoms (operator heads, attribute keywords, string literals)
    untouched. Op heads are never rewritten because heads sit in
    position 0 of a list, but we still allow rewriting in case a let
    binding shadows an op name (extremely uncommon)."""
    if isinstance(expr, str):
        if expr in bare_names:
            return bare_names[expr]
        return expr
    if isinstance(expr, list):
        if not expr:
            return expr
        # Don't rewrite the head — let names never shadow real ops in
        # the rule library, and rewriting the head would break things
        # like `(IntegerAttr value type)` if a let bound `IntegerAttr`.
        rewritten_tail = [_rewrite_bare_refs(c, bare_names) for c in expr[1:]]
        return [expr[0]] + rewritten_tail
    return expr


def _is_vec_shape(expr: Any) -> bool:
    """True if `expr` is a literal vec-construction (vec-of / vec-push /
    vec-empty). Such expressions are not BitVec values; they're vec
    shapes that only make sense inside a comb_X op's vec slot."""
    if not isinstance(expr, list) or not expr:
        return False
    head = expr[0]
    return head in ("vec-of", "vec-push", "vec-empty")


def _inline_vec_refs(expr: Any, vec_map: Dict[str, Any]) -> Any:
    """Replace any `?varname` reference that is bound to a vec shape with
    the vec shape itself. Recursive. Does not rewrite list heads (op
    names are never vec-bound)."""
    if isinstance(expr, str):
        if expr in vec_map:
            return vec_map[expr]
        return expr
    if isinstance(expr, list):
        if not expr:
            return expr
        return [expr[0]] + [_inline_vec_refs(c, vec_map) for c in expr[1:]]
    return expr


def _extract_leading_comment(src: str) -> str:
    """The leading `;`-prefixed block, before the first non-comment line."""
    lines: List[str] = []
    for raw in src.splitlines():
        stripped = raw.lstrip()
        if not stripped:
            # Allow blank lines inside the leading comment block.
            if lines:
                lines.append("")
            continue
        if stripped.startswith(";"):
            lines.append(stripped.lstrip(";").strip())
            continue
        break
    return "\n".join(lines).strip()


def _walk_collect_widths(expr: Any, out: List[int]) -> None:
    """Walk an s-expression and collect every explicit `(IN)` annotation."""
    if isinstance(expr, list) and expr:
        head = expr[0]
        if isinstance(head, str) and len(expr) == 1:
            m = re.fullmatch(r"I(\d+)", head)
            if m:
                w = int(m.group(1))
                if w not in out:
                    out.append(w)
                return
        if isinstance(head, str) and head == "Int" and len(expr) == 2 and isinstance(expr[1], int):
            w = expr[1]
            if w not in out:
                out.append(w)
        for child in expr:
            _walk_collect_widths(child, out)


def _parse_rule_form(form: Any) -> Optional[Tuple[List[Tuple[str, Any]], List[Any]]]:
    """Pull (premises, actions) out of `(rule (premises...) (actions...)
    :ruleset rules)`. Returns None if `form` is not a rule shape."""
    if not (isinstance(form, list) and form and form[0] == "rule"):
        return None
    if len(form) < 3:
        return None
    premises_block = form[1]
    actions_block = form[2]
    if not (isinstance(premises_block, list) and isinstance(actions_block, list)):
        return None
    # Every premise must be modeled: `(= ?var expr)` (either orientation) or
    # an exact length guard `(= N (vec-length ?v))` (either orientation).
    # Anything else makes the whole rule unsupported -- silently dropping a
    # premise could drop information (e.g. vector shape) the proof relies on.
    premises: List[Tuple[str, Any]] = []
    lengths: Dict[str, int] = {}
    for p in premises_block:
        if not (isinstance(p, list) and len(p) == 3 and p[0] == "="):
            return None
        a, b = p[1], p[2]
        guard = _length_guard(a, b) or _length_guard(b, a)
        if guard is not None:
            v, n = guard
            if lengths.get(v, n) != n:
                return None
            lengths[v] = n
        elif isinstance(a, str) and a.startswith("?"):
            premises.append((a, b))
        elif isinstance(b, str) and b.startswith("?"):
            premises.append((b, a))
        else:
            return None
    return premises, actions_block, lengths


def _length_guard(n: Any, v: Any) -> Optional[Tuple[str, int]]:
    if isinstance(n, int) and n >= 0 and isinstance(v, list) and len(v) == 2 and v[0] == "vec-length" \
            and isinstance(v[1], str) and v[1].startswith("?"):
        return v[1], n
    return None


def _parse_rewrite_form(form: Any) -> Optional[Tuple[List[Tuple[str, Any]], List[Any]]]:
    """Desugar `(rewrite L R :ruleset rules)` into the rule shape:
       premises = [(?lhs, L)], actions = [(union ?lhs R)]."""
    if not (isinstance(form, list) and form and form[0] == "rewrite"):
        return None
    if len(form) < 3:
        return None
    lhs = form[1]
    rhs = form[2]
    premises = [("?lhs", lhs)]
    actions = [["union", "?lhs", rhs]]
    return premises, actions, {}    # a rewrite cannot guard vector lengths


def parse_rule_file(filepath: Path) -> Tuple[Optional[ParsedRule], Optional[str]]:
    """Parse a single rule file. Returns (rule, error_reason). On success
    error_reason is None; on failure rule is None and error_reason is a
    human-readable classification (never "parse failure")."""
    src = filepath.read_text()
    leading_comment = _extract_leading_comment(src)

    try:
        forms = parse_top_forms(src)
    except ValueError as exc:
        return None, f"malformed-sexp ({exc})"

    rule_form = None
    rule_kind = None
    for form in forms:
        if isinstance(form, list) and form:
            if form[0] == "rule":
                rule_form = form
                rule_kind = "rule"
                break
            if form[0] == "rewrite":
                rule_form = form
                rule_kind = "rewrite"
                break

    if rule_form is None:
        return None, "no-rule-form"

    if rule_kind == "rule":
        parsed = _parse_rule_form(rule_form)
    else:
        parsed = _parse_rewrite_form(rule_form)
    if parsed is None:
        return None, f"malformed-or-unsupported-{rule_kind}-form (premises must be (= ?var term) or (= N (vec-length ?v)))"
    premises, actions, vec_lengths = parsed

    # First, peel any vec-shape premises out of `premises` into the
    # inline map: `(= ?v (vec-push ...))` premises bind a vec name to
    # a literal vec, which is *not* a BitVec value. Inlining lets the
    # translator see the vec literal at each use site.
    vec_inline_map: Dict[str, Any] = {}  # ?name → vec-shape expr to inline
    pruned_premises: List[Tuple[str, Any]] = []
    for var, expr in premises:
        if _is_vec_shape(expr):
            vec_inline_map[var] = expr
        else:
            pruned_premises.append((var, expr))
    # Apply inlining within remaining premises so a comb_X that refers
    # to a vec-bound ?v sees the literal vec.
    premises = [(v, _inline_vec_refs(e, vec_inline_map)) for v, e in pruned_premises]

    # Collect `(let ?x expr)` bindings — treat as additional premises.
    # Egglog's `let` allows bare (no leading `?`) names. Normalize all
    # let-bound names to the `?name` form and rewrite later references
    # to the bare form into `?name` so the type/translator pipeline
    # sees a uniform variable namespace.
    #
    # Special case: let-bindings whose RHS is a vec expression
    # (vec-push / vec-of / vec-empty) get inlined at point of use
    # instead of becoming a Z3 equality, because vecs aren't BV values
    # — they're "shape" containers for the comb_X arg slot.
    union_targets: List[Tuple[str, Any]] = []  # list of (lhs_var, rhs_expr)
    let_bindings: List[Tuple[str, Any]] = []
    bare_let_names: Dict[str, str] = {}  # bare → "?bare"
    for act in actions:
        if not isinstance(act, list) or not act:
            continue
        head = act[0]
        if head == "let" and len(act) == 3 and isinstance(act[1], str):
            raw_name = act[1]
            if raw_name.startswith("?"):
                norm_name = raw_name
            else:
                norm_name = "?" + raw_name
                bare_let_names[raw_name] = norm_name
            # Rewrite the expression body so references to earlier
            # bare let-binding names get the leading `?`, and any
            # earlier vec-shape let-bindings get inlined.
            expr_rewritten = _rewrite_bare_refs(act[2], bare_let_names)
            expr_rewritten = _inline_vec_refs(expr_rewritten, vec_inline_map)
            if _is_vec_shape(expr_rewritten):
                vec_inline_map[norm_name] = expr_rewritten
            else:
                let_bindings.append((norm_name, expr_rewritten))
            continue
        if head == "union" and len(act) == 3 and isinstance(act[1], str):
            lhs_name = act[1]
            if not lhs_name.startswith("?"):
                lhs_name = bare_let_names.get(lhs_name, "?" + lhs_name)
            rhs_rewritten = _rewrite_bare_refs(act[2], bare_let_names)
            rhs_rewritten = _inline_vec_refs(rhs_rewritten, vec_inline_map)
            union_targets.append((lhs_name, rhs_rewritten))
            continue
        # Other actions (panic, set, subsume, ...) — ignored for verification.

    if not union_targets:
        return None, "no-union-action"
    if len(union_targets) > 1:
        return None, "multi-union"

    # Resolve let bindings: a let `(let ?x expr)` may reference earlier
    # lets. Append them to premises in order so the typing pass sees them.
    full_premises = list(premises) + list(let_bindings)

    lhs_var, rhs_expr = union_targets[0]

    annotated: List[int] = []
    for _, p_expr in full_premises:
        _walk_collect_widths(p_expr, annotated)
    _walk_collect_widths(rhs_expr, annotated)

    rule = ParsedRule(
        name=filepath.stem,
        description=" ".join(line for line in leading_comment.splitlines() if line)[:300],
        leading_comment=leading_comment,
        body_text=src,
        premises=full_premises,
        lhs_var=lhs_var,
        rhs_expr=rhs_expr,
        annotated_widths=annotated,
        vec_lengths=vec_lengths,
    )
    return rule, None


# --------------------------------------------------------------------------
# Banned-op / banned-phrase gates.
# --------------------------------------------------------------------------


def check_banned_op(body_text: str) -> Optional[str]:
    for op in BANNED_OPS:
        # Match both `seq.firreg` (MLIR canonical) and `seq_firreg`
        # (egglog symbol form, since `.` is not a legal egglog identifier
        # character — DialEgg's eggify pass rewrites the dialect separator
        # to `_`). Return the canonical dotted name so error messages stay
        # consistent regardless of which spelling triggered the match.
        if op in body_text or op.replace(".", "_") in body_text:
            return op
    return None


def check_banned_phrase(leading_comment: str) -> Optional[str]:
    lc = leading_comment.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lc:
            return phrase
    return None


# --------------------------------------------------------------------------
# Type inference: map every ?var → bitwidth.
# --------------------------------------------------------------------------

# Operators whose result width equals their explicit `type` argument
# (a `(IN)` or `(Int N)` literal sitting at a fixed position).
_OP_RESULT_TYPE_AT: Dict[str, int] = {
    "comb_add": -1,      # last positional
    "comb_sub": -1,
    "comb_mul": -1,
    "comb_and": -1,
    "comb_or": -1,
    "comb_xor": -1,
    "comb_mux": -1,
    "comb_shl": -1,
    "comb_shru": -1,
    "comb_shrs": -1,
    "comb_extract": -1,
    "comb_concat": -1,
    "comb_replicate": -1,
}

# comb_icmp returns I1 regardless of its `type` slot.
# comb_mux's cond (positional 1) is I1.


# Type constructors of the base template that are NOT signless bit vectors.
# The proof model only knows signless integers; any of these in a type slot
# must stop the proof instead of falling back to a default width.
UNSUPPORTED_TYPE_HEADS = frozenset({
    "SInt", "UInt", "OtherInt", "F16", "F32", "F64", "F80", "F128", "TF32", "Index", "None",
    "Complex", "Tuple", "Function", "Vector", "RankedTensor", "UnrankedTensor", "OpaqueType", "type-of",
})


class UnsupportedType(Exception):
    pass


def _type_of_annotation(expr: Any, env: Optional["TypeEnv"] = None) -> Optional[int]:
    """Return the bitwidth of an `(IN)` or `(Int N)` annotation. If `expr`
    is a `?var` that was resolved as a type-slot variable in `env`, return
    its inferred width; otherwise None. A type the proof model cannot
    represent (signed/unsigned, floating, aggregate, opaque) raises
    UnsupportedType -- it is never given a default width."""
    if isinstance(expr, list) and expr and isinstance(expr[0], str) and expr[0] in UNSUPPORTED_TYPE_HEADS:
        raise UnsupportedType(f"unsupported type {sexp_to_str(expr)}")
    if isinstance(expr, list) and len(expr) == 1 and isinstance(expr[0], str):
        m = re.fullmatch(r"I(\d+)", expr[0])
        if m:
            return int(m.group(1))
    if isinstance(expr, list) and len(expr) == 2 and expr[0] == "Int" and isinstance(expr[1], int):
        return expr[1]
    if isinstance(expr, str) and expr.startswith("?") and env is not None:
        return env.type_var_width.get(expr)
    return None


class TypeEnv:
    """Per-rule type environment. Maps `?var` → bitwidth.

    Unification: when a variable's width is constrained twice, the two
    constraints must agree (else the rule's type system is over-specified
    and we record a conflict — typically harmless, just a warning).
    """

    def __init__(self) -> None:
        self.widths: Dict[str, int] = {}
        self.conflicts: List[str] = []
        # Per-vec-variable: highest `vec-get(?ops, N)` index seen plus the
        # element width inferred from the vec's containing op (or from a
        # vec-of/vec-push literal).
        self.vec_max_index: Dict[str, int] = {}
        self.vec_elem_width: Dict[str, int] = {}
        self.vec_lengths: Dict[str, int] = {}
        # When a variable appears in a Type slot (e.g.
        # `comb_add(a, ops, ?t)`), its identity is a CIRCT type, not a BV
        # value. Track the inferred bit-width of that *type* separately
        # from the BV variable bindings.
        self.type_var_width: Dict[str, int] = {}

    def bind(self, var: str, width: int) -> None:
        if var in self.widths:
            if self.widths[var] != width:
                self.conflicts.append(f"{var}: {self.widths[var]} vs {width}")
        else:
            self.widths[var] = width

    def bind_vec_elem(self, var: str, width: int) -> None:
        if var in self.vec_elem_width:
            if self.vec_elem_width[var] != width:
                self.conflicts.append(f"vec {var}: elem {self.vec_elem_width[var]} vs {width}")
        else:
            self.vec_elem_width[var] = width

    def note_vec_index(self, var: str, idx: int) -> None:
        if var not in self.vec_max_index or idx > self.vec_max_index[var]:
            self.vec_max_index[var] = idx

    def bind_type_var(self, var: str, width: int) -> None:
        if var in self.type_var_width:
            if self.type_var_width[var] != width:
                self.conflicts.append(
                    f"type-var {var}: {self.type_var_width[var]} vs {width}"
                )
        else:
            self.type_var_width[var] = width


def _infer_op_widths(expr: Any, env: TypeEnv, expected_width: Optional[int]) -> None:
    """Walk `expr`. If `expected_width` is provided, the expression itself
    is constrained to that width; that constraint propagates inward to
    operands per op semantics."""
    if isinstance(expr, str):
        if expr.startswith("?") and expected_width is not None:
            env.bind(expr, expected_width)
        return
    if isinstance(expr, int):
        return
    if not isinstance(expr, list) or not expr:
        return

    head = expr[0]
    if not isinstance(head, str):
        # Nested calls in head slot — shouldn't happen for our rules.
        for child in expr:
            _infer_op_widths(child, env, None)
        return

    # Annotation atom — already handled by the parent.
    if len(expr) == 1 and re.fullmatch(r"I\d+", head):
        return
    if head == "Int":
        return

    # Per-op typing.
    if head in {"comb_add", "comb_sub", "comb_mul", "comb_and", "comb_or", "comb_xor"}:
        # (comb_X first ops type)
        if len(expr) >= 4:
            type_w = _type_of_annotation(expr[3], env)
            if type_w is None and expected_width is not None:
                # The whole expression's result is `expected_width`, and
                # for these ops result-width == operand-width.
                type_w = expected_width
                # Also bind the type-slot var so siblings see it.
                if isinstance(expr[3], str) and expr[3].startswith("?"):
                    env.bind_type_var(expr[3], type_w)
            if type_w is not None:
                _infer_op_widths(expr[1], env, type_w)
                _infer_op_widths(expr[2], env, type_w)  # the vec elements are also type_w
                if isinstance(expr[2], str) and expr[2].startswith("?"):
                    env.bind_vec_elem(expr[2], type_w)
                return
        for child in expr[1:]:
            _infer_op_widths(child, env, None)
        return

    if head == "comb_mux":
        # (comb_mux cond t f type)
        if len(expr) >= 5:
            type_w = _type_of_annotation(expr[4], env)
            cond_w = 1
            _infer_op_widths(expr[1], env, cond_w)
            if type_w is None and expected_width is not None:
                type_w = expected_width
                if isinstance(expr[4], str) and expr[4].startswith("?"):
                    env.bind_type_var(expr[4], type_w)
            if type_w is not None:
                _infer_op_widths(expr[2], env, type_w)
                _infer_op_widths(expr[3], env, type_w)
                return
        for child in expr[1:]:
            _infer_op_widths(child, env, None)
        return

    if head == "comb_icmp":
        # (comb_icmp a b pred_attr type) — result is always I1. The
        # `type` slot in our dialect carries the result type, which
        # is I1, so if the slot is a `?var` bind it to I1.
        if len(expr) >= 5:
            if isinstance(expr[4], str) and expr[4].startswith("?"):
                env.bind_type_var(expr[4], 1)
            for child in expr[1:3]:
                _infer_op_widths(child, env, None)
            return
        for child in expr[1:3]:
            _infer_op_widths(child, env, None)
        return

    if head == "comb_extract":
        # (comb_extract src attr type) — result is `type`, src width is
        # unknown (a strict subset must include the extracted slice).
        if len(expr) >= 4:
            _infer_op_widths(expr[1], env, None)
            type_w = _type_of_annotation(expr[3], env)
            if type_w is None and expected_width is not None:
                type_w = expected_width
                if isinstance(expr[3], str) and expr[3].startswith("?"):
                    env.bind_type_var(expr[3], type_w)
            # Don't propagate `type_w` to `src` — they're different widths.
            return
        for child in expr[1:]:
            _infer_op_widths(child, env, None)
        return

    if head == "comb_concat":
        # (comb_concat high vec type) — result width is `type`, but the
        # operand widths sum to `type`. We can't decompose without
        # knowing each operand's width, so leave operands unconstrained.
        for child in expr[1:]:
            _infer_op_widths(child, env, None)
        return

    if head == "comb_replicate":
        # (comb_replicate src type) — result is `type`, src is unknown.
        if len(expr) >= 3:
            _infer_op_widths(expr[1], env, None)
        return

    if head in {"comb_shl", "comb_shru", "comb_shrs"}:
        # (comb_shX value amount type) — both same width as type.
        if len(expr) >= 4:
            type_w = _type_of_annotation(expr[3], env)
            if type_w is None and expected_width is not None:
                type_w = expected_width
                if isinstance(expr[3], str) and expr[3].startswith("?"):
                    env.bind_type_var(expr[3], type_w)
            if type_w is not None:
                _infer_op_widths(expr[1], env, type_w)
                _infer_op_widths(expr[2], env, type_w)
                return
        for child in expr[1:]:
            _infer_op_widths(child, env, None)
        return

    if head == "hw_constant":
        # (hw_constant (NamedAttr "value" (IntegerAttr V T)) T)
        if len(expr) >= 3:
            type_w = _type_of_annotation(expr[2], env)
            if type_w is None and expected_width is not None:
                if isinstance(expr[2], str) and expr[2].startswith("?"):
                    env.bind_type_var(expr[2], expected_width)
                    type_w = expected_width
            if type_w is not None and expected_width is not None and type_w != expected_width:
                env.conflicts.append(
                    f"hw_constant: annotated {type_w} but expected {expected_width}"
                )
        return

    if head == "vec-get":
        # (vec-get ?ops N) — record the index, and tie ?elem width to
        # the surrounding type if the parent passed `expected_width`.
        if len(expr) >= 3:
            ops_var = expr[1]
            idx = expr[2]
            if isinstance(ops_var, str) and ops_var.startswith("?") and isinstance(idx, int):
                env.note_vec_index(ops_var, idx)
                if expected_width is not None:
                    env.bind_vec_elem(ops_var, expected_width)
        return

    if head in {"vec-push", "vec-empty", "vec-of"}:
        # `(vec-push V e)` — element width matches expected_width when
        # known (vec is being passed to a typed op slot).
        for child in expr[1:]:
            _infer_op_widths(child, env, expected_width)
        return

    # Unknown / unmodeled op: don't propagate widths but still descend
    # so any nested `?var` references with explicit annotations bind.
    for child in expr[1:]:
        _infer_op_widths(child, env, None)


def infer_types(rule: ParsedRule) -> TypeEnv:
    env = TypeEnv()
    env.vec_lengths = dict(getattr(rule, "vec_lengths", {}) or {})
    # Premises of the form (= ?var expr): the ?var inherits expr's type
    # when we can deduce it. We do a fixpoint: keep walking until no
    # new bindings appear.
    for _ in range(8):
        before = (dict(env.widths), dict(env.vec_elem_width), dict(env.vec_max_index))
        for var, expr in rule.premises:
            # `?var == expr`. Push expected_width = width(?var) into expr.
            w = env.widths.get(var)
            if w is not None:
                _infer_op_widths(expr, env, w)
            else:
                # Try to infer from expr's structure.
                _infer_op_widths(expr, env, None)
                inferred = _expr_result_width(expr, env)
                if inferred is not None:
                    env.bind(var, inferred)
        _infer_op_widths(rule.rhs_expr, env, env.widths.get(rule.lhs_var))
        # Vec elements pick up width from their parent op annotations
        # already; nothing extra to do.
        after = (dict(env.widths), dict(env.vec_elem_width), dict(env.vec_max_index))
        if before == after:
            break
    return env


def _expr_result_width(expr: Any, env: TypeEnv) -> Optional[int]:
    """Best-effort: return the bitwidth of an expression's result."""
    if isinstance(expr, str):
        if expr.startswith("?"):
            return env.widths.get(expr)
        return None
    if isinstance(expr, int):
        return None
    if not isinstance(expr, list) or not expr:
        return None
    head = expr[0]
    if not isinstance(head, str):
        return None

    if head == "comb_icmp":
        return 1
    if head in {"comb_add", "comb_sub", "comb_mul", "comb_and", "comb_or",
                "comb_xor", "comb_mux", "comb_shl", "comb_shru", "comb_shrs"}:
        # type slot is the last arg (positions: head, op1, op2, ..., type).
        if len(expr) >= 2:
            return _type_of_annotation(expr[-1], env)
        return None
    if head == "comb_extract":
        if len(expr) >= 4:
            return _type_of_annotation(expr[3], env)
        return None
    if head == "comb_concat":
        if len(expr) >= 4:
            return _type_of_annotation(expr[3], env)
        return None
    if head == "comb_replicate":
        if len(expr) >= 3:
            return _type_of_annotation(expr[2], env)
        return None
    if head == "hw_constant":
        if len(expr) >= 3:
            return _type_of_annotation(expr[2], env)
        return None
    if head == "vec-get":
        if len(expr) >= 3 and isinstance(expr[1], str) and expr[1].startswith("?"):
            return env.vec_elem_width.get(expr[1])
        return None
    return None


# --------------------------------------------------------------------------
# Z3 translation.
# --------------------------------------------------------------------------


class TranslationError(Exception):
    """Recoverable: triggers UNKNOWN: <reason> for the current rule."""


class WidthIncompatible(TranslationError):
    """The rule's type constraints are incoherent at the chosen sweep
    width (e.g., comb_concat at width 1 with a 1-bit `high` slot leaves
    no room for the low vec). The sweep skips this width; if no width
    is compatible the rule is reported UNKNOWN: width-incompatible."""


class Z3Translator:
    """Translate an egglog s-expression to a Z3 BitVec expression, given
    a TypeEnv and a chosen `default_width` for any variable the type
    environment couldn't pin down."""

    def __init__(self, env: TypeEnv, default_width: int):
        self.env = env
        self.default_width = default_width
        # Variable cache: clean_name → BitVec.
        self.bv_vars: Dict[str, z3.BitVecRef] = {}
        # Uninterpreted-function cache: (op_name, signature) → FuncDecl.
        self.uf_cache: Dict[Tuple[str, Tuple[int, ...], int], z3.FuncDeclRef] = {}
        # Per-vec UF: (vec_var, elem_width) → FuncDecl[Int → BV[w]].
        self.vec_funcs: Dict[Tuple[str, int], z3.FuncDeclRef] = {}
        # Fresh-id counter for non-?-bound bits.
        self._fresh = 0

    # ---- helpers ----

    def fresh(self) -> int:
        self._fresh += 1
        return self._fresh

    def _width_of_var(self, var: str) -> int:
        return self.env.widths.get(var, self.default_width)

    def _get_var(self, var: str, width: int) -> z3.BitVecRef:
        key = f"{var}@{width}"
        if key not in self.bv_vars:
            clean = var.lstrip("?")
            self.bv_vars[key] = z3.BitVec(f"{clean}_{width}", width)
        return self.bv_vars[key]

    def _vec_func(self, vec_var: str, elem_width: int) -> z3.FuncDeclRef:
        key = (vec_var, elem_width)
        if key not in self.vec_funcs:
            clean = vec_var.lstrip("?")
            self.vec_funcs[key] = z3.Function(
                f"vec_{clean}_w{elem_width}", z3.IntSort(), z3.BitVecSort(elem_width)
            )
        return self.vec_funcs[key]

    def _uf(self, op: str, arg_widths: Tuple[int, ...], result_width: int) -> z3.FuncDeclRef:
        key = (op, arg_widths, result_width)
        if key not in self.uf_cache:
            sorts = [z3.BitVecSort(w) for w in arg_widths] + [z3.BitVecSort(result_width)]
            self.uf_cache[key] = z3.Function(
                f"uf_{op}_" + "x".join(str(w) for w in arg_widths) + f"_to_{result_width}", *sorts
            )
        return self.uf_cache[key]

    # ---- vec utilities ----

    def _vec_elements(self, vec_expr: Any, elem_width: int) -> Optional[List[z3.BitVecRef]]:
        """If vec_expr is a literal (vec-push (vec-empty) ...) chain or a
        `(vec-of ...)`, return the explicit element list. Otherwise None
        (caller should fall back to the per-index UF)."""
        if isinstance(vec_expr, list) and vec_expr:
            head = vec_expr[0]
            if head == "vec-empty":
                return []
            if head == "vec-of":
                # (vec-of e1 e2 ...)
                return [self.translate(e, elem_width) for e in vec_expr[1:]]
            if head == "vec-push" and len(vec_expr) == 3:
                inner = self._vec_elements(vec_expr[1], elem_width)
                if inner is None:
                    return None
                return inner + [self.translate(vec_expr[2], elem_width)]
        return None

    def _vec_index(self, vec_expr: Any, idx: int, elem_width: int) -> z3.BitVecRef:
        """Resolve `(vec-get vec_expr idx)` to a Z3 BV[elem_width]."""
        literal = self._vec_elements(vec_expr, elem_width)
        if literal is not None:
            if 0 <= idx < len(literal):
                return literal[idx]
            raise TranslationError(f"vec-get-out-of-range index={idx} on literal vec of length {len(literal)}")
        if isinstance(vec_expr, str) and vec_expr.startswith("?"):
            n = getattr(self.env, "vec_lengths", {}).get(vec_expr)
            if n is None:
                raise TranslationError(f"vector {vec_expr} has no exact (= N (vec-length {vec_expr})) guard")
            if not 0 <= idx < n:
                raise TranslationError(f"vec-get-out-of-range index={idx} on {vec_expr} of length {n}")
            fn = self._vec_func(vec_expr, elem_width)
            return fn(z3.IntVal(idx))
        raise TranslationError(f"unsupported-vec-shape {sexp_to_str(vec_expr)[:80]}")

    # ---- main translation ----

    def translate(self, expr: Any, expected_width: Optional[int] = None) -> z3.BitVecRef:
        # Variable.
        if isinstance(expr, str):
            if expr.startswith("?"):
                w = self.env.widths.get(expr, expected_width or self.default_width)
                return self._get_var(expr, w)
            # Bare symbol like `true`/`false` — uncommon in our rules.
            raise TranslationError(f"bare-symbol {expr}")

        if isinstance(expr, int):
            w = expected_width or self.default_width
            return z3.BitVecVal(expr, w)

        if not isinstance(expr, list) or not expr:
            raise TranslationError(f"empty-expr {expr}")

        head = expr[0]
        if not isinstance(head, str):
            raise TranslationError(f"nested-head {head}")

        # Annotations: caller shouldn't translate these; if reached, treat
        # as a 1-width 1-value sentinel.
        if len(expr) == 1 and re.fullmatch(r"I\d+", head):
            raise TranslationError(f"annotation-as-value {head}")

        # ---- comb ops ----

        if head in {"comb_add", "comb_sub", "comb_mul", "comb_and",
                    "comb_or", "comb_xor"}:
            if len(expr) < 4:
                raise TranslationError(f"{head}-bad-arity {len(expr)}")
            type_w = _type_of_annotation(expr[3], self.env)
            if type_w is None:
                # Fall back to expected_width or default.
                type_w = expected_width or self.default_width
            type_w = min(type_w, SWEEP_CAP)
            a = self.translate(expr[1], type_w)
            vec = expr[2]
            elements = self._vec_elements(vec, type_w)
            if elements is None:
                # Vec is a variable bound by premises: its elements come
                # from the per-index UF.
                if isinstance(vec, str) and vec.startswith("?"):
                    # Exactly the guarded length -- never reconstructed from
                    # the indices that happen to be read (0 elements allowed).
                    n = getattr(self.env, "vec_lengths", {}).get(vec)
                    if n is None:
                        raise TranslationError(f"vector {vec} has no exact (= N (vec-length {vec})) guard")
                    fn = self._vec_func(vec, type_w)
                    elements = [fn(z3.IntVal(i)) for i in range(n)]
                else:
                    raise TranslationError(f"{head}-unmodeled-vec")
            # n-ary fold.
            if head == "comb_add":
                acc = a
                for e in elements:
                    acc = acc + e
                return acc
            if head == "comb_sub":
                # (comb_sub a [b, c, ...]) is a - sum(rest), but in
                # practice all our rules use sub as binary.
                acc = a
                for e in elements:
                    acc = acc - e
                return acc
            if head == "comb_mul":
                acc = a
                for e in elements:
                    acc = acc * e
                return acc
            if head == "comb_and":
                acc = a
                for e in elements:
                    acc = acc & e
                return acc
            if head == "comb_or":
                acc = a
                for e in elements:
                    acc = acc | e
                return acc
            if head == "comb_xor":
                acc = a
                for e in elements:
                    acc = acc ^ e
                return acc

        if head == "comb_mux":
            if len(expr) < 5:
                raise TranslationError(f"comb_mux-bad-arity {len(expr)}")
            type_w = _type_of_annotation(expr[4], self.env) or (expected_width or self.default_width)
            type_w = min(type_w, SWEEP_CAP)
            cond = self.translate(expr[1], 1)
            t_val = self.translate(expr[2], type_w)
            f_val = self.translate(expr[3], type_w)
            return z3.If(cond == z3.BitVecVal(1, 1), t_val, f_val)

        if head == "comb_icmp":
            if len(expr) < 5:
                raise TranslationError(f"comb_icmp-bad-arity {len(expr)}")
            # (comb_icmp a b (NamedAttr "predicate" (IntegerAttr P t64)) result_type)
            type_w = _type_of_annotation(expr[4], self.env) or 1
            pred_attr = expr[3]
            pred_code = self._icmp_predicate(pred_attr)
            # Operand width = the type slot, but in some cases it's I1.
            # We use the type slot (which is the result-vs-operand depending
            # on dialect version). Look up operand width by inspecting
            # the operands' inferred width via the env.
            a_w = _expr_result_width(expr[1], self.env)
            b_w = _expr_result_width(expr[2], self.env)
            op_w = a_w or b_w or expected_width or self.default_width
            op_w = min(op_w, SWEEP_CAP)
            a = self.translate(expr[1], op_w)
            b = self.translate(expr[2], op_w)
            cmp_bool = self._apply_icmp(pred_code, a, b)
            # Return a 1-bit BV.
            return z3.If(cmp_bool, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))

        if head == "comb_extract":
            if len(expr) < 4:
                raise TranslationError(f"comb_extract-bad-arity {len(expr)}")
            res_w = _type_of_annotation(expr[3], self.env) or (expected_width or self.default_width)
            res_w = min(res_w, SWEEP_CAP)
            # Try to get a concrete lowBit. If the attr is symbolic
            # (the rule uses `?attr` as a placeholder), fall through
            # to the unknown-op UF path so both sides of the union
            # share an identical opaque extract.
            try:
                low = self._extract_low_bit(expr[2])
            except TranslationError:
                low = None
            if low is None:
                # UF fallback. Hash the attr expression's textual form
                # into a Z3 BV argument so two calls with the *same*
                # attr expression match, while two calls with different
                # attrs don't.
                src = self.translate(expr[1], None)
                attr_id = self._attr_identity(expr[2])
                fn = self._uf("comb_extract", (src.size(), attr_id[1]), res_w)
                return fn(src, attr_id[0])
            src_w_hint = _expr_result_width(expr[1], self.env) or self.default_width
            src_w_hint = max(src_w_hint, low + res_w)
            src_w_hint = min(src_w_hint, SWEEP_CAP)
            src = self.translate(expr[1], src_w_hint)
            # z3 Extract takes (high, low, bv).
            high = low + res_w - 1
            if high >= src.size():
                raise WidthIncompatible(f"extract-oob hi={high} src_w={src.size()}")
            return z3.Extract(high, low, src)

        if head == "comb_concat":
            if len(expr) < 4:
                raise TranslationError(f"comb_concat-bad-arity {len(expr)}")
            res_w = _type_of_annotation(expr[3], self.env) or (expected_width or self.default_width)
            res_w = min(res_w, SWEEP_CAP)
            # We don't know individual operand widths in general;
            # translate `high` first, then fit the vec elements to
            # match `res_w - high_w`.
            high = self.translate(expr[1], None)
            high_w = high.size()
            if high_w > res_w:
                raise WidthIncompatible(f"concat-high-too-wide high={high_w} res={res_w}")
            vec = expr[2]
            elements: Optional[List[z3.BitVecRef]] = None
            if isinstance(vec, list):
                elements = self._vec_elements(vec, None)
            if elements is None:
                if isinstance(vec, str) and vec.startswith("?"):
                    # Element widths of a matched concat vector are not fixed
                    # by the pattern, so only the guarded empty vector is modeled.
                    n = getattr(self.env, "vec_lengths", {}).get(vec)
                    if n != 0:
                        raise TranslationError(
                            f"comb_concat over variable vector {vec} (length {n}) is unsupported")
                    elements = []
                else:
                    raise TranslationError("comb_concat-vec-not-literal")
            pieces = [high] + elements
            acc = pieces[0]
            for p in pieces[1:]:
                acc = z3.Concat(acc, p)
            if acc.size() != res_w:
                raise WidthIncompatible(f"concat-width-mismatch got={acc.size()} want={res_w}")
            return acc

        if head == "comb_replicate":
            if len(expr) < 3:
                raise TranslationError(f"comb_replicate-bad-arity {len(expr)}")
            res_w = _type_of_annotation(expr[2], self.env) or (expected_width or self.default_width)
            res_w = min(res_w, SWEEP_CAP)
            src_w = _expr_result_width(expr[1], self.env) or 1
            src_w = min(src_w, SWEEP_CAP)
            if res_w % src_w != 0:
                raise WidthIncompatible(f"replicate-bad-ratio res={res_w} src={src_w}")
            n = res_w // src_w
            src = self.translate(expr[1], src_w)
            return z3.RepeatBitVec(n, src)

        if head in {"comb_shl", "comb_shru", "comb_shrs"}:
            if len(expr) < 4:
                raise TranslationError(f"{head}-bad-arity {len(expr)}")
            type_w = _type_of_annotation(expr[3], self.env) or (expected_width or self.default_width)
            type_w = min(type_w, SWEEP_CAP)
            a = self.translate(expr[1], type_w)
            b = self.translate(expr[2], type_w)
            if head == "comb_shl":
                return a << b
            if head == "comb_shru":
                return z3.LShR(a, b)
            return a >> b  # arithmetic right shift in z3.BitVecRef.__rshift__

        if head == "hw_constant":
            if len(expr) < 3:
                raise TranslationError(f"hw_constant-bad-arity {len(expr)}")
            type_w = _type_of_annotation(expr[2], self.env) or (expected_width or self.default_width)
            type_w = min(type_w, SWEEP_CAP)
            val = self._extract_const_value(expr[1])
            if val is None:
                raise TranslationError("hw_constant-no-value")
            return z3.BitVecVal(val, type_w)

        # ---- vec ops ----

        if head == "vec-get":
            if len(expr) < 3:
                raise TranslationError("vec-get-bad-arity")
            elem_w = expected_width or self.default_width
            # Try to recover elem_w from the env if this vec is bound.
            vec_expr = expr[1]
            if isinstance(vec_expr, str) and vec_expr.startswith("?"):
                env_w = self.env.vec_elem_width.get(vec_expr)
                if env_w is not None:
                    elem_w = env_w
            idx = expr[2]
            if not isinstance(idx, int):
                raise TranslationError("vec-get-nonint-index")
            return self._vec_index(vec_expr, idx, elem_w)

        if head == "vec-of":
            # Translating a vec directly is meaningless; the caller must
            # use _vec_elements / _vec_index instead.
            raise TranslationError("vec-of-as-value")
        if head == "vec-push":
            raise TranslationError("vec-push-as-value")
        if head == "vec-empty":
            raise TranslationError("vec-empty-as-value")

        # ---- attribute structures (when they show up as values) ----
        if head in {"NamedAttr", "IntegerAttr", "ArrayAttr"}:
            raise TranslationError(f"attribute-as-value {head}")

        # ---- unknown op → uninterpreted function ----
        # Translate children where possible. Skip any annotation/attr.
        arg_bvs: List[z3.BitVecRef] = []
        arg_widths: List[int] = []
        for child in expr[1:]:
            try:
                bv = self.translate(child, None)
            except TranslationError:
                # If a child can't be translated, we have to bail.
                raise TranslationError(f"unsupported-op-with-untranslatable-child {head}")
            arg_bvs.append(bv)
            arg_widths.append(bv.size())
        result_w = expected_width or self.default_width
        result_w = min(result_w, SWEEP_CAP)
        fn = self._uf(head, tuple(arg_widths), result_w)
        return fn(*arg_bvs)

    # ---- icmp helpers ----

    def _icmp_predicate(self, attr_expr: Any) -> int:
        """Pull the predicate code out of (NamedAttr \"predicate\" (IntegerAttr P T))."""
        if not isinstance(attr_expr, list) or len(attr_expr) < 3:
            raise TranslationError("icmp-bad-pred-attr")
        if attr_expr[0] != "NamedAttr":
            raise TranslationError("icmp-bad-pred-attr-head")
        inner = attr_expr[2]
        if not isinstance(inner, list) or len(inner) < 2 or inner[0] != "IntegerAttr":
            raise TranslationError("icmp-bad-pred-inner")
        if not isinstance(inner[1], int):
            raise TranslationError("icmp-symbolic-pred")
        return inner[1]

    def _apply_icmp(self, pred: int, a: z3.BitVecRef, b: z3.BitVecRef) -> z3.BoolRef:
        # CIRCT comb.ICmpPredicate enum.
        if pred == 0:    # eq
            return a == b
        if pred == 1:    # ne
            return a != b
        if pred == 2:    # slt
            return a < b
        if pred == 3:    # sle
            return a <= b
        if pred == 4:    # sgt
            return a > b
        if pred == 5:    # sge
            return a >= b
        if pred == 6:    # ult
            return z3.ULT(a, b)
        if pred == 7:    # ule
            return z3.ULE(a, b)
        if pred == 8:    # ugt
            return z3.UGT(a, b)
        if pred == 9:    # uge
            return z3.UGE(a, b)
        if pred == 10:   # ceq (case-equal, equivalent to eq when no X/Z)
            return a == b
        if pred == 11:   # cne
            return a != b
        raise TranslationError(f"icmp-unknown-predicate {pred}")

    def _attr_identity(self, attr_expr: Any) -> Tuple[z3.BitVecRef, int]:
        """Build a BV value that uniquely identifies `attr_expr` (by its
        textual form) so two opaque calls with the same attr produce
        the same UF result. Returns (bv_value, width)."""
        # If attr_expr is a ?-var, route through the normal var path
        # (so two refs to the same ?-var yield identical bv values).
        if isinstance(attr_expr, str) and attr_expr.startswith("?"):
            bv = self._get_var(attr_expr + "__attr", 32)
            return bv, 32
        # 32-bit hash of textual form is plenty to separate the handful
        # of distinct attrs in a single rule.
        identity_str = sexp_to_str(attr_expr) if isinstance(attr_expr, list) else str(attr_expr)
        h = abs(hash(identity_str)) & 0xFFFFFFFF
        return z3.BitVecVal(h, 32), 32

    def _extract_low_bit(self, attr_expr: Any) -> int:
        if not isinstance(attr_expr, list):
            raise TranslationError("extract-bad-attr")
        # Either (NamedAttr "lowBit" (IntegerAttr V T)) or directly an int.
        if attr_expr[0] == "NamedAttr" and len(attr_expr) >= 3:
            inner = attr_expr[2]
            if isinstance(inner, list) and len(inner) >= 2 and inner[0] == "IntegerAttr":
                if isinstance(inner[1], int):
                    return inner[1]
        if attr_expr[0] == "IntegerAttr" and len(attr_expr) >= 2 and isinstance(attr_expr[1], int):
            return attr_expr[1]
        raise TranslationError("extract-symbolic-lowbit")

    def _extract_const_value(self, attr_expr: Any) -> Optional[int]:
        if not isinstance(attr_expr, list):
            return None
        if attr_expr[0] == "NamedAttr" and len(attr_expr) >= 3:
            inner = attr_expr[2]
            if isinstance(inner, list) and len(inner) >= 2 and inner[0] == "IntegerAttr":
                if isinstance(inner[1], int):
                    return inner[1]
        if attr_expr[0] == "IntegerAttr" and len(attr_expr) >= 2 and isinstance(attr_expr[1], int):
            return attr_expr[1]
        return None


# --------------------------------------------------------------------------
# Verification driver.
# --------------------------------------------------------------------------


def _select_sweep(rule: ParsedRule) -> List[int]:
    widths = set(DEFAULT_SWEEP)
    for w in rule.annotated_widths:
        widths.add(min(w, SWEEP_CAP))
    return sorted(widths)


def verify_rule(rule: ParsedRule, sweep: Optional[List[int]] = None,
                timeout_ms: int = DEFAULT_RULE_TIMEOUT_MS,
                per_bitwidth_budget: bool = False) -> Tuple[str, str]:
    """Returns (status, detail) where status ∈ {VALID, INVALID, UNKNOWN}.
    detail is a short message for UNKNOWN cases or counterexample for
    INVALID.

    Timeout semantics (`timeout_ms`):
      * Default (total budget): `timeout_ms` is the *total* Z3 budget for
        the whole rule, apportioned across the bitwidth sweep. Each solve
        gets the budget remaining after subtracting the wall time already
        spent on earlier sweep widths, so the rule can never consume more
        than `timeout_ms` of solver time end-to-end.
      * `per_bitwidth_budget=True` (legacy): `timeout_ms` is applied per
        Z3 solve (per sweep width), giving a worst-case per-rule bound of
        `timeout_ms * len(sweep)`. This reproduces the historical behavior
        (`--timeout-ms 10000 --per-bitwidth-budget` == old default).
    """
    if sweep is None:
        sweep = _select_sweep(rule)

    env = infer_types(rule)

    # If env reported conflicts, treat as UNKNOWN: type-conflict.
    if env.conflicts:
        return "UNKNOWN", f"type-conflict ({env.conflicts[0]})"

    last_unknown: Optional[str] = None
    width_incompat: List[str] = []
    n_proven = 0
    # Total-budget bookkeeping: track elapsed Z3 wall time and stop once
    # the rule's whole budget is exhausted.
    rule_start = time.monotonic()
    for w in sweep:
        # Pick a sensible default_width for unconstrained variables in this
        # sweep run.
        w_capped = min(w, SWEEP_CAP)
        if per_bitwidth_budget:
            solve_budget_ms = timeout_ms
        else:
            elapsed_ms = int((time.monotonic() - rule_start) * 1000)
            solve_budget_ms = timeout_ms - elapsed_ms
            if solve_budget_ms <= 0:
                # Whole-rule budget already spent on earlier widths.
                last_unknown = (
                    f"rule-budget-exhausted-before-{w_capped}bit "
                    f"(budget={timeout_ms}ms)"
                )
                break
        try:
            t = Z3Translator(env, default_width=w_capped)
            # Translate premises into Z3 equalities.
            assertions: List[z3.BoolRef] = []
            for var, expr in rule.premises:
                # ?var's width is determined by the env; default if absent.
                w_var = env.widths.get(var, w_capped)
                w_var = min(w_var, SWEEP_CAP)
                bv_var = t._get_var(var, w_var)
                bv_expr = t.translate(expr, w_var)
                if bv_var.size() != bv_expr.size():
                    # Width mismatch — the rule is malformed for this sweep.
                    raise WidthIncompatible(
                        f"premise-width-mismatch {var}: var={bv_var.size()} expr={bv_expr.size()}"
                    )
                assertions.append(bv_var == bv_expr)

            # Translate the union goal.
            lhs_w = env.widths.get(rule.lhs_var, w_capped)
            lhs_w = min(lhs_w, SWEEP_CAP)
            lhs_bv = t._get_var(rule.lhs_var, lhs_w)
            rhs_bv = t.translate(rule.rhs_expr, lhs_w)
            if lhs_bv.size() != rhs_bv.size():
                raise WidthIncompatible(
                    f"union-width-mismatch lhs={lhs_bv.size()} rhs={rhs_bv.size()}"
                )

            solver = z3.Solver()
            solver.set("timeout", solve_budget_ms)
            for a in assertions:
                solver.add(a)
            solver.add(lhs_bv != rhs_bv)
            result = solver.check()
            if result == z3.sat:
                model = solver.model()
                cex = {str(d): model[d] for d in model.decls()}
                cex_short = ", ".join(f"{k}={v}" for k, v in list(cex.items())[:6])
                return "INVALID", f"counterexample-at-{w_capped}bit ({cex_short})"
            if result == z3.unknown:
                reason = solver.reason_unknown()
                last_unknown = f"z3-unknown-at-{w_capped}bit ({reason})"
                continue
            # unsat → valid at this width; continue sweep.
            n_proven += 1
        except WidthIncompatible as exc:
            # Rule's type constraints don't admit this width — skip,
            # but require at least one width to succeed.
            width_incompat.append(f"{w_capped}bit: {exc}")
            continue
        except TranslationError as exc:
            return "UNKNOWN", str(exc)
        except Exception as exc:
            return "UNKNOWN", f"z3-exception ({type(exc).__name__}: {exc})"

    if n_proven == 0:
        if last_unknown is not None:
            return "UNKNOWN", last_unknown
        if width_incompat:
            return "UNKNOWN", f"width-incompatible ({width_incompat[0]})"
        return "UNKNOWN", "no-sweep-width-checked"
    if last_unknown is not None and n_proven == 0:
        return "UNKNOWN", last_unknown
    return "VALID", f"sweep={sweep} proven={n_proven}"


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------


def _classify_file(
    filepath: Path,
    *,
    phrase_lint: bool,
    sweep_override: Optional[List[int]] = None,
    timeout_ms: int = DEFAULT_RULE_TIMEOUT_MS,
    per_bitwidth_budget: bool = False,
) -> Tuple[str, str, Optional[ParsedRule]]:
    """Run the full pipeline on one file. Returns (status, detail, rule_or_None).

    status is one of: VALID, INVALID, UNKNOWN, REJECTED.
    detail is the human-readable suffix.
    """
    src = filepath.read_text()
    leading = _extract_leading_comment(src)

    # Banned-op gate runs first (criterion 6 — "Before any Z3 work").
    banned_op = check_banned_op(src)
    if banned_op is not None:
        return "REJECTED", f"banned-op {banned_op}", None

    if phrase_lint:
        banned_phrase = check_banned_phrase(leading)
        if banned_phrase is not None:
            return "REJECTED", f'banned-phrase "{banned_phrase}"', None

    rule, parse_err = parse_rule_file(filepath)
    if rule is None:
        # Per criterion 1 we must NEVER emit "parse failure" — name the
        # shape we couldn't classify.
        return "UNKNOWN", f"unsupported-rule-shape {parse_err}", None

    status, detail = verify_rule(
        rule,
        sweep=sweep_override,
        timeout_ms=timeout_ms,
        per_bitwidth_budget=per_bitwidth_budget,
    )
    return status, detail, rule


def _format_line(name: str, status: str, detail: str, fmt: str = "plain") -> str:
    label = status
    if status in {"UNKNOWN", "REJECTED"} and detail:
        label = f"{status}: {detail}"
    elif status == "INVALID" and detail:
        label = f"{status}: {detail}"
    elif status == "VALID":
        label = "VALID"
    return f"{name:38s} {label}"


def _resolve_timeout_ms(args: argparse.Namespace) -> int:
    """Resolve the per-rule solver timeout (ms) from CLI flags.

    `--timeout-ms` wins if given; otherwise `--timeout-s` (seconds) is
    converted; otherwise the default 60 s/rule bound is used."""
    if getattr(args, "timeout_ms", None) is not None:
        return int(args.timeout_ms)
    if getattr(args, "timeout_s", None) is not None:
        return int(round(float(args.timeout_s) * 1000))
    return DEFAULT_RULE_TIMEOUT_MS


def cmd_all(args: argparse.Namespace) -> int:
    rules_dir = Path(args.rules_dir) if args.rules_dir else PROJECT_ROOT / "rules"
    rule_files = sorted(rules_dir.glob("*.egg"))
    if not rule_files:
        print(f"No rules found in {rules_dir}", file=sys.stderr)
        return 1

    timeout_ms = _resolve_timeout_ms(args)
    per_bw = bool(getattr(args, "per_bitwidth_budget", False))

    # Print the exact timeout constraint so the "per rule" bound is a
    # reported value rather than folklore (criterion 3).
    if per_bw:
        # In per-bitwidth mode the worst-case per-rule bound depends on the
        # rule's sweep length; report the longest sweep across the pool so
        # the worst-case is unambiguous.
        max_sweep_len = 1
        for f in rule_files:
            r, _err = parse_rule_file(f)
            if r is not None:
                max_sweep_len = max(max_sweep_len, len(_select_sweep(r)))
        worst_case_ms = timeout_ms * max_sweep_len
        print(
            f"Timeout mode: per-bitwidth budget = {timeout_ms} ms/solve; "
            f"worst-case per-rule bound = {worst_case_ms} ms "
            f"({worst_case_ms / 1000:.0f} s = {timeout_ms} ms x "
            f"{max_sweep_len} sweep widths)"
        )
    else:
        print(
            f"Timeout mode: total per-rule budget = {timeout_ms} ms "
            f"({timeout_ms / 1000:.0f} s/rule), apportioned across the "
            f"bitwidth sweep"
        )
    print(f"Per-rule bound: {timeout_ms / 1000 if not per_bw else worst_case_ms / 1000:.0f} s")
    print("=" * 70)

    counts = {"VALID": 0, "INVALID": 0, "UNKNOWN": 0, "REJECTED": 0}
    for f in rule_files:
        t0 = time.monotonic()
        status, detail, _ = _classify_file(
            f,
            phrase_lint=not args.no_phrase_lint,
            timeout_ms=timeout_ms,
            per_bitwidth_budget=per_bw,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000
        counts[status] = counts.get(status, 0) + 1
        # Per-rule wall-clock time appended after the status label so the
        # canonical name/status columns stay intact.
        print(f"{_format_line(f.stem, status, detail)}  [{elapsed_ms:8.1f} ms]")

    print()
    print("=" * 70)
    print(
        f"Summary: {counts.get('VALID', 0)} VALID, "
        f"{counts.get('INVALID', 0)} INVALID, "
        f"{counts.get('UNKNOWN', 0)} UNKNOWN, "
        f"{counts.get('REJECTED', 0)} REJECTED "
        f"(total {len(rule_files)})"
    )
    return 0


def cmd_one(args: argparse.Namespace) -> int:
    filepath = Path(args.rule_file)
    if not filepath.exists():
        print(f"No such file: {filepath}", file=sys.stderr)
        return 1

    sweep_override = None
    if args.bitwidth is not None:
        sweep_override = [min(args.bitwidth, SWEEP_CAP)]

    timeout_ms = _resolve_timeout_ms(args)
    per_bw = bool(getattr(args, "per_bitwidth_budget", False))

    t0 = time.monotonic()
    status, detail, rule = _classify_file(
        filepath,
        phrase_lint=not args.no_phrase_lint,
        sweep_override=sweep_override,
        timeout_ms=timeout_ms,
        per_bitwidth_budget=per_bw,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000

    if rule is not None:
        print(f"Rule: {rule.name}")
        if rule.description:
            print(f"Description: {rule.description}")
        print(f"Premises ({len(rule.premises)}):")
        for var, expr in rule.premises:
            print(f"  {var} == {sexp_to_str(expr)[:120]}")
        print(f"Union: {rule.lhs_var} == {sexp_to_str(rule.rhs_expr)[:200]}")
        env = infer_types(rule)
        if env.widths:
            print("Inferred widths: " + ", ".join(f"{k}={v}" for k, v in sorted(env.widths.items())))
        sweep_used = sweep_override if sweep_override else _select_sweep(rule)
        print(f"Sweep: {sweep_used}")
        if per_bw:
            bound_s = timeout_ms * len(sweep_used) / 1000
            print(
                f"Timeout: per-bitwidth budget {timeout_ms} ms/solve; "
                f"per-rule bound {bound_s:.0f} s "
                f"({timeout_ms} ms x {len(sweep_used)} widths)"
            )
        else:
            print(
                f"Timeout: total per-rule budget {timeout_ms} ms "
                f"({timeout_ms / 1000:.0f} s/rule)"
            )
        print()

    line = _format_line(filepath.stem, status, detail)
    print(f"{line}  [{elapsed_ms:.1f} ms]")
    # Exit 0 for VALID, REJECTED, UNKNOWN; non-zero only for INVALID
    # (so CI can decide what to fail on). The --all command always
    # exits 0 — its job is to emit one line per rule, not to gate.
    if status == "INVALID":
        return 2
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--all", action="store_true",
                   help="Verify all rules in PROJECT_ROOT/rules/ (or --rules-dir).")
    p.add_argument("--rules-dir", default=None,
                   help="Override the rules directory (default: <project>/rules).")
    p.add_argument("--no-phrase-lint", action="store_true",
                   help="Disable the banned-phrase comment lint.")
    p.add_argument("--bitwidth", type=int, default=None,
                   help="Single-shot bitwidth override (only honoured in single-file mode).")
    p.add_argument("--timeout-ms", type=int, default=None,
                   help=("Per-rule Z3 solver budget in milliseconds "
                         f"(default: {DEFAULT_RULE_TIMEOUT_MS} ms = "
                         f"{DEFAULT_RULE_TIMEOUT_MS // 1000} s/rule). In the "
                         "default total-budget mode this is the whole-rule "
                         "budget apportioned across the bitwidth sweep."))
    p.add_argument("--timeout-s", type=float, default=None,
                   help=("Per-rule Z3 solver budget in seconds (alias for "
                         "--timeout-ms; --timeout-ms wins if both given)."))
    p.add_argument("--per-bitwidth-budget", action="store_true",
                   help=("Apply the timeout per Z3 solve (per sweep width) "
                         "instead of as a total per-rule budget. Worst-case "
                         "per-rule bound becomes timeout x len(sweep). "
                         "Use `--timeout-ms 10000 --per-bitwidth-budget` to "
                         "reproduce the legacy 10 s-per-bitwidth behavior."))
    p.add_argument("rule_file", nargs="?",
                   help="Rule file to verify (omit when using --all).")
    args = p.parse_args()

    if args.all:
        return cmd_all(args)
    if not args.rule_file:
        p.print_usage()
        print("error: provide a rule file or --all", file=sys.stderr)
        return 1
    return cmd_one(args)


if __name__ == "__main__":
    sys.exit(main())
