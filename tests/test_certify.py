# SPDX-License-Identifier: Apache-2.0
from importlib import resources

import pytest

from cedar.rules import certify as C

GOOD = """
(rule
    ((= ?lhs (comb_add ?m1 ?ops ?t)) (= 1 (vec-length ?ops)) (= ?m2 (vec-get ?ops 0))
     (= ?m1 (comb_mul ?a ?bops ?t)) (= 1 (vec-length ?bops)) (= ?b (vec-get ?bops 0))
     (= ?m2 (comb_mul ?a ?cops ?t)) (= 1 (vec-length ?cops)) (= ?c (vec-get ?cops 0)))
    ((let s (comb_add ?b (vec-push (vec-empty) ?c) ?t))
     (let m (comb_mul ?a (vec-push (vec-empty) s) ?t))
     (union ?lhs m))
    :ruleset rules)
"""
BAD = GOOD.replace("(let s (comb_add ?b", "(let s (comb_sub ?b")
UNGUARDED = GOOD.replace("(= 1 (vec-length ?ops)) ", "")


def test_good_rule_proved_at_exact_widths(template_sigs):
    for w in (8, 16, 33):
        c, inst = C.prove_instance("good", GOOD, w, template_sigs)
        assert c.status == "PROVED", c.detail
        assert C.type_term(w) in inst and "?t" not in inst


def test_wrong_rule_refuted_but_true_at_one_bit(template_sigs):
    # a*b + a*c == a*(b-c) holds modulo 2 but not modulo 2^8: a proof at one
    # width must never be reused for another width.
    assert C.prove_instance("bad", BAD, 1, template_sigs)[0].status == "PROVED"
    assert C.prove_instance("bad", BAD, 8, template_sigs)[0].status == "REFUTED"


def test_unguarded_variadic_vector_rejected(template_sigs):
    c, _ = C.prove_instance("unguarded", UNGUARDED, 8, template_sigs)
    assert c.status == "UNSUPPORTED" and "vec-length" in c.detail


def test_banned_and_malformed(template_sigs):
    banned = GOOD.replace("comb_mul ?a ?bops", "seq_firreg ?a ?bops")
    assert C.prove_instance("b", banned, 8, template_sigs)[0].status == "REJECTED"
    assert C.prove_instance("m", "(rule ((= ?x", 8, template_sigs)[0].status == "REJECTED"
    two = GOOD + GOOD
    assert C.prove_instance("two", two, 8, template_sigs)[0].status == "REJECTED"


def test_multi_type_variable_unsupported(template_sigs):
    rule = """(rule ((= ?lhs (comb_extract ?x ?attr ?t1)) (= ?x (comb_add ?a ?ops ?t2))
                    (= 1 (vec-length ?ops)) (= ?b (vec-get ?ops 0)))
                   ((union ?lhs ?lhs)) :ruleset rules)"""
    c, _ = C.prove_instance("multi", rule, 8, template_sigs)
    assert c.status == "UNSUPPORTED"


def test_max_width_and_cache_invalidation(tmp_path, template_sigs):
    assert C.prove_instance("good", GOOD, 512, template_sigs, max_width=256)[0].status == "UNSUPPORTED"
    store = C.CertificateStore(tmp_path)
    adm = C.admit({"good": GOOD}, [8], store, template_sigs)
    assert [i.width for i in adm.admitted] == [8]
    assert store.get(C.rule_sha(GOOD), 8) is not None
    edited = GOOD.replace("comb_add ?b", "comb_sub ?b")
    assert store.get(C.rule_sha(edited), 8) is None          # edit -> must re-prove
    adm2 = C.admit({"good": edited}, [8], store, template_sigs)
    assert not adm2.admitted and adm2.rejected[0].status == "REFUTED"


