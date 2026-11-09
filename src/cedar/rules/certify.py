# SPDX-License-Identifier: Apache-2.0
"""Exact-width, content-hashed rule certificates and the proof admission gate.

Admission contract (paper: verified-only rule pool, concrete-width guards):

1. *Quarantine*: every generated or reused rule is untrusted text.
2. *Parse / type-check*: the rule must contain exactly one ``rule``/``rewrite``
   form with one ``union`` action, no banned (stateful) ops, and at most one
   type variable (or none, if all types are concrete).
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
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

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
    zv._walk_collect_widths(forms, widths)
    return tvars, sorted(set(widths))


def specialize(rule_text: str, width: int, sigs: Dict[str, List[str]]) -> Tuple[Optional[str], str]:
    """Return (instance_text, reason). ``None`` when the rule cannot be
    instantiated at ``width`` (reason says why)."""
    tvars, concrete = type_variables(rule_text, sigs)
    if len(tvars) > 1:
        return None, f"unsupported: {len(tvars)} independent type variables {tvars} (multi-width rule)"
    if not tvars:
        wide = [w for w in concrete if w != 1]
        if (wide and width not in wide) or (not wide and width != 1):
            return None, f"rule is fixed to widths {concrete}"
        return rule_text, "concrete"
    tv = tvars[0]
    pat = re.compile(r"(?<![\w?\-])" + re.escape(tv) + r"(?![\w\-])")
    body = rule_text
    out = pat.sub(type_term(width), body)
    return out, f"type variable {tv} := {type_term(width)}"


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
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._vh = verifier_hash()

    def _path(self, sha: str, width: int) -> Path:
        return self.root / f"{sha[:24]}-w{width}-{self._vh}.json"

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
    try:
        forms = zv.parse_top_forms(zv._strip_line_comments(rule_text))
    except ValueError as exc:
        return cert("REJECTED", f"malformed s-expression: {exc}")
    n_rules = sum(1 for f in forms if isinstance(f, list) and f and f[0] in ("rule", "rewrite"))
    if n_rules != 1:
        return cert("REJECTED", f"expected exactly one rule/rewrite form, found {n_rules}")
    inst, why = specialize(rule_text, width, sigs)
    if inst is None:
        return cert("UNSUPPORTED", why)
    with tempfile.TemporaryDirectory(prefix="cedar-rule-") as td:
        f = Path(td) / f"{re.sub(r'[^A-Za-z0-9_]', '_', rule_name)}.egg"
        f.write_text(inst)
        parsed, err = zv.parse_rule_file(f)
    if parsed is None:
        return cert("UNSUPPORTED", f"verifier cannot model rule shape: {err}", inst)
    # The specialized instance must only mention `width` (and 1-bit selects).
    widths = set(parsed.annotated_widths) - {1}
    if width != 1 and widths and widths != {width}:
        return cert("UNSUPPORTED", f"instance mentions widths {sorted(widths)} besides {width}", inst)
    arity = _unguarded_vectors(inst, sigs)
    if arity:
        return cert("UNSUPPORTED", "variadic operand vector(s) without an exact (= N (vec-length ?v)) "
                    f"guard matching the indices used: {arity}", inst)
    unguarded = _unguarded_operands(parsed, sigs)
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


def _unguarded_operands(parsed, sigs: Dict[str, List[str]]) -> List[str]:
    """Bit-vector variables whose width the verifier would have to *default*.

    A proof at width ``w`` only covers matches whose operands all have the
    widths the proof used; if an operand width is not determined by the
    instance's concrete types, the proof does not cover every possible match.
    """
    env = zv.infer_types(parsed)
    exprs = [e for _, e in parsed.premises] + [parsed.rhs_expr]
    all_syms: set = set()
    for v, e in parsed.premises:
        all_syms.add(v)
        _syms(e, all_syms)
    _syms(parsed.rhs_expr, all_syms)
    non_bv: set = set(env.vec_elem_width) | set(env.vec_max_index) | set(env.type_var_width)

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
    for e in exprs:
        _walk(e, visit)
    # vec-get results are BV values; they are bound through the vec element width.
    return sorted(s for s in all_syms - non_bv if s not in env.widths and not _bound_by_vec_get(s, parsed, env))


def _unguarded_vectors(rule_text: str, sigs: Dict[str, List[str]]) -> List[str]:
    """OpVec pattern variables lacking an exact length guard.

    The verifier models a pattern vector ``?v`` as exactly the elements read
    with ``(vec-get ?v i)``. That model is only faithful if the rule also
    requires ``(vec-length ?v) == max_index + 1``; otherwise the rule would
    also match longer vectors and silently drop the remaining operands.
    """
    forms = zv.parse_top_forms(zv._strip_line_comments(rule_text))
    rule = next((f for f in forms if isinstance(f, list) and f and f[0] in ("rule", "rewrite")), None)
    if rule is None or rule[0] != "rule" or len(rule) < 3 or not isinstance(rule[1], list):
        return []
    premises = rule[1]
    vecs: set = set()
    max_idx: Dict[str, int] = {}
    guards: Dict[str, int] = {}

    def visit(e):
        if isinstance(e, list) and e and isinstance(e[0], str):
            kinds = sigs.get(e[0])
            if kinds:
                for i, kind in enumerate(kinds):
                    if kind == "OpVec" and i + 1 < len(e) and isinstance(e[i + 1], str) and e[i + 1].startswith("?"):
                        vecs.add(e[i + 1])
            if e[0] == "vec-get" and len(e) == 3 and isinstance(e[1], str) and isinstance(e[2], int):
                max_idx[e[1]] = max(max_idx.get(e[1], -1), e[2])
    for p in premises:
        _walk(p, visit)
        if isinstance(p, list) and len(p) == 3 and p[0] == "=":
            a, b = p[1], p[2]
            for n, v in ((a, b), (b, a)):
                if isinstance(n, int) and isinstance(v, list) and len(v) == 2 and v[0] == "vec-length" \
                        and isinstance(v[1], str):
                    guards[v[1]] = n
    bad = []
    for v in sorted(vecs):
        need = max_idx.get(v, -1) + 1
        if guards.get(v) != need:
            bad.append(f"{v} (reads {need} element(s), guard={guards.get(v)})")
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
        tvars, concrete = type_variables(text, sigs)
        targets = sorted(set(widths)) if tvars else (sorted(set(concrete) - {1}) or [1])
        for w in targets:
            cached = store.get(sha, w) if store else None
            if cached is not None:
                inst, _ = specialize(text, w, sigs)
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
