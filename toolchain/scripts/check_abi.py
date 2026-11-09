#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Portability check for Cedar toolchain binaries (manylinux_2_28 policy).

For each ELF binary, verify that:
  * every DT_NEEDED library is in the manylinux system-library allowlist,
  * no versioned symbol requires GLIBC > 2.28 or GLIBCXX > 3.4.25 / CXXABI > 1.3.11,
  * the binary is x86-64 and has no RPATH/RUNPATH pointing at the build tree.

Uses ``readelf`` (binutils). Exits non-zero on any violation. ``--json`` prints
a machine-readable report.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ALLOWED_NEEDED = {
    # PEP 600 / manylinux_2_28 allowlist subset relevant to CLI tools.
    "libgcc_s.so.1", "libstdc++.so.6", "libm.so.6", "libdl.so.2", "librt.so.1",
    "libc.so.6", "libnsl.so.1", "libutil.so.1", "libpthread.so.0", "libresolv.so.2",
    "ld-linux-x86-64.so.2",
}
LIMITS = {"GLIBC": (2, 28), "GLIBCXX": (3, 4, 25), "CXXABI": (1, 3, 11)}


def _readelf(args: list[str], path: Path) -> str:
    return subprocess.run(["readelf", *args, str(path)], check=True, capture_output=True, text=True).stdout


def check(path: Path) -> dict:
    rep: dict = {"file": path.name, "errors": []}
    header = _readelf(["-h"], path)
    if "X86-64" not in header and "Advanced Micro Devices X86-64" not in header:
        rep["errors"].append("not an x86-64 ELF")
    dyn = _readelf(["-d"], path)
    needed = re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", dyn)
    rep["needed"] = needed
    for lib in needed:
        if lib not in ALLOWED_NEEDED:
            rep["errors"].append(f"non-allowlisted shared library dependency: {lib}")
    for tag in ("RPATH", "RUNPATH"):
        m = re.search(rf"\({tag}\)\s+Library \w+: \[([^\]]+)\]", dyn)
        if m:
            rep["errors"].append(f"{tag} set: {m.group(1)}")
    syms = _readelf(["-V", "-W"], path)
    maxes: dict = {}
    for fam, ver in re.findall(r"\b(GLIBC|GLIBCXX|CXXABI)_([0-9]+(?:\.[0-9]+)+)\b", syms):
        t = tuple(int(x) for x in ver.split("."))
        if fam not in maxes or t > maxes[fam]:
            maxes[fam] = t
    rep["max_versions"] = {k: ".".join(map(str, v)) for k, v in maxes.items()}
    for fam, t in maxes.items():
        if t > LIMITS[fam]:
            rep["errors"].append(f"requires {fam}_{'.'.join(map(str, t))} > {'.'.join(map(str, LIMITS[fam]))}")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bindir", type=Path)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    reports = []
    for p in sorted(args.bindir.iterdir()):
        if p.is_file() and p.read_bytes()[:4] == b"\x7fELF":
            reports.append(check(p))
    if not reports:
        print("no ELF binaries found", file=sys.stderr)
        return 1
    bad = [r for r in reports if r["errors"]]
    if args.json:
        print(json.dumps({"ok": not bad, "binaries": reports}, indent=2))
    else:
        for r in reports:
            status = "OK " if not r["errors"] else "BAD"
            print(f"[{status}] {r['file']}: needs {', '.join(r['needed'])}; max {r['max_versions']}")
            for e in r["errors"]:
                print(f"      - {e}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
