//===- LowerInterProcessLLHDSignalsPass.cpp - lower shared module sigs ===//
//
// SPDX-License-Identifier: Apache-2.0
//
// Lowers module-scope `llhd.sig` / `llhd.drv` / `llhd.prb` triples that
// survive at `hw.module` scope because they are read AND written across
// several sibling `llhd.process` regions (an inter-process register), and
// therefore are NOT handled by either companion pass:
//
//   * `lower-free-llhd-signals` (orphan path) only collapses sigs that
//     have a single unique real drive (orphan/shadow init pairs); it bails
//     the moment a sig has multiple real (conditional) drives.
//   * `lower-loop-carry-processes` (LCP) lowers each `llhd.process` to
//     `seq.firreg` + comb mux. After LCP runs, the processes are gone, but
//     the module-scope state sigs they shared (read by one process, written
//     by several) are left behind as plain conditional `llhd.drv ... if`
//     ops at module scope. LCP declines them because the references cross
//     the process boundary — they were never internal to a single process.
//

//
// This pass MUST run AFTER LCP: it only fires on the straight-line,
// process-free shape LCP leaves behind. The expected post-LCP shape for
// one inter-process state register `X` is:
//
//   %cX     = hw.constant <init> : iN
//   %X      = llhd.sig %cX : iN                 // module-scope state sig
//   %p      = llhd.prb %X : iN                  // single combinational read
//   ... (combinational fan-out of %p across the whole module) ...
//   %t      = llhd.constant_time <0ns, 1d, 0e>  // 1-delta = next cycle
//   %v0     = ...                               // candidate next-value 0
//   %e0     = ...                               // enable 0 (state guard)
//   llhd.drv %X, %v0 after %t if %e0 : iN       // conditional write 0
//   %v1     = ...
//   %e1     = ...
//   llhd.drv %X, %v1 after %t if %e1 : iN       // conditional write 1
//   ...
//   hw.output %p (transitively) ...
//
// Semantically each `llhd.drv ... if %ek` schedules `vk` for the NEXT
// delta (the `after %t` time is `1d`), and the probe `%p` reads the
// CURRENT (clocked) value. The mutually-/priority-ordered conditional
// drives plus the implicit "hold when no enable fires" make this exactly
// an enable-mux register. We lower it to:
//
//   %X_reg = seq.firreg %next clock %clk {name = "X"} preset <init> : iN
//   %next  = mux(eK, vK, ... mux(e1, v1, mux(e0, v0, %X_reg)) ...)
//
// i.e. a priority mux chain in program order (the LAST drive in source
// order wins, matching LLHD delta last-writer-wins), whose innermost
// default is the register's own output (hold). Every use of the probe is
// then replaced with the register output, and the sig / probe / drives are
// erased.
//
// Conservative guards — the pass leaves a sig UNTOUCHED (so a later pass
// or ExportVerilog diagnostic still sees it) unless ALL of these hold:
//   * the sig is free-standing (hw.module scope, not inside a process);
//   * every user is an `llhd.prb` or `llhd.drv` (no aliasing/extract);
//   * there is exactly ONE probe;
//   * there is at least one drive and EVERY drive is enabled (conditional)
//     with the SAME time operand (the 1-delta clocked-write shape);
//   * no drive value is an `llhd.process` result (those still belong to
//     LCP — defensive: this pass runs after LCP, so none should remain);
//   * a module clock can be inferred from an existing `seq.to_clock`.
// Anything else is out of this narrow scope and is intentionally skipped.
//
//===----------------------------------------------------------------------===//

#include "LowerInterProcessLLHDSignalsPass.h"

#include "mlir/IR/Builders.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Support/LLVM.h"
#include "llvm/ADT/STLExtras.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/Debug.h"

#include "circt/Dialect/Comb/CombDialect.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWDialect.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/LLHD/LLHDDialect.h"
#include "circt/Dialect/LLHD/LLHDOps.h"
#include "circt/Dialect/Seq/SeqDialect.h"
#include "circt/Dialect/Seq/SeqOps.h"

#define DEBUG_TYPE "lower-interprocess-llhd-signals"

using namespace mlir;
using namespace circt;

