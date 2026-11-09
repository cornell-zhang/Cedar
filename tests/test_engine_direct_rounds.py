# SPDX-License-Identifier: Apache-2.0
"""Direct-rewrite rounds converge on the weighted objective, not on area (A8).

EDA measurement and equivalence checking are stubbed; the toolchain front end
still checks every submitted candidate and Z3 still certifies the rule pool.
"""
from pathlib import Path

import yaml

from cedar.metrics import PPA, Metric
from cedar.workflow import engine as E

RTL = "module m(input clk, input [3:0] a, b, output reg [3:0] y); always @(posedge clk) y <= a * b + a; endmodule\n"
VARIANT = "module m(input clk, input [3:0] a, b, output reg [3:0] y); always @(posedge clk) y <= a * (b + 4'd1); endmodule\n"

# name -> (area um2, delay ns, power uW); objective weights all on power, so a
# smaller-area but higher-power round-2 variant must NOT count as improvement.
FAKE = {"baseline": (100.0, 1.0, 100.0), "direct_1": (90.0, 1.0, 80.0), "direct_r2_1": (50.0, 1.0, 85.0)}


def _fake_measure(self, name, rtl):
    a, d, p = FAKE[name]
    ppa = PPA({"area": Metric.measured("area", a, "um2", "post-synthesis", "stub"),
               "delay": Metric.measured("delay", d, "ns", "post-synthesis", "stub"),
               "slack": Metric.measured("slack", 0.1, "ns", "post-synthesis", "stub"),
               "power": Metric.measured("power", p, "uW", "post-route", "stub")},
              True, self.s["constraints_id"], "full-ppa")
    return {"ppa": ppa.to_dict(), "stages": [], "pruned": False, "complete": True, "info": {}}


def _fake_verify(self, name, rtl):
    return {"status": "passed", "backend": "stub", "detail": "stub", "seconds": 0.0, "evidence": ""}


class _NoSynth:
    ok = False
    status = "skipped"
    detail = "stub"
    outputs = {}


def test_direct_rounds_converge_on_objective(tmp_path, toolchain, monkeypatch):
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(E.Run, "_measure", _fake_measure)
    monkeypatch.setattr(E.Run, "_verify", _fake_verify)
    monkeypatch.setattr(E.flow, "synthesize", lambda *a, **k: _NoSynth())
    monkeypatch.setattr(E.flow, "synth_metrics", lambda *a, **k: {})
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    (tmp_path / "m.v").write_text(RTL)
    stages = {"synthesis": "synopsys-dc", "place_route": "cadence-innovus", "simulation": "synopsys-vcs",
              "saif": "synopsys-vcd2saif", "power": "synopsys-primetime", "equivalence": "cadence-conformal"}
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump({"schema": "cedar/tool-profile@1", "name": "t", "tools": {
        k: {"adapter": v, "executable": "true"} for k, v in stages.items()}}))
    (tmp_path / "tech.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/technology@1", "name": "x", "units": {"time": "ps"}, "default_corner": "tt",
        "corners": {"tt": {"db": ["a.db"], "liberty": ["a.lib"], "sim_models": ["a.v"], "rc_tech": "rc"}},
        "lef": {"tech": "tech.lef", "cells": ["cells.lef"]}, "place_route": {"site": "core"}}))
    (tmp_path / "design.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/design@1", "name": "m", "top": "m", "rtl": ["m.v"],
        "profiles": {"tools": "tools.yaml", "technology": "tech.yaml"},
        "clocks": [{"port": "clk", "period_ns": 1.0}], "activity": {"mode": "testbench", "testbench": "m.v"},
        "verification": {"mode": "conformal"}, "objective": {"weights": {"power": 1.0}},
        "search": {"preset": "paper", "direct_variants": 1, "direct_rounds": 4, "direct_min_rounds": 2,
                   "rules_to_propose": 0}}))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = E.Run.create(str(tmp_path / "design.yaml"), str(tmp_path / "runs"), toolchain_prefix=sel)
    assert run.advance() == "awaiting_agent"
    t1 = run.pending_task()
    assert t1.kind == "direct_rewrite" and "round 1" in Path(t1.prompt_file).read_text()
    Path(t1.outputs[0].path).write_text(VARIANT)
    Path(t1.outputs[-1].path).write_text('{"variants": []}')
    assert run.submit(t1.id, {"host": "test"})[0]
    assert run.advance() == "awaiting_agent"
    t2 = run.pending_task()
    assert t2.kind == "direct_rewrite" and t2.meta["round"] == 2
    ctx = Path(t2.prompt_file).read_text()
    assert "Best so far: direct_1" in ctx
    Path(t2.outputs[0].path).write_text(VARIANT)
    Path(t2.outputs[-1].path).write_text('{"variants": []}')
    assert run.submit(t2.id, {"host": "test"})[0]
    status = run.advance()
    rounds = run.s["direct_rounds"]
    assert [r["round"] for r in rounds] == [1, 2]
    assert rounds[0]["improved"] is True and rounds[0]["decision"] == "continue"
    # round 2 halves the area but raises power: no objective improvement -> converge
    assert rounds[1]["improved"] is False and rounds[1]["decision"].startswith("converged")
    assert status == "awaiting_agent" and run.pending_task().kind == "select_rules"   # paper preset
    assert run.s["candidates"]["direct_r2_1"]["round"] == 2


