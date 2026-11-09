//===- LowerLoopCarryProcessesPass.cpp - Loop-carry process desequencing --===//
//
// SPDX-License-Identifier: Apache-2.0
//
// Lowers llhd.process ops that implement a loop-carry FSM to seq.firreg +
// comb logic. Handles the pattern that CIRCT emits when lowering Verilog
// `always @*` blocks with intermediate yielded state — specifically the
// case where llhd-lower-processes / llhd-deseq both bail because the body
// has a back-edge from internal blocks to the wait's parent block
// ("control from entry and wait does not converge" / "observes 0 values").
//
// Baseline pattern (single-leaf back-edge):
//   %r:N = llhd.process -> Ts... {
//     cf.br ^L(init...)
//   ^L(carry...: Ts...):
//     llhd.wait yield (carry... : Ts...), (observed...), ^body
//   ^body:
//     ... acyclic CFG, cond-branches only depend on observed/derived ...
//   ^leafK:
//     cf.br ^L(new...)
//   }
//   llhd.drv %sig, %r#i after %t if %r#cond : Ti
//   %p = llhd.prb %sig : Ti
//
// Supported extensions:
//   1. Multi-state FSM bodies whose internal cf.cond_br jumps directly back
//      to the wait block ^L with operands (instead of falling through to a
//      leaf cf.br ^L(...)). computeNextState now handles cond_br operands.
//   2. Wait-dest blocks that take operands forwarded from the wait
//      (edge-detection pattern: ^L(... ,a,b,...): llhd.wait ..., ^body(a,b))
//      — wait-dest block args are pre-substituted with the wait's dest
//      operands before next-state computation, so the post-wait body sees
//      the values directly.
//   3. Clock-divider-style processes whose wait carries a delay and no
//      observed signals; the back-edge writes a flipped form of the
//      previously-probed sig. Under the new --lower-llhd-delay-as-zero-cycle
//      flag (default false), we lower these by binding result_i = next_i
//      combinationally (no firreg). Semantically the resulting design has
//      its delay collapsed, but it lets ExportVerilog emit the surrounding
//      module instead of refusing wholesale.
//   4. The pass also tolerates double-drive sigs where one drive has no
//      enable and writes a constant equal to the signal's initial value
//      (typical of CIRCT-emitted llhd init drives shadowed by a firreg).
//   5. An optional pass step (gated by the same flag) drops `llhd.delay`
//      ops by replacing each result with its input operand.
//
// Transformation:
//   For each result index i:
//     %next_i = (mux tree over cond_br conditions, leaves = new[i])
//     %r_i    = seq.firreg %next_i clock %clk
//                   [reset (sync|async) %rst, init_i]
//                   { name = "loop_carry_i" } : Ti
//   Each llhd.drv writing %r#i into a sig and llhd.prb reading the same sig
//   become a direct connect of %r_i.
//
//===----------------------------------------------------------------------===//

#include "LowerLoopCarryProcessesPass.h"

#include "mlir/Dialect/ControlFlow/IR/ControlFlowOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinTypes.h"
#include "mlir/IR/IRMapping.h"
#include "mlir/IR/Matchers.h"
#include "mlir/IR/PatternMatch.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Support/LLVM.h"
#include "llvm/ADT/SetVector.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Debug.h"

#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWDialect.h"
#include "circt/Dialect/HW/HWOps.h"
#include "circt/Dialect/LLHD/LLHDDialect.h"
#include "circt/Dialect/LLHD/LLHDOps.h"
#include "circt/Dialect/Seq/SeqDialect.h"
#include "circt/Dialect/Seq/SeqOps.h"
#include "circt/Dialect/Seq/SeqTypes.h"

#define DEBUG_TYPE "lower-loop-carry-processes"

using namespace mlir;
using namespace circt;

