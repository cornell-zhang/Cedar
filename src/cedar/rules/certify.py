# SPDX-License-Identifier: Apache-2.0
"""Exact-width, content-hashed rule certificates and the proof admission gate.

Admission contract (paper: verified-only rule pool, concrete-width guards):

1. *Quarantine*: every generated or reused rule is untrusted text.
2. *Parse / type-check*: the rule must contain exactly one ``rule``/``rewrite``
   form with one ``union`` action, no banned (stateful) ops, and at most one
   type variable (or none, if all types are concrete). Premises are
   ``(= ?var term)`` (reversed forms are normalized) or exact length guards
   ``(= N (vec-length ?v))``; every matched vector has exactly one guard and is
   proved as exactly ``N`` elements, with literal reads in ``[0, N)``.
3. *Specialize*: for each bit width ``w`` present in the target design, the
   rule's type variable is replaced by the concrete egglog type (``(I8)``,
   ``(Int 12)``, ...). The specialized instance can only match e-nodes of that
   exact width — this is the application guard.
4. *Prove*: Z3 proves ``premises => lhs == rhs`` for the specialized instance at
   exactly ``w`` bits (no truncation, no inference from other widths). Only
   ``PROVED`` (UNSAT of the negation) admits the instance; ``REFUTED``,
   ``UNKNOWN`` (incl. solver timeout), ``UNSUPPORTED`` and ``REJECTED`` never do.
5. *Certificate*: results are cached by ``(sha256(rule text), width,
   verifier hash)``. Editing the rule or the verifier invalidates the cache, so
   the instance is re-proved before it can be applied again.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import z3

from . import z3verify as zv

VERIFIER_SOURCE = Path(zv.__file__)
STATUSES = ("PROVED", "REFUTED", "UNKNOWN", "UNSUPPORTED", "REJECTED")
DEFAULT_TIMEOUT_MS = 60000
DEFAULT_MAX_WIDTH = 256

# Argument kinds of the CIRCT constructors declared in the egglog template
# (circt.egg). Index of the Type argument for each constructor; used to find
# type variables. Parsed from the active template when available.
_FALLBACK_SIGNATURES: Dict[str, List[str]] = {
    "comb_mux": ["Op", "Op", "Op", "Type"], "comb_and": ["Op", "OpVec", "Type"],
    "comb_or": ["Op", "OpVec", "Type"], "comb_xor": ["Op", "OpVec", "Type"],
    "comb_replicate": ["Op", "Type"], "comb_concat": ["Op", "OpVec", "Type"],
    "comb_icmp": ["Op", "Op", "AttrPair", "Type"], "comb_extract": ["Op", "AttrPair", "Type"],
    "comb_add": ["Op", "OpVec", "Type"], "comb_sub": ["Op", "OpVec", "Type"],
    "comb_mul": ["Op", "OpVec", "Type"], "comb_shl": ["Op", "Op", "Type"],
    "comb_shru": ["Op", "Op", "Type"], "comb_shrs": ["Op", "Op", "Type"],
    "hw_constant": ["AttrPair", "Type"], "Value": ["i64", "Type"], "IntegerAttr": ["i64", "Type"],
}
_FUNC_RE = re.compile(r"\(function\s+(\S+)\s+\(([^()]*)\)\s+\S+\)")


def verifier_hash() -> str:
    h = hashlib.sha256(VERIFIER_SOURCE.read_bytes())
    h.update(Path(__file__).read_bytes())
    h.update(CERT_SEMANTICS.encode())
    h.update(z3.get_version_string().encode())
    return h.hexdigest()[:16]


def rule_sha(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


def signatures_from_template(template_text: Optional[str]) -> Dict[str, List[str]]:
    sigs = dict(_FALLBACK_SIGNATURES)
    if template_text:
        for m in _FUNC_RE.finditer(template_text):
            sigs[m.group(1)] = m.group(2).split()
    return sigs


def type_term(width: int) -> str:
    return f"(I{width})" if width in (1, 4, 8, 16, 32, 64) else f"(Int {width})"


def _walk(expr, fn):
    fn(expr)
    if isinstance(expr, list):
        for c in expr:
            _walk(c, fn)


_ATTR_HEADS = ("IntegerAttr", "NamedAttr", "ValueAttr", "ArrayAttr", "TypeAttr")


def data_widths(expr) -> List[int]:
    """Bit widths of *data* types in an s-expression (attribute types excluded)."""
    out: List[int] = []

    def rec(e):
        if isinstance(e, list) and e:
            head = e[0]
            if isinstance(head, str) and head in _ATTR_HEADS:
                return
            if isinstance(head, str) and len(e) == 1 and re.fullmatch(r"I(\d+)", head):
                out.append(int(head[1:]))
                return
            if head == "Int" and len(e) == 2 and isinstance(e[1], int):
                out.append(e[1])
                return
            for c in e:
                rec(c)
    rec(expr)
    return sorted(set(out))


def type_variables(rule_text: str, sigs: Dict[str, List[str]]) -> Tuple[List[str], List[int]]:
    """Return (type variables, concrete widths) mentioned in type positions."""
    forms = zv.parse_top_forms(zv._strip_line_comments(rule_text))
    tvars: List[str] = []
    widths: List[int] = []

    def visit(e):
        if isinstance(e, list) and e and isinstance(e[0], str) and e[0] in sigs:
            kinds = sigs[e[0]]
            for i, kind in enumerate(kinds):
                if kind == "Type" and i + 1 < len(e):
                    arg = e[i + 1]
                    if isinstance(arg, str) and arg.startswith("?"):
                        if arg not in tvars:
                            tvars.append(arg)
    for f in forms:
        _walk(f, visit)
    widths = data_widths(forms)
    return tvars, widths


# egglog commands/actions that must never appear inside an admitted rule other
# than the single top-level rule/rewrite and its let/union actions.
_COMMAND_HEADS = {
    "rule", "rewrite", "birewrite", "union", "set", "delete", "subsume", "panic", "extract", "let",
    "check", "fail", "run", "run-schedule", "saturate", "repeat", "seq", "push", "pop", "include",
    "ruleset", "datatype", "function", "relation", "sort", "constructor", "input", "output",
    "print-function", "print-size", "print-stats", "query-extract", "simplify", "calc", "define",
}
_RULE_KEYWORDS = {":ruleset", ":name"}


def _forbidden_heads(expr) -> List[str]:
    """Command heads nested anywhere inside a term."""
    found: List[str] = []

    def rec(e):
        if isinstance(e, list) and e:
            if isinstance(e[0], str) and e[0] in _COMMAND_HEADS:
                found.append(e[0])
            for c in e[1:]:
                rec(c)
    rec(expr)
    return found


def validate_rule_text(rule_text: str) -> Tuple[Optional[list], str]:
    """Validate the *entire* executable content of a rule file.

    Accepted: exactly one top-level ``(rule (premises) (actions) :ruleset rules
    [:name "..."])`` whose premises are all ``(= a b)`` and whose actions are
    ``(let name term)`` bindings plus exactly one ``(union ?lhs term)``; or one
    ``(rewrite lhs rhs :ruleset rules)``. Any other top-level command, action
    (set, delete, panic, extract, a second union, ...), keyword (``:when``,
    ``:subsume``) or nested command is rejected. Returns (form, "") or
    (None, reason). Only the returned form is ever serialized into e-graphs.
    """
    try:
        forms = zv.parse_top_forms(zv._strip_line_comments(rule_text))
    except ValueError as exc:
        return None, f"malformed s-expression: {exc}"
    if len(forms) != 1:
        return None, f"expected exactly one top-level form (one rule or rewrite), found {len(forms)}"
    form = forms[0]
    if not (isinstance(form, list) and form and form[0] in ("rule", "rewrite")):
        return None, "the top-level form must be a rule or a rewrite"
    if form[0] == "rule":
        if len(form) < 3 or not isinstance(form[1], list) or not isinstance(form[2], list):
            return None, "rule needs (premises) and (actions) lists"
        for p in form[1]:
            if not (isinstance(p, list) and len(p) == 3 and p[0] == "="):
                return None, f"unsupported premise {zv.sexp_to_str(p)[:80]} (only (= a b) is allowed)"
            if _premise_kind(p) is None:
                return None, (f"unsupported premise shape {zv.sexp_to_str(p)[:80]}: write (= ?var term) "
                              "or an exact length guard (= N (vec-length ?v))")
            bad = _forbidden_heads(p[1]) + _forbidden_heads(p[2])
            if bad:
                return None, f"command {bad[0]!r} inside a premise"
        unions = 0
        for a in form[2]:
            if not (isinstance(a, list) and a and isinstance(a[0], str)):
                return None, f"malformed action {zv.sexp_to_str(a)[:80]}"
            if a[0] == "let" and len(a) == 3 and isinstance(a[1], str) and not a[1].startswith((":", '"')):
                bad = _forbidden_heads(a[2])
            elif a[0] == "union" and len(a) == 3:
                unions += 1
                bad = _forbidden_heads(a[1]) + _forbidden_heads(a[2])
            else:
                return None, f"unmodeled action {a[0]!r} (only let bindings and one union are allowed)"
            if bad:
                return None, f"command {bad[0]!r} nested inside an action"
        if unions != 1:
            return None, f"expected exactly one union action, found {unions}"
        kw = form[3:]
        allowed = _RULE_KEYWORDS
    else:
        if len(form) < 3:
            return None, "rewrite needs a left- and right-hand side"
        bad = _forbidden_heads(form[1]) + _forbidden_heads(form[2])
        if bad:
            return None, f"command {bad[0]!r} inside the rewrite"
        kw = form[3:]
        allowed = {":ruleset"}
    if len(kw) % 2:
        return None, "dangling keyword argument"
    seen = {}
    for k, v in zip(kw[0::2], kw[1::2]):
        if k not in allowed:
            return None, f"unsupported keyword {k!r}"
        seen[k] = v
    if seen.get(":ruleset") != "rules":
        return None, "the rule must declare :ruleset rules"
    if ":name" in seen and not (isinstance(seen[":name"], str) and seen[":name"].startswith('"')):
        return None, ":name must be a string"
    return form, ""


# Bumped whenever admission semantics change; part of every certificate's
# verifier identity (together with the source hashes of this module and z3verify).
CERT_SEMANTICS = "vector-lengths-v3"
_PAT_PREFIX = "?cv"          # pattern variables  -> ?cv1, ?cv2, ...
_LET_PREFIX = "cedar_let_"   # let-bound names    -> cedar_let_1, ...


def alpha_rename(form: list) -> Tuple[Optional[list], str]:
    """Rename every pattern variable and let-bound name to a fresh,
    collision-free identifier (alpha-equivalent rule).

    The proof model normalizes names (it desugars rewrites with an internal
    ``?lhs``, turns bare let names into ``?name`` and strips ``?`` for Z3
    symbols). Renaming first guarantees that distinct egglog symbols stay
    distinct in the model, and the *renamed* text is both what is proved and
    what egglog executes.
    """
    pats: Dict[str, str] = {}

    def pat(sym: str) -> str:
        if sym not in pats:
            pats[sym] = f"{_PAT_PREFIX}{len(pats) + 1}"
        return pats[sym]

    def rn(e, lets: Dict[str, str], head_pos: bool = False):
        if isinstance(e, str):
            if head_pos:
                return e
            if e.startswith("?"):
                return pat(e)
            return lets.get(e, e)
        if isinstance(e, list):
            return [rn(c, lets, i == 0) for i, c in enumerate(e)]
        return e

    if form[0] == "rewrite":
        return ["rewrite", rn(form[1], {}), rn(form[2], {})] + list(form[3:]), ""
    premises = [["=", rn(q[1], {}), rn(q[2], {})] for q in (_normalize_premise(p) for p in form[1])]
    lets: Dict[str, str] = {}
    actions = []
    for a in form[2]:
        if a[0] == "let":
            name = a[1]
            if name.startswith("?"):
                return None, f"let name {name!r} looks like a pattern variable"
            if name in lets:
                return None, f"let name {name!r} is bound twice"
            expr = rn(a[2], lets)                     # no self-reference
            lets[name] = f"{_LET_PREFIX}{len(lets) + 1}"
            actions.append(["let", lets[name], expr])
        else:
            actions.append(["union", rn(a[1], lets), rn(a[2], lets)])
    clash = set(lets) & {h for h in _heads(form)}
    if clash:
        return None, f"let name(s) {sorted(clash)} shadow constructor names"
    return ["rule", premises, actions] + list(form[3:]), ""


def _heads(e) -> set:
    out: set = set()

    def rec(x):
        if isinstance(x, list) and x:
            if isinstance(x[0], str):
                out.add(x[0])
            for c in x[1:]:
                rec(c)
    rec(e)
    return out


def _is_var(x) -> bool:
    return isinstance(x, str) and x.startswith("?")


def _is_len(x) -> bool:
    return isinstance(x, list) and len(x) == 2 and x[0] == "vec-length" and _is_var(x[1])


def _premise_kind(p: list) -> Optional[str]:
    """'guard' | 'var-left' | 'var-right', or None for an unsupported shape."""
    a, b = p[1], p[2]
    if (isinstance(a, int) and a >= 0 and _is_len(b)) or (isinstance(b, int) and b >= 0 and _is_len(a)):
        return "guard"
    if _is_var(a):
        return "var-left"
    if _is_var(b):
        return "var-right"
    return None


def _normalize_premise(p: list) -> list:
    """Canonical orientation: (= ?var term) and (= N (vec-length ?v))."""
    k = _premise_kind(p)
    if k == "guard":
        return ["=", p[1], p[2]] if isinstance(p[1], int) else ["=", p[2], p[1]]
    if k == "var-right":
        return ["=", p[2], p[1]]
    return list(p)


def _type_sexp(width: int) -> list:
    return [f"I{width}"] if width in (1, 4, 8, 16, 32, 64) else ["Int", width]


def _subst(expr, var: str, value):
    if isinstance(expr, str) and expr == var:
        return value
    if isinstance(expr, list):
        return [_subst(e, var, value) for e in expr]
    return expr


def specialize(rule_text: str, width: int, sigs: Dict[str, List[str]]) -> Tuple[Optional[str], str]:
    """Return (canonical instance text, reason), or (None, reason).

    The instance is re-serialized from the validated parse tree, so comments and
    anything outside the single validated rule can never reach egglog.
    """
    form, err = validate_rule_text(rule_text)
    if form is None:
        return None, f"rejected: {err}"
    form, err = alpha_rename(form)
    if form is None:
        return None, f"rejected: {err}"
    canonical = zv.sexp_to_str(form)
    tvars, concrete = type_variables(canonical, sigs)
    if len(tvars) > 1:
        return None, f"unsupported: {len(tvars)} independent type variables {tvars} (multi-width rule)"
    if not tvars:
        wide = [w for w in concrete if w != 1]
        if (wide and width not in wide) or (not wide and width != 1):
            return None, f"rule is fixed to widths {concrete}"
        return canonical, "concrete"
    tv = tvars[0]
    return zv.sexp_to_str(_subst(form, tv, _type_sexp(width))), f"type variable {tv} := {type_term(width)}"


@dataclass
class Certificate:
    rule_sha256: str
    rule_name: str
    width: int
    status: str
    detail: str
    instance_sha256: str
    verifier: str
    z3_version: str
    timeout_ms: int
    seconds: float
    created_at: str

    @property
    def admitted(self) -> bool:
        return self.status == "PROVED"


@dataclass
class Instance:
    rule_name: str
    rule_sha256: str
    width: int
    text: str
    certificate: Certificate


@dataclass
class Admission:
    admitted: List[Instance] = field(default_factory=list)
    rejected: List[Certificate] = field(default_factory=list)

    def summary(self) -> Dict[str, Dict[str, str]]:
        out: Dict[str, Dict[str, str]] = {}
        for i in self.admitted:
            out.setdefault(i.rule_name, {})[str(i.width)] = "PROVED"
        for c in self.rejected:
            out.setdefault(c.rule_name, {})[str(c.width)] = f"{c.status}: {c.detail}"
        return out


class CertificateStore:
    def __init__(self, root: Path, sigs: Optional[Dict[str, List[str]]] = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._vh = verifier_hash()
        # Specialization depends on the template's operator signatures, so they
        # are part of the cache identity together with the verifier code.
        self._sig = hashlib.sha256(json.dumps(sigs or {}, sort_keys=True).encode()).hexdigest()[:8]

    def _path(self, sha: str, width: int) -> Path:
        return self.root / f"{sha[:24]}-w{width}-{self._vh}-{self._sig}.json"

    def get(self, sha: str, width: int) -> Optional[Certificate]:
        p = self._path(sha, width)
        if p.is_file():
            c = Certificate(**json.loads(p.read_text()))
            if c.verifier == self._vh and c.rule_sha256 == sha and c.width == width:
                return c
        return None

    def put(self, c: Certificate) -> None:
        self._path(c.rule_sha256, c.width).write_text(json.dumps(asdict(c), indent=2))


def prove_instance(rule_name: str, rule_text: str, width: int, sigs: Dict[str, List[str]],
                   timeout_ms: int = DEFAULT_TIMEOUT_MS, max_width: int = DEFAULT_MAX_WIDTH,
                   phrase_lint: bool = False) -> Tuple[Certificate, Optional[str]]:
    """Prove ``rule_text`` at exactly ``width`` bits. Returns (certificate, instance_text)."""
    sha = rule_sha(rule_text)
    t0 = time.monotonic()

    def cert(status: str, detail: str, inst: Optional[str] = None) -> Tuple[Certificate, Optional[str]]:
        return Certificate(
            rule_sha256=sha, rule_name=rule_name, width=width, status=status, detail=detail[:400],
            instance_sha256=rule_sha(inst) if inst else "", verifier=verifier_hash(),
            z3_version=z3.get_version_string(), timeout_ms=timeout_ms,
            seconds=round(time.monotonic() - t0, 3),
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ), inst

    if width > max_width:
        return cert("UNSUPPORTED", f"width {width} exceeds max_width {max_width}")
    banned = zv.check_banned_op(rule_text)
    if banned:
        return cert("REJECTED", f"banned-op {banned}")
    if phrase_lint:
        phrase = zv.check_banned_phrase(zv._extract_leading_comment(rule_text))
        if phrase:
            return cert("REJECTED", f"banned-phrase {phrase!r}")
    _form, err = validate_rule_text(rule_text)
    if _form is None:
        return cert("REJECTED", err)
    if alpha_rename(_form)[0] is None:
        return cert("REJECTED", alpha_rename(_form)[1])
    inst, why = specialize(rule_text, width, sigs)
    if inst is None:
        return cert("UNSUPPORTED", why)
    with tempfile.TemporaryDirectory(prefix="cedar-rule-") as td:
        f = Path(td) / f"{re.sub(r'[^A-Za-z0-9_]', '_', rule_name)}.egg"
        f.write_text(inst)
        parsed, err = zv.parse_rule_file(f)
    if parsed is None:
        return cert("UNSUPPORTED", f"verifier cannot model rule shape: {err}", inst)
    # The specialized instance may only mention data types of `width` and the
    # 1-bit boolean type; every other operand width must be fixed by the match
    # pattern (checked below).
    others = set(data_widths(zv.parse_top_forms(zv._strip_line_comments(inst)))) - {width, 1}
    if others:
        return cert("UNSUPPORTED", f"instance mentions data widths {sorted(others)} besides {width}", inst)
    arity = _unguarded_vectors(inst, sigs)
    if arity:
        return cert("UNSUPPORTED", "operand vector(s) without one exact (= N (vec-length ?v)) guard, or "
                    f"read out of the guarded bounds: {arity}", inst)
    unguarded = _unguarded_operands(parsed, sigs, inst)
    if unguarded:
        return cert("UNSUPPORTED", f"operand width not fixed by the instance types: {unguarded}", inst)
    try:
        status, detail = zv.verify_rule(parsed, sweep=[width], timeout_ms=timeout_ms)
    except Exception as exc:  # noqa: BLE001
        return cert("UNKNOWN", f"verifier exception {type(exc).__name__}: {exc}", inst)
    mapped = {"VALID": "PROVED", "INVALID": "REFUTED"}.get(status, "UNKNOWN")
    return cert(mapped, f"{why}; {detail}", inst)


def _syms(expr, out: set) -> None:
    if isinstance(expr, str) and expr.startswith("?"):
        out.add(expr)
    elif isinstance(expr, list):
        for c in expr:
            _syms(c, out)


def _rule_form(rule_text: str):
    forms = zv.parse_top_forms(zv._strip_line_comments(rule_text))
    return next((f for f in forms if isinstance(f, list) and f and f[0] in ("rule", "rewrite")), None)


def _unguarded_operands(parsed, sigs: Dict[str, List[str]], rule_text: str) -> List[str]:
    """Bit-vector variables whose width is not fixed by the *match pattern*.

    A proof at width ``w`` only covers matches whose operands all have the
    widths the proof used. Widths must therefore be determined by the premises
    alone; a width implied only by the right-hand side (for example by the
    ``union`` with the matched node) does not constrain what egglog matches.
    """
    form = _rule_form(rule_text)
    if form is None:
        return ["<unparsed rule>"]
    if form[0] == "rewrite":
        prem_pairs = [("?lhs", form[1])]
        lhs_var = "?lhs"
    else:
        prem_pairs = [(p[1], p[2]) for p in form[1]
                      if isinstance(p, list) and len(p) == 3 and p[0] == "=" and isinstance(p[1], str)
                      and p[1].startswith("?") and not zv._is_vec_shape(p[2])]
        lhs_var = parsed.lhs_var
    prem_rule = zv.ParsedRule(name="premises", description="", leading_comment="", body_text="",
                              premises=prem_pairs, lhs_var=lhs_var, rhs_expr=lhs_var, annotated_widths=[])
    env = zv.infer_types(prem_rule)
    full_env = zv.infer_types(parsed)
    let_names: set = set()
    if form[0] == "rule" and len(form) > 2 and isinstance(form[2], list):
        for act in form[2]:
            if isinstance(act, list) and len(act) == 3 and act[0] == "let" and isinstance(act[1], str):
                let_names.add(act[1] if act[1].startswith("?") else "?" + act[1])
    all_syms: set = set()
    for v, e in parsed.premises:
        all_syms.add(v)
        _syms(e, all_syms)
    _syms(parsed.rhs_expr, all_syms)
    non_bv: set = (set(env.vec_elem_width) | set(env.vec_max_index) | set(env.type_var_width)
                   | set(full_env.vec_elem_width) | set(full_env.vec_max_index) | set(full_env.type_var_width))

    def visit(e):
        if isinstance(e, list) and e and isinstance(e[0], str):
            kinds = sigs.get(e[0])
            if kinds:
                for i, kind in enumerate(kinds):
                    if i + 1 < len(e) and kind in ("AttrPair", "AttrPairVec", "OpVec", "Type", "i64"):
                        _syms(e[i + 1], non_bv)
            if e[0] in ("vec-get", "vec-length", "vec-push", "vec-of"):
                if len(e) > 1:
                    _syms(e[1], non_bv)
    for e in [x for _, x in parsed.premises] + [parsed.rhs_expr] + [x for _, x in prem_pairs]:
        _walk(e, visit)
    bad = []
    for sym in sorted(all_syms - non_bv - let_names):
        if sym in env.widths or _bound_by_vec_get(sym, prem_rule, env):
            continue
        bad.append(sym)
    return bad


def _unguarded_vectors(rule_text: str, sigs: Dict[str, List[str]]) -> List[str]:
    """Vector pattern variables the proof model cannot represent exactly.

    The verifier models a pattern vector ``?v`` as exactly ``N`` elements,
    where ``N`` comes from the rule's single ``(= N (vec-length ?v))`` premise
    (``N = 0`` included). Every vector variable -- an ``OpVec`` operand, or one
    read with ``vec-get`` / ``vec-length`` -- therefore needs exactly one such
    guard, and every read must use an integer literal index in ``[0, N)``.
    The number of operands modeled never depends on which indices are read.
    Rewrites have no premises and so can never guard a vector.
    """
    forms = zv.parse_top_forms(zv._strip_line_comments(rule_text))
    rule = next((f for f in forms if isinstance(f, list) and f and f[0] in ("rule", "rewrite")), None)
    if rule is None:
        return []
    if rule[0] == "rewrite":
        # A rewrite has no premises, so a matched operand vector can never be
        # length-guarded: reject every OpVec pattern variable.
        found: set = set()

        def vis(e):
            if isinstance(e, list) and e and isinstance(e[0], str):
                for i, kind in enumerate(sigs.get(e[0]) or []):
                    if kind == "OpVec" and i + 1 < len(e) and _is_var(e[i + 1]):
                        found.add(e[i + 1])
                if e[0] in ("vec-get", "vec-length") and len(e) >= 2 and _is_var(e[1]):
                    found.add(e[1])
        for part in rule[1:3]:
            _walk(part, vis)
        return [f"{v} (a rewrite cannot guard vector length; use a rule with (= N (vec-length {v})))"
                for v in sorted(found)]
    if rule[0] != "rule" or len(rule) < 3 or not isinstance(rule[1], list):
        return []
    premises = rule[1]
    vecs: set = set()
    reads: Dict[str, List[Any]] = {}
    guards: Dict[str, List[int]] = {}

    def visit(e):
        if isinstance(e, list) and e and isinstance(e[0], str):
            kinds = sigs.get(e[0])
            if kinds:
                for i, kind in enumerate(kinds):
                    if kind == "OpVec" and i + 1 < len(e) and _is_var(e[i + 1]):
                        vecs.add(e[i + 1])
            if e[0] == "vec-get" and len(e) == 3 and _is_var(e[1]):
                vecs.add(e[1])
                reads.setdefault(e[1], []).append(e[2])
            if e[0] == "vec-length" and len(e) == 2 and _is_var(e[1]):
                vecs.add(e[1])
    # Reads count wherever they occur; guards are premises only.
    _walk(rule[2] if isinstance(rule[2], list) else [], visit)
    for p in premises:
        if isinstance(p, list) and len(p) == 3 and p[0] == "=" and _premise_kind(p) == "guard":
            q = _normalize_premise(p)
            guards.setdefault(q[2][1], []).append(q[1])
            continue
        _walk(p, visit)
    bad = []
    for v in sorted(vecs):
        g = sorted(set(guards.get(v, [])))
        if len(g) != 1:
            bad.append(f"{v} (needs exactly one length guard (= N (vec-length {v})), found {g or 'none'})")
            continue
        n = g[0]
        for idx in reads.get(v, []):
            if not isinstance(idx, int) or not 0 <= idx < n:
                bad.append(f"{v} (read index {idx!r} outside guarded length {n})")
    return bad


def _bound_by_vec_get(sym: str, parsed, env) -> bool:
    for v, e in parsed.premises:
        if v == sym and isinstance(e, list) and e and e[0] == "vec-get" and isinstance(e[1], str):
            return e[1] in env.vec_elem_width
    return False


def admit(rules: Dict[str, str], widths: Iterable[int], store: Optional[CertificateStore],
          sigs: Dict[str, List[str]], timeout_ms: int = DEFAULT_TIMEOUT_MS,
          max_width: int = DEFAULT_MAX_WIDTH) -> Admission:
    """Admission gate for a design: certify every (rule, width) instance."""
    adm = Admission()
    for name, text in sorted(rules.items()):
        sha = rule_sha(text)
        form, err = validate_rule_text(text)
        if form is None:
            c, _ = prove_instance(name, text, min(widths, default=1), sigs, timeout_ms, max_width)
            adm.rejected.append(c)
            continue
        renamed, _err = alpha_rename(form)
        if renamed is None:
            c, _ = prove_instance(name, text, min(widths, default=1), sigs, timeout_ms, max_width)
            adm.rejected.append(c)
            continue
        tvars, concrete = type_variables(zv.sexp_to_str(renamed), sigs)
        targets = sorted(set(widths)) if tvars else (sorted(set(concrete) - {1}) or [1])
        for w in targets:
            cached = store.get(sha, w) if store else None
            inst, _ = specialize(text, w, sigs)
            if cached is not None and inst is not None and cached.instance_sha256 == rule_sha(inst):
                c = cached
            else:
                c, inst = prove_instance(name, text, w, sigs, timeout_ms, max_width)
                if store:
                    store.put(c)
            if c.admitted and inst is not None:
                adm.admitted.append(Instance(name, sha, w, inst, c))
            else:
                adm.rejected.append(c)
    return adm