namespace {

/// True iff `sig` is at `hw.module` scope, i.e. NOT nested inside an
/// `llhd.process`. Walks ancestors and bails the first time we see a
/// ProcessOp.
static bool isFreeStanding(llhd::SignalOp sig) {
  Operation *parent = sig->getParentOp();
  while (parent) {
    if (isa<llhd::ProcessOp>(parent))
      return false;
    if (isa<hw::HWModuleOp>(parent))
      return true;
    parent = parent->getParentOp();
  }
  return false;
}

/// Collect a sig's drives and probes; bail (return false) if any user is
/// something other than a drive or probe.
static bool collectSigUsers(llhd::SignalOp sig,
                            SmallVectorImpl<llhd::DriveOp> &drvs,
                            SmallVectorImpl<llhd::ProbeOp> &prbs) {
  for (Operation *user : sig.getResult().getUsers()) {
    if (auto d = dyn_cast<llhd::DriveOp>(user)) {
      drvs.push_back(d);
    } else if (auto p = dyn_cast<llhd::ProbeOp>(user)) {
      prbs.push_back(p);
    } else {
      LLVM_DEBUG(llvm::dbgs() << "Skip inter-process sig: unsupported user op "
                              << user->getName() << "\n");
      return false;
    }
  }
  return true;
}

/// Find an existing seq.to_clock op in the surrounding module. Used as the
/// inferred clock for the synthesised firreg. Assumes a single-clock design
/// (every state register in the post-LCP IR is clocked by the same edge).
static Value findExistingClock(hw::HWModuleOp module) {
  Value clock;
  module.walk([&](seq::ToClockOp op) {
    if (!clock)
      clock = op.getResult();
  });
  return clock;
}

/// True iff this sig matches the narrow inter-process state-register shape:
/// exactly one probe, >=1 drive, every drive enabled with the same time
/// operand, no drive value defined by an llhd.process.
static bool matchesInterProcessShape(ArrayRef<llhd::DriveOp> drvs,
                                     ArrayRef<llhd::ProbeOp> prbs) {
  if (prbs.size() != 1)
    return false;
  if (drvs.empty())
    return false;
  Value commonTime;
  for (auto d : drvs) {
    // Every drive must be conditional (enabled). An unconditional drive at
    // module scope would mean an always-on write, which is not the
    // enable-mux register shape and is out of this narrow scope.
    if (!d.getEnable())
      return false;
    // All drives must schedule for the same delta (the clocked next-cycle
    // write). Differing times would be a transport-delay shape we do not
    // model here.
    if (!commonTime)
      commonTime = d.getTime();
    else if (commonTime != d.getTime())
      return false;
    // Defensive: a drive value that is still an llhd.process result belongs
    // to LCP, which must have run first. Decline so we never race LCP.
    if (auto *def = d.getValue().getDefiningOp()) {
      if (isa<llhd::ProcessOp>(def))
        return false;
    }
  }
  return true;
}

/// Lower one inter-process state sig into a seq.firreg + priority mux chain.
/// Returns true on success (sig/probe/drives erased), false if it could not
/// be lowered (left untouched).
static bool lowerInterProcessSig(hw::HWModuleOp module, llhd::SignalOp sig,
                                 ArrayRef<llhd::DriveOp> drvs,
                                 ArrayRef<llhd::ProbeOp> prbs, Value clock) {
  if (!matchesInterProcessShape(drvs, prbs))
    return false;

  Value sigInit = sig.getInit();

  // The hw.module body is a graph region, so we may build the firreg and
  // its next-state chain anywhere and reference values defined later in the
  // block. We insert at the sig's position to keep emission order stable.
  OpBuilder builder(sig);
  Location loc = sig.getLoc();
  Type ty = sig.getInit().getType();

  // Pre-create the firreg with a placeholder `next` (its own init constant)
  // so the priority mux chain can reference the register output for "hold".
  // We patch the real `next` operand in after building the chain.
  StringAttr name;
  if (auto n = sig.getNameAttr())
    name = n;
  else
    name = builder.getStringAttr("");

  // Preset (power-on value) from the sig init, when it is a constant int.
  // Mirrors the llhd.sig init semantics. Non-constant inits leave preset
  // unset (the register is then implicitly randomized, same as plain LCP
  // firregs without a reset).
  Attribute preset;
  IntegerAttr initInt;
  if (matchPattern(sigInit, m_Constant(&initInt)))
    preset = builder.getIntegerAttr(builder.getIntegerType(
                                        initInt.getValue().getBitWidth()),
                                    initInt.getValue());

  auto firreg = builder.create<seq::FirRegOp>(loc, sigInit, clock, name,
                                              hw::InnerSymAttr{}, preset);
  Value regOut = firreg.getResult();

  // Build the priority mux chain. Innermost default = register output
  // (hold). We must wrap drives in PROGRAM order so the LAST drive in
  // source order ends up the OUTERMOST (highest-priority) mux, matching
  // LLHD delta last-writer-wins semantics. `getUsers()` does not iterate in
  // program order, so sort by block position first. (For float_multi the
  // per-state enables are mutually exclusive so priority is moot, but we
  // make it deterministic and correct for the general overlapping case.)
  SmallVector<llhd::DriveOp> ordered(drvs.begin(), drvs.end());
  llvm::sort(ordered, [](llhd::DriveOp a, llhd::DriveOp b) {
    return a->isBeforeInBlock(b);
  });
  Value next = regOut;
  for (auto d : ordered) {
    next = builder.create<comb::MuxOp>(d.getLoc(), ty, d.getEnable(),
                                       d.getValue(), next,
                                       /*twoState=*/false);
  }

  // Patch the real next-state expression into the firreg.
  firreg.getNextMutable().assign(next);

  // Replace the single probe's uses with the register output and erase it.
  // Copy the op handle out of the (const) ArrayRef so the mutating calls
  // are not on a const-qualified element.
  llhd::ProbeOp probe = prbs[0];
  probe.getResult().replaceAllUsesWith(regOut);
  probe.erase();

  // Erase all drives, then the sig.
  for (auto d : drvs)
    d.erase();
  sig.erase();
  return true;
}

/// Pass entry point.
struct LowerInterProcessLLHDSignalsPass
    : public PassWrapper<LowerInterProcessLLHDSignalsPass,
                         OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(
      LowerInterProcessLLHDSignalsPass)