def test_seed_library_proves(template_sigs):
    rules = {f.name[:-4]: f.read_text() for f in resources.files("cedar").joinpath("data/rules").iterdir()
             if f.name.endswith(".egg")}
    assert len(rules) >= 4
    adm = C.admit(rules, [1, 8, 16], None, template_sigs)
    assert not adm.rejected, adm.summary()
    assert len(adm.admitted) == 3 * len(rules)


def test_vector_read_in_action_counts_for_guard(template_sigs):
    # Agent-proposed rule from a regression run: the operand vector is guarded in
    # the premises and read (vec-get) only in the action.
    rule = """(rule ((= ?m (comb_mux ?s (comb_mul ?a ?ops ?t) ?z ?t))
       (= ?z (hw_constant (NamedAttr "value" (IntegerAttr 0 ?t)) ?t))
       (= 1 (vec-length ?ops)))
      ((union ?m (comb_mul (comb_mux ?s ?a ?z ?t) (vec-push (vec-empty) (vec-get ?ops 0)) ?t)))
      :ruleset rules)"""
    for w in (1, 8):
        c, _ = C.prove_instance("mux_mul_gate", rule, w, template_sigs)
        assert c.status == "PROVED", c.detail
    # At 16 bits Z3 does not finish within the budget: the instance is UNKNOWN
    # and therefore not admitted (no inference from the 8-bit proof).
    c16, _ = C.prove_instance("mux_mul_gate", rule, 16, template_sigs, timeout_ms=2000)
    assert c16.status == "UNKNOWN" and not c16.admitted
    wrong = rule.replace("(IntegerAttr 0 ?t)", "(IntegerAttr 1 ?t)")
    assert C.prove_instance("wrong", wrong, 8, template_sigs)[0].status == "REFUTED"
    unguarded = rule.replace("(= 1 (vec-length ?ops))", "")
    assert C.prove_instance("ung", unguarded, 8, template_sigs)[0].status == "UNSUPPORTED"


def test_review_counterexamples_rejected(template_sigs):
    # rewrite forms cannot length-guard a matched operand vector
    rw = "(rewrite (comb_add ?a ?ops ?t) (comb_add (vec-get ?ops 0) (vec-push (vec-empty) ?a) ?t) :ruleset rules)"
    for w in (1, 8):
        assert C.prove_instance("rw", rw, w, template_sigs)[0].status == "UNSUPPORTED"
    # operand width fixed only through the union / RHS, attribute type counted as data
    ex = '(rewrite (comb_extract ?x (NamedAttr "lowBit" (IntegerAttr 0 (I32))) ?t) ?x :ruleset rules)'
    ex1 = '(rewrite (comb_extract ?x (NamedAttr "lowBit" (IntegerAttr 0 (I32))) (I1)) ?x :ruleset rules)'
    for rule in (ex, ex1):
        for w in (1, 8, 32):
            assert C.prove_instance("ex", rule, w, template_sigs)[0].status != "PROVED"
    ne = """(rule ((= ?l (comb_icmp ?a ?b (NamedAttr "predicate" (IntegerAttr 1 (I64))) ?t)))
                 ((union ?l (comb_xor ?a (vec-push (vec-empty) ?b) ?t))) :ruleset rules)"""
    assert C.prove_instance("ne", ne, 1, template_sigs)[0].status != "PROVED"
    assert C.type_variables(ex1, template_sigs)[1] == [1]          # attribute I32 is not a data width


# --- review regressions: unchecked egglog commands must never be admitted ---

_FALSE_UNION = ('(union (hw_constant (NamedAttr "value" (IntegerAttr 0 (I8))) (I8)) '
                '(hw_constant (NamedAttr "value" (IntegerAttr 1 (I8))) (I8)))')
_CONSTS_EQUAL = ('(check (= (hw_constant (NamedAttr "value" (IntegerAttr 0 (I8))) (I8)) '
                 '(hw_constant (NamedAttr "value" (IntegerAttr 1 (I8))) (I8))))')


def _seed(name):
    return resources.files("cedar").joinpath(f"data/rules/{name}.egg").read_text()


