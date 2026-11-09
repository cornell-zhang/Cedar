# SPDX-License-Identifier: Apache-2.0
"""Design-level weighted PPA objective (ranking, prompts, stopping).

This is the *whole-design* score

    score = w_area * A / A_ref + w_delay * D / D_ref + w_power * P / P_ref

used to rank measured candidates and to brief the agent. It is deliberately
separate from the *per-e-node* weighted cost model consumed by the extractor
(:mod:`cedar.egraph.costmodel`), which implements the paper's
``cost(n) = alpha*area(n) + beta*delay(n) + gamma*power(n)`` from node feature
estimates. A design score is not a per-node physical attribution.

Normalization: ``baseline`` divides each metric by the baseline's measured
value of the same quantity at the same stage. A reference value that is
missing, zero or negative makes the objective *unscorable* for any active
weight (explicit error, never a silent default). ``absolute`` uses raw
canonical units (um2, ns, uW) and is mostly useful for tests.

Timing is a hard constraint separate from the soft delay preference: with
``require_timing_met`` a candidate that violates the declared clock is
infeasible and never ranks above a feasible one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .metrics import PPA

QUANTITIES = ("area", "delay", "power")

PRESETS: Dict[str, Dict[str, float]] = {
    "balanced": {"area": 1 / 3, "delay": 1 / 3, "power": 1 / 3},
    "area": {"area": 0.6, "delay": 0.2, "power": 0.2},
    "delay": {"area": 0.2, "delay": 0.6, "power": 0.2},
    "power": {"area": 0.2, "delay": 0.2, "power": 0.6},
    "paper-power": {"area": 0.3, "delay": 0.2, "power": 0.5},
    "area-delay": {"area": 0.5, "delay": 0.5, "power": 0.0},
}


class ObjectiveError(ValueError):
    pass


@dataclass
class Objective:
    weights: Dict[str, float]
    normalization: str = "baseline"           # baseline | absolute
    require_timing_met: bool = True
    weight_schedule: List[Dict] = field(default_factory=list)  # [{from_iteration:int, weights:{...}}]
    preset: Optional[str] = None

    @classmethod
    def from_config(cls, cfg: Optional[dict]) -> "Objective":
        cfg = dict(cfg or {})
        preset = cfg.get("preset")
        weights = {k: v for k, v in (cfg.get("weights") or {}).items() if v is not None} or None
        if weights is None:
            if preset is None:
                raise ObjectiveError("objective needs explicit weights or a preset")
            if preset not in PRESETS:
                raise ObjectiveError(f"unknown objective preset {preset!r}; choose from {sorted(PRESETS)}")
            weights = PRESETS[preset]
        obj = cls(
            weights={q: float(weights.get(q, 0.0)) for q in QUANTITIES},
            normalization=cfg.get("normalization", "baseline"),
            require_timing_met=bool(cfg.get("require_timing_met", True)),
            weight_schedule=list(cfg.get("weight_schedule", []) or []),
            preset=preset,
        )
        unknown = set(weights) - set(QUANTITIES)
        if unknown:
            raise ObjectiveError(f"unknown objective weight(s): {sorted(unknown)}")
        obj.validate()
        return obj

    def validate(self) -> None:
        _check_weights(self.weights, "weights")
        if self.normalization not in ("baseline", "absolute"):
            raise ObjectiveError("normalization must be 'baseline' or 'absolute'")
        last = -1
        for i, step in enumerate(self.weight_schedule):
            it = step.get("from_iteration")
            if not isinstance(it, int) or it <= last:
                raise ObjectiveError(f"weight_schedule[{i}].from_iteration must be increasing ints")
            last = it
            _check_weights({q: float(step["weights"].get(q, 0.0)) for q in QUANTITIES}, f"weight_schedule[{i}]")

    def weights_at(self, iteration: int) -> Dict[str, float]:
        w = dict(self.weights)
        for step in self.weight_schedule:
            if iteration >= step["from_iteration"]:
                w = {q: float(step["weights"].get(q, 0.0)) for q in QUANTITIES}
        return w

    def active(self, iteration: int = 0) -> List[str]:
        return [q for q, v in self.weights_at(iteration).items() if v > 0]

    def score(self, ppa: PPA, reference: Optional[PPA], iteration: int = 0) -> Tuple[Optional[float], List[str]]:
        """Return (score, problems). Lower is better. ``None`` if unscorable."""
        w = self.weights_at(iteration)
        problems: List[str] = []
        total = 0.0
        for q in QUANTITIES:
            if w[q] <= 0:
                continue
            v = ppa.get(q)
            if v is None:
                problems.append(f"{q} unavailable")
                continue
            if self.normalization == "baseline":
                ref = reference.get(q) if reference is not None else None
                if ref is None or ref <= 0:
                    problems.append(f"{q}: baseline reference missing or non-positive")
                    continue
                total += w[q] * v / ref
            else:
                total += w[q] * v
        if problems:
            return None, problems
        return total, []

    def feasible(self, ppa: PPA) -> bool:
        return (ppa.timing_met is True) if self.require_timing_met else True

    def rank_key(self, ppa: PPA, reference: Optional[PPA], iteration: int = 0):
        s, _ = self.score(ppa, reference, iteration)
        return (0 if self.feasible(ppa) else 1, math.inf if s is None else s)

    def describe(self, iteration: int = 0) -> str:
        w = self.weights_at(iteration)
        txt = (
            f"Objective (lower is better): score = {w['area']:.3g}*area/area_ref + "
            f"{w['delay']:.3g}*delay/delay_ref + {w['power']:.3g}*power/power_ref "
            f"(normalization={self.normalization}; units: area um2, delay ns, power uW). "
        )
        txt += (
            "Timing is a hard constraint: candidates that miss the declared clock are infeasible."
            if self.require_timing_met else "Timing violations are allowed but reported."
        )
        if self.weight_schedule:
            txt += f" Weight schedule: {self.weight_schedule}."
        return txt

    def to_dict(self) -> dict:
        return {
            "weights": self.weights,
            "normalization": self.normalization,
            "require_timing_met": self.require_timing_met,
            "weight_schedule": self.weight_schedule,
            "preset": self.preset,
        }


def _check_weights(w: Dict[str, float], where: str) -> None:
    for q, v in w.items():
        if not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
            raise ObjectiveError(f"{where}.{q} must be a finite non-negative number")
    if sum(w.values()) <= 0:
        raise ObjectiveError(f"{where}: at least one weight must be positive")
