#!/bin/bash

design_name="CEDAR_DESIGN_NAME"

# Cedar import: put egg-opt, egglog, circt-* and extract_result on PATH via
# DIALEGG_TOOLS_PATH (previously a hard-coded developer path).
export PATH="${DIALEGG_TOOLS_PATH:+$DIALEGG_TOOLS_PATH:}$PATH"

extract_result ${design_name}.egraph.json > ${design_name}_extracted.txt

# reconstruct
egg-opt --reconstruct-from-extraction \
    --extraction-file=${design_name}_extracted.txt \
    --egg-file=${design_name}.egg \
    ${design_name}.mlir \
    -o ${design_name}_optimized.mlir

# # Step 3: Generate optimized Verilog from MLIR
circt-opt --lower-seq-to-sv ${design_name}_optimized.mlir | circt-opt --export-split-verilog --prettify-verilog --hw-cleanup

mv ${design_name}.sv ${design_name}_optimized.v
