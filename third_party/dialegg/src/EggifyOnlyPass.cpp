#include <cctype>
#include <chrono>
#include "llvm/Support/CommandLine.h"
#include "EggifyOnlyPass.h"

#define DEBUG_TYPE "dialegg"

extern llvm::cl::opt<std::string> eggFileOpt;
extern llvm::cl::opt<bool> hierarchicalOpt;
extern llvm::cl::opt<std::string> optModuleOpt;
mlir::LogicalResult validateModuleSelection(mlir::ModuleOp module);

void EggifyOnlyPass::runEgglog(const std::vector<EggifiedOp*>& block, const std::string& blockName) {
    std::ifstream eggFile(eggFilePath);
    std::vector<std::string> egglogLines;

    // Read the egglog file
    std::string opsTarget = ";; OPS HERE ;;";
    std::string extractsTarget = ";; EXTRACTS HERE ;;";

    bool insertedOps = false;
    bool insertedExtracts = false;

    auto start = std::chrono::high_resolution_clock::now();

    std::string line;
    while (std::getline(eggFile, line)) {
        egglogLines.push_back(line);

        if (!insertedOps && line == opsTarget) {
            egglogLines.push_back("; " + blockName);
            for (const EggifiedOp* op: block) {
                egglogLines.push_back(op->egglogLet());
            }
            insertedOps = true;
        } else if (!insertedExtracts && line == extractsTarget) {
            // Define RootNode for each operation that should be extracted
            // This marks the extraction targets in the E-graph
            for (const EggifiedOp* op: block) {
                if (op->shouldBeExtracted()) {
                    egglogLines.push_back("(let root_" + op->getPrintId() + " (RootNode " + op->getPrintId() + "))");
                }
            }
            insertedExtracts = true;
        }
    }
    eggFile.close();

    // Write the egglog program to a .ops.egg file
    std::string name = mlirFilePath.substr(0, mlirFilePath.find(".mlir"));
    std::string opsEggFilePath = name + ".ops.egg";
    std::ofstream eggFileOut(opsEggFilePath);
    for (const std::string& line: egglogLines) {
        eggFileOut << line << "\n";
    }
    eggFileOut.close();

    auto end = std::chrono::high_resolution_clock::now();
    mlirToEgglogTime += std::chrono::duration<double>(end - start).count();

    // Run egglog with --to-json to generate e-graph
    std::string egglogCmd = "egglog " + opsEggFilePath + " --to-json > " + egglogOutputFilename + " 2> " + egglogLogFilename;

    LLVM_DEBUG(llvm::dbgs() << "Running egglog: " << egglogCmd << "\n");

    start = std::chrono::high_resolution_clock::now();
    int ret = std::system(egglogCmd.c_str());
    end = std::chrono::high_resolution_clock::now();

    if (ret != 0) {
        printFileContents(egglogLogFilename);
        printFileContents(egglogOutputFilename);
        llvm::errs() << "Egglog failed\n";
        exit(1);
    }

    egglogExecTime += std::chrono::duration<double>(end - start).count();

    LLVM_DEBUG(printFileContents(egglogLogFilename));
    LLVM_DEBUG(printFileContents(egglogOutputFilename));
    LLVM_DEBUG(llvm::dbgs() << "Done running egglog\n");
}

