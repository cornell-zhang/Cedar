# cedar-toolchain

Prebuilt compiler toolchain for [Cedar](https://github.com/cornell-zhang/Cedar):
`circt-verilog`, `circt-opt`, `circt-lec` (CIRCT), `egg-opt` (DialEgg),
`egglog` (egg-smol fork) and `extract_result` (extraction-gym), with the
matching egglog templates (`base.egg`, `circt.egg`, `module.egg`).

* Platform: Linux x86-64, glibc >= 2.28 (manylinux_2_28), CPU baseline x86-64-v2.
* Every binary's source revision is recorded in `cedar-toolchain-manifest.json`;
  license files are under `cedar_toolchain/_licenses/`.
* Cedar resolves this package explicitly (`cedar_toolchain.bin_dir()`); it never
  falls back to binaries found on `PATH`.

`cedar-toolchain info` prints the manifest; `cedar-toolchain path` prints the
binary directory; `cedar-toolchain exec <tool> [args...]` runs a bundled tool.
