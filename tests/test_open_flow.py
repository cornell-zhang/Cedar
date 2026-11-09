# SPDX-License-Identifier: Apache-2.0
"""RTL -> MLIR -> e-graph -> extraction -> RTL with the selected toolchain,
checked by circt-lec + Z3 (no commercial tools)."""
import json
from pathlib import Path

from cedar.cli import main
from cedar.verification import check


def test_open_example(tmp_path, toolchain, monkeypatch):
    monkeypatch.setenv("CEDAR_TOOLCHAIN_PREFIX", str(toolchain.bin_dir.parent))
    assert main(["example", "open", "--output-dir", str(tmp_path), "--toolchain-prefix",
                 str(toolchain.bin_dir.parent)]) == 0
    res = json.loads(next(tmp_path.glob("open-example-*/result.json")).read_text())
    assert res["passed"]
    for r in res["results"].values():
        assert r["lec"] == "passed" and r["multipliers"] == 1     # factored: one multiplier
    assert any(x.startswith("mul_factor_common@") for x in res["admitted_rule_instances"])


def test_circt_lec_detects_non_equivalence(tmp_path, toolchain):
    a = tmp_path / "a.v"
    b = tmp_path / "b.v"
    a.write_text("module m(input [7:0] x, y, output [7:0] z); assign z = x * y + x; endmodule\n")
    b.write_text("module m(input [7:0] x, y, output [7:0] z); assign z = x * (y + 8'd1); endmodule\n")
    assert check("circt-lec", [str(a)], [str(b)], "m", tmp_path / "eq", toolchain=toolchain).status == "passed"
    b.write_text("module m(input [7:0] x, y, output [7:0] z); assign z = x * (y + 8'd2); endmodule\n")
    assert check("circt-lec", [str(a)], [str(b)], "m", tmp_path / "ne", toolchain=toolchain).status == "failed"
    s = tmp_path / "s.v"
    s.write_text("module m(input clk, input [7:0] x, y, output reg [7:0] z); always @(posedge clk) z <= x; endmodule\n")
    assert check("circt-lec", [str(s)], [str(s)], "m", tmp_path / "seq", toolchain=toolchain).status == "unavailable"
