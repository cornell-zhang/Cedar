#include "llvm/Support/FileSystem.h"
#include "llvm/Support/CommandLine.h"
#include "mlir/InitAllDialects.h"
#include "mlir/InitAllPasses.h"
#include "mlir/Tools/mlir-opt/MlirOptMain.h"
#include "mlir/AsmParser/AsmParser.h"

// Register every CIRCT dialect so any input MLIR (llhd, firrtl, sv, moore,
// hw, comb, seq, etc.) parses without "Dialect `foo' not found" errors.
#include "circt/InitAllDialects.h"

// Temporarily commenting out StableHLO includes due to compatibility issues
// #include "stablehlo/dialect/Register.h"
// #include "stablehlo/conversions/linalg/transforms/Passes.h"
// #include "stablehlo/tests/CheckOps.h"
// #include "stablehlo/tests/TestUtils.h"
// #include "stablehlo/transforms/Passes.h"
// #include "stablehlo/transforms/optimization/Passes.h"

#include "Egglog.h"
#include "EqualitySaturationPass.h"
#include "EggifyPass.h"
#include "EggifyOnlyPass.h"
#include "ReconstructFromExtractionPass.h"
#include "LowerLoopCarryProcessesPass.h"
#include "LowerFreeLLHDSignalsPass.h"
#include "LowerInterProcessLLHDSignalsPass.h"
#include "EgglogCustomDefs.h"

#define DEBUG_TYPE "dialegg"

// Global command-line option for egg file (shared by all passes)
llvm::cl::opt<std::string> eggFileOpt("egg-file", llvm::cl::desc("Path to egg file"));

// Global command-line flag for hierarchical eggify. Default OFF so the flat
// (op-level) flow is behavior-identical to before. When ON, the eggify-only
// pass emits every hw.module (including private submodules) into ONE combined
// egglog program AND adds module-level HwModule/Instance/Submodule e-nodes so
// the module layer and the op layer (comb_*/seq_*) coexist in the same
// e-graph. See harness/dialegg/circt.egg for the sibling `Module` datatype.
llvm::cl::opt<bool> hierarchicalOpt(
    "hierarchical",
    llvm::cl::desc("Emit hierarchical (module-level + op-level) egglog program"),
    llvm::cl::init(false));

// When set, the reconstruct-from-extraction pass reconstructs ONLY the
// top-level (non-instantiated) module(s) from the extraction file, and leaves
// every instantiated submodule body verbatim from the input MLIR. This is for
// the hierarchical flow: rewrites fire in the top module
// datapath while body-less / DesignWare submodules (DW02_tree) and untouched
// leaf modules (design_mac_booth) survive as their original hw.instance +
// hw.module, so DC can resolve DesignWare cells. The per-block re-eggification
// assigns block-local op ids, so a single global extraction file can only be
// replayed against ONE module; reconstructing submodules too would run the
// shared extraction stream off the end / reference cross-module Value ids.
// Default off: the flat 91/91 path reconstructs every public module as before.
llvm::cl::opt<bool> hierarchicalReconstructOpt(
    "hierarchical-reconstruct",
    llvm::cl::desc("Reconstruct only top-level (non-instantiated) modules; leave submodule bodies verbatim"),
    llvm::cl::init(false));

std::string getMlirFile(int argc, char** argv) {
    for (int i = 1; i < argc; i++) {
        if (argv[i][0] != '-') {
            return argv[i];
        }
    }
    return "";
}

int main(int argc, char** argv) {
    // Register dialects
    mlir::DialectRegistry dialectRegistry;
    mlir::registerAllDialects(dialectRegistry);
    circt::registerAllDialects(dialectRegistry);
    
    // Temporarily commenting out StableHLO dialect registration
    // mlir::stablehlo::registerAllDialects(dialectRegistry);

    // Register passes
    mlir::registerAllPasses();
    // Temporarily commenting out StableHLO pass registration
    // mlir::stablehlo::registerPassPipelines();
    // mlir::stablehlo::registerPasses();
    // mlir::stablehlo::registerOptimizationPasses();
    // mlir::stablehlo::registerStablehloLinalgTransformsPasses();
    mlir::PassRegistration<EggifyPass>();
    registerLowerLoopCarryProcessesPass();
    registerLowerFreeLLHDSignalsPass();
    registerLowerInterProcessLLHDSignalsPass();

    std::map<std::string, AttrStringifyFunction, std::less<>> attrStringifiers = {
            {mlir::arith::FastMathFlagsAttr::name.str(), stringifyFastMathFlagsAttr}};
    std::map<std::string, AttrParseFunction, std::less<>> attrParsers = {
            {"arith_fastmath", parseFastMathFlagsAttr}};

    std::map<std::string, TypeStringifyFunction, std::less<>> typeStringifiers = {
            {mlir::RankedTensorType::name.str(), stringifyRankedTensorType}};
    std::map<std::string, TypeParseFunction, std::less<>> typeParsers = {
            {"RankedTensor", parseRankedTensorType}};

    EgglogCustomDefs funcs = {attrStringifiers, attrParsers, typeStringifiers, typeParsers};
    
    // Register the full equality saturation pass (MLIR -> Egglog -> Extract -> MLIR)
    mlir::PassRegistration<EqualitySaturationPass>([&] {
        return std::make_unique<EqualitySaturationPass>(getMlirFile(argc, argv), funcs);
    });

    // Register the eggify-only pass (MLIR -> Egglog -> JSON, no extraction)
    mlir::PassRegistration<EggifyOnlyPass>([&] {
        return std::make_unique<EggifyOnlyPass>(getMlirFile(argc, argv), funcs);
    });

    // Register the reconstruction pass (Extraction results -> MLIR)
    mlir::PassRegistration<ReconstructFromExtractionPass>([&] {
        return std::make_unique<ReconstructFromExtractionPass>(getMlirFile(argc, argv), funcs);
    });

    // Run the main MLIR opt
    mlir::LogicalResult result = mlir::MlirOptMain(argc, argv, "EqSat", dialectRegistry);
    return mlir::asMainReturnCode(result);
}
