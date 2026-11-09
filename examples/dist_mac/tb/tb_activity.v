// SPDX-License-Identifier: Apache-2.0
// Activity (switching) testbench for power analysis ONLY. It applies random
// stimulus and does not check outputs; it is never used as correctness
// evidence. Instantiates the DUT as Top.DUT. CYCLE_TIME is in ns.
`timescale 1ns/1ps
`ifndef CYCLE_TIME
`define CYCLE_TIME 1.0
`endif
module Top;
  reg clk = 1'b0;
  reg rst = 1'b1;
  reg [7:0] a = 8'd0, b = 8'd0, c = 8'd0;
  wire [15:0] y;
  integer i;
  integer seed;

  dist_mac DUT (.clk(clk), .rst(rst), .a(a), .b(b), .c(c), .y(y));

  always #(`CYCLE_TIME / 2.0) clk = ~clk;

  initial begin
    seed = 1;
    repeat (4) @(negedge clk);
    rst = 1'b0;
    for (i = 0; i < 1000; i = i + 1) begin
      @(negedge clk);
      a = $random(seed);
      b = $random(seed);
      c = $random(seed);
    end
    @(negedge clk);
    $finish;
  end
endmodule
