# SPDX-License-Identifier: Apache-2.0
"""Shared verified Pareto frontier over measured candidates.

Only candidates that (a) passed the configured correctness check and (b) have
complete, comparable measurements for every frontier axis may enter. Missing
metrics never become zeros. ``add`` reports whether the frontier *membership*
changed (a new point entered, possibly displacing dominated points), which is
the improvement signal used for stagnation tracking — not the cardinality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .metrics import PPA, comparable


@dataclass
class Point:
    name: str
    ppa: PPA
    origin: str                       # baseline | direct | extracted
    verified: bool
    info: Dict = field(default_factory=dict)

    def vec(self, axes: Sequence[str]) -> Tuple[float, ...]:
        return tuple(self.ppa.get(a) for a in axes)  # type: ignore[misc]


def dominates(a: Sequence[float], b: Sequence[float]) -> bool:
    return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))


class Frontier:
    def __init__(self, axes: Sequence[str] = ("area", "delay", "power"), reference: Optional[PPA] = None,
                 require_timing_met: bool = True):
        self.axes = tuple(axes)
        self.reference = reference
        self.require_timing_met = require_timing_met
        self._points: List[Point] = []
        self.rejected: List[Tuple[str, str]] = []

    def eligible(self, p: Point) -> List[str]:
        problems = []
        if not p.verified:
            problems.append("not verified")
        problems += p.ppa.require(self.axes)
        if self.reference is not None:
            problems += comparable(p.ppa, self.reference, self.axes)
        if self.require_timing_met and p.ppa.timing_met is not True:
            problems.append("timing not met")
        return problems

    def add(self, p: Point) -> bool:
        """Insert ``p``; return True iff frontier membership changed."""
        problems = self.eligible(p)
        if problems:
            self.rejected.append((p.name, "; ".join(problems)))
            return False
        v = p.vec(self.axes)
        for q in self._points:
            qv = q.vec(self.axes)
            if dominates(qv, v) or qv == v:
                return False
        self._points = [q for q in self._points if not dominates(v, q.vec(self.axes))]
        self._points.append(p)
        return True

    def points(self) -> List[Point]:
        return list(self._points)

    def names(self) -> List[str]:
        return [p.name for p in self._points]

    def to_dict(self) -> dict:
        return {
            "axes": list(self.axes),
            "units": {"area": "um2", "delay": "ns", "power": "uW"},
            "points": [
                {"name": p.name, "origin": p.origin, "verified": p.verified, "ppa": p.ppa.to_dict(), "info": p.info}
                for p in self._points
            ],
            "rejected": [{"name": n, "reason": r} for n, r in self.rejected],
        }
