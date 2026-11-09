#!/bin/bash

# Add egglog to PATH
# Cedar import: put egg-opt, egglog, circt-* and extract_result on PATH via
# DIALEGG_TOOLS_PATH (previously a hard-coded developer path).
export PATH="${DIALEGG_TOOLS_PATH:+$DIALEGG_TOOLS_PATH:}$PATH"

egg-opt --eq-sat --egg-file=concatvar.egg concatvar.mlir -o concatvar_optimized.mlir