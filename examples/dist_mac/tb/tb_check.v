// SPDX-License-Identifier: Apache-2.0
// Self-checking testbench (verification.mode: simulation). Compares the DUT
// against a reference model every cycle and prints CEDAR_TB_PASS only if all
// comparisons succeed. Distinct from the activity testbench.
`timescale 1ns/1ps
module TbCheck;
  reg clk = 1'b0;
  reg rst = 1'b1;
  reg [7:0] a = 8'd0, b = 8'd0, c = 8'd0;
  wire [15:0] y;
  reg  [15:0] expect_y;
  integer i, errors, seed;

  dist_mac DUT (.clk(clk), .rst(rst), .a(a), .b(b), .c(c), .y(y));
  always #5 clk = ~clk;

  initial begin
    errors = 0; seed = 7;
    @(negedge clk); @(negedge clk);
    if (y !== 16'd0) errors = errors + 1;
    rst = 1'b0;
    for (i = 0; i < 2000; i = i + 1) begin
      a = $random(seed); b = $random(seed); c = $random(seed);
      if (i % 97 == 0) begin a = 8'hff; b = 8'hff; c = 8'hff; end
      expect_y = {8'd0, a} * {8'd0, b} + {8'd0, a} * {8'd0, c};
      @(posedge clk); #1;
      if (y !== expect_y) begin
        errors = errors + 1;
        if (errors < 5) $display("mismatch a=%h b=%h c=%h y=%h expect=%h", a, b, c, y, expect_y);
      end
      @(negedge clk);
    end
    if (errors == 0) $display("CEDAR_TB_PASS");
    else $display("CEDAR_TB_FAIL errors=%0d", errors);
    $finish;
  end
endmodule