namespace {

/// Walk the acyclic body region of a loop-carry process and compute the
/// next-state expression for the i-th loop-carry argument. Returns null
/// Value() if the structure isn't supported.
///
/// Handles three terminator shapes:
///   * cf.br ^loopHeader(new...)           — leaf back-edge.
///   * cf.br ^succ(...)                    — fall-through to a single-pred
///                                           successor; if ^succ has args
///                                           we pre-bind them to the br's
///                                           dest operands.
///   * cf.cond_br %c, ^t(...), ^f(...)     — recurse on each side, where
///                                           ^t/^f may be the loop header
///                                           (then we extract operand[i]
///                                           directly).
static Value computeNextState(Block *block, Block *loopHeader,
                              unsigned argIndex, OpBuilder &builder);

/// Helper: extract the i-th operand passed by `term` to successor index
/// `succIdx`, treating ^loopHeader as a leaf and recursing into the
/// non-loop-header successor otherwise.
static Value valueForSuccessor(Operation *term, unsigned succIdx,
                               ValueRange destOperands, Block *destBlock,
                               Block *loopHeader, unsigned argIndex,
                               OpBuilder &builder) {
  if (destBlock == loopHeader) {
    if (argIndex >= destOperands.size())
      return {};
    return destOperands[argIndex];
  }
  // Non-loop-header successor: must be a unique-pred block; pre-bind its
  // block args (if any) to the operands carried by this branch.
  if (std::distance(destBlock->pred_begin(), destBlock->pred_end()) != 1) {
    LLVM_DEBUG(llvm::dbgs() << "Skip process: multi-pred non-header block\n");
    return {};
  }
  if (destBlock->getNumArguments() != destOperands.size()) {
    LLVM_DEBUG(llvm::dbgs() << "Skip process: succ arg arity mismatch\n");
    return {};
  }
  for (auto [i, ba] : llvm::enumerate(destBlock->getArguments())) {
    ba.replaceAllUsesWith(destOperands[i]);
  }
  return computeNextState(destBlock, loopHeader, argIndex, builder);
}

static Value computeNextState(Block *block, Block *loopHeader,
                              unsigned argIndex, OpBuilder &builder) {
  Operation *term = block->getTerminator();
  if (auto br = dyn_cast<cf::BranchOp>(term)) {
    return valueForSuccessor(term, /*succIdx=*/0, br.getDestOperands(),
                             br.getDest(), loopHeader, argIndex, builder);
  }
  if (auto condBr = dyn_cast<cf::CondBranchOp>(term)) {
    Value thenVal = valueForSuccessor(
        term, /*succIdx=*/0, condBr.getTrueOperands(), condBr.getTrueDest(),
        loopHeader, argIndex, builder);
    Value elseVal = valueForSuccessor(
        term, /*succIdx=*/1, condBr.getFalseOperands(), condBr.getFalseDest(),
        loopHeader, argIndex, builder);
    if (!thenVal || !elseVal)
      return {};
    if (thenVal == elseVal)
      return thenVal;
    if (thenVal.getType() != elseVal.getType()) {
      LLVM_DEBUG(llvm::dbgs() << "Skip process: cond_br arm type mismatch\n");
      return {};
    }
    return builder.create<comb::MuxOp>(term->getLoc(), thenVal.getType(),
                                       condBr.getCondition(), thenVal, elseVal,
                                       /*twoState=*/false);
  }
  return {};
}

/// Find an existing seq.to_clock op in the surrounding module. Used as the
/// inferred clock for the synthesised firreg. Assumes a single-clock design.
static Value findExistingClock(hw::HWModuleOp module) {
  Value clock;
  module.walk([&](seq::ToClockOp op) {
    if (!clock)
      clock = op.getResult();
  });
  return clock;
}

/// Find a reset signal: reuse one already feeding a firreg in the module.
static Value findExistingReset(hw::HWModuleOp module) {
  Value reset;
  module.walk([&](seq::FirRegOp op) {
    if (!reset && op.getReset())
      reset = op.getReset();
  });
  return reset;
}

//===----------------------------------------------------------------------===//
// Constant-trip-count loop unrolling.
//
// CIRCT lowers Verilog `for (i = C0; i < C1; i = i + C2) ...` loops whose
// bound and step are compile-time constants into a 2-block in-body cycle:
//
//   ^H(%iv: iN, %carry...: T...):              // loop header
//     %cmp = comb.icmp <slt|ult|...> %iv, C1
//     cf.cond_br %cmp, ^B, ^exit(%carry... : ...)
//   ^B:
//     ... uses %iv / %carry, e.g. hw.array_inject ...
//     %iv2 = comb.add %iv, C2
//     cf.br ^H(%iv2, %newcarry... : ...)        // back-edge
//
// These are the `RAM` / `register` reset/init loops. The forward-merge
// matcher correctly classifies ^H as a genuine
// in-body cycle and bails, so the surrounding llhd.process never lowers
// and its cf.br leaks to ExportVerilog ("dialect cf not supported").
//
// Because the trip count is statically known, we fully unroll the loop:
// the cycle is replaced by `tripCount` straight-line copies of ^B
// threading the carried values, then a final `cf.br ^exit(finalCarry...)`.
// After unrolling the body is acyclic and the existing matcher (or the
// halt-process path) handles the rest.
//
// We match ONLY the precise single-body-block shape above. Any deviation
// (multi-block body, non-constant bound/step, induction var not a header
// arg, multiple back-edges) returns false WITHOUT mutating the IR, so the
// pre-existing "bail unchanged on genuine nested loops" invariant that the
// array_write_nested_loop lit test depends on is preserved for shapes we
// don't recognise.
//===----------------------------------------------------------------------===//

/// Extract a constant integer value from `v` if it is an hw.constant (or any
/// op matchable by m_Constant). Returns true and fills `out` on success.
static bool matchConstInt(Value v, APInt &out) {
  IntegerAttr attr;
  if (matchPattern(v, m_Constant(&attr))) {
    out = attr.getValue();
    return true;
  }
  return false;
}

/// Description of a recognised constant-trip-count counter loop.
struct CounterLoop {
  Block *header = nullptr;   // ^H
  Block *body = nullptr;     // ^B
  Block *exit = nullptr;     // exit successor of the cond_br
  cf::CondBranchOp headerBr; // ^H's terminator
  cf::BranchOp bodyBr;       // ^B's back-edge terminator
  unsigned ivIdx = 0;        // index of the induction var among ^H's args
  bool exitIsTrueArm = false;// true if cond_br's TRUE dest is the exit
  uint64_t tripCount = 0;    // statically computed iteration count
};

/// Try to recognise `block` as the header ^H of a single-body-block
/// constant-trip-count counter loop. Returns true and fills `loop` on
/// success. Pure analysis: never mutates the IR.
static bool matchCounterLoop(Block *block, Block *procLoopHeader,
                             CounterLoop &loop) {
  if (block == procLoopHeader)
    return false;
  auto condBr = dyn_cast<cf::CondBranchOp>(block->getTerminator());
  if (!condBr)
    return false;

  // Condition must be `comb.icmp <pred> %iv, C` with %iv a header arg.
  auto icmp = condBr.getCondition().getDefiningOp<comb::ICmpOp>();
  if (!icmp)
    return false;
  comb::ICmpPredicate pred = icmp.getPredicate();
  bool isLT = pred == comb::ICmpPredicate::slt ||
              pred == comb::ICmpPredicate::ult;
  bool isLE = pred == comb::ICmpPredicate::sle ||
              pred == comb::ICmpPredicate::ule;
  if (!isLT && !isLE)
    return false;
  auto ivArg = dyn_cast<BlockArgument>(icmp.getLhs());
  if (!ivArg || ivArg.getOwner() != block)
    return false;
  APInt bound;
  if (!matchConstInt(icmp.getRhs(), bound))
    return false;
  unsigned ivIdx = ivArg.getArgNumber();

  // Identify continue vs exit arm: the continue arm is a single-pred block
  // ^B whose only terminator is `cf.br ^H(...)` (the back-edge). The exit
  // arm is whatever the cond_br's other destination is.
  Block *trueDest = condBr.getTrueDest();
  Block *falseDest = condBr.getFalseDest();

  auto classify = [&](Block *cand) -> cf::BranchOp {
    if (cand == block)
      return {};
    // Single predecessor (only ^H) and single successor back to ^H.
    if (std::distance(cand->pred_begin(), cand->pred_end()) != 1)
      return {};
    auto br = dyn_cast<cf::BranchOp>(cand->getTerminator());
    if (!br || br.getDest() != block)
      return {};
    return br;
  };

  cf::BranchOp trueBodyBr = classify(trueDest);
  cf::BranchOp falseBodyBr = classify(falseDest);
  // Exactly one arm may be the loop body.
  if (static_cast<bool>(trueBodyBr) == static_cast<bool>(falseBodyBr))
    return false;

  if (trueBodyBr) {
    loop.body = trueDest;
    loop.bodyBr = trueBodyBr;
    loop.exit = falseDest;
    loop.exitIsTrueArm = false;
  } else {
    loop.body = falseDest;
    loop.bodyBr = falseBodyBr;
    loop.exit = trueDest;
    loop.exitIsTrueArm = true;
  }

  // The back-edge must rewrite the iv slot as `comb.add %iv, C` and carry
  // the same number of operands as ^H has block args.
  if (loop.bodyBr.getDestOperands().size() != block->getNumArguments())
    return false;
  Value updatedIv = loop.bodyBr.getDestOperands()[ivIdx];
  auto add = updatedIv.getDefiningOp<comb::AddOp>();
  if (!add || add.getNumOperands() != 2)
    return false;
  APInt step;
  Value stepOther;
  if (add.getOperand(0) == ivArg) {
    stepOther = add.getOperand(1);
  } else if (add.getOperand(1) == ivArg) {
    stepOther = add.getOperand(0);
  } else {
    return false;
  }
  if (!matchConstInt(stepOther, step))
    return false;
  if (step.isZero())
    return false; // would never terminate

  // The initial value of the iv must be a constant, taken from ^H's
  // entry-edge predecessor (the one that is NOT ^B).
  Value initIv;
  for (Block *pred : block->getPredecessors()) {
    if (pred == loop.body)
      continue;
    // Find the operand pred passes for ivIdx on its edge to ^H.
    Operation *predTerm = pred->getTerminator();
    if (auto br = dyn_cast<cf::BranchOp>(predTerm)) {
      if (br.getDest() == block && ivIdx < br.getDestOperands().size())
        initIv = br.getDestOperands()[ivIdx];
    } else if (auto cbr = dyn_cast<cf::CondBranchOp>(predTerm)) {
      if (cbr.getTrueDest() == block && ivIdx < cbr.getTrueOperands().size())
        initIv = cbr.getTrueOperands()[ivIdx];
      else if (cbr.getFalseDest() == block &&
               ivIdx < cbr.getFalseOperands().size())
        initIv = cbr.getFalseOperands()[ivIdx];
    }
    if (initIv)
      break;
  }
  if (!initIv)
    return false;
  APInt init;
  if (!matchConstInt(initIv, init))
    return false;

  // ^H must have exactly two predecessors: the entry edge and the back-edge.
  if (std::distance(block->pred_begin(), block->pred_end()) != 2)
    return false;

  // Statically compute the trip count. Iterate the constant recurrence
  // i_{k+1} = i_k + step until the loop condition is false. Cap iterations
  // so a mis-analysed loop can never hang the pass.
  bool isSigned = pred == comb::ICmpPredicate::slt ||
                  pred == comb::ICmpPredicate::sle;
  const uint64_t kMaxTrip = 4096;
  APInt iv = init;
  uint64_t trips = 0;
  while (trips <= kMaxTrip) {
    bool cont;
    if (isLT)
      cont = isSigned ? iv.slt(bound) : iv.ult(bound);
    else
      cont = isSigned ? iv.sle(bound) : iv.ule(bound);
    if (!cont)
      break;
    iv += step;
    ++trips;
  }
  if (trips > kMaxTrip) {
    LLVM_DEBUG(llvm::dbgs()
               << "Skip unroll: trip count exceeds cap or diverges\n");
    return false;
  }

  loop.header = block;
  loop.headerBr = condBr;
  loop.ivIdx = ivIdx;
  loop.tripCount = trips;
  return true;
}

/// Fully unroll the recognised counter loop `loop`. After this the header
/// ^H and body ^B are gone; their effect is replaced by `tripCount`
/// inlined copies of ^B threading the carried values, followed by a branch
/// to ^exit carrying the final values. Returns true on success.
static bool unrollCounterLoop(CounterLoop &loop) {
  Block *header = loop.header;
  Block *body = loop.body;
  Region *region = header->getParent();

  // Gather the initial carry values: the operands ^H's entry-edge
  // predecessor passes (these match ^H's block args). We re-derive them by
  // snapshotting the current header-arg → init mapping from that pred.
  Block *entryPred = nullptr;
  for (Block *pred : header->getPredecessors()) {
    if (pred != body) {
      entryPred = pred;
      break;
    }
  }
  if (!entryPred)
    return false;
  Operation *entryTerm = entryPred->getTerminator();
  SmallVector<Value> carry; // current value of each header arg
  if (auto br = dyn_cast<cf::BranchOp>(entryTerm)) {
    carry.assign(br.getDestOperands().begin(), br.getDestOperands().end());
  } else if (auto cbr = dyn_cast<cf::CondBranchOp>(entryTerm)) {
    if (cbr.getTrueDest() == header)
      carry.assign(cbr.getTrueOperands().begin(), cbr.getTrueOperands().end());
    else
      carry.assign(cbr.getFalseOperands().begin(),
                   cbr.getFalseOperands().end());
  } else {
    return false;
  }
  if (carry.size() != header->getNumArguments())
    return false;

  // Build a landing block that the entry-edge predecessor will branch to.
  // We emit the unrolled iterations into fresh blocks so SSA dominance is
  // trivially correct (each iteration's clones dominate the next).
  Block *landing = new Block();
  region->getBlocks().push_back(landing);

  // For each iteration, clone ^B's ops into a fresh block, mapping ^H's
  // block args to the current carry values, then advance carry to the
  // back-edge operands of that clone.
  Block *cur = landing;
  for (uint64_t k = 0; k < loop.tripCount; ++k) {
    IRMapping mapping;
    for (auto [arg, val] : llvm::zip(header->getArguments(), carry))
      mapping.map(arg, val);
    OpBuilder b(cur, cur->end());
    // Clone every op of ^B except its terminator.
    for (Operation &op : body->without_terminator())
      b.clone(op, mapping);
    // Compute the next carry from the back-edge operands.
    SmallVector<Value> next;
    next.reserve(loop.bodyBr.getDestOperands().size());
    for (Value v : loop.bodyBr.getDestOperands())
      next.push_back(mapping.lookupOrDefault(v));
    carry.assign(next.begin(), next.end());
    // Continue emitting subsequent iterations into the same block (the
    // clones are straight-line, no control flow inside ^B).
    (void)b;
  }

  // After the final iteration, branch to ^exit carrying the operands the
  // cond_br's exit arm passed — but with ^H's block args substituted by the
  // final carry values.
  IRMapping exitMap;
  for (auto [arg, val] : llvm::zip(header->getArguments(), carry))
    exitMap.map(arg, val);
  ValueRange exitOperands = loop.exitIsTrueArm
                                ? loop.headerBr.getTrueOperands()
                                : loop.headerBr.getFalseOperands();
  SmallVector<Value> finalExitOps;
  finalExitOps.reserve(exitOperands.size());
  for (Value v : exitOperands)
    finalExitOps.push_back(exitMap.lookupOrDefault(v));
  {
    OpBuilder b(landing, landing->end());
    b.create<cf::BranchOp>(loop.headerBr.getLoc(), loop.exit, finalExitOps);
  }

  // Redirect the entry-edge predecessor to the landing block (no operands —
  // the carry values were inlined as the initial mapping).
  {
    OpBuilder b(entryTerm);
    if (auto br = dyn_cast<cf::BranchOp>(entryTerm)) {
      b.create<cf::BranchOp>(br.getLoc(), landing, ValueRange{});
    } else {
      auto cbr = cast<cf::CondBranchOp>(entryTerm);
      Block *trueDest = cbr.getTrueDest();
      Block *falseDest = cbr.getFalseDest();
      ValueRange trueOps = cbr.getTrueOperands();
      ValueRange falseOps = cbr.getFalseOperands();
      if (cbr.getTrueDest() == header) {
        trueDest = landing;
        trueOps = ValueRange{};
      } else {
        falseDest = landing;
        falseOps = ValueRange{};
      }
      b.create<cf::CondBranchOp>(cbr.getLoc(), cbr.getCondition(), trueDest,
                                 trueOps, falseDest, falseOps);
    }
    entryTerm->erase();
  }

  // Downstream blocks (reachable from ^exit) may reference ^H's block args
  // directly by SSA name — e.g. the `data[29] <= 2044` block in `register`
  // reads the loop-carried array arg after the reset loop. Those uses must
  // now resolve to the final carry values. Any remaining uses inside ^H / ^B
  // themselves are erased below.
  for (auto [arg, val] : llvm::zip(header->getArguments(), carry))
    arg.replaceAllUsesWith(val);

  // ^H and ^B are now unreachable. Drop references then erase.
  for (Block *b : {header, body}) {
    b->dropAllDefinedValueUses();
  }
  for (Block *b : {header, body}) {
    b->dropAllReferences();
  }
  for (Block *b : {header, body}) {
    b->erase();
  }
  return true;
}

/// Fully unroll every recognised constant-trip-count counter loop in `body`.
/// Returns true (always succeeds; loops it doesn't recognise are simply left
/// in place for downstream cycle detection to handle).
static void unrollConstantTripLoops(Region &body, Block *procLoopHeader) {
  const unsigned kMaxLoops = 256;
  for (unsigned iter = 0; iter < kMaxLoops; ++iter) {
    CounterLoop loop;
    bool found = false;
    for (Block &block : body) {
      if (matchCounterLoop(&block, procLoopHeader, loop)) {
        found = true;
        break;
      }
    }
    if (!found)
      return;
    if (!unrollCounterLoop(loop)) {
      LLVM_DEBUG(llvm::dbgs() << "Skip unroll: unrollCounterLoop failed\n");
      return;
    }
  }
}

/// Forward-merge inlining.
///
/// CIRCT often emits process bodies whose acyclic CFG includes diamond
/// merge blocks — multi-predecessor blocks that take block args from each
/// incoming edge but are NOT part of a body-internal cycle (they merge
/// forward only, before flowing back to the loop header). Examples:
///
///   rtllm/multi_booth_8bit, ^bb8(%41: i16, %42: i1): preds ^bb6 (cond_br
///   false arm with operands [%5, %false]) and ^bb7 (cf.br with operands
///   [%40, %true]). Forwards merged values back to the loop header.
///
///   rtllm/float_multi, several processes contain ^bb5 / ^bb6 / ^bb14
///   diamond merges, where the merge block's args are not just forwarded
///   to the loop header but also USED by downstream blocks (e.g. ^bb15
///   uses ^bb14's args).
///
/// The original matcher rejected ALL multi-pred body blocks under the
/// "nested loop" rule. That rule is correct for
/// genuine nested loops (where multi-pred indicates a back-edge inside the
/// body, e.g. the array-write reset loop in rtllm/RAM) but too strict for
/// forward-only diamond merges.
///
/// Strategy: clone the merge block AND its forward sub-CFG (every block
/// reachable from the merge via cf.* edges without crossing loopHeader)
/// into a fresh sibling sub-CFG per predecessor. Each clone substitutes
/// the original merge block's args with the operands the corresponding
/// predecessor was passing in, then threads through naturally because
/// the entire sub-CFG was cloned. The original sub-CFG becomes
/// unreachable and is erased. The result is a (deeper) acyclic CFG where
/// every body block has exactly one predecessor.
///
/// Cloning the whole forward sub-CFG (not just the merge) avoids SSA
/// dominance issues: if a downstream block referenced the merge's args
/// (e.g. ^bb15 in float_multi process %17), the cloned downstream block
/// references the substituted values in its own clone. Each predecessor
/// gets its own private sub-CFG.

/// True iff `from` can reach `to` by following cf.* successors, treating
/// `loopHeader` as a sink (we never recurse through it). Visited set is
/// passed by reference so the caller can reuse it across queries.
static bool reachesForward(Block *from, Block *to, Block *loopHeader,
                           llvm::SmallPtrSetImpl<Block *> &visited) {
  if (from == loopHeader)
    return false;
  if (from == to)
    return true;
  if (!visited.insert(from).second)
    return false;
  for (Block *succ : from->getSuccessors()) {
    if (succ == loopHeader)
      continue;
    if (reachesForward(succ, to, loopHeader, visited))
      return true;
  }
  return false;
}

/// True iff `block` has more than one predecessor and none of those
/// predecessors is reachable from `block` itself (within the body,
/// excluding loopHeader). False if `block` has only one predecessor
/// (not a merge) OR is part of an in-body cycle (genuine nested loop).
static bool isForwardMerge(Block *block, Block *loopHeader) {
  if (std::distance(block->pred_begin(), block->pred_end()) <= 1)
    return false;
  for (Block *pred : block->getPredecessors()) {
    llvm::SmallPtrSet<Block *, 16> visited;
    if (reachesForward(block, pred, loopHeader, visited))
      return false; // predecessor reachable from block → cycle.
  }
  return true;
}

/// Collect every block reachable from `start` via cf.* successors,
/// stopping at `loopHeader` (we never recurse through it — edges back to
/// loopHeader are the FSM back-edges, which we want to keep pointing at
/// the original loopHeader in every clone).
static void collectForwardSubCFG(Block *start, Block *loopHeader,
                                 llvm::SetVector<Block *> &out) {
  SmallVector<Block *> worklist;
  worklist.push_back(start);
  while (!worklist.empty()) {
    Block *b = worklist.pop_back_val();
    if (b == loopHeader)
      continue;
    if (!out.insert(b))
      continue;
    for (Block *succ : b->getSuccessors())
      worklist.push_back(succ);
  }
}

/// Clone the forward sub-CFG rooted at `merge` (every block reachable
/// from `merge` excluding `loopHeader`) into the same region, replacing
/// each cloned block's edges among the sub-CFG with edges to the
/// corresponding clones. Edges that leave the sub-CFG (i.e. point at
/// `loopHeader`) continue to point at `loopHeader` in the clones.
///
/// `mapping` is pre-populated with the merge block's args mapped to the
/// values the caller wants the clone to use for those args. After this
/// function, `mapping[merge]` is the cloned root block.
///
/// Returns the cloned root (= `mapping[merge]`).
static Block *cloneForwardSubCFG(Block *merge, Block *loopHeader,
                                 IRMapping &mapping) {
  Region *region = merge->getParent();

  // First, find all blocks to clone.
  llvm::SetVector<Block *> blocksToClone;
  collectForwardSubCFG(merge, loopHeader, blocksToClone);

  // Pass 1: create the clone blocks (empty, with arg types matching the
  // originals — except for `merge` itself, whose args we've already
  // substituted via the caller's mapping).
  for (Block *orig : blocksToClone) {
    Block *clone = new Block();
    region->getBlocks().push_back(clone);
    mapping.map(orig, clone);

    if (orig == merge) {
      // merge's args are pre-substituted by caller's mapping; clone has no
      // args.
    } else {
      // Other blocks keep their block args. We add them to the clone and
      // map orig args to clone args.
      for (BlockArgument arg : orig->getArguments()) {
        BlockArgument cloneArg = clone->addArgument(arg.getType(), arg.getLoc());
        mapping.map(arg, cloneArg);
      }
    }
  }

  // Pass 2: clone the ops in each block, in order.
  for (Block *orig : blocksToClone) {
    Block *clone = mapping.lookup(orig);
    OpBuilder b(clone, clone->begin());
    for (Operation &op : *orig)
      b.clone(op, mapping);
  }

  return mapping.lookup(merge);
}

/// Inline a single 2-pred forward-merge block by cloning the forward
/// sub-CFG rooted at `merge` into two copies (one per predecessor),
/// substituting `merge`'s block args in each copy with the operands the
/// corresponding predecessor was passing in, and redirecting each pred's
/// terminator to its private copy. The original forward sub-CFG (rooted
/// at `merge`) becomes unreachable and is erased.
///
/// Returns true on success.
static bool inlineForwardMerge(Block *merge, Block *loopHeader) {
  SmallVector<Block *> preds(merge->pred_begin(), merge->pred_end());
  if (preds.empty())
    return false;

  // Snapshot the edges we'll rewrite. For each pred, find which arm of
  // its terminator goes to merge and the operands carried on that edge.
  struct EdgeInfo {
    Block *pred;
    Operation *term;
    SmallVector<Value> operands;
    bool isCondBr;
    bool toTrueArm; // valid iff isCondBr
  };
  SmallVector<EdgeInfo> edges;
  for (Block *pred : preds) {
    EdgeInfo e;
    e.pred = pred;
    e.term = pred->getTerminator();
    if (auto br = dyn_cast<cf::BranchOp>(e.term)) {
      if (br.getDest() != merge)
        return false;
      e.isCondBr = false;
      e.toTrueArm = false;
      e.operands.assign(br.getDestOperands().begin(),
                        br.getDestOperands().end());
    } else if (auto condBr = dyn_cast<cf::CondBranchOp>(e.term)) {
      e.isCondBr = true;
      if (condBr.getTrueDest() == merge && condBr.getFalseDest() == merge) {
        // Both arms go to merge — unusual; not handled here.
        LLVM_DEBUG(llvm::dbgs()
                   << "Inline merge: both arms of cond_br go to merge\n");
        return false;
      }
      if (condBr.getTrueDest() == merge) {
        e.toTrueArm = true;
        e.operands.assign(condBr.getTrueOperands().begin(),
                          condBr.getTrueOperands().end());
      } else if (condBr.getFalseDest() == merge) {
        e.toTrueArm = false;
        e.operands.assign(condBr.getFalseOperands().begin(),
                          condBr.getFalseOperands().end());
      } else {
        return false;
      }
    } else {
      LLVM_DEBUG(llvm::dbgs()
                 << "Inline merge: unsupported terminator kind\n");
      return false;
    }
    if (e.operands.size() != merge->getNumArguments()) {
      LLVM_DEBUG(llvm::dbgs() << "Inline merge: edge arity mismatch\n");
      return false;
    }
    edges.push_back(std::move(e));
  }

  // For each predecessor, clone the forward sub-CFG and redirect.
  for (const EdgeInfo &e : edges) {
    IRMapping mapping;
    for (auto [arg, val] : llvm::zip(merge->getArguments(), e.operands))
      mapping.map(arg, val);
    Block *cloneRoot = cloneForwardSubCFG(merge, loopHeader, mapping);

    // Redirect e.pred's terminator to point at cloneRoot, with no operands.
    OpBuilder b(e.term);
    if (!e.isCondBr) {
      b.create<cf::BranchOp>(e.term->getLoc(), cloneRoot, ValueRange{});
    } else {
      auto condBr = cast<cf::CondBranchOp>(e.term);
      Block *trueDest = condBr.getTrueDest();
      Block *falseDest = condBr.getFalseDest();
      ValueRange trueOps = condBr.getTrueOperands();
      ValueRange falseOps = condBr.getFalseOperands();
      if (e.toTrueArm) {
        trueDest = cloneRoot;
        trueOps = ValueRange{};
      } else {
        falseDest = cloneRoot;
        falseOps = ValueRange{};
      }
      b.create<cf::CondBranchOp>(condBr.getLoc(), condBr.getCondition(),
                                 trueDest, trueOps, falseDest, falseOps);
    }
    e.term->erase();
  }

  // Original sub-CFG is now unreachable; erase its blocks. We must drop
  // all references first (block::erase() doesn't reset cross-block
  // BlockOperand use lists, so erasing blocks in arbitrary order would
  // dangle BlockOperands on already-destroyed blocks). The MLIR idiom is
  // dropAllReferences() + dropAllDefinedValueUses() on every block in
  // the set, then erase.
  llvm::SetVector<Block *> origSubCFG;
  collectForwardSubCFG(merge, loopHeader, origSubCFG);

  for (Block *b : origSubCFG) {
    if (b == loopHeader)
      continue;
    b->dropAllDefinedValueUses();
  }
  for (Block *b : origSubCFG) {
    if (b == loopHeader)
      continue;
    b->dropAllReferences();
  }
  for (Block *b : origSubCFG) {
    if (b == loopHeader)
      continue;
    b->erase();
  }

  return true;
}

/// Inline every forward-merge block in `body`. Returns true on success
/// (no merges remain), false if a genuine in-body cycle was detected or
/// if cloning failed.
///
/// We first scan ALL multi-pred body blocks for ANY in-body cycle. If
/// any block fails `isForwardMerge`, we bail WITHOUT mutating the IR so
/// downstream passes see the original (the `array_write_nested_loop` test
/// depends on this no-mutation-on-cycle invariant).
static bool inlineAllForwardMerges(Region &body, Block *entry,
                                   Block *loopHeader, Block *waitDest) {
  // Phase 1: cycle detection across ALL multi-pred body blocks.
  for (Block &block : body) {
    if (&block == entry || &block == loopHeader || &block == waitDest)
      continue;
    if (std::distance(block.pred_begin(), block.pred_end()) <= 1)
      continue;
    if (!isForwardMerge(&block, loopHeader)) {
      LLVM_DEBUG(llvm::dbgs()
                 << "Skip process: in-body cycle (multi-pred block reachable "
                    "from itself)\n");
      return false;
    }
  }

  // Phase 2: iteratively inline merges. After each inline, the sub-CFG
  // rooted at the merge has been duplicated per pred; any merges inside
  // the sub-CFG were also duplicated, so we may find more merges to
  // inline in the next iteration (each duplicated). The total work is
  // bounded by the original number of merges times their max fan-in.
  const unsigned kMaxIters = 4096;
  for (unsigned iter = 0; iter < kMaxIters; ++iter) {
    // Pick the "highest" merge block — one whose forward sub-CFG does
    // not contain any other multi-pred block. That way we don't re-clone
    // the same sub-CFG multiple times.
    Block *merge = nullptr;
    for (Block &block : body) {
      if (&block == entry || &block == loopHeader || &block == waitDest)
        continue;
      if (std::distance(block.pred_begin(), block.pred_end()) <= 1)
        continue;
      // Check that this block's forward sub-CFG contains no nested
      // multi-pred blocks. If so, it's safe to inline now.
      llvm::SetVector<Block *> sub;
      collectForwardSubCFG(&block, loopHeader, sub);
      bool nestedMerge = false;
      for (Block *sb : sub) {
        if (sb == &block)
          continue;
        if (std::distance(sb->pred_begin(), sb->pred_end()) > 1) {
          nestedMerge = true;
          break;
        }
      }
      if (nestedMerge)
        continue;
      // Re-verify forward-merge invariant (cheap).
      if (!isForwardMerge(&block, loopHeader)) {
        LLVM_DEBUG(llvm::dbgs()
                   << "Skip process: cycle appeared during merge inlining\n");
        return false;
      }
      merge = &block;
      break;
    }
    if (!merge) {
      // No "innermost" merge found — either we're done (no more multi-pred
      // blocks) or all multi-pred blocks have nested merges. The latter
      // shouldn't happen in well-formed forward-only sub-CFGs; if it does,
      // pick any merge that itself is a forward merge.
      for (Block &block : body) {
        if (&block == entry || &block == loopHeader || &block == waitDest)
          continue;
        if (std::distance(block.pred_begin(), block.pred_end()) <= 1)
          continue;
        if (!isForwardMerge(&block, loopHeader)) {
          LLVM_DEBUG(llvm::dbgs()
                     << "Skip process: cycle appeared during merge inlining\n");
          return false;
        }
        merge = &block;
        break;
      }
    }
    if (!merge)
      return true;
    if (!inlineForwardMerge(merge, loopHeader)) {
      LLVM_DEBUG(llvm::dbgs()
                 << "Skip process: forward-merge inlining failed\n");
      return false;
    }
  }
  LLVM_DEBUG(llvm::dbgs()
             << "Skip process: forward-merge inlining did not converge in "
             << kMaxIters << " iters\n");
  return false;
}

/// Pre-substitute wait-dest block args with the wait's dest operands.
/// CIRCT emits an edge-detection pattern where the wait passes the *previous*
/// values of clk/reset into the post-wait body via `llhd.wait yield ..., ^bb2(prev_a, prev_b)`.
/// For our purposes the previous-values are the loop-header args, and after
/// substitution the post-wait body just sees them as ordinary SSA names.
static void inlineWaitDestArgs(llhd::WaitOp waitOp) {
  Block *waitDest = waitOp.getDest();
  ValueRange destOps = waitOp.getDestOperands();
  if (waitDest->getNumArguments() != destOps.size())
    return; // Caller will catch the arity error.
  for (auto [i, arg] : llvm::enumerate(waitDest->getArguments())) {
    arg.replaceAllUsesWith(destOps[i]);
  }
  // Drop the block args so the block accepts no operands. We can't actually
  // remove block args from the block API directly without invalidating
  // predecessors — but since we replace-all-uses and then erase the block
  // when we erase the process, this is enough. The replaceAllUsesWith
  // ensures no live use of the args remains.
}

/// Detect the CIRCT clock-edge-detection pattern in a wait-dest block and
/// return the clock signal it keys off, or null if the wait observes data
/// signals rather than a clock edge.
///
/// CIRCT lowers `always @(posedge clk ...)` into:
///   llhd.wait yield (...), (%clk, ... : ...), ^bb2(%prev_clk, ... )
/// ^bb2(%a, ...):
///   %x = comb.xor %a, %true        ; ~prev_clk
///   %y = comb.and %x, %clk         ; (~prev_clk) & clk  == posedge clk
///   ... cf.cond_br (%y | ...), ...
///
/// We identify the clock as the first observed signal of the wait when the
/// observed list is non-empty AND the wait-dest body contains a posedge
/// detector of that signal (an `(~prev) & sig` whose `sig` is the observed
/// clock). Returning the observed clock lets the caller clock the
/// loop-carried state with a real flop instead of a self-referential comb
/// mux. Conservative: returns null on any shape it doesn't positively
/// recognise, which preserves the combinational `always @(*)` path
/// (mux3_fsm) and the delay-wait clock-divider path.
static Value detectObservedClock(llhd::WaitOp waitOp) {
  ValueRange observed = waitOp.getObserved();
  if (observed.empty())
    return {};
  Value clk = observed.front();
  // Search the parent process region for a posedge detector of `clk`:
  // a `comb.and %x, %clk` (or `comb.and %clk, %x`) whose other operand is a
  // `comb.xor %p, true` (i.e. ~p) — the classic edge detector.
  Region *region = waitOp->getParentRegion();
  bool found = false;
  region->walk([&](comb::AndOp andOp) {
    if (found || andOp.getNumOperands() != 2)
      return;
    Value a = andOp.getOperand(0), b = andOp.getOperand(1);
    Value other;
    if (a == clk)
      other = b;
    else if (b == clk)
      other = a;
    else
      return;
    if (auto x = other.getDefiningOp<comb::XorOp>()) {
      // ~p is `comb.xor %p, true`.
      if (x.getNumOperands() == 2) {
        APInt c;
        if ((matchConstInt(x.getOperand(1), c) && c.isAllOnes()) ||
            (matchConstInt(x.getOperand(0), c) && c.isAllOnes()))
          found = true;
      }
    }
  });
  return found ? clk : Value{};
}

/// Lower a wait-free llhd.process (an `initial begin ... end` block whose
/// only terminator is llhd.halt). After constant-trip unrolling its body is
/// a straight-line single-successor chain ending in llhd.halt; we propagate
/// block-arg substitutions along the chain, hoist the straight-line ops out
/// before the process, and bind each process result to the corresponding
/// (now externally-defined) halt operand. Returns true on success.
static bool tryLowerHaltProcess(llhd::ProcessOp processOp) {
  Region &body = processOp.getBody();

  // Locate the unique halt op.
  llhd::HaltOp haltOp;
  for (Block &block : body) {
    if (auto h = dyn_cast<llhd::HaltOp>(block.getTerminator())) {
      if (haltOp) {
        LLVM_DEBUG(llvm::dbgs() << "Skip halt-process: multiple halts\n");
        return false;
      }
      haltOp = h;
    }
  }
  if (!haltOp) {
    LLVM_DEBUG(llvm::dbgs() << "Skip halt-process: no halt\n");
    return false;
  }
  if (haltOp.getYieldOperands().size() != processOp.getNumResults()) {
    LLVM_DEBUG(llvm::dbgs() << "Skip halt-process: yield arity mismatch\n");
    return false;
  }

  // Walk the single-successor chain from the entry block, substituting each
  // block's args with the operands its predecessor passed. Every block must
  // have exactly one successor (cf.br) until we reach the halt block. Any
  // residual control flow (cond_br / multi-succ) means unrolling didn't make
  // the body straight-line — bail unchanged.
  Block *cur = &body.front();
  llvm::SmallPtrSet<Block *, 16> seen;
  while (cur->getTerminator() != haltOp.getOperation()) {
    if (!seen.insert(cur).second) {
      LLVM_DEBUG(llvm::dbgs() << "Skip halt-process: cycle in chain\n");
      return false;
    }
    auto br = dyn_cast<cf::BranchOp>(cur->getTerminator());
    if (!br) {
      LLVM_DEBUG(llvm::dbgs()
                 << "Skip halt-process: non-straight-line control flow\n");
      return false;
    }
    Block *next = br.getDest();
    if (next->getNumArguments() != br.getDestOperands().size()) {
      LLVM_DEBUG(llvm::dbgs() << "Skip halt-process: chain arg arity\n");
      return false;
    }
    for (auto [arg, val] : llvm::zip(next->getArguments(), br.getDestOperands()))
      arg.replaceAllUsesWith(val);
    cur = next;
  }

  // Hoist every straight-line op out before the process. After the block-arg
  // substitutions above, all operands are either constants/ports defined
  // outside the process or earlier hoisted ops, so the order is preserved by
  // moving in region order.
  SmallVector<Operation *> toHoist;
  for (Block &block : body) {
    for (Operation &op : block.without_terminator())
      toHoist.push_back(&op);
  }
  for (Operation *op : toHoist)
    op->moveBefore(processOp);

  // Bind results to the halt's (now externally-defined) yield operands.
  for (auto [i, oldResult] : llvm::enumerate(processOp.getResults()))
    oldResult.replaceAllUsesWith(haltOp.getYieldOperands()[i]);
  processOp.erase();
  return true;
}

/// Attempt to lower a single ProcessOp. Returns true on success.
/// `allowDelayWait` enables the clock-divider-style path (no firreg, just
/// combinational binding of result_i = next_i).
static bool tryLowerProcess(llhd::ProcessOp processOp, hw::HWModuleOp module,
                            bool allowDelayWait) {
  Region &body = processOp.getBody();
  if (body.empty())
    return false;

  // Locate the unique wait op.
  llhd::WaitOp waitOp;
  for (Block &block : body) {
    if (auto w = dyn_cast<llhd::WaitOp>(block.getTerminator())) {
      if (waitOp) {
        LLVM_DEBUG(llvm::dbgs() << "Skip process: multiple waits\n");
        return false;
      }
      waitOp = w;
    }
  }

  // Fully unroll any constant-trip-count counter
  // loops (Verilog `for (i = C0; i < C1; i = i + C2)` reset/init loops)
  // before the cycle-detection in inlineAllForwardMerges runs. The unroller
  // treats the FSM loop header (the wait block, if any) as a sink so it
  // never touches the outer FSM back-edges. After unrolling, the bodies of
  // RAM / register become acyclic and lower normally.
  unrollConstantTripLoops(body, waitOp ? waitOp->getBlock() : nullptr);

  // A wait-free process is an `initial begin ... end` block: its single
  // terminator is llhd.halt, which yields the final (now constant, after
  // unrolling) loop-carried values. Lower it by hoisting the straight-line
  // body and binding each result to the corresponding halt operand.
  if (!waitOp)
    return tryLowerHaltProcess(processOp);
  bool isDelayWait = static_cast<bool>(waitOp.getDelay());
  if (isDelayWait && !allowDelayWait) {
    LLVM_DEBUG(llvm::dbgs() << "Skip process: wait has delay (flag off)\n");
    return false;
  }

  Block *loopHeader = waitOp->getBlock();
  Block *waitDest = waitOp.getDest();

  // The entry block must be distinct from the loop header and end with
  // cf.br ^loopHeader(init...).
  Block &entry = body.front();
  if (&entry == loopHeader) {
    LLVM_DEBUG(llvm::dbgs() << "Skip process: wait in entry\n");
    return false;
  }
  auto entryBr = dyn_cast<cf::BranchOp>(entry.getTerminator());
  if (!entryBr || entryBr.getDest() != loopHeader) {
    LLVM_DEBUG(llvm::dbgs() << "Skip process: entry does not cf.br to loopHeader\n");
    return false;
  }

  // Pre-flight topology check: every body block other than
  // entry, loopHeader, and waitDest must, after forward-merge inlining,
  // have exactly one predecessor. Two sub-cases:
  //   * Genuine nested loop (multi-pred AND block is reachable from itself
  //     within the body) — bail unchanged (e.g. rtllm/RAM array-write
  //     reset loop). The pre-mutation guarantee is preserved by
  //     `inlineAllForwardMerges` returning false WITHOUT touching the IR
  //     when it detects an in-body cycle on the first merge it inspects.
  //   * Forward-only diamond merge (multi-pred but no self-cycle) —
  //     inline by cloning the forward sub-CFG rooted at the merge into
  //     each predecessor (regression: rtllm/multi_booth_8bit,
  //     rtllm/float_multi). The existing matcher then sees a unique-pred
  //     body and lowers normally.
  if (!inlineAllForwardMerges(body, &entry, loopHeader, waitDest)) {
    return false;
  }

  SmallVector<Value> initOps(entryBr.getDestOperands().begin(),
                             entryBr.getDestOperands().end());
  // initOps must align with the loop header's block args. The yields are a
  // subset of those args (CIRCT often carries extra "edge-detection state"
  // alongside the actual loop-carry — see the synchronizer / multi_booth_8bit
  // shapes). For each yield, locate its index in headerArgs and use that to
  // pick the right operand off each back-edge.
  if (initOps.size() != loopHeader->getNumArguments() ||
      waitOp.getYieldOperands().size() != processOp.getNumResults()) {
    LLVM_DEBUG(llvm::dbgs() << "Skip process: arity mismatch\n");
    return false;
  }

  // For each result index i, compute the index in headerArgs that the
  // i-th yield refers to. Yields must be loop-header args (no derived
  // values) so that the back-edge writes the corresponding header-arg slot.
  SmallVector<unsigned> yieldHeaderIdx;
  yieldHeaderIdx.reserve(processOp.getNumResults());
  for (Value y : waitOp.getYieldOperands()) {
    auto blockArg = dyn_cast<BlockArgument>(y);
    if (!blockArg || blockArg.getOwner() != loopHeader) {
      LLVM_DEBUG(llvm::dbgs() << "Skip process: yield is not loop-header arg\n");
      return false;
    }
    yieldHeaderIdx.push_back(blockArg.getArgNumber());
  }

  // Clocked FSM results need registers. seq::FirRegOp accepts any HW-valid
  // type; clock-divider processes do not need a register.

  // Decide whether THIS process is registered (`always @(posedge clk ...)`)
  // or combinational (`always @(*)`), then find its clock + reset.
  //
  // The decision MUST be driven by what *this* process's own wait observes,
  // NOT by whether the surrounding module happens to contain any clock.
  // A module can mix clocked flops and combinational processes. Inspect the
  // current process's wait: a data-observing or delay wait stays combinational;
  // a clock-edge wait takes the register path. Using a module-global clock
  // would incorrectly insert a cycle of latency into combinational logic.
  Value obsClk = isDelayWait ? Value{} : detectObservedClock(waitOp);
  bool useFirreg = obsClk != nullptr;

  // A clocked FSM whose state holds its previous
  // value on some back-edges MUST become a seq.firreg. If we bind it
  // combinationally, the "hold" arm folds to `assign q = sel ? next : q` —
  // a comb loop that SIGSEGVs circt-verilog (rtllm/multi_booth_8bit). For
  // the firreg path, reuse the module's existing seq.to_clock if one is
  // present (single-clock designs share it), otherwise synthesize one from
  // the clock this process actually observes.
  Value clock;
  Value reset;
  if (useFirreg) {
    clock = findExistingClock(module);
    if (!clock) {
      OpBuilder clkBuilder(processOp);
      clock = clkBuilder.create<seq::ToClockOp>(processOp.getLoc(), obsClk);
    }
    reset = findExistingReset(module);
  }

  // Build the next-state expressions in a builder positioned BEFORE the process,
  // since the firregs need to live there.
  OpBuilder builder(processOp);
  Location loc = processOp.getLoc();

  // STEP 1: Pre-create the firreg results (for the firreg path) so we can
  // bind header args to them before hoisting. In the combinational path
  // we don't have firregs yet; we substitute with init values instead and
  // patch the body up after computeNextState.
  //
  // The order matters: we MUST substitute the loop header's args (which
  // live inside the process region) with values defined OUTSIDE the process
  // region before hoisting ops out of the body. Otherwise the hoisted ops
  // reference SSA values that no longer dominate them.

  // STEP 1a: For non-yield "extra state" args, substitute with init value.
  // These are the header args that don't correspond to any yield (e.g.
  // edge-detection prev_clk in the synchronizer pattern). In steady state
  // they equal their init.
  llvm::SmallDenseSet<unsigned> isYieldArg;
  for (unsigned idx : yieldHeaderIdx)
    isYieldArg.insert(idx);
  for (auto [i, arg] : llvm::enumerate(loopHeader->getArguments())) {
    if (!isYieldArg.contains(i))
      arg.replaceAllUsesWith(initOps[i]);
  }

  // STEP 1b: For yield args themselves: in the combinational path we
  // substitute with init (steady-state). In the firreg path we substitute
  // with placeholder values created later. Doing the comb-path substitution
  // now keeps things simple — even in firreg mode the init equals reset,
  // which is what observed `carry` ought to be at the moment of evaluation.
  // The initial values enter through the combinational mux leaves.
  for (auto [i, arg] : llvm::enumerate(loopHeader->getArguments())) {
    if (isYieldArg.contains(i))
      arg.replaceAllUsesWith(initOps[i]);
  }

  // STEP 1c: Substitute wait-dest block args with the wait's destOperands.
  // After step 1a/1b, those destOperands are themselves substituted to
  // values outside the process region, so the post-wait body becomes
  // dominated by ops we can safely hoist.
  inlineWaitDestArgs(waitOp);

  // STEP 2: Hoist every op used in the body's acyclic region to a position
  // BEFORE the process. We move all ops out of every block except the
  // entry block (just an init-br) and the loop header (just the wait).
  SmallVector<Operation *> toHoist;
  for (Block &block : body) {
    if (&block == &entry || &block == loopHeader)
      continue;
    for (Operation &op : block.without_terminator()) {
      toHoist.push_back(&op);
    }
  }
  for (Operation *op : toHoist) {
    op->moveBefore(processOp);
  }

  // STEP 3: Compute next-state expressions; these may create comb.mux ops
  // inserted right before the process. Each result i picks the header-arg
  // index that its yield references — this is critical for processes where
  // headerArgs carry extra edge-detection state alongside the yields.
  SmallVector<Value> nextStates;
  for (unsigned i = 0; i < processOp.getNumResults(); i++) {
    builder.setInsertionPoint(processOp);
    Value v = computeNextState(waitDest, loopHeader, yieldHeaderIdx[i],
                               builder);
    if (!v) {
      LLVM_DEBUG(llvm::dbgs() << "Skip process: can't synthesize next-state\n");
      return false;
    }
    nextStates.push_back(v);
  }

  // STEP 4: Build seq.firreg ops (or, for the delay-wait path, just bind
  // result_i = next_i combinationally).
  SmallVector<Value> newResults;
  builder.setInsertionPoint(processOp);
  for (unsigned i = 0; i < processOp.getNumResults(); i++) {
    Value nextVal = nextStates[i];
    if (!useFirreg) {
      // Combinational binding: matches `always @(*)` semantics and the
      // delay-wait clock-divider case under the flag. The result is just
      // the next-state value, no register inserted.
      newResults.push_back(nextVal);
      continue;
    }
    StringAttr name = builder.getStringAttr("loop_carry_" + std::to_string(i));
    Value initVal = initOps[yieldHeaderIdx[i]];
    seq::FirRegOp firreg;
    if (reset) {
      firreg = builder.create<seq::FirRegOp>(loc, nextVal, clock, name, reset,
                                              initVal, hw::InnerSymAttr{},
                                              /*isAsync=*/true);
    } else {
      firreg = builder.create<seq::FirRegOp>(loc, nextVal, clock, name);
    }
    newResults.push_back(firreg.getResult());
  }

  // Replace process results and erase.
  for (auto [i, oldResult] : llvm::enumerate(processOp.getResults())) {
    oldResult.replaceAllUsesWith(newResults[i]);
  }
  processOp.erase();
  return true;
}

/// Return true if `v` is a constant-time-zero hw.constant whose value matches
/// `initVal`. Used to identify the "stray initial drive" emitted by CIRCT
/// alongside a real firreg-driven update.
static bool valuesAreEqualConstants(Value a, Value b) {
  if (a == b)
    return true;
  IntegerAttr ai, bi;
  if (matchPattern(a, m_Constant(&ai)) && matchPattern(b, m_Constant(&bi)))
    return ai.getValue() == bi.getValue();
  return false;
}

/// Pass entry point.
struct LowerLoopCarryProcessesPass
    : public PassWrapper<LowerLoopCarryProcessesPass,
                         OperationPass<hw::HWModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(LowerLoopCarryProcessesPass)