void EggifyOnlyPass::runHierarchicalEgglog(const std::vector<EggifiedOp*>& allOps,
                                           const std::vector<std::string>& moduleLets) {
    std::ifstream eggFile(eggFilePath);
    std::vector<std::string> egglogLines;

    std::string opsTarget = ";; OPS HERE ;;";
    std::string extractsTarget = ";; EXTRACTS HERE ;;";

    bool insertedOps = false;
    bool insertedExtracts = false;

    auto start = std::chrono::high_resolution_clock::now();

    std::string line;
    while (std::getline(eggFile, line)) {
        egglogLines.push_back(line);

        if (!insertedOps && line == opsTarget) {
            // Op-level lets first (every module's body, globally-unique ids),
            // then the module-level HwModule/Instance/Submodule lets that
            // reference those op ids.
            egglogLines.push_back("; hierarchical op-level e-nodes");
            for (const EggifiedOp* op : allOps)
                egglogLines.push_back(op->egglogLet());
            egglogLines.push_back("; hierarchical module-level e-nodes");
            for (const std::string& mlet : moduleLets)
                egglogLines.push_back(mlet);
            insertedOps = true;
        } else if (!insertedExtracts && line == extractsTarget) {
            for (const EggifiedOp* op : allOps) {
                if (op->shouldBeExtracted())
                    egglogLines.push_back("(let root_" + op->getPrintId() +
                                          " (RootNode " + op->getPrintId() + "))");
            }
            insertedExtracts = true;
        }
    }
    eggFile.close();

    std::string name = mlirFilePath.substr(0, mlirFilePath.find(".mlir"));
    std::string opsEggFilePath = name + ".ops.egg";
    std::ofstream eggFileOut(opsEggFilePath);
    for (const std::string& l : egglogLines)
        eggFileOut << l << "\n";
    eggFileOut.close();

    auto end = std::chrono::high_resolution_clock::now();
    mlirToEgglogTime += std::chrono::duration<double>(end - start).count();

    std::string egglogCmd = "egglog " + opsEggFilePath + " --to-json > " +
                            egglogOutputFilename + " 2> " + egglogLogFilename;

    start = std::chrono::high_resolution_clock::now();
    int ret = std::system(egglogCmd.c_str());
    end = std::chrono::high_resolution_clock::now();

    if (ret != 0) {
        printFileContents(egglogLogFilename);
        printFileContents(egglogOutputFilename);
        llvm::errs() << "Egglog failed (hierarchical)\n";
        exit(1);
    }

    egglogExecTime += std::chrono::duration<double>(end - start).count();
}

void EggifyOnlyPass::saveExtractionMetadata(const std::vector<EggifiedOp*>& block, const std::string& blockName) {
    std::ofstream metadataFile(metadataFilename, std::ios::app);

    metadataFile << "BLOCK: " << blockName << "\n";

    for (const EggifiedOp* eggOp: block) {
        if (eggOp->shouldBeExtracted()) {
            metadataFile << eggOp->getPrintId() << "\t";

            // Save the MLIR operation for reference
            if (eggOp->mlirOp != nullptr) {
                std::string mlirOpStr;
                llvm::raw_string_ostream rso(mlirOpStr);
                eggOp->mlirOp->print(rso);
                metadataFile << mlirOpStr;
            }
            metadataFile << "\n";
        }
    }

    metadataFile << "END_BLOCK\n\n";
    metadataFile.close();
}

void EggifyOnlyPass::runOnBlock(mlir::Block& block, const std::string& blockName) {
    auto start = std::chrono::high_resolution_clock::now();

    // Eggify the block
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

    // Run egglog to generate e-graph
    runEgglog(egglog.eggifiedBlock, blockName);

    // Save metadata about which operations should be extracted
    saveExtractionMetadata(egglog.eggifiedBlock, blockName);

    LLVM_DEBUG(llvm::dbgs() << "\n");
}

// Sanitize an egglog let identifier: keep [A-Za-z0-9_], map anything else to '_'.
static std::string sanitizeEgglogId(llvm::StringRef in) {
    std::string out;
    out.reserve(in.size());
    for (char c : in) {
        out.push_back((std::isalnum(static_cast<unsigned char>(c)) || c == '_') ? c : '_');
    }
    return out;
}

// Internal modules are optimizable unless the user explicitly makes them opaque.
// External declarations are always opaque, regardless of their symbol names.
static llvm::cl::list<std::string> opaqueModules(
    "opaque-module", llvm::cl::desc("Opaque internal module symbol (requires --hierarchical; repeatable)"),
    llvm::cl::ZeroOrMore, llvm::cl::CommaSeparated);

