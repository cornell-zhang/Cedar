# SPDX-License-Identifier: Apache-2.0
"""Measurement stages fail closed on tool errors, not only on exit status.

Real DC/PrimeTime/VCS/Innovus report most command failures as messages and
exit 0 after reaching the end of the script. These tests run Cedar's stage
adapters against stand-in tools (``tests/fake_eda.py``) that produce the real
tools' message formats, and check that every failure -- a constraint command
error, a malformed or missing SPEF, an aborted or missing SDF annotation, an
Innovus error outside the profile's accepted list -- fails its stage, makes
power or synthesis metrics unavailable, and propagates: the baseline is
rejected, and a candidate is excluded from acceptance, the frontier and
successful-iteration credit. Harmless warnings stay accepted.
"""
import json
import sys
from pathlib import Path

import pytest
import yaml

from cedar import config as C
from cedar import measure as M
from cedar.eda import flow
from cedar.eda.runner import ToolEnv

FAKE = Path(__file__).with_name("fake_eda.py")
RTL = "module m(input clk, input [3:0] a, b, output reg [3:0] y); always @(posedge clk) y <= a * b + a; endmodule\n"
VARIANT = "module m(input clk, input [3:0] a, b, output reg [3:0] y); always @(posedge clk) y <= a * (b + 4'd1); endmodule\n"


def _profiles(tmp_path: Path, accepted=("IMPTR-2101",)) -> None:
    wrapper = tmp_path / "fake-eda"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    (tmp_path / "m.v").write_text(RTL)
    tools = {"synthesis": ("synopsys-dc", "dc"), "place_route": ("cadence-innovus", "innovus"),
             "simulation": ("synopsys-vcs", "vcs"), "saif": ("synopsys-vcd2saif", "vcd2saif"),
             "power": ("synopsys-primetime", "pt"), "equivalence": ("cadence-conformal", None)}
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump({"schema": "cedar/tool-profile@1", "name": "fake", "tools": {
        k: ({"adapter": a, "executable": str(wrapper), "args": [t]} if t else {"adapter": a, "executable": "true"})
        for k, (a, t) in tools.items()}}))
    (tmp_path / "tech.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/technology@1", "name": "x", "units": {"time": "ps"}, "default_corner": "tt",
        "corners": {"tt": {"db": ["a.db"], "liberty": ["a.lib"], "sim_models": ["a.v"], "rc_tech": "rc"}},
        "lef": {"tech": "tech.lef", "cells": ["cells.lef"]},
        "place_route": {"site": "core", "accepted_errors": list(accepted)}}))


def _ctx(tmp_path: Path, accepted=("IMPTR-2101",)) -> flow.FlowContext:
    _profiles(tmp_path, accepted)
    return flow.FlowContext(ToolEnv(C.load_tool_profile(str(tmp_path / "tools.yaml"))),
                            C.load_technology(str(tmp_path / "tech.yaml")), "tt", "m",
                            [{"port": "clk", "period_ns_resolved": 1.0}], 0.1)


ACTIVITY = {"testbench": "tb.v", "tb_top": "Top", "dut_instance": "DUT"}


def _measure(tmp_path, monkeypatch, fault="", accepted=("IMPTR-2101",)):
    monkeypatch.setenv("CEDAR_FAKE_FAULT", fault)
    ctx = _ctx(tmp_path, accepted)
    return M.measure(ctx, [str(tmp_path / "m.v")], tmp_path / "work", ACTIVITY)


def _stage(m, name):
    return next(s for s in m.stages if s.stage == name)


# ------------------------------------------------------------ stage adapters

def test_valid_inputs_with_harmless_warnings_are_measured(tmp_path, monkeypatch):
    m = _measure(tmp_path, monkeypatch)
    assert m.complete, [(s.stage, s.status, s.detail) for s in m.stages]
    assert m.ppa.metrics["power"].ok and m.ppa.metrics["area"].ok
    pnr = _stage(m, "place_route")
    assert [e["code"] for e in pnr.evidence["accepted_errors"]] == ["IMPTR-2101"]
    assert pnr.evidence["accepted_errors"][0]["step"] == "power_route"
    assert "init" in pnr.evidence["steps"]                # marker interleaved with tool output
    pw = _stage(m, "power")
    assert pw.evidence["parasitics"]["ok"] and pw.evidence["parasitics"]["annotated_nets"] == 683
    assert pw.evidence["parasitic_annotation"]["annotated_pct"] == 100.0
    act = _stage(m, "activity")
    assert act.evidence["sdf_annotation"]["ok"] and act.evidence["sdf_annotation"]["annotated_instances"] == 599
    assert {"compile_log", "sdf_annotate_info", "run_log", "saif_log"} <= set(act.outputs)
    assert json.dumps(m.to_dict())                      # evidence is recorded with the stage result


