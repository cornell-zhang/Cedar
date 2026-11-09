#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Inspect built Cedar distributions (wheels and sdists) before release.

* ``cedar_toolchain-*.whl``: expected binaries/templates/licenses/manifest are
  present, manifest SHA-256s match the packaged binaries, the platform tag is
  manylinux_2_28_x86_64, and every binary passes check_abi.py.
* ``cedar_workflow-*.whl`` / ``cedar_workflow-*.tar.gz``: only allow-listed
  paths are present (package sources + declared data, metadata).
* All archives: no file content or name matches private-data patterns
  (developer home/work paths, credential markers, local profile names).

Exit status is non-zero on any finding.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = json.loads((ROOT / "toolchain" / "sources.lock.json").read_text())

# Generic private-data patterns. Site-specific patterns (user names, scratch
# directory names, local profile names) are supplied at check time with
# --private-patterns FILE (one regular expression per line) so they never
# have to be committed.
PRIVATE_PATTERNS = [
    re.compile(rb"/home/[a-z][a-z0-9_-]+/"), re.compile(rb"/Users/[A-Za-z][A-Za-z0-9_-]+/"),
    re.compile(rb"BEGIN (RSA |OPENSSH |EC |DSA )?PRIVATE KEY"), re.compile(rb"gh[opsu]_[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"), re.compile(rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
]
# Strings that legitimately appear inside third-party binaries (compiler/runtime
# defaults), never as local configuration.
BINARY_ALLOW = [re.compile(rb"/home/[a-z][a-z0-9_-]+/\.cargo/registry")]

WORKFLOW_ALLOWED = re.compile(
    r"^(cedar/.*\.(py|md|tcl|v|egg|json|typed)|cedar_workflow-[^/]+\.dist-info/.*|"
    r"cedar_workflow-[^/]+/(src/cedar/.*|src/cedar_workflow\.egg-info/.*|pyproject\.toml|README\.md|LICENSE|"
    r"THIRD_PARTY_NOTICES\.md|PKG-INFO|setup\.cfg|MANIFEST\.in|tests/[A-Za-z0-9_]+\.py))$")


def _scan(name: str, data: bytes, is_binary: bool) -> list:
    hits = []
    for pat in PRIVATE_PATTERNS:
        for m in pat.finditer(data):
            ctx = data[max(0, m.start() - 20): m.end() + 40]
            if is_binary and any(a.search(ctx) for a in BINARY_ALLOW):
                continue
            hits.append(f"{name}: matches {pat.pattern!r}: {ctx[:90]!r}")
            break
    return hits


def check_toolchain_wheel(path: Path) -> list:
    errs = []
    if not path.name.endswith("-py3-none-manylinux_2_28_x86_64.whl"):
        errs.append(f"unexpected wheel tag in {path.name}")
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        want = [f"cedar_toolchain/_bin/{b}" for b in LOCK["outputs"]["bin"]]
        want += [f"cedar_toolchain/_share/{t}" for t in LOCK["outputs"]["share"]]
        want += ["cedar_toolchain/cedar-toolchain-manifest.json", "cedar_toolchain/__init__.py",
                 "cedar_toolchain/_licenses/THIRD_PARTY_NOTICES.md"]
        errs += [f"missing {w}" for w in want if w not in names]
        lic = [n for n in names if n.startswith("cedar_toolchain/_licenses/")]
        for comp in ("circt", "llvm", "mlir", "slang", "fmt", "egglog", "dialegg", "extraction-gym", "cedar"):
            if not any(f"/{comp}-LICENSE" in n for n in lic):
                errs.append(f"license file for {comp} missing")
        if "cedar_toolchain/cedar-toolchain-manifest.json" in names:
            man = json.loads(z.read("cedar_toolchain/cedar-toolchain-manifest.json"))
            for b, sha in man.get("binaries", {}).items():
                n = f"cedar_toolchain/_bin/{b}"
                if n in names and hashlib.sha256(z.read(n)).hexdigest() != sha:
                    errs.append(f"{b}: SHA-256 differs from manifest")
            for comp, info in man.get("components", {}).items():
                lk = LOCK["components"].get(comp, {})
                if info.get("revision") != lk.get("revision") or info.get("import_revision") != lk.get("import_revision"):
                    errs.append(f"manifest component {comp} does not match the current lockfile")
        wheel_meta = [n for n in names if n.endswith(".dist-info/WHEEL")]
        if wheel_meta and b"manylinux_2_28_x86_64" not in z.read(wheel_meta[0]):
            errs.append("WHEEL metadata tag is not manylinux_2_28_x86_64")
        with tempfile.TemporaryDirectory() as td:
            bdir = Path(td) / "bin"
            bdir.mkdir()
            for n in names:
                if n.startswith("cedar_toolchain/_bin/") and not n.endswith(".gitkeep"):
                    p = bdir / Path(n).name
                    p.write_bytes(z.read(n))
                    if (z.getinfo(n).external_attr >> 16) & 0o111 == 0:
                        errs.append(f"{n} is not executable in the wheel")
            r = subprocess.run([sys.executable, str(ROOT / "toolchain/scripts/check_abi.py"), str(bdir)],
                               capture_output=True, text=True)
            print(r.stdout.rstrip())
            if r.returncode != 0:
                errs.append("ABI check failed")
        for n in names:
            data = z.read(n)
            errs += _scan(n, data, "/_bin/" in n)
    return errs


def check_workflow_archive(path: Path) -> list:
    errs = []
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as z:
            entries = [(n, z.read(n)) for n in z.namelist() if not n.endswith("/")]
    else:
        with tarfile.open(path) as t:
            entries = [(m.name, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]
    names = [n for n, _ in entries]
    for n in names:
        if not WORKFLOW_ALLOWED.match(n):
            errs.append(f"unexpected file in distribution: {n}")
    for need in ("cedar/cli.py", "cedar/data/prompts/direct_rewrite.md", "cedar/data/rules/mul_factor_common.egg",
                 "cedar/rules/z3verify.py"):
        if not any(n.endswith(need) for n in names):
            errs.append(f"missing {need}")
    for n, data in entries:
        errs += _scan(n, data, False)
    return errs


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("archives", nargs="+", type=Path)
    ap.add_argument("--private-patterns", type=Path, help="file with extra regular expressions (one per line)")
    args = ap.parse_args()
    if args.private_patterns:
        for line in args.private_patterns.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                PRIVATE_PATTERNS.append(re.compile(line.strip().encode()))
    errs = []
    for p in args.archives:
        e = check_toolchain_wheel(p) if p.name.startswith("cedar_toolchain") else check_workflow_archive(p)
        print(f"{p.name}: {'OK' if not e else str(len(e)) + ' problem(s)'}")
        for x in e:
            print("  -", x)
        errs += e
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
