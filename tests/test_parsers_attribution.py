# SPDX-License-Identifier: Apache-2.0
"""Report parsers fail closed (no fabricated metrics or passes)."""
from pathlib import Path

from cedar import attribution as A
from cedar.eda import flow
from cedar.eda.runner import StageResult

PT = """Report : Averaged Power
  Net Switching Power  = 1.314e-05   (23.67%)
Total Power            = 5.553e-05  (100.00%)
"""
DC_TIMING = """  data arrival time                                                    340.71
  data required time                                                  1159.33
  data arrival time                                                   -340.71
  slack (MET)                                                          818.62
"""
INNOVUS_TIMING = "= Slack Time                  294.652\n"


def w(tmp_path: Path, name: str, text: str) -> Path:
    p = tmp_path / name
    p.write_text(text)
    return p


def test_power_and_timing(tmp_path):
    assert flow.parse_pt_power(w(tmp_path, "p.rpt", PT)) == 5.553e-05
    assert flow.parse_pt_power(w(tmp_path, "e.rpt", "nothing here")) is None
    t = flow.parse_timing(w(tmp_path, "t.rpt", DC_TIMING))
    assert t == {"arrival": 340.71, "slack": 818.62, "met": True}
    assert flow.parse_timing(w(tmp_path, "i.rpt", INNOVUS_TIMING))["slack"] == 294.652
    assert flow.parse_saif_coverage(w(tmp_path, "pt.log", "Number of annotated nets = 727 (100.00%)\n")) == 100.0


def test_power_metric_fails_closed(tmp_path):
    r = StageResult("power", "ok", outputs={"power_rpt": str(w(tmp_path, "p.rpt", "garbage"))})
    m = flow.power_metric(r)
    assert not m.ok and m.value is None and m.status == "missing"
    m2 = flow.power_metric(StageResult("power", "failed", detail="license"))
    assert m2.status == "failed"


def _conf(tmp_path, ver: str, log: str, status="ok"):
    v = w(tmp_path, "verification.rpt", ver)
    lg = w(tmp_path, "conformal.log", log)
    return flow.parse_conformal(StageResult("equivalence", status, log=str(lg),
                                            outputs={"verification_rpt": str(v), "log": str(lg)}))


def test_conformal_parsing(tmp_path):
    ok = _conf(tmp_path, "2. Incomplete verification: 0\n6. Compare Results: PASS\n", "Equivalent           4      4         8\n")
    assert ok["status"] == "passed" and ok["points"] == 8
    fail = _conf(tmp_path, "6. Compare Results: FAIL\n", "Equivalent 1 1 2\nNon-equivalent 15 0 15\n", "failed")
    assert fail["status"] == "failed"
    inc = _conf(tmp_path, "2. Incomplete verification: 3\n6. Compare Results: PASS\n", "Equivalent 4 4 8\n")
    assert inc["status"] == "failed"
    none = _conf(tmp_path, "", "", "failed")
    assert none["status"] == "error"
    unav = flow.parse_conformal(StageResult("equivalence", "unavailable", detail="lec not found"))
    assert unav["status"] == "unavailable"


def test_line_ops_and_cell_origin():
    assert sorted(A.line_ops("  assign y = (c + b) * a;  // x.mlir:3")) == ["comb_add", "comb_mul"]
    assert A.line_ops("    y_0 <= ~rst ? ({8'h0, b} + {8'h0, c}) * {8'h0, a} : 16'h0;").count("comb_mux") == 1
    assert A.line_ops("module foo(input a);") == []
    xp = {"y_reg_0_": ("d.v", 5), "U3": ("d.v", 7)}
    assert A.cell_origin("mult_57_U12", "FAx1", xp, "d.v")[:3] == ("d.v", 57, "comb_mul")
    assert A.cell_origin("y_reg_0_", "DFFHQNx1_ASAP7_75t_R", xp, "d.v")[2] == "seq_firreg"
    assert A.cell_origin("U3", "NAND2xp5", xp, "d.v")[2] is None
    assert A.cell_origin("FE_DBTN0", "BUF", xp, "d.v") is None
