// RUN: %eggopt %s --lower-loop-carry-processes | FileCheck %s
//
// Multi-state FSM body: cf.cond_br jumps directly back to the loop header
// with operands on either side (no leaf cf.br ^L(...) terminator). The
// original design matcher rejected this shape because cond_br operands had
// to be empty. The extended matcher synthesizes a comb.mux tree over the
// branch conditions and binds the process results combinationally.

module {
  hw.module @mux3_fsm(in %sel : i2, in %a : i8, in %b : i8, in %c : i8, out r : i8) {
    %0 = llhd.constant_time <0ns, 1d, 0e>
    %true = hw.constant true
    %false = hw.constant false
    %c0_i8 = hw.constant 0 : i8
    %c1_i2 = hw.constant 1 : i2
    %c2_i2 = hw.constant 2 : i2
    %r = llhd.sig %c0_i8 : i8
    %1 = llhd.prb %r : i8
    %2:2 = llhd.process -> i8, i1 {
      cf.br ^bb1(%c0_i8, %false : i8, i1)
    ^bb1(%3: i8, %4: i1):
      llhd.wait yield (%3, %4 : i8, i1), (%sel, %a, %b, %c : i2, i8, i8, i8), ^bb2
    ^bb2:
      %5 = comb.icmp ceq %sel, %c1_i2 : i2
      cf.cond_br %5, ^bb1(%a, %true : i8, i1), ^bb3
    ^bb3:
      %6 = comb.icmp ceq %sel, %c2_i2 : i2
      cf.cond_br %6, ^bb1(%b, %true : i8, i1), ^bb1(%c, %true : i8, i1)
    }
    llhd.drv %r, %2#0 after %0 if %2#1 : i8
    hw.output %1 : i8
  }
}

// CHECK-LABEL: hw.module @mux3_fsm
// The llhd.process must be gone after the pass.
// CHECK-NOT: llhd.process
// CHECK-NOT: cf.br
// CHECK-NOT: cf.cond_br
// The lowered result should mux over the two icmp conditions.
// CHECK: comb.icmp ceq
// CHECK: comb.mux
