//===- LowerFreeLLHDSignalsPass.cpp - Lower free-standing llhd.sig --======//
//
// SPDX-License-Identifier: Apache-2.0
//
// Lowers free-standing `llhd.sig` / `llhd.drv` / `llhd.prb` triples that
// sit at `hw.module` scope (NOT enclosed in an `llhd.process`). These
// shapes show up after the eggify / reconstruct round-trip for designs
// whose only stateful element is a `seq.firreg` already lowered out of
// the process (e.g. `rtllm/right_shifter`). They confuse ExportVerilog,
// which requires the llhd dialect to be lowered before emission.
//
// Companion pass to LowerLoopCarryProcessesPass: that one handles sigs
// created/touched by lowering an llhd.process; this one handles the
// already-free-standing case the eggify round-trip can produce.
//

//
// Post-reconstruct shape (rtllm/right_shifter):
//
//   %c0_i8 = hw.constant 0 : i8
//   %q     = llhd.sig %c0_i8 : i8                    // "orphan": no drives,
//                                                     //  has probe
//   %c0_i8_0 = hw.constant 0 : i8
//   %q_1   = llhd.sig name "q" %c0_i8_0 : i8         // "shadow": has drives,
//                                                     //  no probes
//   %0     = llhd.constant_time <0ns, 0d, 1e>
//   llhd.drv %q_1, %c0_i8_0 after %0 : i8            // init-style drive
//   %1 = comb.extract %4 from 1 : (i8) -> i7
//   %2 = comb.concat %d, %1 : i1, i7
//   %3 = seq.to_clock %clk
//   %q_2 = seq.firreg %2 clock %3 {name = "q"} : i8  // real driver
//   llhd.drv %q_1, %q_2 after %0 : i8                // real (firreg) drive
//   %4 = llhd.prb %q : i8                            // probe orphan
//   hw.output %4 : i8
//
// The reconstruct pass splits one declared sig into TWO: an "orphan" sig
// the probe reads from, and a "shadow" sig the drives write to. The two
// share their init constant (often the same `hw.constant` op, sometimes
// duplicated). We pair them by init-value-equality, then lower the pair:
//
//   * Identify the "real" drive (non-init-style: not an unconditional
//     write of the init constant).
//   * Substitute all probes (from either sig) with the real drive's
//     value.
//   * Erase all drives, probes, and both sigs.
//
// For un-paired sigs (single sig with drives AND probes, or an orphan
// without a sister) we fall back to the same logic LCP's Phase 2 uses.
//
//===----------------------------------------------------------------------===//

#include "LowerFreeLLHDSignalsPass.h"

#include "mlir/IR/Builders.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Support/LLVM.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/Debug.h"

#include "circt/Dialect/HW/HWDialect.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/LLHD/LLHDDialect.h"
#include "circt/Dialect/LLHD/LLHDOps.h"
#include "circt/Dialect/Seq/SeqDialect.h"
#include "circt/Dialect/Seq/SeqOps.h"

#define DEBUG_TYPE "lower-free-llhd-signals"

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

/// True iff `a` and `b` are equal at the IR level: either the exact
/// same SSA value, or both `hw.constant` ops with equal integer values.
static bool valuesAreEqualConstants(Value a, Value b) {
  if (a == b)
    return true;
  IntegerAttr ai, bi;
  if (matchPattern(a, m_Constant(&ai)) && matchPattern(b, m_Constant(&bi)))
    return ai.getValue() == bi.getValue();
  return false;
}

/// Collect a sig's drives and probes; bail (return false) if any user
/// is something other than a drive or probe.
static bool collectSigUsers(llhd::SignalOp sig,
                            SmallVectorImpl<llhd::DriveOp> &drvs,
                            SmallVectorImpl<llhd::ProbeOp> &prbs) {
  for (Operation *user : sig.getResult().getUsers()) {
    if (auto d = dyn_cast<llhd::DriveOp>(user)) {
      drvs.push_back(d);
    } else if (auto p = dyn_cast<llhd::ProbeOp>(user)) {
      prbs.push_back(p);
    } else {
      LLVM_DEBUG(llvm::dbgs()
                 << "Skip sig: unsupported user op " << user->getName() << "\n");
      return false;
    }
  }
  return true;
}

/// True iff `drv` is an "init-style" unconditional drive whose value
/// equals the signal's init constant. CIRCT emits these alongside the
/// real datapath drive when lowering Verilog `initial` blocks.
static bool isInitStyleDrive(llhd::DriveOp drv, Value sigInit) {
  if (drv.getEnable())
    return false;
  if (!sigInit)
    return false;
  return valuesAreEqualConstants(drv.getValue(), sigInit);
}

/// Among the given drives, find the single non-init-style drive. Returns
/// null if there is no such drive, or more than one.
static llhd::DriveOp pickRealDrive(ArrayRef<llhd::DriveOp> drvs,
                                   Value sigInit) {
  llhd::DriveOp real;
  for (auto d : drvs) {
    if (isInitStyleDrive(d, sigInit))
      continue;
    if (real)
      return {};
    real = d;
  }
  return real;
}

/// Lower a single sig (with drives AND probes): substitute probes with
/// the real drive's value, drop init-style drives, erase the sig.
/// Returns true on success.
static bool lowerSelfContainedSig(llhd::SignalOp sig,
                                  ArrayRef<llhd::DriveOp> drvs,
                                  ArrayRef<llhd::ProbeOp> prbs) {
  Value sigInit = sig.getInit();
  llhd::DriveOp keep = pickRealDrive(drvs, sigInit);
  if (!keep) {
    LLVM_DEBUG(llvm::dbgs()
               << "Skip sig: no unique real drive (count="
               << drvs.size() << ")\n");
    return false;
  }
  // Drop init-style drives, erase the real drive after replacing probes.
  for (auto d : drvs) {
    if (d == keep)
      continue;
    if (isInitStyleDrive(d, sigInit))
      d.erase();
  }
  for (auto p : prbs) {
    p.getResult().replaceAllUsesWith(keep.getValue());
    p.erase();
  }
  keep.erase();
  sig.erase();
  return true;
}

