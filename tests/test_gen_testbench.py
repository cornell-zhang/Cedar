# SPDX-License-Identifier: Apache-2.0
from cedar.eda.gen_testbench import generate_activity_testbench, ports


def test_ports_and_generated_activity_tb(tmp_path, toolchain):
    rtl = tmp_path / "m.v"
    rtl.write_text("module m(input clk, input rst_n, input [7:0] a, input s, output reg [9:0] y);\n"
                   "always @(posedge clk) if (!rst_n) y <= 0; else y <= s ? {2'b0, a} : 10'd3; endmodule\n")
    assert ports(toolchain, [str(rtl)], "m", tmp_path / "p") == [
        ("in", "clk", 1), ("in", "rst_n", 1), ("in", "a", 8), ("in", "s", 1), ("out", "y", 10)]
    tb = generate_activity_testbench(toolchain, [str(rtl)], "m", tmp_path / "tb.v", [{"port": "clk"}],
                                     [{"port": "rst_n", "active": "low"}], cycles=10)
    text = tb.read_text()
    assert "ACTIVITY-ONLY" in text and "m DUT (" in text
    assert "rst_n = 1'b0;" in text and "rst_n = 1'b1;" in text          # asserted, then released
    assert "a = $random(seed);" in text and "clk = $random" not in text and "rst_n = $random" not in text