def _bypass_variants():
    add = _seed("add_commute")
    return {
        "extra top-level union": add + "\n" + _FALSE_UNION + "\n",
        "extra top-level set": add + "\n(set (comb_add (Value 0 (I8)) (vec-empty) (I8)) (Value 0 (I8)))\n",
        "set action beside union": add.replace("(union ?lhs s))", "(union ?lhs s)\n     (set (comb_add ?a ?ops ?t) ?a))"),
        "second union action": add.replace("(union ?lhs s))", "(union ?lhs s)\n     (union ?a ?b))"),
        "panic action": add.replace("(union ?lhs s))", "(union ?lhs s)\n     (panic \"x\"))"),
        "nested command in let": add.replace("(let s (comb_add", "(let s (set (comb_add"),
        "non-equality premise": add.replace("(= 1 (vec-length ?ops))", "(!= ?a ?b) (= 1 (vec-length ?ops))"),
        "foreign ruleset": add.replace(":ruleset rules", ":ruleset other"),
        "subsume keyword": add.replace(":ruleset rules", ":ruleset rules :subsume true"),
        "rewrite with when": "(rewrite (comb_mul ?a ?ops ?t) ?a :when ((= ?a ?a)) :ruleset rules)",
    }


def test_unchecked_commands_rejected(template_sigs):
    for label, text in _bypass_variants().items():
        assert C.validate_rule_text(text)[0] is None, label
        c, inst = C.prove_instance(label, text, 8, template_sigs)
        assert c.status == "REJECTED" and inst is None, (label, c.status, c.detail)
        adm = C.admit({"x": text}, [1, 8, 16], None, template_sigs)
        assert not adm.admitted, label


def test_admitted_instance_is_canonical_single_rule(template_sigs):
    text = "; comment (union a b)\n" + _seed("add_commute") + "\n; trailing (set x y)\n"
    c, inst = C.prove_instance("add_commute", text, 8, template_sigs)
    assert c.status == "PROVED"
    forms = __import__("cedar.rules.z3verify", fromlist=["x"]).parse_top_forms(inst)
    assert len(forms) == 1 and forms[0][0] == "rule" and ";" not in inst and "(I8)" in inst


def test_injection_has_no_effect_in_real_egglog(tmp_path, toolchain):
    """Execute admitted instances with the real egglog binary: the false
    constant equality must not hold. The control (raw injected text) shows the
    check would detect the bypass."""
    import shutil
    import subprocess

    from cedar.egraph import pipeline
    sigs = C.signatures_from_template(toolchain.template("circt.egg").read_text())
    shutil.copy(toolchain.template("base.egg"), tmp_path / "base.egg")

    def egglog_check(rule_texts, name):
        prog = pipeline.build_program(toolchain, rule_texts, 1) + "\n" + _CONSTS_EQUAL + "\n"
        (tmp_path / f"{name}.egg").write_text(prog)
        return subprocess.run([toolchain.tool("egglog"), f"{name}.egg"], cwd=tmp_path, capture_output=True,
                              text=True, timeout=120).returncode

    injected = _bypass_variants()["extra top-level union"]
    assert egglog_check([injected], "control") == 0          # raw text: constants (wrongly) equal
    clean = C.admit({"add_commute": _seed("add_commute")}, [8], None, sigs)
    assert egglog_check([i.text for i in clean.admitted], "clean") != 0
    adm = C.admit({"add_commute": injected}, [8], None, sigs)
    assert not adm.admitted
    assert egglog_check([i.text for i in adm.admitted], "admitted") != 0


# --- review regressions: distinct egglog symbols must stay distinct in the proof ---

def _c8(n):
    return f'(hw_constant (NamedAttr "value" (IntegerAttr {n} (I8))) (I8))'