static bool isOpaqueModule(circt::hw::HWModuleOp module) {
    if (module->hasAttr("cedar.blackbox"))
        return true;
    for (const std::string& name : opaqueModules)
        if (module.getName() == name)
            return true;
    return false;
}

void EggifyOnlyPass::runHierarchical(mlir::ModuleOp module) {
    // ONE shared Egglog instance for the whole design so op ids are globally
    // unique across all module blocks (the flat path uses a fresh Egglog per
    // block, which restarts ids at op0 -- fine when each block is emitted to
    // its own file, but it would collide if we concatenated blocks). With a
    // single instance every block contributes distinct op<N> lets.
    mlir::MLIRContext& context = getContext();
    Egglog egglog(context, customFunctions, supportedOps);

    // Per-module bookkeeping captured during the op-level eggify pass.
    struct ModuleInfo {
        std::string name;                       // hw.module symbol name
        bool isPrivate = false;
        bool isBlackbox = false;                // external or explicitly opaque module
        std::vector<std::string> bodyOpIds;     // print-ids of extractable body ops
        std::vector<std::string> portNames;     // input+output port names
        std::vector<mlir::Operation*> instances;// hw.instance ops in this module
    };
    std::vector<ModuleInfo> modules;

    auto start = std::chrono::high_resolution_clock::now();

    for (mlir::Operation& op : module.getOps()) {
        auto hwModuleOp = llvm::dyn_cast<circt::hw::HWModuleOp>(&op);
        if (auto external = llvm::dyn_cast<circt::hw::HWModuleExternOp>(&op)) {
            ModuleInfo info;
            info.name = external.getName().str();
            info.isBlackbox = true;
            for (const circt::hw::PortInfo& port : external.getPortList())
                info.portNames.push_back(port.getName().str());
            modules.push_back(std::move(info));
            continue;
        }
        if (!hwModuleOp)
            continue;

        ModuleInfo info;
        info.name = hwModuleOp.getName().str();
        info.isPrivate = hwModuleOp.isPrivate();
        info.isBlackbox = isOpaqueModule(hwModuleOp);
        llvm::SmallVector<circt::hw::PortInfo> ports = hwModuleOp.getPortList();
        for (const circt::hw::PortInfo& port : ports)
            info.portNames.push_back(port.getName().str());

        // Preserve the interface while excluding explicitly opaque bodies.
        if (info.isBlackbox) {
            modules.push_back(std::move(info));
            continue;
        }

        // Eggify every block of this (non-blackbox) module into the SHARED
        // egglog. Unlike the flat path we do NOT skip private submodules --
        // their bodies are eligible for optimization too.
        for (mlir::Block& block : hwModuleOp.getBodyRegion().getBlocks()) {
            for (mlir::Value arg : block.getArguments())
                egglog.eggifyValue(arg);
            for (mlir::Operation& inner : block.getOperations()) {
                egglog.eggifyOperation(&inner);
                if (inner.getName().getStringRef() == "hw.instance")
                    info.instances.push_back(&inner);
            }
        }
        modules.push_back(std::move(info));
    }

    // Collect each module's extractable (root) op print-ids for its body vec.
    // eggifiedBlock holds ALL ops across all modules; map each back to its
    // parent hw.module via the MLIR op's symbol-table ancestor.
    for (EggifiedOp* eggOp : egglog.eggifiedBlock) {
        if (eggOp->opaque || eggOp->mlirOp == nullptr)
            continue;
        if (!eggOp->shouldBeExtracted())
            continue;
        mlir::Operation* parent = eggOp->mlirOp->getParentOfType<circt::hw::HWModuleOp>();
        if (!parent)
            continue;
        llvm::StringRef pname = llvm::cast<circt::hw::HWModuleOp>(parent).getName();
        for (ModuleInfo& mi : modules) {
            if (mi.name == pname.str()) {
                mi.bodyOpIds.push_back(eggOp->getPrintId());
                break;
            }
        }
    }

    auto end = std::chrono::high_resolution_clock::now();
    mlirToEgglogTime += std::chrono::duration<double>(end - start).count();

    // ---- Build the module-level e-nodes ----
    std::vector<std::string> moduleLets;
    for (const ModuleInfo& mi : modules) {
        std::string sym = sanitizeEgglogId(mi.name);

        if (mi.isBlackbox) {
            // Opaque modules retain their port interfaces in the module layer.
            std::string portsVec = "(vec-of";
            if (mi.portNames.empty())
                portsVec = "(vec-empty)";
            else {
                for (const std::string& p : mi.portNames)
                    portsVec += " \"" + p + "\"";
                portsVec += ")";
            }
            moduleLets.push_back("(let mod_" + sym + " (Submodule \"" + mi.name +
                                 "\" " + portsVec + "))");
            continue;
        }

        // A module with op-level body: HwModule node binding the body op roots.
        std::string bodyVec = "(vec-of";
        for (const std::string& id : mi.bodyOpIds)
            bodyVec += " " + id;
        bodyVec += ")";
        moduleLets.push_back("(let mod_" + sym + " (HwModule \"" + mi.name +
                             "\" " + bodyVec + "))");
    }

    // Instance e-nodes: one per hw.instance, binding the target module node and
    // the operand op print-ids (the cross-module connections).
    for (const ModuleInfo& mi : modules) {
        for (mlir::Operation* inst : mi.instances) {
            auto instNameAttr = inst->getAttrOfType<mlir::StringAttr>("instanceName");
            auto modNameAttr = inst->getAttrOfType<mlir::FlatSymbolRefAttr>("moduleName");
            if (!instNameAttr || !modNameAttr)
                continue;
            std::string instName = instNameAttr.getValue().str();
            std::string targetSym = sanitizeEgglogId(modNameAttr.getValue().str());

            std::string connVec = "(vec-of";
            bool anyConn = false;
            for (mlir::Value operand : inst->getOperands()) {
                EggifiedOp* eo = egglog.findEggifiedOp(operand);
                if (eo) {
                    connVec += " " + eo->getPrintId();
                    anyConn = true;
                }
            }
            connVec = anyConn ? (connVec + ")") : "(vec-empty)";

            moduleLets.push_back("(let inst_" + sanitizeEgglogId(mi.name) + "_" +
                                 sanitizeEgglogId(instName) + " (Instance \"" +
                                 instName + "\" mod_" + targetSym + " " + connVec +
                                 "))");
        }
    }

    // ---- Assemble the combined program and run egglog once ----
    runHierarchicalEgglog(egglog.eggifiedBlock, moduleLets);

    // Reuse the flat metadata writer over the combined op list.
    saveExtractionMetadata(egglog.eggifiedBlock, "hierarchical");

    llvm::outs() << "\xE2\x9C\x85 Eggify-only (hierarchical) pass completed!\n";
    llvm::outs() << "   - Modules emitted: " << modules.size() << "\n";
    llvm::outs() << "   - Module-level e-nodes: " << moduleLets.size() << "\n";
    llvm::outs() << "   - E-graph JSON: " << egglogOutputFilename << "\n";
    llvm::outs() << "   - Extraction metadata: " << metadataFilename << "\n";
}

