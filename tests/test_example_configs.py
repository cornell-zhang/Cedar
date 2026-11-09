# SPDX-License-Identifier: Apache-2.0
"""The public placeholder configuration templates stay schema-valid."""
from pathlib import Path

import pytest

from cedar import config as C

EX = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.skipif(not EX.is_dir(), reason="examples/ not available (installed package only)")
def test_example_templates_validate(tmp_path, monkeypatch):
    pdk = tmp_path / "pdk"
    for rel in ("db/x_TT.db", "lib/x_TT.lib", "verilog/x_TT.v", "qrc/qrcTechFile", "lef/tech.lef", "lef/cells.lef"):
        (pdk / rel).parent.mkdir(parents=True, exist_ok=True)
        (pdk / rel).write_text("")
    env = tmp_path / "site-env.sh"
    env.write_text("true\n")
    monkeypatch.setenv("PDK_ROOT", str(pdk))
    monkeypatch.setenv("CEDAR_SITE_ENV", str(env))
    tools = C.load_tool_profile(str(EX / "config" / "tool-profile.example.yaml"))
    tech = C.load_technology(str(EX / "config" / "technology.example.yaml"))
    design = C.load_design(str(EX / "dist_mac" / "design.yaml"))
    assert set(tools.data["tools"]) == {"synthesis", "place_route", "simulation", "saif", "power", "equivalence"}
    assert tech.data["corners"]["tt"]["db"][0]["matches"]
    assert design.data["clocks"][0]["period_ns_resolved"] == 1.0
    for f in ("tool-profile.example.yaml", "technology.example.yaml"):
        text = (EX / "config" / f).read_text()
        assert "/home/" not in text and "/work/" not in text
