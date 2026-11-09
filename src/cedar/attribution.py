# SPDX-License-Identifier: Apache-2.0
"""Netlist-to-e-node physical attribution and critical-partition feedback (A2).

Correspondence chain (each link is recorded with its coverage):

1. **gate -> RTL line**: DC ``report_cross_probing`` maps every mapped cell to a
   file/line of the candidate RTL (``hdlin_enable_rtlinfo``).
2. **RTL line -> e-nodes**: the candidate RTL is CIRCT ``ExportVerilog`` output
   of the extracted term, so each operator token on a line (``*``, ``+``, ``?:``
   ...) corresponds to one extracted e-node of that operator kind. A line with
   ``n`` operator tokens maps to ``n`` e-nodes.
3. **measurements**: per-cell area (DC ``report_reference`` unit areas) and
   per-cell post-route power (PrimeTime ``report_power -cell_power`` with SAIF).

Shared-gate apportionment: a cell mapped to a line with several e-nodes is
split *equally* among them, so every gate's area/power is counted exactly once.
Cells created after synthesis (P&R buffers, fillers) or without line
information remain *unattributed* and lower the reported coverage.

Critical partition: cells on the DC critical path are traced to e-nodes; the
operator kinds on that path receive a delay calibration > 1 proportional to
their measured share of the path delay.

Calibration: per operator kind, ``(measured share of area/power/delay) /
(estimated share)`` from :mod:`cedar.egraph.costmodel`; the next iteration's
node features are multiplied by these factors. Approximations: the equal
split inside a line and the token-level e-node identification are heuristics;
the result is a calibrated *estimate*, not a measured per-e-node power.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .egraph.costmodel import features_for

_TOKENS: List[Tuple[str, str]] = [
    (r"<<<|<<", "comb_shl"), (r">>>", "comb_shrs"), (r">>", "comb_shru"),
    (r"==|!=|<=|>=|(?<![<=>])<(?![<=])|(?<![-=<>])>(?![>=])", "comb_icmp"),
    (r"\*", "comb_mul"), (r"\+", "comb_add"), (r"(?<![\w\])}])-|(?<=[\w\])}])\s*-", "comb_sub"),
    (r"\?", "comb_mux"), (r"&(?!&)", "comb_and"), (r"\|(?!\|)", "comb_or"), (r"\^", "comb_xor"),
]


def _strip_comment(line: str) -> str:
    return re.sub(r"//.*", "", line)


def line_ops(line: str) -> List[str]:
    code = _strip_comment(line)
    code = re.sub(r"\[[^\]]*\]", "", code)          # bit selects / ranges are wiring
    code = re.sub(r"\d+'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+", "0", code)  # literals
    if not re.search(r"\b(assign)\b|<=|=", code):
        return []
    rhs = re.split(r"<=|(?<![=!<>])=(?!=)", code, maxsplit=1)[-1]
    ops: List[str] = []
    for pat, op in _TOKENS:
        n = len(re.findall(pat, rhs))
        ops += [op] * n
    if "seq_firreg" not in ops and re.search(r"<=", code) and not re.search(r"\bassign\b", code):
        ops.append("seq_firreg")
    return ops


def parse_cross_probing(rpt: Path) -> Dict[str, Tuple[str, int]]:
    out: Dict[str, Tuple[str, int]] = {}
    for line in rpt.read_text(errors="replace").splitlines():
        m = re.match(r"^cell\s+(\S+)\s+(\S+)\s+(\d+)\s+\S+\s+(\S+)", line)
        if m:
            out[m.group(4)] = (m.group(2), int(m.group(3)))
    return out


# DC synthetic-operator instance names encode the operator and RTL line,
# e.g. mult_57_U12, add_57_S2_3, sub_12_..., gt_30_... (DesignWare naming).
_DW_NAME = re.compile(r"^(mult|add|sub|sll|srl|sra|ash[lr]|gt|lt|gte|lte|eq|ne|neq)_(\d+)(?:_|$)")
_DW_OP = {"mult": "comb_mul", "add": "comb_add", "sub": "comb_sub", "sll": "comb_shl", "ashl": "comb_shl",
          "srl": "comb_shru", "sra": "comb_shrs", "ashr": "comb_shrs", "gt": "comb_icmp", "lt": "comb_icmp",
          "gte": "comb_icmp", "lte": "comb_icmp", "eq": "comb_icmp", "ne": "comb_icmp", "neq": "comb_icmp"}
_FLOP_REF = re.compile(r"(DFF|SDF|DHL|DLL|LATCH|FF)", re.I)


def cell_origin(cell: str, ref: str, xp: Dict[str, Tuple[str, int]], default_file: Optional[str]):
    """(file, line, op_kind or None, how) for one cell, or None if untraceable."""
    m = _DW_NAME.match(cell)
    if m and default_file:
        return default_file, int(m.group(2)), _DW_OP[m.group(1)], "dc-synthetic-operator-name"
    if cell in xp:
        f, ln = xp[cell]
        return f, ln, ("seq_firreg" if _FLOP_REF.search(ref or "") else None), "dc-cross-probing"
    return None


def parse_cell_refs(rpt: Path) -> Dict[str, str]:
    out = {}
    for line in rpt.read_text(errors="replace").splitlines():
        m = re.match(r"^cell\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(\S+)", line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def parse_ref_area(rpt: Path) -> Dict[str, float]:
    out = {}
    for line in rpt.read_text(errors="replace").splitlines():
        m = re.match(r"^(\S+)\s+\S+\s+([0-9.]+)\s+\d+\s+[0-9.]+", line)
        if m:
            out[m.group(1)] = float(m.group(2))
    return out


def parse_cell_power(rpt: Path) -> Dict[str, float]:
    """Cell -> total power in W."""
    out = {}
    started = False
    for line in rpt.read_text(errors="replace").splitlines():
        if line.startswith("-----"):
            started = True
            continue
        if not started:
            continue
        m = re.match(r"^(\S+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)\s+([0-9.eE+-]+)", line)
        if m:
            out[m.group(1)] = float(m.group(5))
    return out


def parse_critical_cells(rpt: Path) -> List[Tuple[str, float]]:
    cells = []
    for line in rpt.read_text(errors="replace").splitlines():
        m = re.match(r"^\s+(\S+)/\S+\s+\((\S+)\)\s+([0-9.]+)\s+[0-9.]+", line)
        if m:
            cells.append((m.group(1), float(m.group(3))))
    return cells


def _term_estimates(term: str) -> Dict[str, Dict[str, float]]:
    """Estimated feature totals per operator kind for the extracted term."""
    from .rules.z3verify import parse_top_forms

    est: Dict[str, Dict[str, float]] = defaultdict(lambda: {"area": 0.0, "delay": 0.0, "power": 0.0, "count": 0})
    lets: Dict[str, object] = {}
    try:
        forms = parse_top_forms(term)
    except ValueError:
        return {}

    def width(e) -> int:
        if isinstance(e, list) and e:
            if isinstance(e[0], str) and re.fullmatch(r"I\d+", e[0]):
                return int(e[0][1:])
            if e[0] == "Int" and len(e) == 2 and isinstance(e[1], int):
                return e[1]
        return 1

    def walk(e):
        if isinstance(e, list) and e and isinstance(e[0], str):
            head = e[0]
            if head == "let" and len(e) == 3:
                lets[e[1]] = e[2]
            if head.startswith(("comb_", "seq_")):
                k = 1
                for c in e[1:]:
                    if isinstance(c, list) and c and c[0] == "OpVec":
                        k += len(c) - 1
                f = features_for(head, width(e[-1]), k)
                r = est[head]
                r["area"] += f.area
                r["delay"] += f.delay
                r["power"] += f.power
                r["count"] += 1
            for c in e[1:]:
                walk(c)
    for f in forms:
        walk(f)
    return {k: dict(v) for k, v in est.items()}


def attribute_candidate(egraph_json: Path, measure_dir: Path, top: str) -> dict:
    """Attribute measured area/power of one extracted candidate to its e-nodes."""
    synth = measure_dir / "synth" / "results"
    rtl_copy = None
    cp = synth / "cross_probing.rpt"
    if not cp.is_file():
        return {"status": "unavailable", "detail": "no DC cross-probing report"}
    xp = parse_cross_probing(cp)
    refs = parse_cell_refs(cp)
    unit_area = parse_ref_area(synth / "ref.rpt") if (synth / "ref.rpt").is_file() else {}
    cell_pw = parse_cell_power(measure_dir / "power" / "power_cells.rpt") if (measure_dir / "power" / "power_cells.rpt").is_file() else {}
    files = {f for f, _ in xp.values()}
    rtl_lines: Dict[str, List[str]] = {}
    for d in (measure_dir.parents[1] / "candidates" / "extracted", measure_dir.parents[1] / "inputs"):
        for f in files:
            p = d / f
            if p.is_file() and f not in rtl_lines:
                rtl_lines[f] = p.read_text(errors="replace").splitlines()
                rtl_copy = str(p)
    per_enode: Dict[Tuple[str, int, int], dict] = {}
    kind_meas = defaultdict(lambda: {"area": 0.0, "power": 0.0, "delay": 0.0})
    all_cells = set(refs) | set(cell_pw)
    tot_area = sum(unit_area.get(refs.get(c, ""), 0.0) for c in refs)
    tot_power = sum(cell_pw.values())
    att_area = att_power = 0.0
    default_file = next(iter(rtl_lines), None)
    how_counts: Dict[str, int] = defaultdict(int)

    def targets(cell: str):
        o = cell_origin(cell, refs.get(cell, ""), xp, default_file)
        if o is None:
            return None
        f, ln, kind, how = o
        lines = rtl_lines.get(f)
        if not lines or ln < 1 or ln > len(lines):
            return None
        ops = line_ops(lines[ln - 1])
        if kind == "seq_firreg":
            return [(f, ln, "reg", "seq_firreg")], how
        idx = [i for i, op in enumerate(ops) if kind is None or op == kind]
        if not idx:
            if kind is None:
                return None
            return [(f, ln, f"{kind}?", kind)], how      # operator inferred from the cell name only
        return [(f, ln, i, ops[i]) for i in idx], how

    for cell in sorted(all_cells):
        t = targets(cell)
        if t is None:
            how_counts["unattributed"] += 1
            continue
        tg, how = t
        how_counts[how] += 1
        a = unit_area.get(refs.get(cell, ""), 0.0)
        p = cell_pw.get(cell, 0.0)
        att_area += a
        att_power += p
        for f, ln, idx, op in tg:
            e = per_enode.setdefault((f, ln, str(idx)), {"line": ln, "op": op, "area_um2": 0.0, "power_uW": 0.0, "cells": 0})
            e["area_um2"] += a / len(tg)
            e["power_uW"] += p * 1e6 / len(tg)
            e["cells"] += 1
            kind_meas[op]["area"] += a / len(tg)
            kind_meas[op]["power"] += p / len(tg)
    crit = parse_critical_cells(synth / "critical_path.rpt") if (synth / "critical_path.rpt").is_file() else []
    crit_total = sum(d for _, d in crit) or 0.0
    crit_kinds: Dict[str, float] = defaultdict(float)
    for cell, d in crit:
        t = targets(cell)
        if t is None:
            continue
        tg, _ = t
        for _f, _ln, _i, op in tg:
            crit_kinds[op] += d / len(tg)
            kind_meas[op]["delay"] += d / len(tg)
    term_file = Path(egraph_json).with_name(f"{top}_extracted_raw.txt")
    est = _term_estimates(term_file.read_text()) if term_file.is_file() else {}
    calibration: Dict[str, Dict[str, float]] = {}
    for q, meas_total in (("area", att_area), ("power", att_power), ("delay", sum(crit_kinds.values()))):
        est_total = sum(v[q] for v in est.values())
        if meas_total <= 0 or est_total <= 0:
            continue
        for op, v in est.items():
            if v[q] <= 0 or kind_meas.get(op, {}).get(q, 0.0) <= 0:
                continue
            ratio = (kind_meas[op][q] / meas_total) / (v[q] / est_total)
            calibration.setdefault(op, {})[q] = round(max(0.1, min(10.0, ratio)), 4)
    return {
        "status": "ok",
        "rtl": rtl_copy,
        "coverage": {
            "area": round(att_area / tot_area, 4) if tot_area else None,
            "power": round(att_power / tot_power, 4) if tot_power else None,
            "critical_path_delay": round(sum(crit_kinds.values()) / crit_total, 4) if crit_total else None,
        },
        "per_kind_measured": {k: {"area_um2": round(v["area"], 4), "power_uW": round(v["power"] * 1e6, 4),
                                  "critical_delay_libunit": round(v["delay"], 4)} for k, v in kind_meas.items()},
        "per_kind_estimated": est,
        "critical_kinds": {k: round(v, 3) for k, v in crit_kinds.items()},
        "mapping_methods": dict(how_counts),
        "calibration": calibration,
        "per_enode": [dict(v, file=k[0], token=k[2]) for k, v in sorted(per_enode.items())],
        "approximation": "equal split of each cell among the operator tokens of its RTL line; "
                         "post-synthesis cells only (P&R-inserted cells unattributed)",
    }
