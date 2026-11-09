// RUN: %eggopt %s --pass-pipeline='builtin.module(hw.module(lower-free-llhd-signals))' | FileCheck %s
//
// Regression: rtllm/right_shifter
// shape. After the eggify / reconstruct round-trip, the original sig
// declaration gets split into TWO sigs:
//
//   * orphan %q (no drives, has the probe feeding hw.output)
//   * shadow %q_1 (has both the init-style drive and the real
//     firreg-fed drive; no probes)
//
// They share their init constant (%c0_i8). The
// `lower-free-llhd-signals` pass pairs them by init-value-equality,
// then substitutes the orphan's probe with the shadow's real drive
// value (here: the firreg result) and erases both sigs plus all the
// drives.
//
// Before this pass landed, `lower-loop-carry-processes` would have
// orphan-elided the probe with the init constant (because Phase 2
// treats sigs in isolation), producing a Verilog module whose output
// is hardwired to 0. With this sibling pass running first, the
// firreg result correctly drives hw.output.

module {
  hw.module @right_shifter(in %clk : i1, out q : i8, in %d : i1) {
    %c0_i8 = hw.constant 0 : i8
    %q = llhd.sig %c0_i8 : i8
    %c0_i8_0 = hw.constant 0 : i8
    %q_1 = llhd.sig name "q" %c0_i8_0 : i8
    %0 = llhd.constant_time <0ns, 0d, 1e>
    llhd.drv %q_1, %c0_i8_0 after %0 : i8
    %1 = comb.extract %4 from 1 : (i8) -> i7
    %2 = comb.concat %d, %1 : i1, i7
    %3 = seq.to_clock %clk
    %q_2 = seq.firreg %2 clock %3 {name = "q"} : i8
    llhd.drv %q_1, %q_2 after %0 : i8
    %4 = llhd.prb %q : i8
    hw.output %4 : i8
  }
}

// CHECK-LABEL: hw.module @right_shifter
// All llhd ops must be gone after this pass.
// CHECK-NOT: llhd.sig
// CHECK-NOT: llhd.drv
// CHECK-NOT: llhd.prb
// CHECK-NOT: llhd.constant_time
// The firreg must remain (we do not touch it).
// CHECK: seq.firreg
// The output must be the firreg result, NOT the init constant.
// CHECK: hw.output %{{.*}} : i8