void EggifyOnlyPass::runOnOperation() {
    mlir::ModuleOp module = getOperation();
    if (mlir::failed(validateModuleSelection(module))) {
        signalPassFailure();
        return;
    }

    if (!hierarchicalOpt && !opaqueModules.empty()) {
        module.emitError("--opaque-module requires --hierarchical");
        signalPassFailure();
        return;
    }
    for (const std::string& name : opaqueModules) {
        auto* target = module.lookupSymbol(name);
        if (!target || !llvm::isa<circt::hw::HWModuleOp, circt::hw::HWModuleExternOp>(target)) {
            module.emitError("--opaque-module does not name an HW module: ") << name;
            signalPassFailure();
            return;
        }
    }
    for (auto internal : module.getOps<circt::hw::HWModuleOp>()) {
        auto marker = internal->getAttr("cedar.blackbox");
        if (!marker)
            continue;
        if ((!hierarchicalOpt && optModuleOpt.empty()) || !llvm::isa<mlir::UnitAttr>(marker)) {
            internal.emitError("cedar.blackbox must be a unit attribute and requires --hierarchical");
            signalPassFailure();
            return;
        }
    }

    // Hierarchical emit is a separate, additive code path gated behind
    // --hierarchical. The flat path below remains unchanged.
    if (hierarchicalOpt) {
        runHierarchical(module);
        return;
    }

    // Find all functions and hw modules in the module
    for (mlir::Operation& op: module.getOps()) {
        if (auto funcOp = llvm::dyn_cast<mlir::func::FuncOp>(&op)) {
            if (!optModuleOpt.empty())
                continue;
            llvm::StringRef funcName = funcOp.getName();
            LLVM_DEBUG(llvm::dbgs() << "Running on function: " << funcName << "\n");

            for (mlir::Block& block: funcOp.getRegion().getBlocks()) {
                std::string parentOpName = block.getParentOp()->getName().getStringRef().str();
                std::string blockName = funcName.str() + "_" + parentOpName;
                runOnBlock(block, blockName);
            }
        } else if (auto hwModuleOp = llvm::dyn_cast<circt::hw::HWModuleOp>(&op)) {
            llvm::StringRef moduleName = hwModuleOp.getName();

            if ((!optModuleOpt.empty() && hwModuleOp.getName() != optModuleOpt) ||
                (optModuleOpt.empty() && hwModuleOp.isPrivate())) {
                llvm::outs() << "Skipping private HW module: " << moduleName << " (submodule; flatten design to include)\n";
                continue;
            }

            LLVM_DEBUG(llvm::dbgs() << "Running on HW module: " << moduleName << "\n");

            for (mlir::Block& block: hwModuleOp.getBodyRegion().getBlocks()) {
                std::string parentOpName = block.getParentOp()->getName().getStringRef().str();
                std::string blockName = moduleName.str() + "_" + parentOpName;
                runOnBlock(block, blockName);
            }
        } else {
            llvm::errs() << "Skipping non-function/non-hw-module operation: " << op.getName() << "\n";
        }
    }

    llvm::outs() << "✅ Eggify-only pass completed!\n";
    llvm::outs() << "   - E-graph JSON: " << egglogOutputFilename << "\n";
    llvm::outs() << "   - Extraction metadata: " << metadataFilename << "\n";
    llvm::outs() << "   - MLIR→Egglog time: " << mlirToEgglogTime << "s\n";
    llvm::outs() << "   - Egglog execution time: " << egglogExecTime << "s\n";
}

