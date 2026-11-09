// RUN: %eggopt %S/Inputs/asyn_fifo_sanitize.mlir --reconstruct-from-extraction --egg-file=%S/Inputs/circt.egg --extraction-file=%S/Inputs/asyn_fifo_sanitize.extracted.txt -o - 2>/dev/null | FileCheck %s
//
// Regression: FIRRTL-derived names carry a '/' hierarchical separator (e.g.
// "dual_port_RAM/rdata"). On --lower-seq-to-sv a seq.firmem becomes an
// hw.module.generated whose sym_name is derived from that name -> a symbol with
// '/', which ExportVerilog diagnoses then SIGSEGVs on
// (getVerilogModuleNameAttr). The reconstruct pass now sanitizes every
// name/sym_name string attribute (non [A-Za-z0-9_$] -> '_') so no illegal
// symbol ever reaches CIRCT (rtllm/asyn_fifo).

// The '/' must be gone everywhere; the firreg's sanitized name surfaces as the
// register's SSA name in pretty form.
// CHECK: hw.module @asyn_fifo_sanitize
// CHECK: %dual_port_RAM_rdata = seq.firreg %d clock %{{.+}} : i8
// CHECK-NOT: dual_port_RAM/rdata