@pytest.mark.parametrize("fault, stage, needle", [
    ("dc_sdc", "synthesis", "constraints: CMD-005"),
    ("dc_compile", "synthesis", "compile: UID-109"),
    ("innovus_sdc", "place_route", "init: TCLCMD-917"),
    ("innovus_sdc_skipped", "place_route", "init: CTE-25"),
    ("innovus_extract", "place_route", "extract: IMPEXT-2773"),
    ("innovus_no_spef", "place_route", "post_route.spef"),
    ("sdf_aborted", "activity", "SDF annotation aborted"),
    ("pt_spef_bad", "power", "SPFP-100"),
    ("pt_sdc", "power", "constraints: CMD-005"),
])
def test_tool_errors_fail_the_stage(fault, stage, needle, tmp_path, monkeypatch):
    m = _measure(tmp_path, monkeypatch, fault)
    st = _stage(m, stage)
    assert st.status == "failed" and needle in st.detail, (st.status, st.detail)
    assert not m.complete
    if stage == "synthesis":
        assert not m.ppa.metrics["area"].ok and not m.ppa.metrics["delay"].ok
    assert not m.ppa.metrics["power"].ok and m.ppa.metrics["power"].value is None


def test_innovus_error_needs_explicit_acceptance(tmp_path, monkeypatch):
    m = _measure(tmp_path, monkeypatch, accepted=())
    st = _stage(m, "place_route")
    assert st.status == "failed" and "power_route: IMPTR-2101" in st.detail
    assert not m.ppa.metrics["power"].ok


def test_accepted_error_ids_never_cover_critical_steps(tmp_path, monkeypatch):
    m = _measure(tmp_path, monkeypatch, "innovus_sdc", accepted=("IMPTR-2101", "TCLCMD-917"))
    st = _stage(m, "place_route")
    assert st.status == "failed" and "init: TCLCMD-917" in st.detail


def test_missing_sdf_and_spef_fail(tmp_path, monkeypatch):
    m = _measure(tmp_path, monkeypatch)
    assert m.complete
    w = tmp_path / "work"
    pnr = _stage(m, "place_route").outputs
    act = flow.activity_sim(m_ctx := _ctx(tmp_path), pnr["netlist"], str(w / "nope.sdf"), "tb.v", "Top", "DUT",
                            w / "activity-missing")
    assert act.status == "failed" and "SDF Error" in act.detail
    assert not (w / "activity-missing" / "waves.saif").exists()      # never simulated
    pw = flow.power_pt(m_ctx, pnr["netlist"], str(w / "nope.spef"), str(w / "activity" / "waves.saif"),
                       str(w / "synth" / "constraints.sdc"), "Top", "DUT", w / "power-missing")
    assert pw.status == "failed" and "PARA-001" in pw.detail
    assert not flow.power_metric(pw).ok


# ------------------------------------------------------------- log parsers

def test_tool_error_parsing_ignores_echoes_and_warnings():
    snps = ('puts "CEDAR_STEP constraints"\nCEDAR_STEP constraints\nWarning: ignored (UITE-489)\n'
            'Error: Expected keyword *SPEF but found \'X\'\n\tat line 1 in file x. (SPFP-100)\n'
            'if {[catch {x} e]} { puts "CEDAR_ERROR analyze: $e"; exit 2 }\n')
    assert flow.tool_errors(snps) == [{"step": "constraints", "code": "SPFP-100",
                                       "message": "Expected keyword *SPEF but found 'X'"}]
    cdns = ('    puts "CEDAR_STEP $step"\n    if {[catch {uplevel #0 $body} e]} { puts "CEDAR_ERROR ${step}: $e" }\n'
            "<CMD> set init_verilog /aCEDAR_STEP init\n**WARN: (IMPLF-45):\tno SITE\n"
            "CEDAR_STEP route\n**ERROR: (IMPDB-1234):\tbad\nCEDAR_ERROR route: routeDesign failed\n")
    errs = flow.tool_errors(cdns, "cadence")
    assert [(e["step"], e["code"]) for e in errs] == [("route", "IMPDB-1234"), ("route", "CEDAR")]
    assert flow.steps_seen(cdns) == ["init", "route"]


