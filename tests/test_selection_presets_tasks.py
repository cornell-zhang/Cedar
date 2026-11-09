# SPDX-License-Identifier: Apache-2.0
import json

from cedar.selection import History, Probe, complementary_group, lhs_op_kinds, normalize_rule_name, rank
from cedar.workflow import presets
from cedar.workflow.tasks import Task, TaskOutput, render, validate_outputs


def test_rule_name_normalization():
    assert normalize_rule_name("rules/x.egg") == normalize_rule_name("x") == "x"


def test_history_prioritizes_one_percent_and_context(tmp_path):
    h = History(tmp_path / "h.jsonl")
    feats = {"comb_mul": 2, "comb_add": 1}
    h.append({"rules": ["a.egg", "b"], "verified": True, "score_improvement": 0.005, "features": feats, "technology": "t"})
    h.append({"rules": ["c"], "verified": True, "score_improvement": 0.08, "features": feats, "technology": "t"})
    h.append({"rules": ["d"], "verified": False, "score_improvement": 0.5, "features": feats, "technology": "t"})
    assert h.evidence("a", feats)["prioritized"] is False
    assert h.evidence("c", feats)["prioritized"] is True
    assert h.evidence("d", feats) == {"uses": 0, "prioritized": False}   # unverified ignored
    probes = [Probe("a", 2, 3, ("comb_add",)), Probe("c", 1, 1, ("comb_mul",)), Probe("z", 0, 0, ("comb_xor",))]
    ranked = rank(probes, h, feats, {"technology": "t"})
    assert [p.rule for p, _ in ranked] == ["c", "a"]            # prioritized first; non-applicable dropped


def test_complementary_group_and_exclusion():
    ranked = [(Probe("m1", 1, 1, ("comb_mul",)), {}), (Probe("m2", 1, 1, ("comb_mul",)), {}),
              (Probe("a1", 1, 1, ("comb_add",)), {})]
    g = complementary_group(ranked)
    assert g == ["m1", "a1"]
    g2 = complementary_group(ranked, exclude=[("a1", "m1")])
    assert sorted(g2) != ["a1", "m1"]


def test_lhs_op_kinds():
    assert lhs_op_kinds("(rule ((= ?l (comb_add ?a ?o ?t)) (= ?a (comb_mul ?x ?y ?t))) ((union ?l ?l)))") == ("comb_add", "comb_mul")


def test_contract_status():
    s = presets.resolve({"preset": "paper"})
    c = presets.contract_status(s, 4, 1, 2, attempted=7)
    assert c["status"] == "incomplete" and len(c["problems"]) == 3
    q = presets.resolve({"preset": "quick"})
    assert presets.contract_status(q, 1, 1, 1, 1)["status"] == "met"


def test_prompts_render_and_output_validation(tmp_path):
    txt = render("direct_rewrite", task_id="t1", top="m", n_variants=1, unknown_left_alone="x")
    assert "t1" in txt and "{rtl_files}" in txt
    out = tmp_path / "o.json"
    t = Task("t1", "select_rules", str(tmp_path / "p.md"), [TaskOutput(str(out), "json")])
    assert validate_outputs(t, lambda p: None, lambda p: None) == [f"missing output {out}"]
    out.write_text("{not json")
    assert "invalid JSON" in validate_outputs(t, lambda p: None, lambda p: None)[0]
    out.write_text(json.dumps({"rules": []}))
    assert validate_outputs(t, lambda p: None, lambda p: None) == []
