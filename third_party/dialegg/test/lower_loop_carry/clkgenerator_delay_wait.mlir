// RUN: %eggopt %s --lower-loop-carry-processes='lower-llhd-delay-as-zero-cycle=true' | FileCheck %s
//
// Regression: rtllm/clkgenerator
// shape. Clock-output toggling process with:
//   * NO loop body (the wait's dest goes straight back to the loop
//     header), so the back-edge writes two i1 signals without any
//     intermediate cf.cond_br.
//   * Stray orphan llhd.sig (the output port's underlying sig) which
//     has only a probe and no drive, alongside the real sig that gets
//     written by the process. The new Phase 2 orphan-sig elision drops
//     it by substituting the probe with the sig's init value.
//
// Pre-fix this design produced `cf.br ^bb1(%false, %false : i1, i1)`
// in the post-pass IR which crashed ExportVerilog with
// `dialect "cf" not supported for direct Verilog emission`. The
// delay-wait clock-divider path (gated by
// `lower-llhd-delay-as-zero-cycle=true`) now collapses this into a
// purely combinational module.

module {
  hw.module @clkgenerator(out clk : i1) {
    %true = hw.constant true
    %0 = llhd.constant_time <5000000fs, 0d, 0e>
    %false = hw.constant false
    %clk = llhd.sig %false : i1
    %false_0 = hw.constant false
    %clk_1 = llhd.sig name "clk" %false_0 : i1
    %1 = llhd.constant_time <0ns, 0d, 1e>
    llhd.drv %clk_1, %false_0 after %1 : i1
    %2:2 = llhd.process -> i1, i1 {
      cf.br ^bb1(%false, %false : i1, i1)
    ^bb1(%5: i1, %6: i1):  // 2 preds: ^bb0, ^bb2
      llhd.wait yield (%5, %6 : i1, i1), delay %0, ^bb2
    ^bb2:  // pred: ^bb1
      %7 = comb.xor %3, %true : i1
      cf.br ^bb1(%7, %true : i1, i1)
    }
    llhd.drv %clk_1, %2#0 after %1 if %2#1 : i1
    %3 = llhd.prb %clk : i1
    %4 = llhd.prb %clk_1 : i1
    hw.output %4 : i1
  }
}

// CHECK-LABEL: hw.module @clkgenerator
// The process and all llhd / cf ops must be gone.
// CHECK-NOT: llhd.process
// CHECK-NOT: llhd.wait
// CHECK-NOT: llhd.sig
// CHECK-NOT: llhd.drv
// CHECK-NOT: llhd.prb
// CHECK-NOT: cf.br
// CHECK-NOT: cf.cond_br
// The output should be a comb.xor of the (substituted) initial value.
// CHECK: comb.xor
// CHECK: hw.output
