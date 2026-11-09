# SPDX-License-Identifier: Apache-2.0
"""Rule-selection evidence: contextual cross-design history (A7).

History is an append-only JSONL file of *rule combinations* applied to a
design, with design context (operator histogram, widths, technology, clock,
objective), applicability evidence (egglog match counts, new e-nodes) and the
measured outcome relative to that design's baseline. Gains are attributed to
the *combination* only; individual rule effects are never inferred from a
combination's result.

Selection policy (paper: applicability, >=1% improvement prioritization,
new-e-node checks, complementary combinations):

1. keep only admitted rules whose applicability probe on *this* design fired
   (matches > 0) and added e-nodes;
2. rank rules by the best contextual evidence among past combinations that
   contained them: combinations with >= 1% weighted-score improvement on a
   similar design (cosine similarity of operator histograms) rank first;
3. build a complementary group greedily: add the next-ranked rule whose
   matched operator kinds are not yet covered by the group.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

IMPROVEMENT_THRESHOLD = 0.01


def normalize_rule_name(name: str) -> str:
    base = os.path.basename(str(name))
    return base[:-4] if base.endswith(".egg") else base


def _cos(a: Dict[str, int], b: Dict[str, int]) -> float:
    keys = set(a) | set(b)
    num = sum(a.get(k, 0) * b.get(k, 0) for k in keys)
    da = math.sqrt(sum(v * v for v in a.values()))
    db = math.sqrt(sum(v * v for v in b.values()))
    return num / (da * db) if da and db else 0.0


def design_features(op_histogram: Dict[str, int]) -> Dict[str, int]:
    return {k: v for k, v in op_histogram.items() if k.startswith(("comb_", "seq_", "hw_"))}


class History:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def records(self) -> List[dict]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def append(self, rec: dict) -> None:
        rec = dict(rec)
        rec["rules"] = sorted(normalize_rule_name(r) for r in rec.get("rules", []))
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def evidence(self, rule: str, features: Dict[str, int], context: Optional[dict] = None) -> dict:
        """Best contextual evidence for combinations containing ``rule``."""
        rule = normalize_rule_name(rule)
        best: Optional[dict] = None
        n = 0
        for r in self.records():
            if rule not in r.get("rules", []) or not r.get("verified"):
                continue
            n += 1
            sim = _cos(features, r.get("features", {}))
            imp = r.get("score_improvement")
            if imp is None:
                continue
            same_tech = context is None or r.get("technology") == context.get("technology")
            key = (imp >= IMPROVEMENT_THRESHOLD, same_tech, sim * imp)
            if best is None or key > best["_key"]:
                best = {"_key": key, "improvement": imp, "similarity": round(sim, 3), "design": r.get("design"),
                        "combo": r.get("rules"), "same_technology": same_tech}
        if best is None:
            return {"uses": n, "prioritized": False}
        best.pop("_key")
        best["uses"] = n
        best["prioritized"] = best["improvement"] >= IMPROVEMENT_THRESHOLD
        return best


@dataclass
class Probe:
    rule: str
    matches: int
    new_nodes: int
    op_kinds: Tuple[str, ...]

    @property
    def applicable(self) -> bool:
        return self.matches > 0 and self.new_nodes > 0


def rank(probes: Sequence[Probe], history: Optional[History], features: Dict[str, int],
         context: Optional[dict] = None) -> List[Tuple[Probe, dict]]:
    ranked = []
    for p in probes:
        if not p.applicable:
            continue
        ev = history.evidence(p.rule, features, context) if history else {"uses": 0, "prioritized": False}
        ranked.append((p, ev))
    ranked.sort(key=lambda t: (not t[1].get("prioritized", False), -(t[1].get("improvement") or 0.0),
                               -t[0].new_nodes, t[0].rule))
    return ranked


def complementary_group(ranked: Sequence[Tuple[Probe, dict]], max_rules: int = 4,
                        exclude: Iterable[Tuple[str, ...]] = ()) -> List[str]:
    """Greedy complementary group; avoids repeating an already-tried combination."""
    tried = {tuple(sorted(c)) for c in exclude}
    for start in range(len(ranked)):
        group: List[str] = []
        covered: set = set()
        for p, _ in list(ranked[start:]) + list(ranked[:start]):
            if len(group) >= max_rules:
                break
            if not group or not set(p.op_kinds) <= covered:
                group.append(p.rule)
                covered |= set(p.op_kinds)
        if tuple(sorted(group)) not in tried and group:
            return group
    return [p.rule for p, _ in ranked[:max_rules]]


def lhs_op_kinds(rule_text: str) -> Tuple[str, ...]:
    """Operator kinds appearing in a rule's premises (its match footprint)."""
    import re
    m = re.search(r"\(rule\s*\((.*?)\)\s*\(\s*\(", rule_text, re.S)
    body = m.group(1) if m else rule_text
    return tuple(sorted(set(re.findall(r"\((comb_\w+|seq_\w+|hw_\w+)", body))))
