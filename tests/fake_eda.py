# SPDX-License-Identifier: Apache-2.0
"""Stand-in EDA tools for measurement-gate tests (no licenses needed).

Invoked as ``fake_eda.py <tool> <args...>`` from a tool profile. Each fake
reads the script Cedar generated, announces the same steps, writes plausible
outputs and logs in the formats of the real tools, and exits 0 -- like the
real tools do after most command failures. Faults are injected with
``CEDAR_FAKE_FAULT=<fault>[@<name>]``: the fault applies only when the working
directory path contains ``<name>`` (e.g. one candidate's measurement).
"""

import os
import re
import stat
import sys
from pathlib import Path


def fault() -> str:
    spec = os.environ.get("CEDAR_FAKE_FAULT", "")
    if not spec:
        return ""
    kind, _, where = spec.partition("@")
    return kind if not where or f"/{where}/" in os.getcwd() + "/" else ""


def steps(text: str, pattern: str):
    return list(dict.fromkeys(re.findall(pattern, text)))


def dc(args):
    tcl = Path(args[args.index("-f") + 1]).read_text()
    res = Path("results")
    res.mkdir(exist_ok=True)
    for st in steps(tcl, r'CEDAR_STEP (\w+)'):
        print(f'puts "CEDAR_STEP {st}"\nCEDAR_STEP {st}')
        if st == "constraints" and fault() == "dc_sdc":
            print("Error: unknown command 'bogus_constraint' (CMD-005)")
        if st == "compile" and fault() == "dc_compile":
            print("Error: Cannot find the design 'm' in the library 'WORK'. (UID-109)")
        print("Warning: Design 'm' has '1' unresolved references. (LINT-1)")
    (res / "netlist.v").write_text("module m(input clk, output y); endmodule\n")
    (res / "area.rpt").write_text("Total cell area:              100.000000\n")
    (res / "timing.rpt").write_text("  data arrival time    340.71\n  slack (MET)    818.62\n")
    (res / "power.rpt").write_text("Total Dynamic Power    =   1.0000 uW\nCell Leakage Power     =   0.1000 uW\n")
    print("CEDAR_DC_DONE")


def innovus(args):
    tcl = Path(args[args.index("-files") + 1]).read_text()
    for st in steps(tcl, r"cedar_run (\w+)"):
        print(f"<CMD> set something /x/yCEDAR_STEP {st}" if st == "init" else f"CEDAR_STEP {st}")
        if st == "init" and fault() == "innovus_sdc_skipped":
            print("WARNING (CTE-25): Line: 6 of File constraints.sdc : Skipped unsupported command: bogus")
        if st == "init" and fault() == "innovus_sdc":
            print("**ERROR: (TCLCMD-917):\tCannot find 'ports' that match 'bogus' (File constraints.sdc, Line 4).")
        if st == "power_route":
            print("**ERROR: (IMPTR-2101):\tLayer M10: Pitch=11520x9 is still less than min width=32000 + min spacing=640.")
        if st == "extract" and fault() == "innovus_extract":
            print("**ERROR: (IMPEXT-2773):\tThe extraction failed.")
        print("**WARN: (IMPSP-9025):\tNo scan chain specified/traced.")
    Path("post_route.v").write_text("module m(input clk, output y); endmodule\n")
    if fault() != "innovus_no_spef":
        Path("post_route.spef").write_text('*SPEF "IEEE 1481-1998"\n*D_NET n1 0.1\n*END\n')
    Path("post_route.sdf").write_text('(DELAYFILE (CELL (CELLTYPE "INV") (INSTANCE u1)))\n')
    Path("post_route_timing.rpt").write_text("= Slack Time    294.652\n")
    print("CEDAR_INNOVUS_DONE")


