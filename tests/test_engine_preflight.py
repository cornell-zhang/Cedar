# SPDX-License-Identifier: Apache-2.0
"""Runs refuse to start when a required capability is missing (no silent downgrade)."""
from pathlib import Path

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
