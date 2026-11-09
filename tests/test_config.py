# SPDX-License-Identifier: Apache-2.0
from pathlib import Path

import pytest
import yaml

from cedar import config as C
from cedar.config.schema import ConfigError


def write(p: Path, doc: dict) -> Path:
    p.write_text(yaml.safe_dump(doc))
    return p


def design_doc(**over):
    d = {"schema": "cedar/design@1", "name": "t", "top": "t", "rtl": ["t.v"],
         "profiles": {"tools": "x", "technology": "y"},
         "clocks": [{"port": "clk", "period_ns": 1.0}],
         "activity": {"mode": "generated"}, "verification": {"mode": "conformal"},
         "objective": {"preset": "balanced"}}
    d.update(over)
    return d


def test_paths_resolve_relative_to_file(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "t.v").write_text("module t; endmodule\n")
    d = C.load_design(str(write(tmp_path / "sub" / "design.yaml", design_doc())))
    assert d.data["rtl"][0]["path"] == str(tmp_path / "sub" / "t.v")


def test_unknown_keys_rejected(tmp_path):
    (tmp_path / "t.v").write_text("")
    with pytest.raises(ConfigError) as e:
        C.load_design(str(write(tmp_path / "d.yaml", design_doc(clok=1))))
    assert any("unknown key" in x for x in e.value.errors)


def test_timing_goal_required_and_units(tmp_path):
    (tmp_path / "t.v").write_text("")
    with pytest.raises(ConfigError):
        C.load_design(str(write(tmp_path / "d.yaml", design_doc(clocks=[{"port": "clk"}]))))
    d = C.load_design(str(write(tmp_path / "d2.yaml", design_doc(clocks=[{"port": "clk", "frequency": "1.25 GHz"}]))))
    assert d.data["clocks"][0]["period_ns_resolved"] == pytest.approx(0.8)
    with pytest.raises(ConfigError):
        C.load_design(str(write(tmp_path / "d3.yaml", design_doc(clocks=[{"port": "clk", "frequency": "800"}]))))


def test_multiple_clocks_need_sdc(tmp_path):
    (tmp_path / "t.v").write_text("")
    with pytest.raises(ConfigError) as e:
        C.load_design(str(write(tmp_path / "d.yaml", design_doc(clocks=[
            {"port": "a", "name": "a", "period_ns": 1}, {"port": "b", "name": "b", "period_ns": 2}]))))
    assert any("sdc" in x for x in e.value.errors)


def test_simulation_check_tb_must_differ_from_activity(tmp_path):
    (tmp_path / "t.v").write_text("")
    (tmp_path / "tb.v").write_text("")
    doc = design_doc(activity={"mode": "testbench", "testbench": "tb.v"},
                     verification={"mode": "simulation", "testbench": "tb.v"})
    with pytest.raises(ConfigError) as e:
        C.load_design(str(write(tmp_path / "d.yaml", doc)))
    assert any("distinct" in x for x in e.value.errors)


def test_env_var_paths_and_missing_env(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (lib / n).write_text("")
    monkeypatch.setenv("CEDAR_TEST_LIB", str(lib))
    doc = {"schema": "cedar/technology@1", "name": "t", "units": {"time": "ps"}, "default_corner": "tt",
           "corners": {"tt": {"db": ["${CEDAR_TEST_LIB}/*.db"], "liberty": ["${CEDAR_TEST_LIB}/a.lib"],
                              "sim_models": ["${CEDAR_TEST_LIB}/a.v"], "rc_tech": "${CEDAR_TEST_LIB}/rc"}},
           "lef": {"tech": "${CEDAR_TEST_LIB}/tech.lef", "cells": ["${CEDAR_TEST_LIB}/cells.lef"]},
           "place_route": {"site": "core"}}
    t = C.load_technology(str(write(tmp_path / "t.yaml", doc)))
    assert t.data["corners"]["tt"]["db"][0]["matches"] == [str(lib / "a.db")]
    assert "${CEDAR_TEST_LIB}" in t.provenance()["raw"]["lef"]["tech"]   # provenance keeps the reference
    monkeypatch.delenv("CEDAR_TEST_LIB")
    with pytest.raises(ConfigError) as e:
        C.load_technology(str(tmp_path / "t.yaml"))
    assert any("CEDAR_TEST_LIB" in x for x in e.value.errors)
