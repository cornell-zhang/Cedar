#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test of a built Cedar toolchain (standard library only).

Runs RTL -> MLIR -> e-graph (egglog, with one distributivity rule) ->
extraction -> MLIR -> Verilog on a tiny combinational design, then
re-parses the produced Verilog and, when ``circt-lec`` is available, emits an
SMT-LIB equivalence query between the original and optimized modules. If the
``z3`` Python module is importable the query is solved (UNSAT == equivalent).

This exercises every shipped binary and template together; it does not use
any proprietary tool. Exit status is non-zero on any failure.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DESIGN = """\
module smoke_dist(input [7:0] a, input [7:0] b, input [7:0] c, output [7:0] y);
  assign y = a * b + a * c;
endmodule
"""

# a*b + a*c  ->  a*(b+c)   (modular arithmetic; binary-add guard)
RULE = """\
(rule
    ((= ?lhs (comb_add ?m1 ?ops ?t))
     (= 1 (vec-length ?ops))
     (= ?m2 (vec-get ?ops 0))
     (= ?m1 (comb_mul ?a ?bops ?t))
     (= 1 (vec-length ?bops))
     (= ?b (vec-get ?bops 0))
     (= ?m2 (comb_mul ?a ?cops ?t))
     (= 1 (vec-length ?cops))
     (= ?c (vec-get ?cops 0)))
    ((let s (comb_add ?b (vec-push (vec-empty) ?c) ?t))
     (let m (comb_mul ?a (vec-push (vec-empty) s) ?t))
     (union ?lhs m))
    :ruleset rules)
"""

LOWER_PIPELINE = (
    "builtin.module(hw.module(lower-free-llhd-signals,"
    "lower-loop-carry-processes{lower-llhd-delay-as-zero-cycle=true},"
    "lower-interprocess-llhd-signals))"
)


