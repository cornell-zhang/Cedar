# SPDX-License-Identifier: Apache-2.0
import json
import subprocess
from pathlib import Path

import pytest

from cedar.egraph import costmodel as CM


def synthetic_egraph():
    """Root class X has two alternatives: one 8-bit multiplier, or a chain of
    three 8-bit adders. Area favours the adders, delay favours the multiplier."""
    n = {
        "I8-0": {"op": "I8", "children": [], "eclass": "T"},
        "i0": {"op": "0", "children": [], "eclass": "i0"}, "i1": {"op": "1", "children": [], "eclass": "i1"},
        "i2": {"op": "2", "children": [], "eclass": "i2"}, "i3": {"op": "3", "children": [], "eclass": "i3"},
        "va": {"op": "Value", "children": ["i0", "I8-0"], "eclass": "A"},
        "vb": {"op": "Value", "children": ["i1", "I8-0"], "eclass": "B"},
        "vc": {"op": "Value", "children": ["i2", "I8-0"], "eclass": "Cc"},
        "vd": {"op": "Value", "children": ["i3", "I8-0"], "eclass": "D"},
        "vecb": {"op": "OpVec", "children": ["vb"], "eclass": "VB"},
        "vecc": {"op": "OpVec", "children": ["vc"], "eclass": "VC"},
        "vecd": {"op": "OpVec", "children": ["vd"], "eclass": "VD"},
        "add1": {"op": "comb_add", "children": ["va", "vecb", "I8-0"], "eclass": "S1"},
        "vecs1": {"op": "OpVec", "children": ["add1"], "eclass": "VS1"},
        "add2": {"op": "comb_add", "children": ["vc", "vecs1", "I8-0"], "eclass": "S2"},
        "vecs2": {"op": "OpVec", "children": ["add2"], "eclass": "VS2"},
        "add3": {"op": "comb_add", "children": ["vd", "vecs2", "I8-0"], "eclass": "X"},
        "mul": {"op": "comb_mul", "children": ["va", "vecb", "I8-0"], "eclass": "X"},
        "root": {"op": "RootNode", "children": ["add3"], "eclass": "R"},
    }
    for v in n.values():
        v["cost"] = 1.0
    return {"nodes": n, "root_eclasses": ["R"]}


def test_features_table():
    f_mul = CM.features_for("comb_mul", 8, 2)
    f_add = CM.features_for("comb_add", 8, 2)
    assert f_mul.area == 64 and f_add.area == 16
    assert 3 * f_add.area < f_mul.area and 3 * f_add.delay > f_mul.delay
    assert CM.features_for("comb_shl", 8, 2, const_shift=True).area == 0
    assert CM.features_for("comb_concat", 8).area == 0


def test_weights_change_costs(tmp_path):
    g = synthetic_egraph()
    p = tmp_path / "g.json"
    scales = {"area": 100.0, "delay": 20.0, "power": 100.0}
    p.write_text(json.dumps(g))
    CM.apply_costs(p, {"area": 1.0, "delay": 0.0, "power": 0.0}, scales)
    ga = json.loads(p.read_text())["nodes"]
    p.write_text(json.dumps(g))
    CM.apply_costs(p, {"area": 0.0, "delay": 1.0, "power": 0.0}, scales)
    gd = json.loads(p.read_text())["nodes"]
    adds_a = sum(ga[k]["cost"] for k in ("add1", "add2", "add3"))
    adds_d = sum(gd[k]["cost"] for k in ("add1", "add2", "add3"))
    assert adds_a < ga["mul"]["cost"]       # area: adder chain cheaper
    assert adds_d > gd["mul"]["cost"]       # delay: multiplier cheaper


def test_multiplier_validation(tmp_path):
    with pytest.raises(ValueError):
        CM.validate_multipliers({"comb_mul": 0.0})
    with pytest.raises(ValueError):
        CM.validate_multipliers({"comb_mul": float("nan")})


def test_extractor_choice_flips_with_weights(tmp_path, toolchain):
    """The real extract_result binary consumes the weighted node costs."""
    scales = {"area": 100.0, "delay": 20.0, "power": 100.0}
    picks = {}
    for label, w in (("area", {"area": 1.0, "delay": 0.0, "power": 0.0}),
                     ("delay", {"area": 0.0, "delay": 1.0, "power": 0.0})):
        p = tmp_path / f"{label}.json"
        p.write_text(json.dumps(synthetic_egraph()))
        CM.apply_costs(p, w, scales)
        out = subprocess.run([toolchain.tool("extract_result"), str(p)], capture_output=True, text=True, check=True).stdout
        picks[label] = "comb_mul" if "comb_mul" in out else "comb_add"
    assert picks == {"area": "comb_add", "delay": "comb_mul"}


def test_extractor_is_deterministic(tmp_path, toolchain):
    """Equal-cost alternatives (from commutativity) must be tie-broken the same
    way on every run; this e-graph shape exposed run-to-run differences before
    the extractor used deterministic hashing."""
    from importlib import resources

    from cedar.egraph import pipeline
    from cedar.rules import certify
    rtl = tmp_path / "dist_mac.v"
    rtl.write_text("module dist_mac(input clk, input rst, input [7:0] a, b, c, output reg [15:0] y);\n"
                   "wire [15:0] a16 = {8'd0, a}; wire [15:0] b16 = {8'd0, b}; wire [15:0] c16 = {8'd0, c};\n"
                   "always @(posedge clk) if (rst) y <= 16'd0; else y <= a16 * b16 + a16 * c16;\nendmodule\n")
    sigs = certify.signatures_from_template(toolchain.template("circt.egg").read_text())
    rules = {n: resources.files("cedar").joinpath(f"data/rules/{n}.egg").read_text()
             for n in ("mul_commute", "mul_factor_common")}
    adm = certify.admit(rules, [1, 8, 16], None, sigs)
    analysis = pipeline.eqsat(toolchain, tmp_path / "a", "dist_mac", [str(rtl)], "dist_mac", [], 1)
    eq = pipeline.eqsat(toolchain, tmp_path / "e", "dist_mac", [str(rtl)], "dist_mac",
                        [i.text for i in adm.admitted], 4)
    scales = CM.design_scales(json.loads(Path(analysis.egraph_json).read_text()))
    CM.apply_costs(Path(eq.egraph_json), {"area": 1 / 3, "delay": 1 / 3, "power": 1 / 3}, scales)
    outs = {subprocess.run([toolchain.tool("extract_result"), eq.egraph_json], capture_output=True, text=True,
                           check=True).stdout for _ in range(40)}
    assert len(outs) == 1
