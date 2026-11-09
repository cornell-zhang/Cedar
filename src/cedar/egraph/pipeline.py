# SPDX-License-Identifier: Apache-2.0
"""RTL -> MLIR -> egglog e-graph -> extraction -> MLIR -> Verilog.

Python port of the historical ``run_eqsat.sh`` / ``run_extract_translate.sh``
harness, using an explicitly selected :class:`cedar.toolchain.Toolchain`,
per-step timeouts, structured results and multi-file design input.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..toolchain import Toolchain

LOWER_LLHD_PIPELINE = (
    "builtin.module(hw.module(lower-free-llhd-signals,"
    "lower-loop-carry-processes{lower-llhd-delay-as-zero-cycle=true},"
    "lower-interprocess-llhd-signals))"
)

_MATCH_RE = re.compile(r"Report: Rule \(rule (.*?): search [0-9.]+s, apply [0-9.]+s, num matches (\d+)", re.S)


class PipelineError(RuntimeError):
    def __init__(self, step: str, message: str, log: Optional[str] = None):
        self.step = step
        self.log = log
        super().__init__(f"{step}: {message}")


@dataclass
class StepLog:
    step: str
    cmd: List[str]
    returncode: Optional[int]
    seconds: float
    stderr_tail: str = ""


@dataclass
class EqsatResult:
    work_dir: str
    design_key: str
    mlir: str
    egg_program: str
    ops_egg: str
    egraph_json: str
    egglog_log: str
    n_nodes: int
    n_classes: int
    op_histogram: Dict[str, int]
    rule_matches: List[int]
    widths: List[int]
    steps: List[StepLog] = field(default_factory=list)
    modules: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class ExtractResult:
    work_dir: str
    verilog: str
    extracted_term: str
    steps: List[StepLog] = field(default_factory=list)


def _run(tc: Toolchain, step: str, cmd: Sequence[str], cwd: Path, timeout: int,
         stdout: Optional[Path] = None, logs: Optional[List[StepLog]] = None) -> subprocess.CompletedProcess:
    from .. import proc
    t0 = time.monotonic()
    fh = open(stdout, "w") if stdout else None
    try:
        r = proc.run(list(cmd), cwd=str(cwd), env=tc.env(), timeout=timeout,
                     stdout=fh if fh else subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        if fh:
            fh.close()
    if r.timed_out:
        if logs is not None:
            logs.append(StepLog(step, list(cmd), None, time.monotonic() - t0, "timeout"))
        raise PipelineError(step, "run budget expired" if r.budget_expired else f"timed out after {timeout}s")
    p = subprocess.CompletedProcess(list(cmd), r.returncode, r.stdout, r.stderr)
    if logs is not None:
        logs.append(StepLog(step, list(cmd), p.returncode, round(time.monotonic() - t0, 3), (p.stderr or "")[-2000:]))
    if p.returncode != 0:
        raise PipelineError(step, f"exit {p.returncode}: {(p.stderr or '')[-1500:]}")
    return p


def natural_key(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(s))]


def populate_roots(ops_json: Path, out_json: Path, hierarchical: bool = False) -> dict:
    data = json.loads(ops_json.read_text())
    roots = set()
    for node in data["nodes"].values():
        if node["op"] == "RootNode" or (hierarchical and node["op"] in ("HwModule", "Instance", "Submodule")):
            roots.add(node["eclass"])
    if not roots:
        raise PipelineError("populate-roots", "no RootNode e-classes found")
    data["root_eclasses"] = sorted(roots, key=natural_key)
    out_json.write_text(json.dumps(data))
    return data


def build_program(tc: Toolchain, rules: Sequence[str], iterations: Optional[int], hierarchical: bool = False) -> str:
    text = tc.template("circt.egg").read_text()
    module = tc.template("module.egg").read_text() if hierarchical else ""
    if hierarchical:
        text = text.replace("CEDAR_MODULE_DATATYPE", module)
    else:
        text = text.replace("CEDAR_MODULE_DATATYPE\n\n", "").replace("CEDAR_MODULE_DATATYPE\n", "").replace("CEDAR_MODULE_DATATYPE", "")
    text = text.replace("CEDAR_REWRITE_RULES", "\n".join(r.strip() for r in rules))
    if iterations is not None:
        if "(run-schedule (saturate rules))" not in text:
            raise PipelineError("program", "template has no '(run-schedule (saturate rules))' to bound")
        text = text.replace("(run-schedule (saturate rules))", f"(run-schedule (repeat {int(iterations)} rules))")
    return text


_TYPE_RE = re.compile(r"\((I(\d+))\)|\((?:Int|SInt|UInt) (\d+)\)")
# (OpaqueType is not listed: it carries e.g. !seq.clock in every sequential design.)
_NON_SIGNLESS_RE = re.compile(r"\((SInt|UInt|OtherInt|F16|F32|F64|F80|F128|TF32|Index|Complex|Tuple|Function|"
                              r"Vector|RankedTensor|UnrankedTensor)\b")


def widths_in(text: str) -> List[int]:
    """Bit widths of integer types (signless, signed and unsigned) in an e-graph program."""
    ws = set()
    for m in _TYPE_RE.finditer(text):
        ws.add(int(m.group(2) or m.group(3)))
    return sorted(ws)


def non_signless_types_in(text: str) -> List[str]:
    """Type constructors other than signless integers. Certified rules only
    mention signless integer types, so they never match e-nodes of these."""
    return sorted(set(_NON_SIGNLESS_RE.findall(text)))


def frontend(tc: Toolchain, rtl: Sequence[str], top: str, out_mlir: Path, work: Path,
             include_dirs: Sequence[str] = (), defines: Optional[Dict[str, str]] = None,
             timeout: int = 300, logs: Optional[List[StepLog]] = None, flatten: bool = True) -> None:
    cmd = [tc.tool("circt-verilog"), f"--top={top}"]
    for inc in include_dirs:
        cmd.append(f"-I{inc}")
    for k, v in (defines or {}).items():
        cmd.append(f"-D{k}={v}" if v not in (None, "") else f"-D{k}")
    cmd += [str(Path(f).resolve()) for f in rtl]
    _run(tc, "circt-verilog", cmd, work, timeout, stdout=out_mlir, logs=logs)
    if flatten:
        tmp = out_mlir.with_suffix(".flat.mlir")
        _run(tc, "hw-flatten-modules", [tc.tool("circt-opt"), "--hw-flatten-modules", str(out_mlir), "-o", str(tmp)],
             work, timeout, logs=logs)
        os.replace(tmp, out_mlir)


def eqsat(tc: Toolchain, work_dir: Path, design_key: str, rtl: Sequence[str], top: str,
          rules: Sequence[str] = (), iterations: Optional[int] = None, timeout: int = 300,
          include_dirs: Sequence[str] = (), defines: Optional[Dict[str, str]] = None,
          mlir_in: Optional[Path] = None, hierarchical: bool = False,
          opaque_modules: Sequence[str] = (), opt_module: Optional[str] = None) -> EqsatResult:
    """Run equality saturation; ``design_key`` names all produced files."""
    work = Path(work_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    logs: List[StepLog] = []
    mlir = work / f"{design_key}.mlir"
    if mlir_in is not None:
        shutil.copy(mlir_in, mlir)
    else:
        frontend(tc, rtl, top, mlir, work, include_dirs, defines, timeout, logs, flatten=not hierarchical)
    if hierarchical:
        return _hierarchical_eqsat(tc, work, design_key, mlir, rules, iterations,
                                   timeout, opaque_modules, logs)
    if opaque_modules:
        raise PipelineError("configuration", "opaque_modules requires hierarchical optimization")
    shutil.copy(tc.template("base.egg"), work / "base.egg")
    program = work / f"{design_key}.egg"
    program.write_text(build_program(tc, rules, iterations))
    selector = ["--opt-module=" + opt_module] if opt_module is not None else []
    _run(tc, "egg-opt eggify", [tc.tool("egg-opt"), "--eggify-only", f"--egg-file={program.name}", mlir.name,
                                "-o", f"{design_key}_eggified.mlir"] + selector, work, timeout, logs=logs)
    ops_json = work / f"{design_key}.ops.json"
    if not ops_json.is_file():
        raise PipelineError("egg-opt eggify", f"{ops_json.name} not produced")
    data = populate_roots(ops_json, work / f"{design_key}.egraph.json")
    log_path = work / f"{design_key}-egglog.log"
    log_text = log_path.read_text() if log_path.is_file() else ""
    hist: Dict[str, int] = {}
    classes = set()
    for n in data["nodes"].values():
        hist[n["op"]] = hist.get(n["op"], 0) + 1
        classes.add(n["eclass"])
    ops_egg = work / f"{design_key}.ops.egg"
    return EqsatResult(
        work_dir=str(work), design_key=design_key, mlir=str(mlir), egg_program=str(program),
        ops_egg=str(ops_egg), egraph_json=str(work / f"{design_key}.egraph.json"), egglog_log=str(log_path),
        n_nodes=len(data["nodes"]), n_classes=len(classes), op_histogram=dict(sorted(hist.items())),
        rule_matches=[int(m.group(2)) for m in _MATCH_RE.finditer(log_text)],
        widths=widths_in(ops_egg.read_text() if ops_egg.is_file() else ""), steps=logs,
    )


def _hierarchical_eqsat(tc, work, key, mlir, rules, iterations, timeout, opaque_modules, logs):
    """Independent module e-graphs, combined only for the shared cost model.

    Module boundaries are never rewritten. Namespaced graph IDs prevent
    unrelated values/classes from different modules from merging.
    """
    result = _run(tc, "module inventory", [tc.tool("egg-opt"), mlir.name,
                  "--list-hw-modules", "-o", os.devnull], work, timeout, logs=logs)
    inventory = json.loads(result.stdout)
    names = {m["name"] for m in inventory}
    unknown = set(opaque_modules) - names
    if unknown:
        raise PipelineError("configuration", "unknown opaque modules: " + ", ".join(sorted(unknown)))
    selected = [m["name"] for m in inventory if not m["opaque"] and m["name"] not in opaque_modules]
    if not selected:
        raise PipelineError("hierarchical eggify", "no optimizable internal modules")
    combined = {"nodes": {}, "class_data": {}, "root_eclasses": []}
    modules, op_texts, matches, widths, histogram = [], [], [], set(), {}
    for index, name in enumerate(selected):
        # Filenames use indices: an RTL symbol must never become a path.
        eq = eqsat(tc, work / f"module-{index}", "module", [], name,
                   rules, iterations, timeout, mlir_in=mlir, opt_module=name)
        prefix = f"m{index}/"
        data = json.loads(Path(eq.egraph_json).read_text())
        for nid, node in data["nodes"].items():
            combined["nodes"][prefix + nid] = dict(node, eclass=prefix + node["eclass"],
                                                   children=[prefix + c for c in node["children"]])
        combined["class_data"].update({prefix + cid: v for cid, v in data.get("class_data", {}).items()})
        combined["root_eclasses"].extend(prefix + cid for cid in data["root_eclasses"])
        modules.append({"name": name, "prefix": prefix, "result": eq.to_dict()})
        op_texts.append(Path(eq.ops_egg).read_text())
        matches.extend(eq.rule_matches)
        widths.update(eq.widths)
        for op, count in eq.op_histogram.items():
            histogram[op] = histogram.get(op, 0) + count
        logs.extend(eq.steps)
    graph = work / f"{key}.egraph.json"
    graph.write_text(json.dumps(combined))
    ops = work / f"{key}.ops.egg"
    ops.write_text("\n".join(op_texts))  # Analysis inventory, not an executable program.
    (work / "modules.json").write_text(json.dumps(inventory, indent=2))
    return EqsatResult(str(work), key, str(mlir), "", str(ops), str(graph), "",
                       len(combined["nodes"]), len({n["eclass"] for n in combined["nodes"].values()}),
                       dict(sorted(histogram.items())), matches, sorted(widths), logs, modules)


def extract(tc: Toolchain, eq: EqsatResult, top: str, timeout: int = 300, extractor_timeout: int = 120) -> ExtractResult:
    work = Path(eq.work_dir)
    key = eq.design_key
    logs: List[StepLog] = []
    raw = work / f"{key}_extracted_raw.txt"
    opt = work / f"{key}_optimized.mlir"
    if eq.modules:
        _extract_modules(tc, eq, opt, raw, timeout, extractor_timeout, logs)
    else:
        _run(tc, "extract_result", [tc.tool("extract_result"), "--timeout", str(extractor_timeout), Path(eq.egraph_json).name],
             work, timeout, stdout=raw, logs=logs)
        if not raw.read_text().strip():
            raise PipelineError("extract_result", "empty extraction")
        _run(tc, "egg-opt reconstruct", [tc.tool("egg-opt"), "--reconstruct-from-extraction", f"--extraction-file={raw.name}",
                                         f"--egg-file={Path(eq.egg_program).name}", Path(eq.mlir).name, "-o", opt.name],
             work, timeout, logs=logs)
    lcp = work / f"{key}_optimized.lcp.mlir"
    _run(tc, "egg-opt lower-llhd", [tc.tool("egg-opt"), f"--pass-pipeline={LOWER_LLHD_PIPELINE}", opt.name, "-o", lcp.name],
         work, timeout, logs=logs)
    os.replace(lcp, opt)
    sv = work / f"{key}_optimized.sv.mlir"
    _run(tc, "lower-seq-to-sv", [tc.tool("circt-opt"), "--lower-seq-to-sv", "--hw-memory-sim", opt.name, "-o", sv.name],
         work, timeout, logs=logs)
    out_v = work / f"{key}_optimized.v"
    _run(tc, "export-verilog", [tc.tool("circt-opt"), "--export-verilog", "--prettify-verilog", "--hw-cleanup",
                                sv.name, "-o", os.devnull], work, timeout, stdout=out_v, logs=logs)
    text = out_v.read_text()
    if not re.search(rf"\bmodule\s+{re.escape(top)}\b", text):
        raise PipelineError("export-verilog", f"optimized Verilog lacks module {top}")
    return ExtractResult(str(work), str(out_v), raw.read_text(), logs)


def _extract_modules(tc, eq, output, raw, timeout, extractor_timeout, logs):
    """Replay each weighted component against its own local value IDs."""
    combined = json.loads(Path(eq.egraph_json).read_text())
    current = Path(eq.mlir)
    terms = []
    for module in eq.modules:
        prefix, name = module["prefix"], module["name"]
        sub = module["result"]
        work = Path(sub["work_dir"])
        graph = {"nodes": {}, "class_data": {}, "root_eclasses": []}
        for nid, node in combined["nodes"].items():
            if nid.startswith(prefix):
                graph["nodes"][nid[len(prefix):]] = dict(node, eclass=node["eclass"][len(prefix):],
                                                        children=[c[len(prefix):] for c in node["children"]])
        graph["class_data"] = {cid[len(prefix):]: v for cid, v in combined["class_data"].items() if cid.startswith(prefix)}
        graph["root_eclasses"] = [cid[len(prefix):] for cid in combined["root_eclasses"] if cid.startswith(prefix)]
        weighted = work / "weighted.json"
        weighted.write_text(json.dumps(graph))
        term = work / "extracted.txt"
        _run(tc, "extract module " + name, [tc.tool("extract_result"), "--timeout", str(extractor_timeout), weighted.name],
             work, timeout, stdout=term, logs=logs)
        if not term.read_text().strip():
            raise PipelineError("extract_result", "empty extraction for module " + name)
        source = work / "selected.mlir"
        shutil.copy(current, source)
        reconstructed = work / "reconstructed.mlir"
        _run(tc, "reconstruct module " + name, [tc.tool("egg-opt"), source.name,
             "--reconstruct-from-extraction", "--opt-module=" + name,
             "--extraction-file=" + term.name, "--egg-file=" + sub["egg_program"],
             "-o", reconstructed.name], work, timeout, logs=logs)
        current = reconstructed
        terms.append("; module " + name + "\n" + term.read_text())
    shutil.copy(current, output)
    raw.write_text("\n".join(terms))


def reparse_check(tc: Toolchain, verilog: Path, top: str, work: Path, timeout: int = 120) -> None:
    """The produced Verilog must parse again with the frontend."""
    work.mkdir(parents=True, exist_ok=True)
    frontend(tc, [str(verilog)], top, work / "reparse.mlir", work, timeout=timeout, flatten=False)
