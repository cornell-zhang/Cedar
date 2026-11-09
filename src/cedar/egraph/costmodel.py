# SPDX-License-Identifier: Apache-2.0
"""Per-e-node weighted PPA cost model consumed by the extractor.

Implements the paper's node cost

    cost(n) = alpha * area(n)/S_area + beta * delay(n)/S_delay + gamma * power(n)/S_power

where ``area(n)``, ``delay(n)`` and ``power(n)`` are *estimated* node features
(dimensionless, gate-equivalent scale) computed from the operator kind, its
bit width and operand count, and ``S_*`` are documented normalization scales:
the summed feature over the unoptimized design's e-graph (the analysis run
without rewrite rules). The extractor (``extract_result``, DAG-sharing greedy)
minimizes the sum of node costs over the selected DAG.

Approximations (see docs/cost-model.md):

* Features are technology-independent estimates, not measurements. They can be
  re-scaled per constructor by a calibration table derived from measured
  netlist attribution (:mod:`cedar.attribution`), and by agent-proposed
  per-constructor multipliers.
* The delay feature is a local logic-depth estimate per node. Because the
  extractor sums costs, the delay term approximates the total logic depth of
  the selected DAG, not the critical path; measured delay is reported
  separately from the design-level timing reports.
* The power feature is switched-capacitance proxy (area x operator toggle
  factor, plus clock load for registers); it ignores input activity.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Mapping, Optional, Tuple

EPS = 1e-3            # minimum node cost (prefers fewer nodes among ties)
MULT_RANGE = (0.05, 20.0)

_ZERO = {"Value", "hw_constant", "hw_aggregate_constant", "comb_concat", "comb_extract", "comb_replicate",
         "hw_output", "RootNode", "OpVec", "seq_to_clock", "hw_array_create", "hw_array_get", "NamedAttr",
         "ValueAttr", "IntegerAttr", "llhd_constant_time"}
_TOGGLE = {"comb_mul": 1.5, "comb_xor": 1.2, "comb_add": 1.1, "comb_sub": 1.1}


@dataclass
class Features:
    area: float
    delay: float
    power: float

    def as_tuple(self) -> Tuple[float, float, float]:
        return (self.area, self.delay, self.power)


@dataclass
class CostReport:
    weights: Dict[str, float]
    scales: Dict[str, float]
    multipliers_applied: Dict[str, int] = field(default_factory=dict)
    multipliers_unmatched: list = field(default_factory=list)
    calibration: Dict[str, float] = field(default_factory=dict)
    n_nodes: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _log2(w: int) -> float:
    return math.log2(max(w, 2))


def node_width(node: dict, nodes: Mapping[str, dict]) -> Optional[int]:
    for c in reversed(node.get("children", [])):
        cn = nodes.get(c)
        if cn is None:
            continue
        op = cn["op"]
        if op.startswith("I") and op[1:].isdigit():
            return int(op[1:])
        if op in ("Int", "SInt", "UInt") and cn["children"]:
            lit = nodes.get(cn["children"][0], {}).get("op", "")
            if lit.lstrip("-").isdigit():
                return int(lit)
    return None


def _n_operands(node: dict, nodes: Mapping[str, dict]) -> int:
    k = 0
    for c in node.get("children", []):
        cn = nodes.get(c)
        if cn is None:
            continue
        if cn["op"] == "OpVec":
            k += len(cn["children"])
        elif cn["op"] not in ("I1",) and not (cn["op"].startswith("I") and cn["op"][1:].isdigit()) and cn["op"] != "Int":
            if cn["op"] not in ("NamedAttr", "ValueAttr"):
                k += 1
    return max(k, 1)


def _is_const(nid: str, nodes: Mapping[str, dict]) -> bool:
    return nodes.get(nid, {}).get("op") == "hw_constant"


def features(node: dict, nodes: Mapping[str, dict]) -> Features:
    op = node["op"].split("(")[0].strip()
    if op in _ZERO or not op.startswith(("comb_", "seq_", "llhd_", "hw_", "Opaque")):
        return Features(0.0, 0.0, 0.0)
    w = node_width(node, nodes) or 1
    k = _n_operands(node, nodes)
    ch = node.get("children", [])
    const_shift = op in ("comb_shl", "comb_shru", "comb_shrs") and len(ch) >= 2 and _is_const(ch[1], nodes)
    ow = _operand_width(node, nodes, w) if op == "comb_icmp" else w
    return features_for(op, w, k, const_shift, ow)


def features_for(op: str, w: int, k: int = 2, const_shift: bool = False, operand_width: Optional[int] = None) -> Features:
    """Feature table (documented in docs/cost-model.md)."""
    if op in _ZERO or not op.startswith(("comb_", "seq_", "llhd_", "hw_", "Opaque")):
        return Features(0.0, 0.0, 0.0)
    w = max(int(w or 1), 1)
    k = max(int(k or 1), 1)
    lw = _log2(w)
    if op in ("comb_add", "comb_sub"):
        a, d = 2.0 * w * (k - 1), 2.0 * lw * math.ceil(math.log2(max(k, 2)))
    elif op == "comb_mul":
        a, d = float(w * w) * (k - 1), 3.0 * lw * (k - 1)
    elif op in ("comb_and", "comb_or"):
        a, d = 0.5 * w * (k - 1), 1.0 * math.ceil(math.log2(max(k, 2)))
    elif op == "comb_xor":
        a, d = 1.0 * w * (k - 1), 1.5 * math.ceil(math.log2(max(k, 2)))
    elif op == "comb_mux":
        a, d = 1.0 * w, 1.0
    elif op == "comb_icmp":
        ow = operand_width or w
        a, d = 1.5 * ow, 1.5 * _log2(ow)
    elif op in ("comb_shl", "comb_shru", "comb_shrs"):
        a, d = (0.0, 0.0) if const_shift else (w * lw, lw)
    elif op == "seq_firreg":
        a = 4.0 * w
        return Features(a, 0.0, a * 0.5 + 1.0 * w)
    elif op == "comb_parity":
        a, d = 1.0 * w, lw
    else:                                         # llhd_*, Opaque*, other comb/hw ops: unknown
        a, d = 1.0, 1.0
    return Features(a, d, a * _TOGGLE.get(op, 1.0))


def _operand_width(node: dict, nodes: Mapping[str, dict], default: int) -> int:
    for c in node.get("children", []):
        cn = nodes.get(c)
        if cn is not None and cn["op"] not in ("NamedAttr", "ValueAttr"):
            w = node_width(cn, nodes)
            if w:
                return w
    return default


def design_scales(egraph: dict) -> Dict[str, float]:
    """Summed features over an e-graph (use the rule-free analysis e-graph)."""
    nodes = egraph["nodes"]
    tot = [0.0, 0.0, 0.0]
    for n in nodes.values():
        f = features(n, nodes).as_tuple()
        for i in range(3):
            tot[i] += f[i]
    return {"area": tot[0] or 1.0, "delay": tot[1] or 1.0, "power": tot[2] or 1.0}


def validate_multipliers(mult: Mapping[str, float]) -> Dict[str, float]:
    out = {}
    for k, v in (mult or {}).items():
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"cost multiplier for {k!r} must be finite")
        if not (MULT_RANGE[0] <= v <= MULT_RANGE[1]):
            raise ValueError(f"cost multiplier for {k!r} must be within {MULT_RANGE}")
        out[str(k)] = float(v)
    return out


def node_cost(f: Features, weights: Mapping[str, float], scales: Mapping[str, float]) -> float:
    c = (weights.get("area", 0) * f.area / scales["area"]
         + weights.get("delay", 0) * f.delay / scales["delay"]
         + weights.get("power", 0) * f.power / scales["power"])
    return c


def apply_costs(egraph_json: Path, weights: Mapping[str, float], scales: Mapping[str, float],
                multipliers: Optional[Mapping[str, float]] = None,
                calibration: Optional[Mapping[str, Mapping[str, float]]] = None) -> CostReport:
    """Write weighted per-node costs into ``egraph_json`` (in place)."""
    mult = validate_multipliers(multipliers or {})
    calib = calibration or {}
    data = json.loads(Path(egraph_json).read_text())
    nodes = data["nodes"]
    total_w = sum(weights.values())
    if total_w <= 0:
        raise ValueError("at least one positive weight is required")
    applied: Dict[str, int] = {}
    for n in nodes.values():
        op = n["op"].split("(")[0].strip()
        f = features(n, nodes)
        cal = calib.get(op)
        if cal:
            f = Features(f.area * cal.get("area", 1.0), f.delay * cal.get("delay", 1.0), f.power * cal.get("power", 1.0))
        c = node_cost(f, weights, scales) * 1000.0
        if op in mult:
            c *= mult[op]
            applied[op] = applied.get(op, 0) + 1
        n["cost"] = round(max(c, EPS), 6)
    Path(egraph_json).write_text(json.dumps(data))
    return CostReport(dict(weights), dict(scales), applied, sorted(set(mult) - set(applied)),
                      {k: dict(v) for k, v in calib.items()}, len(nodes))


def selected_features(egraph: dict, chosen_nodes: Iterable[str]) -> Dict[str, float]:
    """Estimated features summed over a chosen set of nodes (for reporting)."""
    nodes = egraph["nodes"]
    tot = {"area": 0.0, "delay": 0.0, "power": 0.0}
    for nid in chosen_nodes:
        f = features(nodes[nid], nodes)
        tot["area"] += f.area
        tot["delay"] += f.delay
        tot["power"] += f.power
    return tot
