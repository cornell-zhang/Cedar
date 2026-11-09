# SPDX-License-Identifier: Apache-2.0
"""Typed PPA measurements with explicit units, stages, sources and validity.

Every metric carries the unit it is stored in, the flow stage that produced it
and its validity. Missing or failed measurements are represented explicitly
(``value is None`` with ``status != "measured"``); they are never encoded as
zero. Canonical units used for scoring, frontiers and prompts:

* area  -> ``um2``
* delay -> ``ns`` (critical-path data arrival time at the stated stage)
* power -> ``uW`` (total power)
* slack -> ``ns``
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, Optional

CANONICAL_UNITS = {"area": "um2", "delay": "ns", "power": "uW", "slack": "ns"}

_TIME = {"s": 1e9, "ms": 1e6, "us": 1e3, "ns": 1.0, "ps": 1e-3, "fs": 1e-6}
_POWER = {"W": 1e6, "mW": 1e3, "uW": 1.0, "nW": 1e-3, "pW": 1e-6}
_AREA = {"um2": 1.0, "mm2": 1e6, "nm2": 1e-6}

STATUSES = ("measured", "estimated", "missing", "failed", "skipped")


class UnitError(ValueError):
    pass


def convert(value: float, unit: str, quantity: str) -> float:
    """Convert ``value`` in ``unit`` to the canonical unit of ``quantity``."""
    table = {"delay": _TIME, "slack": _TIME, "power": _POWER, "area": _AREA}.get(quantity)
    if table is None:
        raise UnitError(f"unknown quantity {quantity!r}")
    u = unit.replace("µ", "u").replace("^2", "2").strip()
    if u not in table:
        raise UnitError(f"unit {unit!r} is not a known {quantity} unit ({', '.join(table)})")
    out = float(value) * table[u]
    if not math.isfinite(out):
        raise UnitError(f"non-finite {quantity} value {value!r}")
    return out


@dataclass
class Metric:
    """One measured (or explicitly unavailable) quantity."""

    quantity: str                 # area | delay | power | slack
    value: Optional[float]        # canonical unit; None unless status is measured/estimated
    unit: str                     # canonical unit string
    stage: str                    # e.g. post-synthesis, post-route
    source: str                   # e.g. dc:area.rpt, primetime:saif
    status: str = "measured"
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"bad metric status {self.status!r}")
        if self.status in ("measured", "estimated"):
            if self.value is None or not math.isfinite(self.value):
                raise ValueError(f"{self.quantity}: {self.status} metric requires a finite value")
        else:
            self.value = None
        if self.unit != CANONICAL_UNITS.get(self.quantity, self.unit):
            raise UnitError(f"{self.quantity} must be stored in {CANONICAL_UNITS[self.quantity]}")

    @property
    def ok(self) -> bool:
        return self.status == "measured" and self.value is not None

    @classmethod
    def measured(cls, quantity: str, value: float, unit: str, stage: str, source: str) -> "Metric":
        return cls(quantity, convert(value, unit, quantity), CANONICAL_UNITS[quantity], stage, source)

    @classmethod
    def unavailable(cls, quantity: str, stage: str, source: str, status: str = "missing", detail: str = "") -> "Metric":
        return cls(quantity, None, CANONICAL_UNITS[quantity], stage, source, status, detail)


@dataclass
class PPA:
    """A set of metrics for one design point plus timing feasibility."""

    metrics: Dict[str, Metric] = field(default_factory=dict)
    timing_met: Optional[bool] = None
    constraints_id: str = ""      # hash of the constraint set used (must match to compare)
    measurement_mode: str = ""    # e.g. full-ppa

    def get(self, q: str) -> Optional[float]:
        m = self.metrics.get(q)
        return m.value if m is not None and m.ok else None

    def require(self, quantities: Iterable[str]) -> list:
        """Return the list of human-readable problems for required quantities."""
        problems = []
        for q in quantities:
            m = self.metrics.get(q)
            if m is None:
                problems.append(f"{q}: not measured")
            elif not m.ok:
                problems.append(f"{q}: {m.status}{(' (' + m.detail + ')') if m.detail else ''}")
        return problems

    def stages(self) -> Dict[str, str]:
        return {q: m.stage for q, m in self.metrics.items()}

    def to_dict(self) -> dict:
        return {
            "metrics": {k: asdict(v) for k, v in self.metrics.items()},
            "timing_met": self.timing_met,
            "constraints_id": self.constraints_id,
            "measurement_mode": self.measurement_mode,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PPA":
        return cls(
            metrics={k: Metric(**v) for k, v in d.get("metrics", {}).items()},
            timing_met=d.get("timing_met"),
            constraints_id=d.get("constraints_id", ""),
            measurement_mode=d.get("measurement_mode", ""),
        )

    def summary(self) -> str:
        """Unit-explicit one-line summary used in logs and agent prompts."""
        parts = []
        for q in ("area", "delay", "power"):
            m = self.metrics.get(q)
            if m is None:
                parts.append(f"{q}=n/a")
            elif m.ok:
                parts.append(f"{q}={m.value:.6g} {m.unit} [{m.stage}]")
            else:
                parts.append(f"{q}=UNAVAILABLE({m.status})")
        if self.timing_met is not None:
            parts.append("timing=" + ("met" if self.timing_met else "VIOLATED"))
        return ", ".join(parts)


def comparable(a: PPA, b: PPA, quantities: Iterable[str]) -> list:
    """Problems that prevent comparing ``a`` and ``b`` on ``quantities``."""
    problems = []
    if a.constraints_id != b.constraints_id:
        problems.append("different constraint sets")
    if a.measurement_mode != b.measurement_mode:
        problems.append("different measurement modes")
    for q in quantities:
        ma, mb = a.metrics.get(q), b.metrics.get(q)
        if ma is None or mb is None or not ma.ok or not mb.ok:
            problems.append(f"{q}: missing on one side")
        elif ma.stage != mb.stage:
            problems.append(f"{q}: stage mismatch {ma.stage} vs {mb.stage}")
    return problems
