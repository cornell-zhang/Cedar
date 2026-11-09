// RUN: %eggopt %S/Inputs/rom_alias.mlir --reconstruct-from-extraction --egg-file=%S/Inputs/rom_alias.egg --extraction-file=%S/Inputs/rom_alias.extracted.txt -o - 2>/dev/null | FileCheck %s
//
// Regression: shared sub-expression aliases (`__sN`) emitted by extract_result
// as `(let __sN <expr>)` and referenced from inside an OpVec, e.g.
// `(comb_and ... (OpVec __s0) ...)`. Before the splitExpression fix, the OpVec
// flattener called splitExpression("__s0"); a '_'-leading bare token is not
// alpha, so it hit the `Invalid expression: __sN` exit(1) path, producing an
// empty `module {}` (rtllm/ROM). The fix accepts '_'-leading
// bare tokens so the alias resolves via Egglog::parsedOps.
//
// The reconstructed module must be NON-EMPTY and share the icmp result (%0)
// across both the comb.xor operand and the comb.and OpVec operand.

// CHECK: hw.module @rom_alias
// CHECK: %[[ICMP:.+]] = comb.icmp eq %a, %c0_i8 : i8
// CHECK: %[[XOR:.+]] = comb.xor %[[ICMP]], %true : i1
// CHECK: comb.and %[[XOR]], %[[ICMP]] : i1
// CHECK: hw.output
