#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Re-create the checked-in ``third_party/`` snapshots from fork revisions.

This script documents (and reproduces) exactly how the DialEgg and
extraction-gym sources under ``third_party/`` were produced:

1. ``git archive <revision>`` of the fork checkout (no Git history imported).
2. Remove the paths listed in ``EXCLUDES`` (internal test inputs, developer
   diaries, large upstream benchmark corpora that are not needed to build).
3. Apply the generic, path-free edits in ``_edit_*`` (remove hard-coded
   developer filesystem paths so the build only depends on CMake/env inputs).

Usage::

    python3 toolchain/scripts/import_third_party.py dialegg <fork-checkout> <rev> [--dest DIR]
    python3 toolchain/scripts/import_third_party.py extraction-gym <fork-checkout> <rev> [--dest DIR]

After importing, update ``third_party/<name>/UPSTREAM.md`` and
``toolchain/sources.lock.json`` with the new revision and re-run
``python3 toolchain/scripts/check_lock.py``.
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

EXCLUDES = {
    "dialegg": [
        # Development diaries containing developer-machine paths.
        "reports",
        # Host-specific helper scripts (hard-coded developer paths; not used by
        # the build).
        "check_llvm_progress.sh",
        "setup_dialegg.sh",
        "run_dialegg.sh",
        # Non-redistributable test inputs that were present in the fork but are
        # not part of any lit test and not needed to build egg-opt.
        "test/circt/sm",
    ],
    "extraction-gym": [
        # 246 MB of upstream benchmark e-graphs; not needed to build
        # extract_result. Available from the upstream repository.
        "data",
    ],
}

# Files whose `export PATH=<absolute developer path>` lines are rewritten.
_PATH_EXPORT_FILES = [
    "circt-test/complete_roundtrip.sh",
    "circt-test/run_hw_optimization.sh",
    "test/anywidth_int/run_extract_translate.sh",
    "test/anywidth_int/run_eqsat.sh",
    "test/circt/concatvar/run.sh",
    "test/circt/fir/roundtrip.sh",
    "test/replacement/run_extract_translate.sh",
    "test/replacement/run_eqsat.sh",
]

_ABS_DEV_PATH = r"(/work/global|/home/)"


def _edit_dialegg(root: Path) -> None:
    cm = root / "CMakeLists.txt"
    text = cm.read_text()
    new_text = re.sub(
        r'set\(CIRCT_SOURCE_DIR "[^"]*" CACHE PATH "Path to CIRCT source tree"\)\n'
        r'set\(CIRCT_BUILD_DIR "[^"]*" CACHE PATH "Path to CIRCT build directory"\)\n',
        "# Cedar import: no machine-specific defaults. Pass -DCIRCT_SOURCE_DIR and\n"
        "# -DCIRCT_BUILD_DIR (see toolchain/build/build-native.sh in the Cedar repo).\n"
        'set(CIRCT_SOURCE_DIR "" CACHE PATH "Path to CIRCT source tree")\n'
        'set(CIRCT_BUILD_DIR "" CACHE PATH "Path to CIRCT build directory")\n'
        "if (NOT CIRCT_SOURCE_DIR OR NOT CIRCT_BUILD_DIR)\n"
        '    message(FATAL_ERROR "Set -DCIRCT_SOURCE_DIR=<circt checkout> and '
        '-DCIRCT_BUILD_DIR=<circt build dir>")\n'
        "endif ()\n",
        text,
    )
    if new_text == text:
        raise SystemExit("CMakeLists.txt: CIRCT default-path block not found")
    cm.write_text(new_text)

    for rel in _PATH_EXPORT_FILES:
        f = root / rel
        if not f.exists():
            continue
        s = f.read_text()
        s2 = re.sub(
            r"^([ \t]*)export PATH=.*" + _ABS_DEV_PATH + r".*$",
            r"\1# Cedar import: put egg-opt, egglog, circt-* and extract_result on PATH via\n"
            r"\1# DIALEGG_TOOLS_PATH (previously a hard-coded developer path).\n"
            r'\1export PATH="${DIALEGG_TOOLS_PATH:+$DIALEGG_TOOLS_PATH:}$PATH"',
            s,
            flags=re.M,
        )
        s2 = re.sub(r"/work/global/[^ ]*/build-release/egg-opt", "egg-opt", s2)
        f.write_text(s2)

    for rel in ["test/vec_of_rule/COST_TUNING_SUMMARY.md", "test/vec_of_rule/README.md"]:
        f = root / rel
        if f.exists():
            f.write_text(re.sub(r"/work/global/[^/]+/dialegg/", "<dialegg>/", f.read_text()))


def _edit_extraction_gym(root: Path) -> None:
    """Cedar change: deterministic extraction.

    The fork accumulated per-class DAG cost sets in std::collections::HashMap,
    whose per-process random seed changes the floating-point summation order,
    so equal-cost alternatives were tie-broken differently from run to run.
    Use rustc-hash's deterministic FxHashMap/FxHashSet instead (also for the
    S-expression printer, so `let` numbering is stable).
    """
    f = root / "src" / "extract" / "faster_greedy_dag.rs"
    s = f.read_text()
    s2 = s.replace(
        "    // It's slightly faster if this is an HashMap rather than an fxHashMap.\n"
        "    costs: HashMap<ClassId, Cost>,",
        "    // Cedar: FxHashMap (deterministic hashing) so that cost totals, and thus\n"
        "    // tie-breaking between equal-cost alternatives, are reproducible.\n"
        "    costs: FxHashMap<ClassId, Cost>,")
    s2 = s2.replace(
        "                costs: HashMap::from([(cid.clone(), node.cost)]),",
        "                costs: std::iter::once((cid.clone(), node.cost)).collect(),")
    if s2 == s or "HashMap<ClassId, Cost>," in s2.replace("FxHashMap<ClassId, Cost>,", ""):
        raise SystemExit("faster_greedy_dag.rs: expected CostSet HashMap not found")
    f.write_text(s2)
    f = root / "src" / "extract_result.rs"
    s = f.read_text()
    s2 = (s.replace("std::collections::HashMap::new()", "rustc_hash::FxHashMap::default()")
           .replace("std::collections::HashMap<", "rustc_hash::FxHashMap<")
           .replace("std::collections::HashSet<", "rustc_hash::FxHashSet<"))
    if s2 == s or "std::collections::Hash" in s2:
        raise SystemExit("extract_result.rs: expected std HashMap/HashSet uses not found")
    f.write_text(s2)


EDITS = {"dialegg": _edit_dialegg, "extraction-gym": _edit_extraction_gym}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("component", choices=sorted(EXCLUDES))
    ap.add_argument("checkout", type=Path, help="local clone of the fork")
    ap.add_argument("revision", help="full commit SHA to import")
    ap.add_argument("--dest", type=Path, default=None, help="output dir (default third_party/<component>)")
    args = ap.parse_args()

    dest = args.dest or (REPO_ROOT / "third_party" / args.component)
    keep = dest / "UPSTREAM.md"
    upstream_md = keep.read_text() if keep.exists() else None

    data = subprocess.run(
        ["git", "-C", str(args.checkout), "archive", "--format=tar", args.revision],
        check=True, capture_output=True,
    ).stdout
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
        else:  # Python < 3.12
            tf.extractall(dest)
    for rel in EXCLUDES[args.component]:
        p = dest / rel
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    EDITS[args.component](dest)
    if upstream_md is not None:
        keep.write_text(upstream_md)
    print(f"imported {args.component}@{args.revision} into {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
