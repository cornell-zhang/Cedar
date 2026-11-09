// RUN: %eggopt %s --pass-pipeline='builtin.module(hw.module(lower-loop-carry-processes{lower-llhd-delay-as-zero-cycle=true}))' | FileCheck %s
//
// Regression: rtllm/multi_booth_8bit
// shape. The process body has:
//   * A forward-only diamond merge block (^bb8) — two predecessors
//     (^bb6 on the cond_br false arm, ^bb7 on the cf.br fall-through),
//     each forwarding different operand tuples to the merge. The merge
//     block's args feed both the loop-header back-edge AND a downstream
//     comb.add inside the merge.
//   * `%clk` and `%reset` threaded as block arguments rather than
//     `llhd.prb`'d sigs — they ride through the wait via dest-operands
//     and edge-detection happens in the post-wait body.
//
// Pre-fix this design produced `Skip process: nested loop` because the
// matcher's pre-flight bailed on ALL multi-pred body blocks. The new
// `inlineAllForwardMerges` step distinguishes true nested loops
// (multi-pred AND reachable-from-self) from forward-only diamonds
// (multi-pred BUT no self-cycle) and inlines the latter by cloning
// the forward sub-CFG into each predecessor.
//
// Bitwidths picked to match the actual multi_booth_8bit shape but
// trimmed down for readability — i16 multiplier value carried through
// the merge.

module {
  hw.module @booth_diamond(out p : i16, in %clk : i1, in %reset : i1,
                           in %a : i8, in %b : i8) {
    %false = hw.constant false
    %true = hw.constant true
    %c1_i5 = hw.constant 1 : i5
    %c0_i5 = hw.constant 0 : i5
    %c0_i16 = hw.constant 0 : i16
    %p = llhd.sig %c0_i16 : i16
    %ctr = llhd.sig %c0_i5 : i5
    %0 = llhd.prb %ctr : i5
    %1 = llhd.prb %p : i16
    %2:6 = llhd.process -> i1, i1, i16, i1, i5, i1 {
      cf.br ^bb1(%clk, %reset, %false, %false, %c0_i16, %false, %c0_i5, %false :
                 i1, i1, i1, i1, i16, i1, i5, i1)
    ^bb1(%8: i1, %9: i1, %10: i1, %11: i1, %12: i16, %13: i1, %14: i5, %15: i1):
      llhd.wait yield (%10, %11, %12, %13, %14, %15 : i1, i1, i16, i1, i5, i1),
                (%clk, %reset : i1, i1),
                ^bb2(%8, %9 : i1, i1)
    ^bb2(%20: i1, %21: i1):  // pred: ^bb1
      %22 = comb.xor bin %20, %true : i1
      %23 = comb.and bin %22, %clk : i1
      cf.cond_br %23, ^bb3,
                 ^bb1(%clk, %reset, %false, %false, %1, %false, %0, %false :
                      i1, i1, i1, i1, i16, i1, i5, i1)
    ^bb3:  // pred: ^bb2
      %34 = comb.icmp ult %0, %c1_i5 : i5
      cf.cond_br %34, ^bb4,
                 ^bb1(%clk, %false, %true, %true, %1, %false, %0, %false :
                      i1, i1, i1, i1, i16, i1, i5, i1)
    ^bb4:  // pred: ^bb3
      %36 = comb.add %1, %1 : i16
      %38 = comb.extract %36 from 0 : (i16) -> i1
      cf.cond_br %38, ^bb5, ^bb6(%1, %false : i16, i1)
    ^bb5:  // pred: ^bb4
      %40 = comb.add %1, %1 : i16
      cf.br ^bb6(%40, %true : i16, i1)
    ^bb6(%41: i16, %42: i1):  // 2 preds: ^bb4, ^bb5
      %43 = comb.add %0, %c1_i5 : i5
      cf.br ^bb1(%clk, %reset, %false, %false, %41, %42, %43, %true :
                 i1, i1, i1, i1, i16, i1, i5, i1)
    }
    %3 = llhd.constant_time <0ns, 1d, 0e>
    llhd.drv %ctr, %2#4 after %3 if %2#5 : i5
    llhd.drv %p, %2#2 after %3 if %2#3 : i16
    hw.output %1 : i16
  }
}

// CHECK-LABEL: hw.module @booth_diamond
// The process and all llhd / cf ops must be gone after the pass +
// orphan-sig elision. Pure comb logic remains.
// CHECK-NOT: llhd.process
// CHECK-NOT: llhd.wait
// CHECK-NOT: cf.br
// CHECK-NOT: cf.cond_br
// CHECK: hw.output
