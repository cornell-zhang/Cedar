# SPDX-License-Identifier: Apache-2.0
"""Runs refuse to start when a required capability is missing (no silent downgrade)."""
from pathlib import Path

import pytest
import yaml

from cedar.workflow.engine import Run

RTL = "module m(input clk, input [3:0] a, output reg [3:0] y); always @(posedge clk) y <= a + 4'd1; endmodule\n"


def _setup(tmp_path: Path, with_equivalence: bool, verification: str = "conformal") -> Path:
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    (tmp_path / "m.v").write_text(RTL)
    tools = {"adapter_stub": None}
    tools = {
        "synthesis": {"adapter": "synopsys-dc", "executable": "true"},
        "place_route": {"adapter": "cadence-innovus", "executable": "true"},
        "simulation": {"adapter": "synopsys-vcs", "executable": "true"},
        "saif": {"adapter": "synopsys-vcd2saif", "executable": "true"},
        "power": {"adapter": "synopsys-primetime", "executable": "true"},
    }
    if with_equivalence:
        tools["equivalence"] = {"adapter": "cadence-conformal", "executable": "true"}
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump({"schema": "cedar/tool-profile@1", "name": "t", "tools": tools}))
    (tmp_path / "tech.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/technology@1", "name": "x", "units": {"time": "ps"}, "default_corner": "tt",
        "corners": {"tt": {"db": ["a.db"], "liberty": ["a.lib"], "sim_models": ["a.v"], "rc_tech": "rc"}},
        "lef": {"tech": "tech.lef", "cells": ["cells.lef"]}, "place_route": {"site": "core"}}))
    (tmp_path / "design.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/design@1", "name": "m", "top": "m", "rtl": ["m.v"],
        "profiles": {"tools": "tools.yaml", "technology": "tech.yaml"},
        "clocks": [{"port": "clk", "period_ns": 1.0}], "activity": {"mode": "generated"},
        "verification": {"mode": verification}, "objective": {"preset": "balanced"}}))
    return tmp_path / "design.yaml"


def test_missing_equivalence_tool_stops_run(tmp_path, toolchain, monkeypatch):
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = Run.create(str(_setup(tmp_path, with_equivalence=False)), str(tmp_path / "runs"), toolchain_prefix=sel)
    assert run.advance() == "missing_capability"
    assert "equivalence" in run.s["error"]
    assert run.s["candidates"] == {}          # nothing was measured or accepted


def test_power_objective_requires_full_ppa(tmp_path, toolchain, monkeypatch):
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    d = _setup(tmp_path, with_equivalence=True)
    doc = yaml.safe_load(d.read_text())
    doc["measurement"] = {"mode": "synthesis-only"}
    d.write_text(yaml.safe_dump(doc))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = Run.create(str(d), str(tmp_path / "runs"), toolchain_prefix=sel)
    assert run.advance() == "missing_capability"
    assert "power" in run.s["error"]