def test_failed_verification_excluded_end_to_end(tmp_path, toolchain, monkeypatch):
    """quick preset: a wrong direct rewrite is excluded; the e-graph candidate
    (real toolchain eqsat + extraction) is measured and reported."""
    import json

    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    FAKE.update({"iter01": (60.0, 1.0, 70.0)})
    monkeypatch.setattr(E.Run, "_measure", _fake_measure)
    monkeypatch.setattr(E.Run, "_verify", lambda self, name, rtl: {
        "status": "failed" if name.startswith("direct") else "passed", "backend": "stub",
        "detail": "stub verdict", "seconds": 0.0, "evidence": ""})
    monkeypatch.setattr(E.flow, "synthesize", lambda *a, **k: _NoSynth())
    monkeypatch.setattr(E.flow, "synth_metrics", lambda *a, **k: {})
    monkeypatch.setattr(E.Run, "phase_attribution", lambda self: "next")
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    (tmp_path / "m.v").write_text(RTL)
    stages = {"synthesis": "synopsys-dc", "place_route": "cadence-innovus", "simulation": "synopsys-vcs",
              "saif": "synopsys-vcd2saif", "power": "synopsys-primetime", "equivalence": "cadence-conformal"}
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump({"schema": "cedar/tool-profile@1", "name": "t", "tools": {
        k: {"adapter": v, "executable": "true"} for k, v in stages.items()}}))
    (tmp_path / "tech.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/technology@1", "name": "x", "units": {"time": "ps"}, "default_corner": "tt",
        "corners": {"tt": {"db": ["a.db"], "liberty": ["a.lib"], "sim_models": ["a.v"], "rc_tech": "rc"}},
        "lef": {"tech": "tech.lef", "cells": ["cells.lef"]}, "place_route": {"site": "core"}}))
    (tmp_path / "design.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/design@1", "name": "m", "top": "m", "rtl": ["m.v"],
        "profiles": {"tools": "tools.yaml", "technology": "tech.yaml"},
        "clocks": [{"port": "clk", "period_ns": 1.0}], "activity": {"mode": "testbench", "testbench": "m.v"},
        "verification": {"mode": "conformal"}, "objective": {"preset": "balanced"},
        "search": {"preset": "quick", "rules_to_propose": 0}}))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = E.Run.create(str(tmp_path / "design.yaml"), str(tmp_path / "runs"), toolchain_prefix=sel)
    assert run.advance() == "awaiting_agent"
    t1 = run.pending_task()
    Path(t1.outputs[0].path).write_text(VARIANT)
    Path(t1.outputs[-1].path).write_text('{"variants": []}')
    assert run.submit(t1.id, {"host": "test"})[0]
    assert run.advance() == "completed"
    rep = json.loads((run.dir / "report.json").read_text())
    assert rep["excluded_candidates"] == ["direct_1"]
    assert "direct_1" not in rep["accepted_candidates"] and "iter01" in rep["accepted_candidates"]
    assert "measurement" not in run.s["candidates"]["direct_1"]          # never measured
    assert [p["name"] for p in rep["frontier"]["points"]] == ["iter01"]   # dominates the baseline here