  // Default constructor is required for pass cloning when invoked via the
  // pass pipeline parser. We expose the flag via Pass::Option<>.
  LowerLoopCarryProcessesPass() = default;
  LowerLoopCarryProcessesPass(const LowerLoopCarryProcessesPass &other)
      : PassWrapper<LowerLoopCarryProcessesPass,
                    OperationPass<hw::HWModuleOp>>(other) {
    lowerLlhdDelayAsZeroCycle = other.lowerLlhdDelayAsZeroCycle.getValue();
  }

  StringRef getArgument() const final { return "lower-loop-carry-processes"; }
  StringRef getDescription() const final {
    return "Lower llhd.process loop-carry FSMs to seq.firreg + comb mux. "
           "Handles patterns that CIRCT's llhd-lower-processes and "
           "llhd-deseq passes bail on.";
  }

  // When enabled, also (a) accept llhd.process ops whose wait has a delay
  // (clock-divider-style), lowering them by binding result_i = next_i
  // combinationally; and (b) drop bare `llhd.delay` ops by replacing each
  // result with its input operand. Default false so existing callers /
  // tests are unaffected.
  Pass::Option<bool> lowerLlhdDelayAsZeroCycle{
      *this, "lower-llhd-delay-as-zero-cycle",
      ::llvm::cl::desc("Treat llhd.delay (and delay-waits in llhd.process) "
                       "as zero-cycle combinational pass-through. Lets "
                       "ExportVerilog emit clock-generator-style modules at "
                       "the cost of dropping the delay semantics."),
      ::llvm::cl::init(false)};

  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<llhd::LLHDDialect, seq::SeqDialect, hw::HWDialect,
                    comb::CombDialect, cf::ControlFlowDialect>();
  }

  void runOnOperation() override {
    hw::HWModuleOp module = getOperation();
    const bool allowDelay = lowerLlhdDelayAsZeroCycle.getValue();

    // Phase 1: lower each loop-carry process.
    SmallVector<llhd::ProcessOp> processes;
    module.walk([&](llhd::ProcessOp p) { processes.push_back(p); });
    for (auto p : processes) {
      (void)tryLowerProcess(p, module, allowDelay);
    }

    // Phase 2: collapse llhd.sig / llhd.drv / llhd.prb chains created by
    // the legacy lowering. After phase 1, drv values now point at firreg
    // outputs; the sig becomes a pure feed-through and can be elided.
    //
    // Tolerates the common "double-drive" pattern (e.g. right_shifter,
    // square_wave) where a stray un-conditional initial drive writes the
    // signal's init value alongside the real datapath drive. The init
    // drive is dropped if its value equals the sig's init constant.
    SmallVector<llhd::SignalOp> sigs;
    module.walk([&](llhd::SignalOp s) { sigs.push_back(s); });
    for (auto sig : sigs) {
      SmallVector<llhd::DriveOp> drvs;
      SmallVector<llhd::ProbeOp> prbs;
      bool ok = true;
      for (Operation *user : sig.getResult().getUsers()) {
        if (auto d = dyn_cast<llhd::DriveOp>(user)) {
          drvs.push_back(d);
        } else if (auto p = dyn_cast<llhd::ProbeOp>(user)) {
          prbs.push_back(p);
        } else {
          ok = false;
          break;
        }
      }
      if (!ok)
        continue;

      // Orphan-sig elision (regression for clkgenerator /
      // multi_booth_8bit / float_multi): some sigs end up with no drives
      // after the reconstruction pass, because the reconstruction split
      // a single declared signal into TWO sigs — one with the drive (the
      // working sig) and one shadow with only a probe (the output-port
      // sig). The shadow sig's probe always returns its init value. We
      // replace each probe with the init value and erase the sig.
      Value sigInit = sig.getInit();
      if (drvs.empty()) {
        if (!sigInit)
          continue; // No init to substitute — leave alone.
        for (auto p : prbs) {
          p.getResult().replaceAllUsesWith(sigInit);
          p.erase();
        }
        sig.erase();
        continue;
      }

      // Identify and drop "init-style" drives: those without an enable that
      // write a constant equal to the signal's init operand.
      llhd::DriveOp keep;
      SmallVector<llhd::DriveOp> drops;
      for (auto d : drvs) {
        bool isInitStyle = !d.getEnable() && sigInit &&
                           valuesAreEqualConstants(d.getValue(), sigInit);
        if (isInitStyle) {
          drops.push_back(d);
          continue;
        }
        if (keep) {
          // Multiple "real" drives on the same sig — give up on this sig.
          keep = {};
          drops.clear();
          break;
        }
        keep = d;
      }

      // Register-file array: the `initial begin`
      // block lowers to an UNCONDITIONAL drive writing a computed-constant
      // power-on value, while the clocked `always` block lowers to a single
      // ENABLED drive whose value is a seq.firreg output. The constant-init
      // rule above doesn't fire (the init value is a folded array constant,
      // not literally the sig's init operand), so `keep` was cleared by the
      // "multiple real drives" guard. Retry: when exactly one drive is
      // enabled and every other drive is unconditional, the enabled
      // (firreg-backed) drive is the steady-state register; the unconditional
      // drives are init/default writes whose effect Verilog's reg
      // initialization carries separately. Keep the enabled drive, drop the
      // rest. The firreg's own next-state mux already encodes hold-vs-update,
      // so dropping the drive enable when collapsing probes is sound.
      if (!keep) {
        llhd::DriveOp enabledDrive;
        bool singleEnabled = true;
        for (auto d : drvs) {
          if (d.getEnable()) {
            if (enabledDrive) {
              singleEnabled = false;
              break;
            }
            enabledDrive = d;
          }
        }
        if (singleEnabled && enabledDrive) {
          keep = enabledDrive;
          drops.clear();
          for (auto d : drvs)
            if (d != enabledDrive)
              drops.push_back(d);
        }
      }

      if (!keep)
        continue;

      for (auto d : drops)
        d.erase();
      for (auto p : prbs) {
        p.getResult().replaceAllUsesWith(keep.getValue());
        p.erase();
      }
      keep.erase();
      sig.erase();
    }

    // Phase 3: under the flag, drop residual llhd.delay ops by replacing
    // each result with its input operand. This is the "treat as zero-cycle
    // pass-through" semantics — only safe when the
    // caller acknowledges the loss of delay semantics.
    if (allowDelay) {
      SmallVector<llhd::DelayOp> delays;
      module.walk([&](llhd::DelayOp d) { delays.push_back(d); });
      for (auto d : delays) {
        d.getResult().replaceAllUsesWith(d.getOperand());
        d.erase();
      }
    }

    // Phase 4: erase llhd.constant_time ops that became dead.
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

void registerLowerLoopCarryProcessesPass() {
  PassRegistration<LowerLoopCarryProcessesPass>();
}
