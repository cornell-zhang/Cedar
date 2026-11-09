# SPDX-License-Identifier: Apache-2.0
"""Hierarchy-preserving optimization uses real compiler, extractor and LEC."""
import json
from pathlib import Path
import re

import pytest

from cedar.egraph import costmodel, pipeline
from cedar.rules import certify
from cedar.verification import check


RTL = """module first(input [7:0] a,b,c, output [7:0] y);
assign y=a*b+a*c; endmodule
module second(input [7:0] a,b,c, output [7:0] y);
assign y=a*b+a*c; endmodule
module preserved(input [7:0] a,b,c, output [7:0] y);
assign y=a*b+a*c; endmodule
module top(input [7:0] a,b,c,d,e,f, output [7:0] y,z,q);
first u(a,b,c,y); second v(d,e,f,z); preserved w(a,e,f,q); endmodule
"""


@pytest.mark.parametrize("hierarchical", [False, True])
def test_optional_hierarchy_round_trip(tmp_path, toolchain, hierarchical):
    source = tmp_path / "design.v"
    source.write_text(RTL)
    sigs = certify.signatures_from_template(toolchain.template("circt.egg").read_text())
    rule_file = Path(__file__).resolve().parents[1] / "src/cedar/data/rules/mul_factor_common.egg"
    certificate, rule = certify.prove_instance("factor", rule_file.read_text(), 8, sigs)
    assert certificate.admitted
    opaque = ["preserved"] if hierarchical else []
    eq = pipeline.eqsat(toolchain, tmp_path / "run", "top", [str(source)], "top",
                        [rule], 3, hierarchical=hierarchical, opaque_modules=opaque)
    # Cost feedback applies to every independent component, without ID aliasing.
    data = json.loads(Path(eq.egraph_json).read_text())
    if hierarchical:
        assert {m["name"] for m in eq.modules} == {"first", "second", "top"}
        assert len(data["root_eclasses"]) == 3
    else:
        assert not eq.modules
    costmodel.apply_costs(Path(eq.egraph_json), {"area": 1, "power": 0, "delay": 0},
                          costmodel.design_scales(data))
    ex = pipeline.extract(toolchain, eq, "top")
    text = Path(ex.verilog).read_text()
    modules = set(re.findall(r"\bmodule\s+(\w+)", text))
    assert modules == ({"first", "second", "preserved", "top"} if hierarchical else {"top"})
    if hierarchical:
        mlir = (Path(eq.work_dir) / "top_optimized.mlir").read_text()
        for name in ("first", "second"):
            body = re.search(r"hw.module[^@]*@" + name + r"\(.*?\n  }", mlir, re.S)[0]
            assert body.count("comb.mul") == 1  # Both private child bodies were optimized.
        preserved = re.search(r"hw.module[^@]*@preserved\(.*?\n  }", mlir, re.S)[0]
        assert preserved.count("comb.mul") == 2
        assert mlir.count("hw.instance") == 3
    assert check("circt-lec", [str(source)], [ex.verilog], "top", tmp_path / "lec",
                 toolchain=toolchain).status == "passed"


def test_unknown_opaque_module_fails(tmp_path, toolchain):
    source = tmp_path / "design.v"
    source.write_text(RTL)
    with pytest.raises(pipeline.PipelineError, match="unknown opaque modules"):
        pipeline.eqsat(toolchain, tmp_path / "run", "top", [str(source)], "top",
                       iterations=1, hierarchical=True, opaque_modules=["missing"])
