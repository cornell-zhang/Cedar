# SPDX-License-Identifier: Apache-2.0
"""Explicit Cedar compiler-toolchain selection.

The toolchain is a self-consistent set of binaries (``circt-verilog``,
``circt-opt``, ``circt-lec``, ``egg-opt``, ``egglog``, ``extract_result``) plus
matching egglog templates (``base.egg``, ``circt.egg``, ``module.egg``).

Resolution order (first match wins; there is deliberately **no** fallback to
whatever happens to be on ``PATH``, so a stale development build or an older
reference wheel cannot silently substitute):

1. ``--toolchain-prefix`` argument / ``prefix`` parameter
2. ``CEDAR_TOOLCHAIN_PREFIX`` environment variable
3. ``toolchain.prefix`` in the selected tool profile
4. the installed ``cedar-toolchain`` Python distribution (``cedar_toolchain``)
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

REQUIRED_BINARIES = ("circt-verilog", "circt-opt", "egg-opt", "egglog", "extract_result")
OPTIONAL_BINARIES = ("circt-lec",)
TEMPLATES = ("base.egg", "circt.egg")


class ToolchainError(RuntimeError):
    pass


@dataclass
class Toolchain:
    bin_dir: Path
    share_dir: Path
    source: str
    manifest: Dict = field(default_factory=dict)

    def tool(self, name: str) -> str:
        p = self.bin_dir / name
        if not os.access(p, os.X_OK):
            raise ToolchainError(f"toolchain binary {name} not found in {self.bin_dir}")
        return str(p)

    def has(self, name: str) -> bool:
        return os.access(self.bin_dir / name, os.X_OK)

    def template(self, name: str) -> Path:
        p = self.share_dir / name
        if not p.is_file():
            raise ToolchainError(f"toolchain template {name} not found in {self.share_dir}")
        return p

    def env(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(os.environ if base is None else base)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        return env

    def identity(self) -> Dict:
        """Provenance: where the toolchain came from and what it contains."""
        ident = {
            "source": self.source,
            "bin_dir": str(self.bin_dir),
            "toolchain_version": self.manifest.get("toolchain_version"),
            "lockfile_sha256": self.manifest.get("lockfile_sha256"),
            "components": {k: v.get("revision") or v.get("import_revision")
                           for k, v in self.manifest.get("components", {}).items()},
            "binaries": {},
        }
        for b in REQUIRED_BINARIES + OPTIONAL_BINARIES:
            p = self.bin_dir / b
            if p.is_file():
                ident["binaries"][b] = _sha256(p)
        expected = self.manifest.get("binaries", {})
        ident["manifest_match"] = bool(expected) and all(
            expected.get(k) == v for k, v in ident["binaries"].items())
        return ident

    def probe(self, timeout: int = 60) -> Dict[str, str]:
        out = {}
        for b in REQUIRED_BINARIES + OPTIONAL_BINARIES:
            if not self.has(b):
                out[b] = "missing" if b in REQUIRED_BINARIES else "absent (optional)"
                continue
            if b == "extract_result":   # has no --version/--help; report presence + size
                out[b] = f"present ({(self.bin_dir / b).stat().st_size} bytes)"
                continue
            args = [str(self.bin_dir / b), "--version"]
            if b == "egglog":
                args = [str(self.bin_dir / b), "--help"]
            try:
                p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
                txt = (p.stdout + p.stderr).strip().splitlines()
                out[b] = (txt[0] if txt else f"exit {p.returncode}")[:120]
            except Exception as exc:  # noqa: BLE001
                out[b] = f"error: {exc}"
        return out


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _from_prefix(prefix: Path, source: str) -> Toolchain:
    prefix = prefix.expanduser().resolve()
    bin_dir, share_dir = prefix / "bin", prefix / "share" / "cedar"
    manifest = {}
    mf = prefix / "cedar-toolchain-manifest.json"
    if mf.is_file():
        manifest = json.loads(mf.read_text())
    return _validate(Toolchain(bin_dir, share_dir, source, manifest))


def _validate(tc: Toolchain) -> Toolchain:
    missing: List[str] = [b for b in REQUIRED_BINARIES if not tc.has(b)]
    missing += [t for t in TEMPLATES if not (tc.share_dir / t).is_file()]
    if missing:
        raise ToolchainError(f"toolchain at {tc.bin_dir.parent} ({tc.source}) is incomplete; missing: {', '.join(missing)}")
    return tc


def resolve(prefix: Optional[str] = None, profile_prefix: Optional[str] = None) -> Toolchain:
    if prefix:
        return _from_prefix(Path(prefix), "argument")
    env = os.environ.get("CEDAR_TOOLCHAIN_PREFIX")
    if env:
        return _from_prefix(Path(env), "CEDAR_TOOLCHAIN_PREFIX")
    if profile_prefix:
        return _from_prefix(Path(profile_prefix), "tool-profile")
    try:
        import cedar_toolchain  # type: ignore
    except ImportError:
        raise ToolchainError(
            "no Cedar toolchain selected: install the cedar-toolchain wheel, set "
            "CEDAR_TOOLCHAIN_PREFIX, pass --toolchain-prefix, or set toolchain.prefix in the tool profile"
        )
    tc = Toolchain(Path(cedar_toolchain.bin_dir()), Path(cedar_toolchain.share_dir()),
                   f"cedar-toolchain wheel {getattr(cedar_toolchain, '__version__', '?')}",
                   cedar_toolchain.manifest())
    return _validate(tc)