def test_run_lock_prevents_concurrent_advance(tmp_path, toolchain, monkeypatch):
    import fcntl

    import pytest

    from cedar.workflow.engine import WorkflowError
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = Run.create(str(_setup(tmp_path, with_equivalence=False)), str(tmp_path / "runs"), toolchain_prefix=sel)
    with open(run.dir / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        with pytest.raises(WorkflowError):
            Run(run.dir).advance()
    assert run.advance() == "missing_capability"


def test_run_wait_reports_final_state(tmp_path, toolchain, monkeypatch):
    from cedar.cli import main
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = Run.create(str(_setup(tmp_path, with_equivalence=False)), str(tmp_path / "runs"), toolchain_prefix=sel)
    assert main(["run", "wait", "--run", str(run.dir), "--timeout", "5"]) == 1   # running, nobody advancing
    run.advance()
    assert main(["run", "wait", "--run", str(run.dir), "--timeout", "5"]) == 1   # missing_capability -> failure


def test_unbound_clock_port_fails_closed(tmp_path, toolchain, monkeypatch):
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    d = _setup(tmp_path, with_equivalence=True)
    doc = yaml.safe_load(d.read_text())
    doc["clocks"] = [{"port": "core_clk", "period_ns": 1.0}]
    d.write_text(yaml.safe_dump(doc))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = Run.create(str(d), str(tmp_path / "runs"), toolchain_prefix=sel)
    assert run.advance() == "failed"
    assert "core_clk" in run.s["error"] and "clk" in run.s["error"]


def _resume_setup(tmp_path):
    d = _setup(tmp_path, with_equivalence=True)
    (tmp_path / "defs.vh").write_text("`define INC 4'd1\n")
    (tmp_path / "m.v").write_text('`include "defs.vh"\n' + RTL.replace("a + 4'd1", "a + `INC"))
    (tmp_path / "goal.sdc").write_text("create_clock -name clk -period 1 [get_ports clk]\n")
    doc = yaml.safe_load(d.read_text())
    doc["sdc"] = "goal.sdc"
    d.write_text(yaml.safe_dump(doc))
    return d


@pytest.mark.parametrize("edit", ["sdc", "header"])
def test_resume_rejects_changed_sdc_or_header(tmp_path, toolchain, monkeypatch, edit):
    from cedar.workflow.engine import WorkflowError
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = Run.create(str(_resume_setup(tmp_path)), str(tmp_path / "runs"), toolchain_prefix=sel)
    fps = run.s["input_fingerprints"]
    assert fps[str(tmp_path / "goal.sdc")][0] == "sdc" and fps[str(tmp_path / "defs.vh")][0] == "hdl"
    Run(run.dir)._check_inputs_unchanged()                    # unchanged: fine
    if edit == "sdc":
        (tmp_path / "goal.sdc").write_text("create_clock -name clk -period 100 [get_ports clk]\n")
    else:
        (tmp_path / "defs.vh").write_text("`define INC 4'd2\n")
    resumed = Run(run.dir)
    with pytest.raises(WorkflowError) as e:
        resumed.advance()
    assert ("goal.sdc" if edit == "sdc" else "defs.vh") in str(e.value)
    if edit == "sdc":
        assert "constraints id" in str(e.value)


def _admitted_run(tmp_path, toolchain, monkeypatch):
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    run = Run.create(str(_setup(tmp_path, with_equivalence=True)), str(tmp_path / "runs"), toolchain_prefix=sel)
    assert run.phase_analysis() == "next"
    assert run.phase_rules_admit() == "next"          # real Z3 admission of the seed rules
    assert run.s["rules"]["admitted_instances"]
    run.save()                                        # the engine saves after every phase
    return run


def test_instances_follow_certified_inventory(tmp_path, toolchain, monkeypatch):
    from cedar.rules import certify
    from cedar.workflow.engine import WorkflowError
    run = _admitted_run(tmp_path, toolchain, monkeypatch)
    insts = run._checked_instances()
    assert len(insts) == len(run.s["rules"]["admitted_instances"])
    for (rule, w, text), e in zip(insts, run.s["rules"]["admitted_instances"]):
        assert certify.rule_sha(text) == e["certificate"]["instance_sha256"]
    idir = run.dir / "rules" / "instances"
    f = idir / "add_commute__w4.egg"
    orig = f.read_text()
    # edited: an unproved top-level union appended to a certified instance
    f.write_text(orig + '\n(union (hw_constant (NamedAttr "value" (IntegerAttr 0 (I4))) (I4)) '
                 '(hw_constant (NamedAttr "value" (IntegerAttr 1 (I4))) (I4)))\n')
    with pytest.raises(WorkflowError, match="differs from its certified text"):
        Run(run.dir)._instances(["add_commute"])
    with pytest.raises(WorkflowError):
        Run(run.dir).phase_probe()
    f.write_text(orig)
    # added: a file that is not in the admission inventory
    (idir / "evil__w4.egg").write_text("(rule () () :ruleset rules)")
    with pytest.raises(WorkflowError, match="unrecorded"):
        Run(run.dir)._checked_instances()
    (idir / "evil__w4.egg").unlink()
    assert Run(run.dir)._instances(["add_commute"])


def test_stale_verifier_instances_are_reproved_or_rejected(tmp_path, toolchain, monkeypatch):
    import json

    from cedar.rules import certify
    from cedar.workflow.engine import WorkflowError
    run = _admitted_run(tmp_path, toolchain, monkeypatch)
    for e in run.s["rules"]["admitted_instances"]:
        e["certificate"]["verifier"] = "0ld-verifier"
    run.save()
    r2 = Run(run.dir)
    texts = r2._instances(["mul_factor_common"])            # re-proved from the recorded source
    assert texts and all(e["certificate"]["verifier"] == certify.verifier_hash()
                         for e in r2.s["rules"]["admitted_instances"] if e["rule"] == "mul_factor_common")
    s = json.loads((run.dir / "run.json").read_text())
    for e in s["rules"]["admitted_instances"]:
        e["certificate"]["verifier"] = "0ld-verifier"
    s["rules"]["sources"] = {}
    (run.dir / "run.json").write_text(json.dumps(s))
    with pytest.raises(WorkflowError, match="older verifier"):
        Run(run.dir)._instances(["add_commute"])