def vcs(args):
    sdf_arg = args[args.index("-sdf") + 1]
    _, scope, sdf = sdf_arg.split(":", 2)
    out = ["   ***   $sdf_annotate() version 1.2R"]
    if fault() == "sdf_aborted":
        out += ["Warning-[SDFCOM_PSIIC] Precompiled SDF file is incomplete", "          Total errors: 0",
                "          Total warnings: 1", "   ***    SDF annotation aborted: today"]
        info = "# Annotated entries in elaborated design:\n#\tNo information read.\n"
    elif not Path(sdf).is_file():
        out += ["Warning-[SDFCOM_CNOF] Cannot Open File", "  SDF Error: Cannot open SDF file ", f"  {sdf}",
                "          Total errors: 1", "   ***    SDF annotation aborted: today"]
        info = "# Annotated entries in elaborated design:\n#\tNo information read.\n"
    else:
        out += [f'   ***    SDF file: "{sdf}"', f"   ***    Annotation scope: {scope}",
                "   ***    SDF annotation begin: today",
                "Warning-[SDFCOM_UHICD] Up-hierarchy Interconnect Delay ignored",
                "          Total errors: 0", "          Total warnings: 3", "   ***    SDF annotation completed: today"]
        info = ("# Annotated entries in elaborated design:\n#  IOPATH Delays       =    3641\n"
                "#  Annotated instances =     599\n#  Total instances     =     607\n"
                "# Static entries in elaborated design:\n#  IOPATH Delays       =    3306\n")
    Path("sdfAnnotateInfo").write_text(info)
    simv = Path("simv_gl")
    simv.write_text("#!/bin/sh\necho '$date today $end' > waves.vcd\necho 'simulation done' > sim.log\n")
    simv.chmod(simv.stat().st_mode | stat.S_IXUSR)
    print("\n".join(out))


def vcd2saif(args):
    Path(args[args.index("-output") + 1]).write_text("(SAIFILE (INSTANCE DUT))\n")
    print("generating backward SAIF file: waves.saif")


def pt(args):
    tcl = Path(args[args.index("-f") + 1]).read_text()
    plog = Path(re.search(r"parasitics_log_file \{([^}]*)\}", tcl).group(1))
    spef = re.search(r"read_parasitics -format spef \{([^}]*)\}", tcl).group(1)
    for st in steps(tcl, r'CEDAR_STEP (\w+)'):
        print(f'puts "CEDAR_STEP {st}"\nCEDAR_STEP {st}')
        if st == "constraints" and fault() == "pt_sdc":
            print("Error: unknown command 'bogus_constraint' (CMD-005)")
        if st == "activity":
            print("Number of annotated nets = 727 (100.00%)")
        print("Warning: Some timing arcs have been disabled for breaking timing loops. (PTE-003)")
    bad = fault() == "pt_spef_bad" or not Path(spef).is_file()
    if not Path(spef).is_file():
        plog.write_text(f"Error: Cannot open file '{spef}'. (PARA-001)\n")
    else:
        n_err, nets = (1, 0) if bad else (0, 683)
        msg = "Error: Expected keyword *SPEF but found 'X'\n\tat line 1. (SPFP-100)\n" if bad else ""
        plog.write_text(f"{msg}Report : read_parasitics {spef}\n        {n_err} error(s)\n"
                        f"        Annotated nets                   :   {nets}\n")
    not_ann = 682 if bad else 0
    Path("parasitics_annotation.rpt").write_text(
        "Internal nets       |         |         |         |         |         |\n"
        f"  - Pin to pin nets |     682 |       0 |       0 |     {682 - not_ann} |       {not_ann} |\n")
    Path("power.rpt").write_text("Total Power            = 2.296e-04  (100.00%)\n")
    for f in ("switching_activity.rpt", "power_cells.rpt"):
        Path(f).write_text("")
    print("CEDAR_PT_DONE")


if __name__ == "__main__":
    {"dc": dc, "innovus": innovus, "vcs": vcs, "vcd2saif": vcd2saif, "pt": pt}[sys.argv[1]](sys.argv[2:])