def test_annotation_parsers(tmp_path):
    good = tmp_path / "good.log"
    good.write_text(f"Report : read_parasitics {tmp_path}/a.spef\n        0 error(s)\n"
                    "        Annotated nets    :   683\n")
    (tmp_path / "a.spef").write_text("*SPEF\n")
    assert flow.parse_parasitics_log(good, str(tmp_path / "a.spef"))["ok"]
    assert not flow.parse_parasitics_log(good, str(tmp_path / "other.spef"))["ok"]      # a different file
    assert not flow.parse_parasitics_log(tmp_path / "absent.log", str(tmp_path / "a.spef"))["ok"]
    rpt = tmp_path / "ann.rpt"
    rpt.write_text("Internal nets |\n  - Pin to pin nets |  100 | 0 | 0 |  90 |  10 |\n")
    a = flow.parse_annotated_parasitics(rpt)
    assert a["annotated_pct"] == 90.0 and not a["ok"]
    log = tmp_path / "c.log"
    info = tmp_path / "info"
    sdf = tmp_path / "d.sdf"
    sdf.write_text("(DELAYFILE)")
    info.write_text("# Annotated entries in elaborated design:\n#  IOPATH Delays = 10\n"
                    "#  Annotated instances = 5\n#  Total instances = 6\n")
    log.write_text(f'$sdf_annotate()\n SDF file: "{sdf}"\n Annotation scope: Top.DUT\n'
                   " Total errors: 0\n SDF annotation completed\n")
    assert flow.check_sdf_annotation(log, info, str(sdf), "Top.DUT")["ok"]
    assert not flow.check_sdf_annotation(log, info, str(sdf), "Top.OTHER")["ok"]       # wrong instance
    info.write_text("# Annotated entries in elaborated design:\n#\tNo information read.\n"
                    "# Static entries in elaborated design:\n#  IOPATH Delays = 3306\n")
    assert not flow.check_sdf_annotation(log, info, str(sdf), "Top.DUT")["ok"]


# ------------------------------------------------------------- propagation

def _run(tmp_path, toolchain, monkeypatch, fault):
    from cedar.workflow import engine as E
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CEDAR_FAKE_FAULT", fault)
    monkeypatch.setattr(E.Run, "_verify", lambda self, name, rtl: {
        "status": "passed", "backend": "stub", "detail": "stub", "seconds": 0.0, "evidence": ""})
    monkeypatch.setattr(E.Run, "phase_attribution", lambda self: "next")
    monkeypatch.setattr(E.Run, "_attribute", lambda self, name, rec: None)
    _profiles(tmp_path)
    (tmp_path / "tb.v").write_text("module Top; endmodule\n")
    (tmp_path / "design.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/design@1", "name": "m", "top": "m", "rtl": ["m.v"],
        "profiles": {"tools": "tools.yaml", "technology": "tech.yaml"},
        "clocks": [{"port": "clk", "period_ns": 1.0}], "activity": {"mode": "testbench", "testbench": "tb.v"},
        "verification": {"mode": "conformal"}, "objective": {"preset": "balanced"},
        "search": {"preset": "quick", "rules_to_propose": 0}}))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    return E.Run.create(str(tmp_path / "design.yaml"), str(tmp_path / "runs"), toolchain_prefix=sel)


@pytest.mark.parametrize("fault", ["pt_spef_bad@baseline", "sdf_aborted@baseline", "dc_sdc@baseline"])
def test_failed_baseline_stage_stops_the_run(fault, tmp_path, toolchain, monkeypatch):
    run = _run(tmp_path, toolchain, monkeypatch, fault)
    assert run.advance() == "failed"
    assert "baseline measurement incomplete" in run.s["error"]
    assert "failed" in run.s["error"]


@pytest.mark.parametrize("fault", ["pt_spef_bad@iter01", "sdf_aborted@iter01", "dc_sdc@iter01"])
def test_failed_candidate_stage_is_excluded_everywhere(fault, tmp_path, toolchain, monkeypatch):
    run = _run(tmp_path, toolchain, monkeypatch, fault)
    assert run.advance() == "awaiting_agent"
    t = run.pending_task()
    Path(t.outputs[0].path).write_text(VARIANT)
    Path(t.outputs[-1].path).write_text('{"variants": []}')
    assert run.submit(t.id, {"host": "test"})[0]
    assert run.advance() == "incomplete"
    rep = json.loads((run.dir / "report.json").read_text())
    assert "iter01" in rep["excluded_candidates"] and "iter01" not in rep["accepted_candidates"]
    assert "iter01" not in [p["name"] for p in rep["frontier"]["points"]]
    assert rep["execution_contract"]["successful_iterations"] == 0
    assert run.s["iterations"][0]["successful"] is False
    assert any("failed" in p for p in rep["iteration_outcomes"][0]["problems"])
    assert run.s["candidates"]["baseline"]["measurement"]["complete"]          # the baseline was measured
