// SPDX-License-Identifier: Apache-2.0
// Cedar example: a registered multiply-accumulate-style datapath with a known
// optimization opportunity. a*b + a*c computes two multiplications that can
// be factored into a*(b+c) (distributivity holds modulo 2^16), removing one
// multiplier without changing cycle behaviour.
module dist_mac (
  input  wire        clk,
  input  wire        rst,
  input  wire [7:0]  a,
  input  wire [7:0]  b,
  input  wire [7:0]  c,
  output reg  [15:0] y
);
  wire [15:0] a16 = {8'd0, a};
  wire [15:0] b16 = {8'd0, b};
  wire [15:0] c16 = {8'd0, c};

  always @(posedge clk) begin
    if (rst)
      y <= 16'd0;
    else
      y <= a16 * b16 + a16 * c16;
  end
endmodule
