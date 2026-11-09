// RUN: %eggopt %s --pass-pipeline='builtin.module(hw.module(lower-loop-carry-processes))' | FileCheck %s
//
// Regression: array-write FSM with a
// constant-trip-count reset loop inside the wait body. Block ^bb4 is the
// header of a `for (i = 0; i < 8; i = i + 1) mem[i] <= 0` reset loop
// (preds ^bb3 starts it, ^bb5 continues it) carrying its own block args
// (the reset index + array-in-flight). This is exactly the rtllm/RAM
// shape.
//
// Previously the matcher bailed on this shape (the nested
// cycle defeated forward-merge inlining), leaving the llhd.process + cf.br
// intact — which then leaked to ExportVerilog ("dialect cf not supported").
// The pass now fully UNROLLS the constant-trip loop: the
// cycle disappears, the 8 iterations become 8 straight-line
// hw.array_inject ops, and the surrounding clocked FSM lowers to
// seq.firreg. The wait observes a real clock edge ((~prev_clk) & clk), so
// the loop-carried state becomes a flop clocked by a synthesized
// seq.to_clock — NOT a self-referential comb mux.

module {
  hw.module @ram_array_write(in %clk : i1, in %rst_n : i1, in %we : i1,
                             in %waddr : i4, in %wdata : i8,
                             out rdata : i8) {
    %true = hw.constant true
    %false = hw.constant false
    %c0_i32 = hw.constant 0 : i32
    %c1_i32 = hw.constant 1 : i32
    %c8_i32 = hw.constant 8 : i32
    %c0_i8 = hw.constant 0 : i8
    %c0_i96 = hw.constant 0 : i96
    %0 = hw.bitcast %c0_i96 : (i96) -> !hw.array<12xi8>
    %mem = llhd.sig %0 : !hw.array<12xi8>
    %1 = llhd.prb %mem : !hw.array<12xi8>
    %2:4 = llhd.process -> i32, i1, !hw.array<12xi8>, i1 {
      cf.br ^bb1(%clk, %rst_n, %c0_i32, %false, %0, %false : i1, i1, i32, i1, !hw.array<12xi8>, i1)
    ^bb1(%14: i1, %15: i1, %16: i32, %17: i1, %18: !hw.array<12xi8>, %19: i1):
      llhd.wait yield (%16, %17, %18, %19 : i32, i1, !hw.array<12xi8>, i1),
                (%clk, %rst_n : i1, i1),
                ^bb2(%14, %15 : i1, i1)
    ^bb2(%20: i1, %21: i1):
      %edge = comb.xor bin %20, %true : i1
      %posedge = comb.and bin %edge, %clk : i1
      cf.cond_br %posedge, ^bb3, ^bb1(%clk, %rst_n, %c0_i32, %false, %1, %false : i1, i1, i32, i1, !hw.array<12xi8>, i1)
    ^bb3:
      %not_rst = comb.xor %rst_n, %true : i1
      cf.cond_br %not_rst,
                 ^bb4(%c0_i32, %1, %false : i32, !hw.array<12xi8>, i1),
                 ^bb1(%clk, %rst_n, %c0_i32, %false, %1, %false : i1, i1, i32, i1, !hw.array<12xi8>, i1)
    // Nested reset loop: ^bb4 has two predecessors (^bb3 and ^bb5),
    // each forwarding the reset-loop carry. Hoisting ops out of ^bb4
    // would orphan the block args.
    ^bb4(%28: i32, %29: !hw.array<12xi8>, %30: i1):
      %31 = comb.icmp slt %28, %c8_i32 : i32
      cf.cond_br %31, ^bb5, ^bb1(%clk, %rst_n, %28, %true, %29, %30 : i1, i1, i32, i1, !hw.array<12xi8>, i1)
    ^bb5:
      %addr = comb.extract %28 from 0 : (i32) -> i4
      %wr = hw.array_inject %29[%addr], %c0_i8 : !hw.array<12xi8>, i4
      %inc = comb.add %28, %c1_i32 : i32
      cf.br ^bb4(%inc, %wr, %true : i32, !hw.array<12xi8>, i1)
    }
    %3 = llhd.constant_time <0ns, 1d, 0e>
    llhd.drv %mem, %2#2 after %3 if %2#3 : !hw.array<12xi8>
    %rd = hw.array_get %1[%waddr] : !hw.array<12xi8>, i4
    hw.output %rd : i8
  }
}

// CHECK-LABEL: hw.module @ram_array_write
// The process / wait / cf back-edges must all be gone after the pass.
// CHECK-NOT: llhd.process
// CHECK-NOT: llhd.wait
// CHECK-NOT: cf.br
// CHECK-NOT: cf.cond_br
// A clock is synthesized for the loop-carry flops (the wait observed a real
// clock edge).
// CHECK: seq.to_clock
// The 8-iteration reset loop is fully unrolled — one array_inject per
// iteration (we check for at least a couple to confirm unrolling happened
// rather than the bailed-out single-process form).
// CHECK: hw.array_inject
// CHECK: hw.array_inject
// The clocked loop-carry state lands in seq.firreg flops, NOT a
// combinational binding.
// CHECK: seq.firreg
// (No "operand does not dominate this use" diagnostic; the RUN line exits 0
// only if the module verifies after the pass.)