llvm::LogicalResult EggifyOnlyPass::initialize(mlir::MLIRContext* context) {
    // Initialize eggFilePath from command-line option
    eggFilePath = eggFileOpt;

    // Make sure both files exist
    if (!llvm::sys::fs::exists(mlirFilePath)) {
        llvm::errs() << "MLIR file does not exist: " << mlirFilePath << "\n";
        return llvm::failure();
    }
    if (!llvm::sys::fs::exists(eggFilePath)) {
        llvm::errs() << "Egg file does not exist: " << eggFilePath << "\n";

        // Try to find egg file with the same name as the mlir file
        std::string name = mlirFilePath.substr(0, mlirFilePath.find(".mlir"));
        eggFilePath = name + ".egg";
        if (!llvm::sys::fs::exists(eggFilePath)) {
            return llvm::failure();
        }

        llvm::errs() << "Using default egg file: " << eggFilePath << "\n";
    }

    // Set output filenames
    std::string name = mlirFilePath.substr(0, mlirFilePath.find(".mlir"));
    egglogOutputFilename = name + "-egglog-output.log";
    egglogLogFilename = name + "-egglog.log";
    metadataFilename = name + ".extraction-metadata.txt";

    // Clear metadata file
    std::ofstream metadataFile(metadataFilename, std::ios::trunc);
    metadataFile.close();

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

