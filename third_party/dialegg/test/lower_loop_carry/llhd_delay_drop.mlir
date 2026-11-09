// RUN: %eggopt %s --lower-loop-carry-processes='lower-llhd-delay-as-zero-cycle=true' | FileCheck %s
// RUN: %eggopt %s --lower-loop-carry-processes | FileCheck %s --check-prefix=NOFLAG
//
// Bare llhd.delay op (no surrounding process). Under the flag, the pass
// drops the op by replacing its result with its input operand (zero-cycle
// pass-through). With the flag off the op is left untouched.

module {
  hw.module @delay_passthrough(in %a : i1, out r : i1) {
    %0 = llhd.delay %a by <0ns, 1d, 0e> : i1
    hw.output %0 : i1
  }
}

// FLAG-ON: the delay must be erased, output drives %a directly.
// CHECK-LABEL: hw.module @delay_passthrough
// CHECK-NOT: llhd.delay
// CHECK: hw.output %a

// FLAG-OFF: the delay must remain (no change from input).
// NOFLAG-LABEL: hw.module @delay_passthrough
// NOFLAG: llhd.delay
