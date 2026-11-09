#include <chrono>
#include "ReconstructFromExtractionPass.h"

#define DEBUG_TYPE "dialegg"

extern llvm::cl::opt<std::string> eggFileOpt;
extern llvm::cl::opt<bool> hierarchicalReconstructOpt;
static llvm::cl::opt<std::string> extractionFileOpt("extraction-file", llvm::cl::desc("Path to extraction results file"));

void ReconstructFromExtractionPass::runOnBlock(mlir::Block& block, const std::string& blockName, std::ifstream& extractionFile) {
    auto start = std::chrono::high_resolution_clock::now();

    // Re-eggify the block to recreate the mapping
    mlir::MLIRContext& context = getContext();
    Egglog egglog(context, customFunctions, supportedOps);

    // Register all block arguments
    for (mlir::Value value: block.getArguments()) {
        egglog.eggifyValue(value);
    }

    for (mlir::Operation& op: block.getOperations()) {
        egglog.eggifyOperation(&op);
    }

    auto end = std::chrono::high_resolution_clock::now();
    mlirToEgglogTime += std::chrono::duration<double>(end - start).count();

    // Dump all ops
    for (const EggifiedOp* eggOp: egglog.eggifiedBlock) {
        LLVM_DEBUG(eggOp->print(llvm::dbgs()));
    }

    start = std::chrono::high_resolution_clock::now();

    // Parse the extracted egglog file and replace the MLIR operations
    std::vector<std::string> extractedLines;
    for (const EggifiedOp* eggOp: egglog.eggifiedBlock) {
        if (!eggOp->shouldBeExtracted()) {
            continue;
        }

        extractedLines.emplace_back();
        if (!std::getline(extractionFile, extractedLines.back())) {
            llvm::errs() << "Warning: extraction file ended before all ops were processed\n";
            break;
        }

        if (extractedLines.back().empty()) {
            llvm::errs() << "Warning: empty extraction line for " << eggOp->getPrintId() << "\n";
            continue;
        }

        mlir::Operation* prevOp = eggOp->mlirOp;
        mlir::OpBuilder builder(prevOp);

        // Parse shared sub-expression definitions: (let __sN <expr>)
        while (extractedLines.back().rfind("(let __s", 0) == 0) {
            std::string_view letLine = extractedLines.back();
            auto split = Egglog::splitExpression(letLine);
            if (split.size() >= 3 && split[0] == "let") {
                std::string alias(split[1]);
                std::string_view exprStr = split[2];
                mlir::Operation* defOp = egglog.parseOperation(exprStr, builder);
                if (defOp != nullptr) {
                    egglog.parsedOps[alias] = defOp;
                }
            }
            extractedLines.emplace_back();
            if (!std::getline(extractionFile, extractedLines.back())) {
                break;
            }
        }

        LLVM_DEBUG(llvm::dbgs() << "Reconstructing " << eggOp->getPrintId() << " from: " << extractedLines.back() << "\n");

        mlir::Operation* newOp = egglog.parseOperation(extractedLines.back(), builder);

        if (newOp == nullptr) {
            mlir::Value value = egglog.parseValue(extractedLines.back());
            prevOp->getResult(0).replaceAllUsesWith(value);
            prevOp->erase();
        } else if (newOp != prevOp) {
            LLVM_DEBUG(llvm::dbgs() << "REPLACING: (" << *prevOp << ") WITH (" << *newOp << ")\n\n");
            if (newOp->getNumResults() == prevOp->getNumResults()) {
                prevOp->replaceAllUsesWith(newOp);
                prevOp->erase();
            } else {
                llvm::errs() << "Warning: result count mismatch for " << eggOp->getPrintId()
                             << " (expected " << prevOp->getNumResults()
                             << ", got " << newOp->getNumResults() << "), skipping replacement\n";
                newOp->erase();
            }
        }
    }

    end = std::chrono::high_resolution_clock::now();
    egglogToMlirTime += std::chrono::duration<double>(end - start).count();

    // Dump parsed ops cache
    for (const auto& [opStr, op]: egglog.parsedOps) {
        LLVM_DEBUG(llvm::dbgs() << opStr << " : " << *op << "\n");
    }

    LLVM_DEBUG(llvm::dbgs() << "\n");
}

