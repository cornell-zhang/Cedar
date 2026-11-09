# SPDX-License-Identifier: Apache-2.0
"""RTL -> MLIR -> e-graph -> extraction -> RTL with the selected toolchain,
checked by circt-lec + Z3 (no commercial tools)."""
import json
from pathlib import Path
import re
import subprocess

from cedar.cli import main
from cedar.egraph import pipeline
from cedar.verification import check


def test_data_observing_process_does_not_gain_register(tmp_path, toolchain):
    """A module clock must not turn an unrelated combinational wait into a flop."""
    source = tmp_path / "mixed_process.mlir"
    source.write_text("""module {
  hw.module @mixed_process(in %clk_in : i1, in %a : i4, in %b : i4, out y : i4) {
    %zero = hw.constant 0 : i4
    %false = hw.constant false
    %true = hw.constant true
    %time = llhd.constant_time <0ns, 0d, 1e>
    %clock = seq.to_clock %clk_in
    %registered = seq.firreg %a clock %clock : i4
    %signal = llhd.sig %zero : i4
    %process:2 = llhd.process -> i4, i1 {
      cf.br ^wait(%zero, %false : i4, i1)
    ^wait(%value: i4, %enable: i1):
      llhd.wait yield (%value, %enable : i4, i1), (%registered, %b : i4, i4), ^body
    ^body:
      %less = comb.icmp ult %registered, %b : i4
      cf.cond_br %less, ^left, ^right
    ^left:
      cf.br ^wait(%registered, %true : i4, i1)
    ^right:
      cf.br ^wait(%b, %true : i4, i1)
    }
    llhd.drv %signal, %process#0 after %time if %process#1 : i4
    %output = llhd.prb %signal : i4
    hw.output %output : i4
  }
}
""")
    result = subprocess.run(
        [str(toolchain.tool("egg-opt")), str(source),
         "--pass-pipeline=builtin.module(hw.module(lower-loop-carry-processes{lower-llhd-delay-as-zero-cycle=true}))"],
        cwd=tmp_path, env=toolchain.env(), capture_output=True, text=True,
        check=True, timeout=30,
    )
    assert result.stdout.count("seq.firreg") == 1
    assert "comb.mux" in result.stdout and "comb.icmp ult" in result.stdout
    for removed in ("llhd.process", "llhd.wait", "llhd.drv", "llhd.prb", "cf.br", "cf.cond_br"):
        assert removed not in result.stdout


def test_opaque_region_captures_preserve_branch_types(tmp_path, toolchain):
    """Captured constants must not shift positional reconstruction roots."""
    source = tmp_path / "opaque_capture.mlir"
    source.write_text("""module {
  hw.module @opaque_capture(in %a : i4, out result : i4, out captured : i4) {
    %flag = hw.constant false
    %zero = hw.constant 0 : i4
    %one = hw.constant 1 : i4
    %region = llhd.combinational -> i4 {
      cf.br ^bb1(%zero, %flag : i4, i1)
    ^bb1(%value: i4, %condition: i1):
      %selected = comb.mux %condition, %value, %zero : i4
      llhd.yield %selected : i4
    }
    %sum = comb.add %a, %one : i4
    hw.output %sum, %region : i4, i4
  }
}
""")
    eq = pipeline.eqsat(toolchain, tmp_path / "run", "opaque_capture", [],
                        "opaque_capture", mlir_in=source, iterations=1)
    graph = json.loads(Path(eq.egraph_json).read_text())
    # Only hw.output is a root. Constants consumed solely by the opaque
    # region must stay verbatim rather than consume extraction lines.
    assert len(graph["root_eclasses"]) == 1
    extracted = tmp_path / "extracted.txt"
    result = subprocess.run(
        [str(toolchain.tool("extract_result")), eq.egraph_json],
        cwd=eq.work_dir, env=toolchain.env(), capture_output=True, text=True,
        check=True, timeout=30,
    )
    extracted.write_text(result.stdout)
    output = tmp_path / "reconstructed.mlir"
    subprocess.run(
        [str(toolchain.tool("egg-opt")), eq.mlir, "--reconstruct-from-extraction",
         "--egg-file=" + eq.egg_program, "--extraction-file=" + str(extracted),
         "-o", str(output)],
        cwd=eq.work_dir, env=toolchain.env(), capture_output=True, text=True,
        check=True, timeout=30,
    )
    text = output.read_text()
    assert "hw.module @opaque_capture" in text
    assert re.search(r"cf\.br \^bb1\([^\n]+ : i4, i1\)", text)
    assert "llhd.combinational" in text and "hw.output" in text


def test_open_example(tmp_path, toolchain):
    # A prefix toolchain (bin/ + share/cedar/) is passed explicitly; the wheel
    # toolchain is found through the installed cedar_toolchain package.
    sel = ["--toolchain-prefix", str(toolchain.bin_dir.parent)] if toolchain.bin_dir.name == "bin" else []
    assert main(["example", "open", "--output-dir", str(tmp_path)] + sel) == 0
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
