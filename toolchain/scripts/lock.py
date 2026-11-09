#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Read toolchain/sources.lock.json for shell scripts and checks.

    lock.py shell            # print LOCK_<COMPONENT>_<FIELD>=... lines (eval-able)
    lock.py get circt.revision
    lock.py check            # validate structure and third_party provenance

Only the Python standard library is used so this works inside minimal build
containers.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "toolchain" / "sources.lock.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def load() -> dict:
    return json.loads(LOCK.read_text())


def cmd_shell(lock: dict) -> int:
    for name, comp in lock["components"].items():
        prefix = "LOCK_" + name.upper()
        for key in ("url", "revision", "path", "tag", "import_revision", "fetchcontent_name"):
            if key in comp:
                print(f"{prefix}_{key.upper()}={shlex.quote(str(comp[key]))}")
    print(f"LOCK_TOOLCHAIN_VERSION={shlex.quote(lock['toolchain_version'])}")
    print(f"LOCK_CPU_BASELINE={shlex.quote(lock['platform']['cpu_baseline'])}")
    return 0


def cmd_get(lock: dict, dotted: str) -> int:
    cur = lock
    for part in dotted.split("."):
        cur = cur["components"][part] if cur is lock and part in lock["components"] else cur[part]
    print(cur if not isinstance(cur, (dict, list)) else json.dumps(cur))
    return 0


def cmd_check(lock: dict) -> int:
    errors = []
    if lock.get("schema") != "cedar-toolchain-lock/1":
        errors.append("unexpected schema")
    for name, comp in lock["components"].items():
        kind = comp.get("kind")
        if kind == "git":
            if not SHA_RE.match(comp.get("revision", "")):
                errors.append(f"{name}: revision must be a full 40-hex SHA")
            if not comp.get("url", "").startswith("https://"):
                errors.append(f"{name}: url must be an anonymous https URL")
        elif kind == "path":
            p = ROOT / comp["path"]
            if not p.is_dir():
                errors.append(f"{name}: missing {comp['path']}")
            if not SHA_RE.match(comp.get("import_revision", "")):
                errors.append(f"{name}: import_revision must be a full SHA")
            up = p / "UPSTREAM.md"
            if not up.is_file():
                errors.append(f"{name}: missing UPSTREAM.md")
            elif comp["import_revision"] not in up.read_text():
                errors.append(f"{name}: UPSTREAM.md does not record import_revision")
        else:
            errors.append(f"{name}: unknown kind {kind!r}")
        if not comp.get("license"):
            errors.append(f"{name}: license missing")
        for patch in comp.get("patches", []):
            pp = ROOT / "toolchain" / "patches" / patch["file"]
            if not pp.is_file():
                errors.append(f"{name}: patch {patch['file']} missing")
    for t in lock["outputs"]["share"]:
        if not (ROOT / lock["outputs"]["share_source"] / t).is_file():
            errors.append(f"template {t} missing")
    for e in errors:
        print("ERROR:", e, file=sys.stderr)
    print("lock OK" if not errors else f"{len(errors)} lock error(s)")
    return 1 if errors else 0


def main(argv: list[str]) -> int:
    lock = load()
    if not argv or argv[0] == "shell":
        return cmd_shell(lock)
    if argv[0] == "get" and len(argv) == 2:
        return cmd_get(lock, argv[1])
    if argv[0] == "check":
        return cmd_check(lock)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