namespace {
// Mirror circt::sv::isValidVerilogCharacter: a Verilog identifier char is
// [A-Za-z0-9_$], with [A-Za-z_] required for the first character. Any other
// character (notably '/', ':', '.', ' ' from FIRRTL hierarchical names) is
// illegal in ExportVerilog symbol/instance names and must be scrubbed before
// emission.
static bool isVerilogNameChar(char c) {
    return std::isalnum(static_cast<unsigned char>(c)) || c == '_' || c == '$';
}

// Returns a sanitized copy of `name` with every Verilog-illegal character
// replaced by '_'. If the first char is not a legal leading char (alpha or
// '_'), a leading '_' is prepended. Returns the input unchanged when already
// valid so we never disturb well-formed names.
static std::string sanitizeVerilogName(llvm::StringRef name) {
    if (name.empty()) {
        return std::string(name);
    }
    bool needsFix = false;
    for (char c : name) {
        if (!isVerilogNameChar(c)) {
            needsFix = true;
            break;
        }
    }
    char first = name.front();
    bool badFirst = !(std::isalpha(static_cast<unsigned char>(first)) || first == '_');
    if (!needsFix && !badFirst) {
        return std::string(name);
    }
    std::string out;
    out.reserve(name.size() + 1);
    if (badFirst) {
        out.push_back('_');
    }
    for (char c : name) {
        out.push_back(isVerilogNameChar(c) ? c : '_');
    }
    return out;
}

// Walk every op in the module and sanitize the string attributes that
// ExportVerilog turns into Verilog identifiers/symbols. This catches both ops
// rebuilt by reconstruction and ops carried through untouched from the input
// MLIR (e.g. seq.firmem / seq.firreg whose FIRRTL name holds a '/').
static void sanitizeModuleNames(mlir::ModuleOp module) {
    static const char* kNameAttrs[] = {"name", "sym_name", "instanceName", "instance_name"};
    module.walk([&](mlir::Operation* op) {
        for (const char* attrName : kNameAttrs) {
            auto strAttr = op->getAttrOfType<mlir::StringAttr>(attrName);
            if (!strAttr) {
                continue;
            }
            std::string sanitized = sanitizeVerilogName(strAttr.getValue());
            if (sanitized != strAttr.getValue().str()) {
                op->setAttr(attrName,
                            mlir::StringAttr::get(op->getContext(), sanitized));
            }
        }
    });
}
} // namespace

void ReconstructFromExtractionPass::runOnOperation() {
    mlir::ModuleOp module = getOperation();
    
    // Open extraction file
    std::ifstream extractionFile(extractionFilename);
    if (!extractionFile.is_open()) {
        llvm::errs() << "Failed to open extraction file: " << extractionFilename << "\n";
        signalPassFailure();
        return;
    }
    
    // For the hierarchical flow, reconstruct only the top-level (non-instantiated)
    // module: a single global extraction file replays against one module only
    // (per-block re-eggification uses block-local op ids). Collect the set of
    // module names that are instantiated by some hw.instance so they can be
    // skipped (left verbatim). No-op when --hierarchical-reconstruct is off.
    std::set<std::string> instantiatedModules;
    if (hierarchicalReconstructOpt) {
        module.walk([&](circt::hw::InstanceOp inst) {
            instantiatedModules.insert(inst.getModuleName().str());
        });
    }

    // Find all functions and hw modules in the module
    for (mlir::Operation& op: module.getOps()) {
        if (auto funcOp = llvm::dyn_cast<mlir::func::FuncOp>(&op)) {
            llvm::StringRef funcName = funcOp.getName();
            LLVM_DEBUG(llvm::dbgs() << "Reconstructing function: " << funcName << "\n");
            
            for (mlir::Block& block: funcOp.getRegion().getBlocks()) {
                std::string parentOpName = block.getParentOp()->getName().getStringRef().str();
                std::string blockName = funcName.str() + "_" + parentOpName;
                runOnBlock(block, blockName, extractionFile);
            }
        } else if (auto hwModuleOp = llvm::dyn_cast<circt::hw::HWModuleOp>(&op)) {
            llvm::StringRef moduleName = hwModuleOp.getName();

            if (hwModuleOp.isPrivate()) {
                continue;
            }

            // Hierarchical reconstruct: leave instantiated submodules verbatim
            // (no rewrites fire in them; their bodies use cross-module Value ids
            // that the global extraction file cannot replay per-block).
            if (hierarchicalReconstructOpt &&
                instantiatedModules.count(moduleName.str())) {
                LLVM_DEBUG(llvm::dbgs() << "Leaving submodule verbatim: " << moduleName << "\n");
                continue;
            }

            LLVM_DEBUG(llvm::dbgs() << "Reconstructing HW module: " << moduleName << "\n");
            
            for (mlir::Block& block: hwModuleOp.getBodyRegion().getBlocks()) {
                std::string parentOpName = block.getParentOp()->getName().getStringRef().str();
                std::string blockName = moduleName.str() + "_" + parentOpName;
                runOnBlock(block, blockName, extractionFile);
            }
        } else {
            llvm::errs() << "Skipping non-function/non-hw-module operation: " << op.getName() << "\n";
        }
    }

    extractionFile.close();

    // Temporary dead code elimination
    bool clean = false;
    while (!clean) {
        clean = true;

        module.walk([&](mlir::Operation* op) {
            bool deadFunctionCall = mlir::isa<mlir::func::CallOp>(op) && op->use_empty();
            bool unusedResults = op->getNumResults() > 0 && op->use_empty();
            if (mlir::isOpTriviallyDead(op) || deadFunctionCall || unusedResults) {
                clean = false;
                op->erase();
            }
        });
    }

    // Scrub Verilog-illegal characters from name/sym_name attributes so
    // downstream --lower-seq-to-sv / --export-split-verilog never sees an
    // illegal symbol (FIRRTL '/'-bearing names crash ExportVerilog).
    sanitizeModuleNames(module);

    llvm::outs() << "✅ Reconstruction pass completed!\n";
    llvm::outs() << "   - Egglog→MLIR time: " << egglogToMlirTime << "s\n";
}

