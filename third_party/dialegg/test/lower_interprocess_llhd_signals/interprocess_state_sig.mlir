// RUN: %eggopt %s --pass-pipeline='builtin.module(hw.module(lower-interprocess-llhd-signals))' | FileCheck %s
//
// Regression: rtllm/float_multi
// shape. A module-scope `llhd.sig` is read by ONE combinational probe and
// written by SEVERAL conditional `llhd.drv ... if %en` ops that originated
// in different sibling `llhd.process` regions (an inter-process state
// register). After `lower-loop-carry-processes` lowers all the processes,
// those processes are gone but the shared state sig survives at module
// scope with multiple conditional drives — a shape neither the orphan
// `lower-free-llhd-signals` path (it requires a single unique real drive)
// nor LCP (the references crossed the process boundary) handle.
//
// This pass lowers the sig to a `seq.firreg` whose next-state is a priority
// mux chain over the conditional drives (program order = last drive wins),
// defaulting to the register's own output (hold). The single probe's uses
// are rewired to the register output. All llhd ops are erased.
//
// The fixture reproduces the post-LCP shape: the `%clk` is wrapped by a
// `seq.to_clock` (the pass infers the module clock from it), and the
// `%state` sig has two conditional drives from already-firreg-clocked
// next-values. The probe is read at the top and fans out to hw.output.

module {
  hw.module @interproc(in %clk : i1, in %a : i8, in %b : i8, in %sel0 : i1, in %sel1 : i1, out z : i8) {
    %c0_i8 = hw.constant 0 : i8
    %state = llhd.sig %c0_i8 : i8
    %p = llhd.prb %state : i8
    %clk_c = seq.to_clock %clk
    %t = llhd.constant_time <0ns, 1d, 0e>
    // Two candidate next-values, each gated by a (mutually exclusive) state
    // enable. Modeled as plain firregs to mirror the post-LCP IR.
    %n0 = comb.add %p, %a : i8
    %e0 = seq.firreg %sel0 clock %clk_c : i1
    %n1 = comb.add %p, %b : i8
    %e1 = seq.firreg %sel1 clock %clk_c : i1
    llhd.drv %state, %n0 after %t if %e0 : i8
    llhd.drv %state, %n1 after %t if %e1 : i8
    hw.output %p : i8
  }
}

// CHECK-LABEL: hw.module @interproc
// All llhd ops must be gone after this pass.
// CHECK-NOT: llhd.sig
// CHECK-NOT: llhd.drv
// CHECK-NOT: llhd.prb
// CHECK-NOT: llhd.constant_time
// The state register becomes a seq.firreg carrying the sig's name ("state",
// printed as the SSA result name so the {name=...} attr is elided) with the
// sig init (0) as its power-on preset.
// CHECK: %state = seq.firreg %{{.+}} clock %{{.+}} preset 0 : i8
// A priority mux chain (last drive in program order wins, register output as
// the innermost hold default).
// CHECK: comb.mux
// CHECK: comb.mux
// The output reads the register, not the old probe / init constant.
// CHECK: hw.output %state : i8
