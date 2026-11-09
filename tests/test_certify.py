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
