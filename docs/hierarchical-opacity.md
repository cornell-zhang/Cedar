<!-- SPDX-License-Identifier: Apache-2.0 -->
# Opaque modules in hierarchical emission

The low-level `egg-opt --eggify-only --hierarchical` mode treats
`hw.module.extern` declarations as opaque and emits their port interfaces.
Internal `hw.module` bodies are eligible for optimization regardless of their
names, including constant-output modules and private submodules.

To preserve an internal module as an opaque boundary, pass its exact symbol:

```sh
egg-opt design.mlir --eggify-only --hierarchical \
  --opaque-module=memory_wrapper --opaque-module=clock_cell \
  --egg-file=program.egg -o preserved.mlir
```

The option is repeatable and also accepts comma-separated names. Unknown module
symbols are errors. Alternatively, annotate the internal module in the MLIR:

```mlir
hw.module @memory_wrapper(in %a : i4, out y : i4)
    attributes {cedar.blackbox} {
  hw.output %a : i4
}
```

`cedar.blackbox` is a unit attribute, not a Boolean. Explicit opacity controls
require `--hierarchical` and fail if used with flat emission. Opaque bodies are
excluded from equality saturation and retained in the output MLIR; their
interfaces appear as `Submodule` nodes. Internal modules are never classified
from library prefixes or the contents of their bodies.

Supply a hierarchical egglog program containing the module datatype (see
`toolchain/templates/module.egg`). This is a low-level exporter option; Cedar's
standard Python optimization pipeline continues to use flat emission.
