// RUN: %eggopt %s --lower-loop-carry-processes='lower-llhd-delay-as-zero-cycle=true' | FileCheck %s
//
// Clock-divider-style body whose back-edge carries a flipped signal value
// (comb.xor %sig, 1). The wait is delay-driven (no observed signals), so
// the original matcher rejected the process outright. Under the new
// --lower-llhd-delay-as-zero-cycle flag the pass binds result_i to the
// flipped value combinationally so ExportVerilog can emit the surrounding
// module. Semantics-wise this collapses the delay; tester guards against
// using this for actually-clocked designs.

module {
  hw.module @clkdiv(out clk : i1) {
    %0 = llhd.constant_time <0ns, 0d, 1e>
    %1 = llhd.constant_time <5000000fs, 0d, 0e>
    %true = hw.constant true
    %false = hw.constant false
    %clk = llhd.sig %false : i1
    llhd.drv %clk, %false after %0 : i1
    %2:2 = llhd.process -> i1, i1 {
      cf.br ^bb1(%false, %false : i1, i1)
    ^bb1(%4: i1, %5: i1):
      llhd.wait yield (%4, %5 : i1, i1), delay %1, ^bb2
    ^bb2:
      %6 = comb.xor %3, %true : i1
      cf.br ^bb1(%6, %true : i1, i1)
    }
    llhd.drv %clk, %2#0 after %0 if %2#1 : i1
    %3 = llhd.prb %clk : i1
    hw.output %3 : i1
  }
}

// CHECK-LABEL: hw.module @clkdiv
// Process and all llhd / cf ops must be gone.
// CHECK-NOT: llhd.process
// CHECK-NOT: llhd.wait
// CHECK-NOT: llhd.sig
// CHECK-NOT: llhd.drv
// CHECK-NOT: llhd.prb
// CHECK-NOT: cf.br
// CHECK-NOT: cf.cond_br
// The flipped value (comb.xor) should remain as the driven output.
// CHECK: comb.xor
// CHECK: hw.output