llvm::LogicalResult ReconstructFromExtractionPass::initialize(mlir::MLIRContext* context) {
    // Initialize eggFilePath from command-line option
    eggFilePath = eggFileOpt;
    
    // Make sure MLIR file exists
    if (!llvm::sys::fs::exists(mlirFilePath)) {
        llvm::errs() << "MLIR file does not exist: " << mlirFilePath << "\n";
        return llvm::failure();
    }

    // Check for egg file
    if (!llvm::sys::fs::exists(eggFilePath)) {
        // Try to find egg file with the same name as the mlir file
        std::string name = mlirFilePath.substr(0, mlirFilePath.find(".mlir"));
        eggFilePath = name + ".egg";
        if (!llvm::sys::fs::exists(eggFilePath)) {
            llvm::errs() << "Egg file does not exist: " << eggFilePath << "\n";
            return llvm::failure();
        }
        llvm::outs() << "Using default egg file: " << eggFilePath << "\n";
    }

    // Set filenames
    std::string name = mlirFilePath.substr(0, mlirFilePath.find(".mlir"));
    
    // Check if extraction file was specified via command line
    if (!extractionFileOpt.empty()) {
        extractionFilename = extractionFileOpt;
    } else {
        extractionFilename = name + ".extracted.txt";
    }
    
    metadataFilename = name + ".extraction-metadata.txt";

    // Make sure extraction file exists
    if (!llvm::sys::fs::exists(extractionFilename)) {
        llvm::errs() << "Extraction file does not exist: " << extractionFilename << "\n";
        llvm::errs() << "Please specify extraction file with --extraction-file=<path>\n";
        return llvm::failure();
    }

    llvm::outs() << "Using extraction file: " << extractionFilename << "\n";

    // Parse supported ops from egg file
    std::ifstream opFile(eggFilePath);
    std::string line;

    while (std::getline(opFile, line)) {
        if (EgglogOpDef::isOpFunction(line)) {
            EgglogOpDef parsedOp = EgglogOpDef::parseOpFunction(line);
            supportedOps.emplace(parsedOp.egglogName(), parsedOp);
            supportedDialects.insert(parsedOp.dialect);
        }
    }

    opFile.close();

    LLVM_DEBUG(llvm::dbgs() << "Supported ops: ");
    for (const auto& [op, _]: supportedOps) {
        LLVM_DEBUG(llvm::dbgs() << op << ", ");
    }

    LLVM_DEBUG(llvm::dbgs() << "\nSupported dialects: ");
    for (const std::string& dialect: supportedDialects) {
        LLVM_DEBUG(llvm::dbgs() << dialect << ", ");
    }

    LLVM_DEBUG(llvm::dbgs() << "\n\n");

    return llvm::success();
}

