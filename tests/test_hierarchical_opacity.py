# SPDX-License-Identifier: Apache-2.0
"""Exercise module opacity with real egg-opt and egglog binaries."""
import shutil
import subprocess

import pytest

from cedar.egraph.pipeline import build_program


def emit(tmp_path, toolchain, flags=(), marker="", hierarchical=True):
    source = tmp_path / "design.mlir"
    source.write_text('''module {
  hw.module.extern @memory_macro(in %a : i4, out y : i4)
  hw.module @DW_user_arithmetic(in %a : i4, out y : i4) {
    %one = hw.constant 1 : i4
    %sum = comb.add %a, %one : i4
    hw.output %sum : i4
  }
  hw.module @constant_source(out y : i4) {
    %zero = hw.constant 0 : i4
    hw.output %zero : i4
  }
  hw.module @custom_cell(in %a : i4, out y : i4) MARKER {
    %inverted = comb.xor %a, %a : i4
    hw.output %inverted : i4
  }
  hw.module @top(in %a : i4, out y : i4) {
    %mem = hw.instance "mem" @memory_macro(a: %a: i4) -> (y: i4)
    %sum = hw.instance "sum" @DW_user_arithmetic(a: %mem: i4) -> (y: i4)
    %cell = hw.instance "cell" @custom_cell(a: %sum: i4) -> (y: i4)
    hw.output %cell : i4
  }
}
'''.replace("MARKER", marker))
    program = tmp_path / "program.egg"
    program.write_text(build_program(toolchain, [], 1, hierarchical=hierarchical))
    shutil.copy(toolchain.template("base.egg"), tmp_path / "base.egg")
    args = [str(toolchain.tool("egg-opt")), source.name, "--eggify-only",
            "--egg-file=" + program.name, "-o", "preserved.mlir"]
    if hierarchical:
        args.append("--hierarchical")
    return subprocess.run(args + list(flags), cwd=tmp_path, env=toolchain.env(),
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("flags,marker,opaque", [
    ([], "", False),
    (["--opaque-module=custom_cell"], "", True),
    ([], "attributes {cedar.blackbox}", True),
    (["--opaque-module=custom_cell", "--opaque-module=memory_macro"], "", True),
])
def test_module_opacity_is_explicit(tmp_path, toolchain, flags, marker, opaque):
    result = emit(tmp_path, toolchain, flags, marker)
    assert result.returncode == 0, result.stdout + result.stderr
    graph = (tmp_path / "design.ops.egg").read_text()
    assert '(Submodule "memory_macro" (vec-of "a" "y"))' in graph
    assert '(HwModule "DW_user_arithmetic"' in graph
    assert '(HwModule "constant_source"' in graph
    constructor = "Submodule" if opaque else "HwModule"
    assert f'({constructor} "custom_cell"' in graph
    assert ('(comb_xor op' in graph) != opaque
    assert '(Instance "mem" mod_memory_macro' in graph
    preserved = (tmp_path / "preserved.mlir").read_text()
    assert "hw.module.extern @memory_macro" in preserved
    assert "comb.xor" in preserved  # Opaque bodies remain verbatim in MLIR.


@pytest.mark.parametrize("flags,marker,hierarchical,message", [
    (["--opaque-module=missing"], "", True, "does not name an HW module"),
    (["--opaque-module=custom_cell"], "", False, "requires --hierarchical"),
    ([], "attributes {cedar.blackbox = false}", True, "must be a unit attribute"),
    ([], "attributes {cedar.blackbox}", False, "requires --hierarchical"),
    (["--opt-module=missing"], "", False, "does not name an internal HW module"),
    (["--opt-module=memory_macro"], "", False, "does not name an internal HW module"),
    (["--opt-module=custom_cell"], "attributes {cedar.blackbox}", False, "cannot optimize a cedar.blackbox"),
    (["--opt-module=custom_cell"], "", True, "cannot be combined"),
])
def test_invalid_opacity_configuration_fails(tmp_path, toolchain, flags, marker,
                                            hierarchical, message):
    result = emit(tmp_path, toolchain, flags, marker, hierarchical)
    assert result.returncode != 0
    assert message in result.stderr
    assert not (tmp_path / "design.ops.egg").exists()
