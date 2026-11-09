// SPDX-License-Identifier: Apache-2.0
// Open example for `cedar example open` (combinational; checked with circt-lec).
// y: a*b + a*c can be factored into a*(b+c) (one multiplier fewer).
// z: s ? (x+p) : (x+q) can share the adder: x + (s ? p : q).
module dot3 (
  input  wire [7:0] a, b, c, x, p, q,
  input  wire       s,
  output wire [7:0] y,
  output wire [7:0] z
);
  assign y = a * b + a * c;
  assign z = s ? (x + p) : (x + q);
endmodule
