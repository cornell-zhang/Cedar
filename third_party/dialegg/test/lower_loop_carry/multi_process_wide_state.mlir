// RUN: %eggopt %s --pass-pipeline='builtin.module(hw.module(lower-loop-carry-processes{lower-llhd-delay-as-zero-cycle=true}))' | FileCheck %s
//
// Regression: rtllm/float_multi
// shape. The module contains MULTIPLE parallel llhd.process ops, each
// observing a small counter signal and conditionally producing one
// piece of the floating-point state vector. Inside one of the
// processes is a forward-only diamond merge ^bb5 carrying wide
// non-power-of-two state (i10 + i1 + i24 + i1) which is then used in
// a downstream block (NOT just forwarded to the loop header).
//
// Pre-fix this design produced `Skip process: nested loop` for every
// merge-carrying process. The matcher now resolves each forward
// merge in turn — including the case where the merge block's args
// are referenced by post-merge ops (^bb6 in float_multi's third
// process uses the merged values). The forward-sub-CFG cloning
// strategy substitutes block args across the entire merged-and-
// downstream region, so dominance is preserved without inserting
// out-of-place mux ops.

module {
  hw.module @float_multi_mini(in %clk : i1, in %rst : i1,
                              in %a : i32, in %b : i32,
                              out z : i32) {
    %c0_i3 = hw.constant 0 : i3
    %c1_i3 = hw.constant 1 : i3
    %c-1_i10 = hw.constant -1 : i10
    %c-1_i24 = hw.constant -1 : i24
    %c0_i24 = hw.constant 0 : i24
    %c0_i10 = hw.constant 0 : i10
    %c0_i32 = hw.constant 0 : i32
    %false = hw.constant false
    %true = hw.constant true
    %z = llhd.sig %c0_i32 : i32
    %counter = llhd.sig %c0_i3 : i3
    %0 = llhd.prb %counter : i3
    %a_mantissa = llhd.sig %c0_i24 : i24
    %1 = llhd.prb %a_mantissa : i24
    %a_exponent = llhd.sig %c0_i10 : i10
    %4 = llhd.prb %a_exponent : i10
    %15 = llhd.constant_time <0ns, 1d, 0e>
    // First process: just a counter tick, no merge — exercises the
    // "multi-process module" axis of regression (matcher must not
    // bail on later processes after rewriting earlier ones).
    %14:2 = llhd.process -> i3, i1 {
      cf.br ^bb1(%clk, %rst, %c0_i3, %false : i1, i1, i3, i1)
    ^bb1(%25: i1, %26: i1, %27: i3, %28: i1):
      llhd.wait yield (%27, %28 : i3, i1), (%clk, %rst : i1, i1),
                ^bb2(%25, %26 : i1, i1)
    ^bb2(%29: i1, %30: i1):
      %31 = comb.xor bin %29, %true : i1
      %32 = comb.and bin %31, %clk : i1
      cf.cond_br %32, ^bb3,
                 ^bb1(%clk, %rst, %0, %false : i1, i1, i3, i1)
    ^bb3:
      %35 = comb.add %0, %c1_i3 : i3
      cf.br ^bb1(%clk, %false, %35, %true : i1, i1, i3, i1)
    }
    llhd.drv %counter, %14#0 after %15 if %14#1 : i3
    // Second process: diamond merge ^bb5 with downstream use in ^bb6.
    // Wide state (i10 + i1 + i24 + i1) carried through the merge.
    %18:4 = llhd.process -> i24, i1, i10, i1 {
      cf.br ^bb1(%c0_i24, %false, %c0_i10, %false : i24, i1, i10, i1)
    ^bb1(%25: i24, %26: i1, %27: i10, %28: i1):
      llhd.wait yield (%25, %26, %27, %28 : i24, i1, i10, i1),
                (%0 : i3), ^bb2
    ^bb2:
      %33 = comb.icmp eq %0, %c1_i3 : i3
      cf.cond_br %33, ^bb3,
                 ^bb1(%1, %false, %4, %false : i24, i1, i10, i1)
    ^bb3:
      %34 = comb.extract %1 from 23 : (i24) -> i1
      %35 = comb.xor %34, %true : i1
      cf.cond_br %35, ^bb4, ^bb5(%1, %false, %4, %false : i24, i1, i10, i1)
    ^bb4:
      %36 = comb.extract %1 from 0 : (i24) -> i23
      %37 = comb.concat %36, %false : i23, i1
      %38 = comb.add %4, %c-1_i10 : i10
      cf.br ^bb5(%37, %true, %38, %true : i24, i1, i10, i1)
    ^bb5(%39: i24, %40: i1, %41: i10, %42: i1):  // 2 preds: ^bb3, ^bb4
      // Downstream use: ^bb6 references the merge block's args.
      cf.br ^bb6
    ^bb6:
      %43 = comb.add %39, %c-1_i24 : i24
      cf.br ^bb1(%43, %40, %41, %42 : i24, i1, i10, i1)
    }
    llhd.drv %a_mantissa, %18#0 after %15 if %18#1 : i24
    llhd.drv %a_exponent, %18#2 after %15 if %18#3 : i10
    hw.output %c0_i32 : i32
  }
}

// CHECK-LABEL: hw.module @float_multi_mini
// All llhd.process / wait / cf ops must be gone after the pass.
// CHECK-NOT: llhd.process
// CHECK-NOT: llhd.wait
// CHECK-NOT: cf.br
// CHECK-NOT: cf.cond_br
// The downstream comb.add (in ^bb6, referencing merge block args) must
// have been duplicated per predecessor.
// CHECK: hw.output