_NAME_CASES = {
    # rule text, a concrete LHS term, the RHS the (false) rule would equate it to
    "rewrite_uses_lhs": (f"(rewrite (comb_add ?lhs (vec-of {_c8(1)}) (I8)) ?lhs :ruleset rules)",
                         f"(comb_add {_c8(2)} (vec-of {_c8(1)}) (I8))", _c8(2)),
    "bare_let_vs_pattern_var": ("(rule ((= ?lhs (comb_add ?a ?ops (I8))) (= 1 (vec-length ?ops)) (= ?b (vec-get ?ops 0))) "
                                "((let a ?b) (union ?lhs (comb_add a (vec-of ?b) (I8)))) :ruleset rules)",
                                f"(comb_add {_c8(1)} (vec-of {_c8(2)}) (I8))",
                                f"(comb_add {_c8(2)} (vec-of {_c8(2)}) (I8))"),
    "question_marks": ("(rule ((= ?lhs (comb_add ?a ?ops (I8))) (= 1 (vec-length ?ops)) (= ??a (vec-get ?ops 0))) "
                       "((union ?lhs (comb_add ?a (vec-of ?a) (I8)))) :ruleset rules)",
                       f"(comb_add {_c8(1)} (vec-of {_c8(2)}) (I8))",
                       f"(comb_add {_c8(1)} (vec-of {_c8(1)}) (I8))"),
}


def _egglog_holds(toolchain, tmp_path, rule_texts, lhs, rhs, name):
    import shutil
    import subprocess

    from cedar.egraph import pipeline
    d = tmp_path / name
    d.mkdir()
    shutil.copy(toolchain.template("base.egg"), d / "base.egg")
    prog = (pipeline.build_program(toolchain, [], 1) + "\n" + "\n".join(rule_texts)
            + f"\n(let sample {lhs})\n(run rules 2)\n(check (= {lhs} {rhs}))\n")
    (d / "p.egg").write_text(prog)
    return subprocess.run([toolchain.tool("egglog"), "p.egg"], cwd=d, capture_output=True, text=True,
                          timeout=120).returncode == 0


@pytest.mark.parametrize("case", sorted(_NAME_CASES))
def test_symbol_collisions_cannot_admit_false_rules(case, tmp_path, toolchain, template_sigs):
    rule, lhs, rhs = _NAME_CASES[case]
    c, inst = C.prove_instance(case, rule, 8, template_sigs)
    assert c.status != "PROVED", (case, c.detail)
    adm = C.admit({case: rule}, [8], None, template_sigs)
    assert not adm.admitted
    # control: executing the raw rule really equates lhs and rhs (it is false) ...
    assert _egglog_holds(toolchain, tmp_path, [rule], lhs, rhs, "control")
    # ... while nothing admitted makes it hold
    assert not _egglog_holds(toolchain, tmp_path, [i.text for i in adm.admitted], lhs, rhs, "admitted")


def test_renamed_correct_rule_still_proves_and_runs(tmp_path, toolchain, template_sigs):
    # x + 0 -> x written with the awkward names (?lhs in a rewrite, ??-variables, let names
    # equal to pattern-variable spellings): true, so it must still be proved and work in egglog.
    good = {
        "rw": f"(rewrite (comb_add ?lhs (vec-of {_c8(0)}) (I8)) ?lhs :ruleset rules)",
        "lets": ("(rule ((= ?lhs (comb_add ?a ?ops (I8))) (= 1 (vec-length ?ops)) (= ??a (vec-get ?ops 0))) "
                 "((let a ??a) (union ?lhs (comb_add a (vec-of ?a) (I8)))) :ruleset rules)"),
    }
    for name, rule in good.items():
        c, inst = C.prove_instance(name, rule, 8, template_sigs)
        assert c.status == "PROVED", (name, c.detail)
        assert "?cv" in inst and "??" not in inst
    adm = C.admit({"rw": good["rw"]}, [8], None, template_sigs)
    x = _c8(5)
    assert _egglog_holds(toolchain, tmp_path, [i.text for i in adm.admitted],
                         f"(comb_add {x} (vec-of {_c8(0)}) (I8))", x, "good")
