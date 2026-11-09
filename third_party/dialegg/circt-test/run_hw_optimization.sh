#!/bin/bash

# SystemVerilog to Optimized MLIR Pipeline using CIRCT + DialEgg
# Usage: ./run_hw_optimization.sh <systemverilog_file>

if [ $# -ne 1 ]; then
    echo "Usage: $0 <systemverilog_file>"
    echo "Example: $0 simplemux.sv"
    exit 1
fi

SV_FILE=$1
BASE_NAME=$(basename "$SV_FILE" .sv)

echo "🚀 Running Hardware Optimization Pipeline"
echo "Input: $SV_FILE"
echo ""

# Step 1: SystemVerilog to MLIR (HW dialect)
echo "Step 1: Converting SystemVerilog to MLIR..."
circt-verilog "$SV_FILE" --ir-hw > "${BASE_NAME}_hw.mlir"
if [ $? -ne 0 ]; then
    echo "❌ Error: Failed to convert SystemVerilog to MLIR"
    exit 1
fi
echo "✅ Generated: ${BASE_NAME}_hw.mlir"

# Step 2: Convert Comb operations to Arith
echo ""
echo "Step 2: Converting Comb operations to Arith dialect..."
circt-opt --convert-comb-to-arith "${BASE_NAME}_hw.mlir" > "${BASE_NAME}_arith.mlir"
if [ $? -ne 0 ]; then
    echo "❌ Error: Failed to convert Comb to Arith"
    exit 1
fi
echo "✅ Generated: ${BASE_NAME}_arith.mlir"

# Step 3: Manual conversion to func dialect (simplified for demo)
echo ""
echo "Step 3: Note - For full automation, implement HW → Func conversion"
echo "   For now, manually convert hw.module → func.func if needed"

# Step 4: Run DialEgg optimization (if func version exists)
FUNC_FILE="${BASE_NAME}_func.mlir"
if [ -f "$FUNC_FILE" ]; then
    echo ""
    echo "Step 4: Running DialEgg equality saturation..."
    # Cedar import: put egg-opt, egglog, circt-* and extract_result on PATH via
    # DIALEGG_TOOLS_PATH (previously a hard-coded developer path).
    export PATH="${DIALEGG_TOOLS_PATH:+$DIALEGG_TOOLS_PATH:}$PATH"
    ../build-release/egg-opt --eq-sat --egg-file=arith_rules.egg "$FUNC_FILE" > "${BASE_NAME}_optimized.mlir"
    if [ $? -eq 0 ]; then
        echo "✅ Generated: ${BASE_NAME}_optimized.mlir"
        echo ""
        echo "🎉 Optimization complete!"
        echo ""
        echo "📊 Results:"
        echo "Original SystemVerilog: $SV_FILE"
        echo "HW Dialect MLIR: ${BASE_NAME}_hw.mlir"
        echo "Arith Dialect MLIR: ${BASE_NAME}_arith.mlir"
        echo "Optimized MLIR: ${BASE_NAME}_optimized.mlir"
    else
        echo "❌ Error: DialEgg optimization failed"
    fi
else
    echo ""
    echo "⚠️  No func dialect version found (${FUNC_FILE})"
    echo "   Create manually from ${BASE_NAME}_arith.mlir to complete the pipeline"
fi

echo ""
echo "🏁 Pipeline complete!"
echo ""
echo "Files generated in circt-test/:"
ls -la "${BASE_NAME}"*.mlir 2>/dev/null || echo "No MLIR files generated" 