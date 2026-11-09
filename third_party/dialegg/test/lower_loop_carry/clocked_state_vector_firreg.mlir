// RUN: %eggopt %s --pass-pipeline='builtin.module(hw.module(lower-loop-carry-processes{lower-llhd-delay-as-zero-cycle=true}))' | FileCheck %s
//
// Regression: rtllm/multi_booth_8bit
// shape. A clocked `always @(posedge clk or posedge reset)` FSM carries a
// multi-element state vector. Several back-edges HOLD the previous value of
// a state element by passing the probe of its own signal (e.g.
// `^bb1(... %prb_acc ...)`), so the next-state mux's "hold" arm resolves to
// the register's own output.
//
// History: the module has no pre-existing seq.to_clock (none of its outputs
// is itself a firreg), so the pass used to take the combinational-binding
// path and emit `assign acc = sel ? next : acc;` — a comb loop that
// SIGSEGVs circt-verilog (FAIL:verilog-unparseable). The pass now detects
// the posedge-clk edge detector in the wait-dest body, synthesizes a
// seq.to_clock from the observed clock, and registers the held state in a
// seq.firreg — turning the comb loop into legal registered feedback.
//
// This mirrors the existing multi_process_wide_state.mlir (which exercised
// the forward-merge inlining) but specifically pins the "held state must
// become a firreg, not a self-referential comb mux" behavior.

module {
  hw.module @booth_state(out p : i16, in %clk : i1, in %reset : i1,
                         in %b : i8) {
    %false = hw.constant false
    %true = hw.constant true
    %c1_i5 = hw.constant 1 : i5
    %c0_i27 = hw.constant 0 : i27
    %c16_i32 = hw.constant 16 : i32
    %c0_i5 = hw.constant 0 : i5
    %c0_i16 = hw.constant 0 : i16
    %acc = llhd.sig %c0_i16 : i16
    %prb_acc = llhd.prb %acc : i16
    %ctr = llhd.sig %c0_i5 : i5
    %prb_ctr = llhd.prb %ctr : i5
    // Two-element clocked FSM state: accumulator + counter. The wait
    // observes (clk, reset); ^bb2 is the posedge-clk edge detector.
    %0:4 = llhd.process -> i16, i1, i5, i1 {
      cf.br ^bb1(%clk, %reset, %c0_i16, %false, %c0_i5, %false
                 : i1, i1, i16, i1, i5, i1)
    ^bb1(%8: i1, %9: i1, %10: i16, %11: i1, %12: i5, %13: i1):
      llhd.wait yield (%10, %11, %12, %13 : i16, i1, i5, i1),
                (%clk, %reset : i1, i1), ^bb2(%8, %9 : i1, i1)
    ^bb2(%20: i1, %21: i1):
      %22 = comb.xor bin %20, %true : i1
      %23 = comb.and bin %22, %clk : i1
      %24 = comb.xor bin %21, %true : i1
      %25 = comb.and bin %24, %reset : i1
      %26 = comb.or bin %23, %25 : i1
      // Not a clock edge → HOLD previous values (pass the probes back).
      cf.cond_br %26, ^bb3,
                 ^bb1(%clk, %reset, %prb_acc, %false, %prb_ctr, %false
                      : i1, i1, i16, i1, i5, i1)
    ^bb3:
      // On reset, clear; else advance.
      cf.cond_br %reset, ^bb4, ^bb5
    ^bb4:
      cf.br ^bb1(%clk, %true, %c0_i16, %true, %c0_i5, %true
                 : i1, i1, i16, i1, i5, i1)
    ^bb5:
      %33 = comb.concat %c0_i27, %prb_ctr : i27, i5
      %34 = comb.icmp ult %33, %c16_i32 : i32
      // Counter done → hold; else accumulate b and bump counter.
      cf.cond_br %34, ^bb6,
                 ^bb1(%clk, %false, %prb_acc, %false, %prb_ctr, %false
                      : i1, i1, i16, i1, i5, i1)
    ^bb6:
      %c0_i8 = hw.constant 0 : i8
      %35 = comb.concat %c0_i8, %b : i8, i8
      %36 = comb.add %prb_acc, %35 : i16
      %37 = comb.add %prb_ctr, %c1_i5 : i5
      cf.br ^bb1(%clk, %reset, %36, %true, %37, %true
                 : i1, i1, i16, i1, i5, i1)
    }
    %1 = llhd.constant_time <0ns, 1d, 0e>
    %p_acc = llhd.sig name "p" %c0_i16 : i16
    llhd.drv %p_acc, %0#0 after %1 if %0#1 : i16
    %p_out = llhd.prb %p_acc : i16
    llhd.drv %ctr, %0#2 after %1 if %0#3 : i5
    hw.output %p_out : i16
  }
}

// CHECK-LABEL: hw.module @booth_state
// All process / wait / cf ops must be gone.
// CHECK-NOT: llhd.process
// CHECK-NOT: llhd.wait
// CHECK-NOT: cf.br
// CHECK-NOT: cf.cond_br
// The held state must be registered: a clock is synthesized and the
// loop-carried state lands in seq.firreg flops (NOT combinational binding).
// CHECK: seq.to_clock
// CHECK: seq.firreg