def run(cmd: list[str], cwd: Path, stdout_path: Path | None = None, timeout: int = 300) -> None:
    out = open(stdout_path, "w") if stdout_path else subprocess.PIPE
    try:
        p = subprocess.run(cmd, cwd=cwd, stdout=out, stderr=subprocess.PIPE, text=True, timeout=timeout)
    finally:
        if stdout_path:
            out.close()
    if p.returncode != 0:
        raise RuntimeError(f"command failed ({p.returncode}): {' '.join(cmd)}\n{p.stderr[-4000:]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bin", type=Path, required=True)
    ap.add_argument("--share", type=Path, required=True)
    ap.add_argument("--keep", type=Path, default=None, help="keep work dir here")
    args = ap.parse_args()
    bindir = args.bin.resolve()
    tools = {t: bindir / t for t in ("circt-verilog", "circt-opt", "egg-opt", "egglog", "extract_result")}
    for t, p in tools.items():
        if not os.access(p, os.X_OK):
            print(f"FAIL: missing tool {p}")
            return 1
    env_path = f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ["PATH"] = env_path

    work = Path(tempfile.mkdtemp(prefix="cedar-smoke-")) if args.keep is None else args.keep
    work.mkdir(parents=True, exist_ok=True)
    name = "smoke_dist"
    (work / f"{name}.v").write_text(DESIGN)
    for t in ("base.egg", "circt.egg"):
        shutil.copy(args.share / t, work / t)
    tmpl = (work / "circt.egg").read_text()
    tmpl = tmpl.replace("CEDAR_MODULE_DATATYPE\n", "").replace("CEDAR_REWRITE_RULES", RULE)
    (work / f"{name}.egg").write_text(tmpl)
    result = {"work_dir": str(work), "steps": {}}
    try:
        run([str(tools["circt-verilog"]), f"{name}.v"], work, work / f"{name}.mlir")
        run([str(tools["circt-opt"]), "--hw-flatten-modules", f"{name}.mlir", "-o", f"{name}.flat.mlir"], work)
        os.replace(work / f"{name}.flat.mlir", work / f"{name}.mlir")
        result["steps"]["parse"] = "ok"
        run([str(tools["egg-opt"]), "--eggify-only", f"--egg-file={name}.egg", f"{name}.mlir",
             "-o", f"{name}_eggified.mlir"], work, timeout=300)
        log = (work / f"{name}-egglog.log")
        data = json.loads((work / f"{name}.ops.json").read_text())
        roots = sorted({n["eclass"] for n in data["nodes"].values() if n["op"] == "RootNode"},
                       key=lambda e: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", e)])
        if not roots:
            raise RuntimeError("no RootNode e-classes in e-graph")
        data["root_eclasses"] = roots
        # Prefer the factored form: make multipliers expensive.
        for n in data["nodes"].values():
            op = n["op"].split("(")[0].strip()
            n["cost"] = {"comb_mul": 100.0, "comb_add": 5.0}.get(op, n.get("cost", 1.0))
        (work / f"{name}.egraph.json").write_text(json.dumps(data))
        n_mul_classes = len({n["eclass"] for n in data["nodes"].values() if n["op"] == "comb_mul"})
        result["steps"]["eqsat"] = {"nodes": len(data["nodes"]), "roots": len(roots),
                                    "mul_eclasses": n_mul_classes,
                                    "egglog_log": log.exists()}
        run([str(tools["extract_result"]), f"{name}.egraph.json"], work, work / f"{name}_extracted_raw.txt")
        extracted = (work / f"{name}_extracted_raw.txt").read_text()
        result["steps"]["extract"] = {"muls_in_extraction": extracted.count("comb_mul")}
        run([str(tools["egg-opt"]), "--reconstruct-from-extraction",
             f"--extraction-file={name}_extracted_raw.txt", f"--egg-file={name}.egg",
             f"{name}.mlir", "-o", f"{name}_optimized.mlir"], work)
        run([str(tools["egg-opt"]), f"--pass-pipeline={LOWER_PIPELINE}", f"{name}_optimized.mlir",
             "-o", f"{name}_optimized.lcp.mlir"], work)
        os.replace(work / f"{name}_optimized.lcp.mlir", work / f"{name}_optimized.mlir")
        run([str(tools["circt-opt"]), "--lower-seq-to-sv", "--hw-memory-sim", f"{name}_optimized.mlir",
             "-o", f"{name}_optimized.sv.mlir"], work)
        run([str(tools["circt-opt"]), "--export-verilog", "--prettify-verilog", "--hw-cleanup",
             f"{name}_optimized.sv.mlir", "-o", os.devnull], work, work / f"{name}_optimized.v")
        opt_v = (work / f"{name}_optimized.v").read_text()
        if f"module {name}" not in opt_v:
            raise RuntimeError("optimized Verilog does not contain the top module")
        code = re.sub(r"/\*.*?\*/", "", re.sub(r"//[^\n]*", "", opt_v), flags=re.S)
        n_mul_out = code.count("*")
        result["steps"]["export"] = {"multipliers_in_output": n_mul_out}
        if n_mul_out != 1:
            raise RuntimeError(f"expected the rule+extraction to produce 1 multiplier, found {n_mul_out}")
        # Re-parse the optimized Verilog with the frontend.
        run([str(tools["circt-verilog"]), f"{name}_optimized.v"], work, work / f"{name}_reparsed.mlir")
        result["steps"]["reparse"] = "ok"
        lec = bindir / "circt-lec"
        if os.access(lec, os.X_OK):
            txt = (work / f"{name}_reparsed.mlir").read_text().replace(f"@{name}(", f"@{name}_opt(")
            (work / "opt_renamed.mlir").write_text(txt)
            run([str(lec), f"{name}.mlir", "opt_renamed.mlir", f"-c1={name}", f"-c2={name}_opt",
                 "--emit-smtlib", "-o", "lec.smt2"], work)
            status = "smtlib-emitted"
            try:
                import z3  # type: ignore

                # circt-lec emits "(check-sat)" and "(reset)" commands; loading the
                # file verbatim would let "(reset)" clear the miter assertion.
                smt = re.sub(r"^\((check-sat|reset)\)\s*$", "", (work / "lec.smt2").read_text(), flags=re.M)
                s = z3.Solver()
                s.add(z3.parse_smt2_string(smt))
                r = s.check()
                status = {"unsat": "equivalent", "sat": "NOT-EQUIVALENT"}.get(str(r), f"unknown:{r}")
                if r != z3.unsat:
                    raise RuntimeError(f"equivalence check failed: {status}")
            except ImportError:
                status = "smtlib-emitted (python z3 unavailable; not solved)"
            result["steps"]["lec"] = status
        print(json.dumps(result, indent=2))
        print("SMOKE PASS")
        return 0
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)
        print(json.dumps(result, indent=2))
        print("SMOKE FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
