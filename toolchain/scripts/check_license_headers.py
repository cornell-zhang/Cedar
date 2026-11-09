#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check SPDX headers in Git-tracked Cedar source and configuration files.

Run from any directory: python3 toolchain/scripts/check_license_headers.py
Third-party snapshots retain their upstream notices. JSON, prose, and empty
package markers are covered by project licensing rather than comment headers.
"""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
COMMENT_PREFIXES = {
    ".py": "#",
    ".sh": "#",
    ".bash": "#",
    ".toml": "#",
    ".yaml": "#",
    ".yml": "#",
    ".tcl": "#",
    ".v": "//",
    ".sv": "//",
    ".c": "//",
    ".h": "//",
    ".cpp": "//",
    ".hpp": "//",
    ".cc": "//",
    ".cxx": "//",
    ".rs": "//",
    ".egg": ";",
}


def main() -> int:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True,
    )
    checked = 0
    failures = []
    for name in result.stdout.decode().split("\0"):
        if not name or name.startswith("third_party/"):
            continue
        path = ROOT / name
        prefix = "#" if path.name == "Dockerfile" else COMMENT_PREFIXES.get(path.suffix)
        if prefix is None:
            continue
        checked += 1
        expected = f"{prefix} SPDX-License-Identifier: Apache-2.0"
        if not path.is_file():
            failures.append(f"{name}: file missing")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        index = 1 if lines and lines[0].startswith("#!") else 0
        if len(lines) <= index or lines[index] != expected:
            failures.append(f"{name}: expected {expected!r} after the optional shebang")
    for failure in failures:
        print(failure, file=sys.stderr)
    if failures:
        print(f"{len(failures)} invalid headers in {checked} files.", file=sys.stderr)
        return 1
    print(f"Verified Apache-2.0 headers in {checked} Cedar-owned files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
