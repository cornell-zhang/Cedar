# SPDX-License-Identifier: Apache-2.0
import math

import pytest

from cedar.frontier import Frontier, Point
from cedar.metrics import PPA, Metric, UnitError, comparable, convert
from cedar.objective import Objective, ObjectiveError


def ppa(a, d, p, met=True, stage_p="post-route", cid="c1"):
    m = {}
    m["area"] = Metric.measured("area", a, "um2", "post-synthesis", "dc") if a is not None else Metric.unavailable("area", "post-synthesis", "dc")
    m["delay"] = Metric.measured("delay", d, "ns", "post-synthesis", "dc") if d is not None else Metric.unavailable("delay", "post-synthesis", "dc")
    m["power"] = Metric.measured("power", p, "uW", stage_p, "pt") if p is not None else Metric.unavailable("power", stage_p, "pt", "failed")
    return PPA(m, met, cid, "full-ppa")


def test_units_convert_explicitly():
    assert convert(661.13, "ps", "delay") == pytest.approx(0.66113)
    assert convert(2.296e-4, "W", "power") == pytest.approx(229.6)
    assert convert(1.5, "mW", "power") == pytest.approx(1500.0)
    with pytest.raises(UnitError):
        convert(1.0, "furlongs", "delay")


def test_missing_metric_is_not_zero():
    m = Metric.unavailable("power", "post-route", "primetime", "failed", "PT crashed")
    assert m.value is None and not m.ok
    p = ppa(10, 1, None)
    assert p.get("power") is None
    assert p.require(["power"]) == ["power: failed"]
    with pytest.raises(ValueError):
        Metric("power", None, "uW", "post-route", "pt", "measured")


def test_objective_normalization_and_missing_reference():
    obj = Objective.from_config({"weights": {"area": 0.3, "delay": 0.2, "power": 0.5}})
    base = ppa(100, 1.0, 200)
    s, probs = obj.score(ppa(50, 1.0, 100), base)
    assert probs == [] and s == pytest.approx(0.3 * 0.5 + 0.2 * 1.0 + 0.5 * 0.5)
    s, probs = obj.score(ppa(50, 1.0, None), base)
    assert s is None and "power unavailable" in probs
    s, probs = obj.score(ppa(50, 1.0, 100), ppa(100, 1.0, 0.0))
    assert s is None and any("non-positive" in x for x in probs)


def test_objective_validation_and_schedule():
    with pytest.raises(ObjectiveError):
        Objective.from_config({"weights": {"area": 0, "delay": 0, "power": 0}})
    with pytest.raises(ObjectiveError):
        Objective.from_config({"weights": {"area": -1, "delay": 1, "power": 0}})
    with pytest.raises(ObjectiveError):
        Objective.from_config({})
    obj = Objective.from_config({"preset": "balanced", "weight_schedule": [
        {"from_iteration": 3, "weights": {"area": 1.0}}]})
    assert obj.weights_at(1)["delay"] > 0
    assert obj.weights_at(3) == {"area": 1.0, "delay": 0.0, "power": 0.0}
    assert "lower is better" in obj.describe()


def test_infeasible_never_ranks_above_feasible():
    obj = Objective.from_config({"preset": "balanced"})
    base = ppa(100, 1.0, 100)
    good_but_violating = ppa(10, 0.1, 10, met=False)
    ok = ppa(90, 1.0, 90, met=True)
    assert obj.rank_key(ok, base) < obj.rank_key(good_but_violating, base)


def test_frontier_membership_change_not_cardinality():
    # Audit probe: replacing (10,10,10) by dominating (9,9,9) keeps |F| == 1 but
    # must be reported as a frontier change.
    fr = Frontier(("area", "delay", "power"))
    assert fr.add(Point("a", ppa(10, 10, 10), "extracted", True))
    assert fr.add(Point("b", ppa(9, 9, 9), "extracted", True))
    assert fr.names() == ["b"]
    assert not fr.add(Point("c", ppa(11, 11, 11), "extracted", True))


def test_frontier_rejects_unverified_missing_and_inconsistent():
    base = ppa(100, 1, 100)
    fr = Frontier(("area", "delay", "power"), reference=base)
    assert not fr.add(Point("unverified", ppa(1, 1, 1), "direct", False))
    assert not fr.add(Point("nopower", ppa(1, 1, None), "direct", True))
    assert not fr.add(Point("stage", ppa(1, 1, 1, stage_p="post-synthesis"), "direct", True))
    assert not fr.add(Point("othercons", ppa(1, 1, 1, cid="other"), "direct", True))
    assert not fr.add(Point("violating", ppa(1, 1, 1, met=False), "direct", True))
    assert fr.points() == []
    assert len(fr.rejected) == 5
    assert comparable(ppa(1, 1, 1), base, ["area"]) == []