/// Lower an orphan-shadow pair: `orphan` has no drives and >=1 probe,
/// `shadow` has drives and no probes. They share an init constant.
/// Returns true on success.
static bool lowerOrphanShadowPair(llhd::SignalOp orphan,
                                  ArrayRef<llhd::ProbeOp> orphanPrbs,
                                  llhd::SignalOp shadow,
                                  ArrayRef<llhd::DriveOp> shadowDrvs) {
  Value shadowInit = shadow.getInit();
  llhd::DriveOp keep = pickRealDrive(shadowDrvs, shadowInit);
  if (!keep) {
    LLVM_DEBUG(llvm::dbgs()
               << "Skip pair: shadow has no unique real drive (count="
               << shadowDrvs.size() << ")\n");
    return false;
  }
  Value driveVal = keep.getValue();
  // Replace orphan probes with the shadow's real-drive value.
  for (auto p : orphanPrbs) {
    p.getResult().replaceAllUsesWith(driveVal);
    p.erase();
  }
  // Drop init-style shadow drives, then the real drive.
  for (auto d : shadowDrvs) {
    if (d == keep)
      continue;
    if (isInitStyleDrive(d, shadowInit))
      d.erase();
  }
  keep.erase();
  shadow.erase();
  orphan.erase();
  return true;
}

/// Pass entry point.
struct LowerFreeLLHDSignalsPass
    : public PassWrapper<LowerFreeLLHDSignalsPass,
                         OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LowerFreeLLHDSignalsPass)

  StringRef getArgument() const final { return "lower-free-llhd-signals"; }
  StringRef getDescription() const final {
    return "Lower free-standing llhd.sig/drv/prb triples at hw.module "
           "scope (not inside llhd.process). Sibling pass to "
           "lower-loop-carry-processes for the orphan-shadow sig pairs "
           "the eggify/reconstruct round-trip can produce.";
  }

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<llhd::LLHDDialect, hw::HWDialect, seq::SeqDialect>();
  }

  void runOnOperation() override {
    hw::HWModuleOp module = getOperation();

    // Snapshot free-standing sigs (we'll erase as we go).
    SmallVector<llhd::SignalOp> sigs;
    module.walk([&](llhd::SignalOp s) {
      if (isFreeStanding(s))
        sigs.push_back(s);
    });
    if (sigs.empty())
      return;

    // Classify each sig as orphan (no drives), shadow (drives but no
    // probes), or self-contained (both).
    struct SigInfo {
      llhd::SignalOp sig;
      SmallVector<llhd::DriveOp> drvs;
      SmallVector<llhd::ProbeOp> prbs;
      bool usable;
    };
    SmallVector<SigInfo> infos;
    infos.reserve(sigs.size());
    for (auto s : sigs) {
      SigInfo info;
      info.sig = s;
      info.usable = collectSigUsers(s, info.drvs, info.prbs);
      infos.push_back(std::move(info));
    }

    // Phase A: pair orphans with shadows by init-value-equality, then
    // lower each pair. Each sig is consumed by at most one pair.
    SmallVector<bool> consumed(infos.size(), false);
    for (size_t i = 0; i < infos.size(); ++i) {
      if (consumed[i] || !infos[i].usable)
        continue;
      // Orphan: no drives, >=1 probe.
      if (!infos[i].drvs.empty() || infos[i].prbs.empty())
        continue;
      Value initI = infos[i].sig.getInit();
      if (!initI)
        continue;
      // Look for a sister shadow with matching init, drives present,
      // no probes.
      for (size_t j = 0; j < infos.size(); ++j) {
        if (j == i || consumed[j] || !infos[j].usable)
          continue;
        if (infos[j].drvs.empty() || !infos[j].prbs.empty())
          continue;
        Value initJ = infos[j].sig.getInit();
        if (!initJ)
          continue;
        if (!valuesAreEqualConstants(initI, initJ))
          continue;
        // Pair!
        if (lowerOrphanShadowPair(infos[i].sig, infos[i].prbs, infos[j].sig,
                                  infos[j].drvs)) {
          consumed[i] = true;
          consumed[j] = true;
        }
        break;
      }
    }

    // Phase B: lower remaining self-contained sigs (drives AND probes).
    // We do NOT touch sigs that LCP would handle (i.e. sigs whose drives
    // come from an llhd.process result) because LCP runs in the same
    // pipeline and already covers those.
    for (size_t i = 0; i < infos.size(); ++i) {
      if (consumed[i] || !infos[i].usable)
        continue;
      if (infos[i].drvs.empty() || infos[i].prbs.empty())
        continue;
      // Skip sigs whose any drive value is an llhd.process result —
      // LCP owns those.
      bool fromProcess = false;
      for (auto d : infos[i].drvs) {
        if (auto *def = d.getValue().getDefiningOp()) {
          if (isa<llhd::ProcessOp>(def)) {
            fromProcess = true;
            break;
          }
        }
      }
      if (fromProcess)
        continue;
      lowerSelfContainedSig(infos[i].sig, infos[i].drvs, infos[i].prbs);
    }

    // Phase C: erase llhd.constant_time ops whose results are now dead.
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

void registerLowerFreeLLHDSignalsPass() {
  PassRegistration<LowerFreeLLHDSignalsPass>();
}