def test_power_timeout_cannot_make_quick_run_succeed(tmp_path, toolchain, monkeypatch):
    """quick preset: the only extracted candidate's power stage times out, so
    there is no successful iteration and the run must end incomplete."""
    import json

    def measure(self, name, rtl):
        FAKE["iter01"] = (60.0, 1.0, 70.0)
        if name != "iter01":
            return _fake_measure(self, name, rtl)
        md = _fake_measure(self, name, rtl)
        md["ppa"]["metrics"]["power"] = Metric.unavailable("power", "post-route", "primetime", "failed",
                                                           "power stage timed out").__dict__
        md["stages"] = [{"stage": "synthesis", "status": "ok", "seconds": 1.0},
                        {"stage": "power", "status": "timeout", "seconds": 900.0}]
        md["complete"] = False
        return md

    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(E.Run, "_measure", measure)
    monkeypatch.setattr(E.Run, "_verify", _fake_verify)
    monkeypatch.setattr(E.flow, "synthesize", lambda *a, **k: _NoSynth())
    monkeypatch.setattr(E.flow, "synth_metrics", lambda *a, **k: {})
    monkeypatch.setattr(E.Run, "phase_attribution", lambda self: "next")
    monkeypatch.setattr(E.Run, "_attribute", lambda self, name, rec: None)
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    (tmp_path / "m.v").write_text(RTL)
    stages = {"synthesis": "synopsys-dc", "place_route": "cadence-innovus", "simulation": "synopsys-vcs",
              "saif": "synopsys-vcd2saif", "power": "synopsys-primetime", "equivalence": "cadence-conformal"}
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump({"schema": "cedar/tool-profile@1", "name": "t", "tools": {
        k: {"adapter": v, "executable": "true"} for k, v in stages.items()}}))
    (tmp_path / "tech.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/technology@1", "name": "x", "units": {"time": "ps"}, "default_corner": "tt",
        "corners": {"tt": {"db": ["a.db"], "liberty": ["a.lib"], "sim_models": ["a.v"], "rc_tech": "rc"}},
        "lef": {"tech": "tech.lef", "cells": ["cells.lef"]}, "place_route": {"site": "core"}}))
    (tmp_path / "design.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/design@1", "name": "m", "top": "m", "rtl": ["m.v"],
        "profiles": {"tools": "tools.yaml", "technology": "tech.yaml"},
        "clocks": [{"port": "clk", "period_ns": 1.0}], "activity": {"mode": "testbench", "testbench": "m.v"},
        "verification": {"mode": "conformal"}, "objective": {"preset": "balanced"},
        "search": {"preset": "quick", "rules_to_propose": 0}}))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = E.Run.create(str(tmp_path / "design.yaml"), str(tmp_path / "runs"), toolchain_prefix=sel)
    assert run.advance() == "awaiting_agent"
    t1 = run.pending_task()
    Path(t1.outputs[0].path).write_text(VARIANT)
    Path(t1.outputs[-1].path).write_text('{"variants": []}')
    assert run.submit(t1.id, {"host": "test"})[0]
    assert run.advance() == "incomplete"
    rep = json.loads((run.dir / "report.json").read_text())
    c = rep["execution_contract"]
    assert c["status"] == "incomplete" and c["successful_iterations"] == 0 and c["attempted_iterations"] == 1
    assert "iter01" in rep["excluded_candidates"] and "iter01" not in rep["accepted_candidates"]
    out = rep["iteration_outcomes"][0]
    assert out["successful"] is False and any("power" in p for p in out["problems"])
    assert any("timeout" in p for p in out["problems"])
    assert run.s["iterations"][0]["successful"] is False
    assert run.s["status"] == "incomplete"
