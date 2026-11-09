<!-- SPDX-License-Identifier: Apache-2.0 -->
# Optional hierarchical optimization

The default workflow flattens the design. Enable hierarchy-preserving e-graph
optimization in the design configuration:

```yaml
search:
  hierarchical: true
  opaque_modules: [memory_wrapper, clock_cell]
```

Set `hierarchical: false` (or omit it) to use the default flat flow.
`opaque_modules` is optional; non-empty lists require hierarchical mode.
Symbols must match modules in the lowered design; unknown symbols fail.

The hierarchical flow optimizes every eligible internal module body, including
private children, in its own e-graph. It namespaces their graph IDs for shared
cost feedback, applies the selected weights, and reconstructs each body using
its own local value IDs. Module definitions and instances survive. External
declarations and explicitly opaque bodies are excluded. A unit attribute
`cedar.blackbox` on an internal MLIR module also marks an opaque boundary.
The assembled RTL passes the same whole-design verification and measurement
gates as a flat candidate. Per-module inventory, e-graphs and extraction terms
are recorded in the run directory.

This mode searches within module bodies. It does not inline modules, move
logic across instance boundaries, or apply cross-module rewrite rules. It
replaces the need for a design-specific optimization driver with a generic
workflow; it does not reproduce the paper's specialized measurement procedure.

## Low-level module-graph export

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

`cedar.blackbox` is a unit attribute, not a Boolean. The combined exporter's
`--opaque-module` flag requires `--hierarchical`. A marked internal module
cannot itself be selected for optimization. Opaque bodies are
excluded from equality saturation and retained in the output MLIR; their
interfaces appear as `Submodule` nodes. Internal modules are never classified
from library prefixes or the contents of their bodies.

Supply a hierarchical egglog program containing the module datatype (see
`toolchain/templates/module.egg`). This combined module-graph exporter is
separate from the independent-module workflow above. Its historical
`--hierarchical-reconstruct` option is not a general inverse of combined
emission: it leaves instantiated children untouched and uses block-local IDs.
Use the workflow option for a supported end-to-end round-trip.

The workflow uses `egg-opt --list-hw-modules` to obtain a structured inventory
and `--opt-module=<symbol>` for matching emission/reconstruction of one body.
The latter rejects external, missing and explicitly opaque modules. These
options require a toolchain rebuilt from the current sources.
