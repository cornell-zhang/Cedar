#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write <prefix>/cedar-toolchain-manifest.json describing a toolchain build.

The manifest records the lockfile hash, every component revision, and the
SHA-256 of each produced binary and template so that Cedar runs can record
exactly which toolchain they used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("prefix", type=Path)
    ap.add_argument("--rust-lock", action="append", default=[], help="name=path/to/Cargo.lock")
    args = ap.parse_args()

    lock_path = ROOT / "toolchain" / "sources.lock.json"
    lock = json.loads(lock_path.read_text())
    prefix: Path = args.prefix
    manifest = {
        "schema": "cedar-toolchain-manifest/1",
        "toolchain_version": lock["toolchain_version"],
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "build_machine": {"system": platform.system(), "machine": platform.machine()},
        "lockfile_sha256": sha256(lock_path),
        "components": {
            name: {k: v for k, v in comp.items() if k in ("url", "revision", "tag", "path", "import_revision")}
            for name, comp in lock["components"].items()
        },
        "binaries": {p.name: sha256(p) for p in sorted((prefix / "bin").iterdir()) if p.is_file()},
        "templates": {p.name: sha256(p) for p in sorted((prefix / "share" / "cedar").iterdir()) if p.is_file()},
        "cargo_locks": {},
    }
    for item in args.rust_lock:
        name, _, path = item.partition("=")
        p = Path(path)
        if p.is_file():
            manifest["cargo_locks"][name] = sha256(p)
    out = prefix / "cedar-toolchain-manifest.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