  StringRef getArgument() const final {
    return "lower-interprocess-llhd-signals";
  }
  StringRef getDescription() const final {
    return "Lower module-scope llhd.sig/drv/prb state registers shared "
           "across sibling llhd.process regions (inter-process registers) "
           "to seq.firreg + comb mux. Runs after lower-loop-carry-processes; "
           "handles the conditional-drive enable-mux shape that neither the "
           "orphan free-sig path nor LCP cover.";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<llhd::LLHDDialect, hw::HWDialect, seq::SeqDialect,
                    comb::CombDialect>();
  }

  void runOnOperation() override {
    hw::HWModuleOp module = getOperation();

    // Snapshot free-standing sigs (we erase as we go).
    SmallVector<llhd::SignalOp> sigs;
    module.walk([&](llhd::SignalOp s) {
      if (isFreeStanding(s))
        sigs.push_back(s);
    });
    if (sigs.empty())
      return;

    Value clock = findExistingClock(module);
    if (!clock) {
      // No clock to register against — this is not the clocked inter-process
      // shape we handle. Leave everything for a later pass / diagnostic.
      LLVM_DEBUG(llvm::dbgs()
                 << "No seq.to_clock in module; skipping all sigs\n");
      return;
    }

    for (auto sig : sigs) {
      SmallVector<llhd::DriveOp> drvs;
      SmallVector<llhd::ProbeOp> prbs;
      if (!collectSigUsers(sig, drvs, prbs))
        continue;
      lowerInterProcessSig(module, sig, drvs, prbs, clock);
    }

    // Erase llhd.constant_time ops whose results are now dead (the drives
    // that referenced them are gone).
    SmallVector<llhd::ConstantTimeOp> times;
    module.walk([&](llhd::ConstantTimeOp t) {
      if (t.getResult().use_empty())
        times.push_back(t);
    });
    for (auto t : times)
      t.erase();
  }
};

} // namespace

void registerLowerInterProcessLLHDSignalsPass() {
  PassRegistration<LowerInterProcessLLHDSignalsPass>();
}
