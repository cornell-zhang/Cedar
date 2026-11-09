# SPDX-License-Identifier: Apache-2.0
"""Tool identity: end-to-end validation and runs notice changed tools.

A validation record (``cedar flow-check``) and a run's provenance store, per
configured tool, the resolved path, the executable's real path and content
hash and an adapter-specific version probe, plus the environment setup
identity. Replacing a tool at the same path, changing what a wrapper runs, or
changing the setup environment makes the validation stale and stops a resume.
"""
import json
import time
from pathlib import Path

import pytest
import yaml

from cedar import config as C
from cedar import setup as S


def _exe(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o755)
    return path


def _profiles(tmp_path: Path, setup_script=None, env_required=()):
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    dc = _exe(tmp_path / "dc", "echo 'dc_shell version    -  V-1'")
    tools = {"schema": "cedar/tool-profile@1", "name": "probe",
             "tools": {"synthesis": {"adapter": "synopsys-dc", "executable": str(dc)}}}
    if setup_script:
        tools["environment"] = {"setup_script": str(setup_script), "env_required": list(env_required)}
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump(tools))
    (tmp_path / "tech.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/technology@1", "name": "probe", "units": {"time": "ps"}, "default_corner": "tt",
        "corners": {"tt": {"db": ["a.db"], "liberty": ["a.lib"], "sim_models": ["a.v"], "rc_tech": "rc"}},
        "lef": {"tech": "tech.lef", "cells": ["cells.lef"]}, "place_route": {"site": "core"}}))
    return dc


def _load(tmp_path):
    return C.load_tool_profile(str(tmp_path / "tools.yaml")), C.load_technology(str(tmp_path / "tech.yaml"))


def _validated(tmp_path, monkeypatch, **kw):
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    dc = _profiles(tmp_path, **kw)
    t, te = _load(tmp_path)
    S.save_validation(t, te, {"passed": True, "stages": {}})
    assert S.load_validation(t, te)["state"] == "validated"
    return dc


def test_replacement_at_the_same_path_is_stale(tmp_path, monkeypatch):
    """The reviewer's case: same path, different executable."""
    dc = _validated(tmp_path, monkeypatch)
    _exe(dc, "echo DC_VERSION_2")
    v = S.load_validation(*_load(tmp_path))
    assert v["state"] == "stale"
    assert any(c.startswith("synthesis: sha256") for c in v["changes"])


def test_changed_wrapper_target_is_stale_by_version(tmp_path, monkeypatch):
    real = _exe(tmp_path / "real-dc", "echo 'dc_shell version    -  V-1'")
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    _profiles(tmp_path)
    _exe(tmp_path / "dc", f'exec "{real}" "$@"')                  # wrapper; its content never changes
    t, te = _load(tmp_path)
    S.save_validation(t, te, {"passed": True, "stages": {}})
    rec = json.loads(Path(S.load_validation(t, te)["record"]).read_text())
    assert rec["identity"]["tools"]["synthesis"]["version"] == "V-1"
    _exe(real, "echo 'dc_shell version    -  V-2'")
    v = S.load_validation(t, te)
    assert v["state"] == "stale" and "synthesis: version V-1 -> V-2" in v["changes"]
    assert not any("sha256" in c for c in v["changes"])          # the wrapper itself is unchanged


def test_changed_setup_script_is_stale(tmp_path, monkeypatch):
    setup = tmp_path / "env.sh"
    setup.write_text("export CEDAR_LICENSE=a\n")
    _validated(tmp_path, monkeypatch, setup_script=setup, env_required=["CEDAR_LICENSE"])
    setup.write_text("export CEDAR_LICENSE=b\n")
    v = S.load_validation(*_load(tmp_path))
    assert v["state"] == "stale"
    assert "environment setup: setup_script_sha256 changed" in v["changes"]
    assert "environment setup: environment_sha256 changed" in v["changes"]


def test_changed_environment_behind_unchanged_setup_script_is_stale(tmp_path, monkeypatch):
    inner = tmp_path / "site.sh"
    inner.write_text("export CEDAR_LICENSE=a\n")
    setup = tmp_path / "env.sh"
    setup.write_text(f". {inner}\n")
    _validated(tmp_path, monkeypatch, setup_script=setup, env_required=["CEDAR_LICENSE"])
    inner.write_text("export CEDAR_LICENSE=b\n")
    v = S.load_validation(*_load(tmp_path))
    assert v["state"] == "stale" and v["changes"] == ["environment setup: environment_sha256 changed"]
    rec = json.loads(Path(v["record"]).read_text())
    assert "=a" not in json.dumps(rec)                            # values are hashed, never stored


def test_unchanged_tools_stay_validated_and_old_records_are_stale(tmp_path, monkeypatch):
    _validated(tmp_path, monkeypatch)
    t, te = _load(tmp_path)
    assert S.load_validation(t, te)["state"] == "validated"
    p = Path(S.load_validation(t, te)["record"])
    rec = json.loads(p.read_text())
    rec.pop("identity")
    p.write_text(json.dumps(rec))
    assert S.load_validation(t, te)["state"] == "stale"


def test_version_probe_is_bounded(tmp_path):
    from cedar.eda.runner import ToolEnv
    _profiles(tmp_path)
    _exe(tmp_path / "dc", "sleep 30 & wait")
    t0 = time.monotonic()
    r = ToolEnv(_load(tmp_path)[0]).version_probe("synthesis", timeout=1)
    assert r["probe_status"] == "timeout" and r["version"] is None and time.monotonic() - t0 < 10


RTL = "module m(input clk, input [3:0] a, b, output reg [3:0] y); always @(posedge clk) y <= a * b + a; endmodule\n"


def test_run_records_identity_and_refuses_resume_after_tool_change(tmp_path, toolchain, monkeypatch):
    from cedar.workflow import engine as E
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    (tmp_path / "m.v").write_text(RTL)
    stages = {"synthesis": "synopsys-dc", "place_route": "cadence-innovus", "simulation": "synopsys-vcs",
              "saif": "synopsys-vcd2saif", "power": "synopsys-primetime", "equivalence": "cadence-conformal"}
    exes = {k: _exe(tmp_path / f"tool-{k}", "exit 0") for k in stages}
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump({"schema": "cedar/tool-profile@1", "name": "t", "tools": {
        k: {"adapter": v, "executable": str(exes[k])} for k, v in stages.items()}}))
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
    assert run.advance(stop_after="preflight") == "running"
    ident = run.s["provenance"]["tool_identity"]
    assert set(ident["tools"]) == set(stages) and ident["tools"]["power"]["sha256"]
    E.Run(run.dir)._check_inputs_unchanged()                    # unchanged tools: resume allowed
    _exe(exes["power"], "echo replaced; exit 0")                 # same path, new tool
    with pytest.raises(E.WorkflowError, match="tool power: sha256 changed"):
        E.Run(run.dir).advance()
