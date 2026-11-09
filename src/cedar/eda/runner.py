# SPDX-License-Identifier: Apache-2.0
"""Run EDA tools through a user tool profile.

Every invocation runs ``bash -c`` with the profile's setup script sourced, the
required environment-variable *names* checked (values are never logged), a
niceness, a per-stage timeout that kills the whole process group, and the
combined output captured to a log file. Results are structured
(:class:`StageResult`) and failures are explicit.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .. import proc as procmod
from ..config import Loaded

STAGE_STATUSES = ("ok", "failed", "timeout", "unavailable", "skipped")


@dataclass
class StageResult:
    stage: str
    status: str
    seconds: float = 0.0
    log: str = ""
    returncode: Optional[int] = None
    detail: str = ""
    outputs: Dict[str, str] = field(default_factory=dict)
    # Structured evidence a stage was judged on (tool errors, annotation
    # coverage, ...), recorded in run.json with the stage result.
    evidence: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return asdict(self)


# Adapter-specific version probes: (arguments, regex for the version string).
# They run inside the profile environment with a short timeout; the parsed
# version (or, if none is recognized, a hash of the probe output) is part of
# a tool's identity.
VERSION_PROBES: Dict[str, tuple] = {
    "synopsys-dc": (["-version"], r"dc_shell version\s*-\s*(\S+)"),
    "synopsys-primetime": (["-version"], r"pt_shell version\s*-\s*(\S+)"),
    "synopsys-vcs": (["-ID"], r"Compiler version\s*=\s*([^\r\n]+?)\s*$"),
    "synopsys-vcd2saif": (["-version"], r"translator version\s+(\S+)"),
    "cadence-innovus": (["-version"], r"Innovus\s+(v\S+)"),
    "cadence-conformal": (["-version"], r"Tool:\s*lec\s+(\S+)"),
}
FILE_HASH_LIMIT = 64 << 20


def file_identity(path: str) -> Dict[str, Any]:
    """Content identity of a resolved executable (symlinks resolved)."""
    import hashlib
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        return {"realpath": real, "sha256": "missing"}
    h = hashlib.sha256()
    with open(real, "rb") as fh:
        if st.st_size <= FILE_HASH_LIMIT:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
            kind = "full"
        else:                               # very large binary: size + mtime + head/tail
            h.update(f"{st.st_size}:{int(st.st_mtime)}".encode())
            h.update(fh.read(1 << 20))
            fh.seek(-(1 << 20), os.SEEK_END)
            h.update(fh.read())
            kind = "size+mtime+head+tail"
    return {"realpath": real, "size": st.st_size, "sha256": h.hexdigest(), "hash": kind}


class ToolEnv:
    """Shell environment derived from a tool profile."""

    def __init__(self, profile: Loaded):
        self.profile = profile
        d = profile.data
        env = d.get("environment") or {}
        self.setup_script = (env.get("setup_script") or {}).get("path")
        self.env_required: List[str] = list(env.get("env_required") or [])
        self.path_prepend = [p["path"] for p in env.get("path_prepend") or []]
        ex = d.get("execution") or {}
        self.nice = int(ex.get("nice", 10))
        self.threads = int(ex.get("threads", 4))
        self.max_parallel = int(ex.get("max_parallel", 2))
        self.tools: Dict[str, dict] = {k: v for k, v in (d.get("tools") or {}).items() if v}

    def configured(self, stage: str) -> bool:
        return stage in self.tools

    def prefix(self, extra_required: Sequence[str] = ()) -> str:
        lines = ["set -o pipefail"]
        if self.setup_script:
            msg = shlex.quote(f"CEDAR: setup script failed: {self.setup_script}")
            lines.append(f"source {shlex.quote(self.setup_script)} >/dev/null 2>&1 || "
                         f"{{ echo {msg} >&2; exit 97; }}")
        for p in self.path_prepend:
            lines.append(f"export PATH={shlex.quote(p)}:\"$PATH\"")
        req = list(dict.fromkeys(list(self.env_required) + list(extra_required)))
        for v in req:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v):
                raise ValueError(f"invalid environment variable name {v!r}")
            lines.append(f"[ -n \"${{{v}:-}}\" ] || {{ echo 'CEDAR: required environment variable {v} is not set' >&2; exit 98; }}")
        return "\n".join(lines)

    def command(self, stage: str, args: Sequence[str]) -> str:
        t = self.tools[stage]
        exe = shlex.quote(t["executable"])
        fixed = " ".join(shlex.quote(a) for a in t.get("args") or [])
        rest = " ".join(shlex.quote(a) for a in args)
        return f"exec nice -n {self.nice} {exe} {fixed} {rest}".rstrip()

    def which(self, stage: str, timeout: int = 60) -> StageResult:
        """Resolve the stage executable inside the profile environment."""
        if stage not in self.tools:
            return StageResult(stage, "unavailable", detail="not configured in tool profile")
        t = self.tools[stage]
        script = self.prefix(t.get("env_required") or []) + f"\ncommand -v {shlex.quote(t['executable'])}"
        t0 = time.monotonic()
        p = procmod.run(["bash", "-c", script], timeout=timeout)
        if p.timed_out:
            return StageResult(stage, "timeout", time.monotonic() - t0, detail="environment setup timed out")
        out = ((p.stdout or "").strip().splitlines() or [""])[-1]
        if p.returncode != 0 or not out:
            return StageResult(stage, "unavailable", time.monotonic() - t0, returncode=p.returncode,
                               detail=((p.stderr or "").strip()[-300:] or f"{t['executable']} not found"))
        return StageResult(stage, "ok", round(time.monotonic() - t0, 2), detail=out)

    def version_probe(self, stage: str, timeout: int = 60) -> Dict[str, Any]:
        """Bounded, adapter-specific version query inside the profile environment."""
        import hashlib
        t = self.tools[stage]
        args, rx = VERSION_PROBES.get(t["adapter"], (["--version"], None))
        script = self.prefix(t.get("env_required") or []) + "\n" + self.command(stage, args)
        p = procmod.run(["bash", "-c", script], timeout=timeout)
        out = ((p.stdout or "") + (p.stderr or ""))
        m = re.search(rx, out, re.M) if rx else None
        return {"version": m.group(1) if m else None, "probe_sha256": hashlib.sha256(out.encode()).hexdigest(),
                "probe_status": "timeout" if p.timed_out else f"exit {p.returncode}",
                "probe_head": out.strip().splitlines()[0][:160] if out.strip() else ""}

    def setup_identity(self, timeout: int = 60) -> Dict[str, Any]:
        """The environment the tools run in: setup script content, PATH after
        setup and (hashes of) the required variables' values."""
        import hashlib
        ident: Dict[str, Any] = {"setup_script": self.setup_script, "path_prepend": list(self.path_prepend)}
        ident["setup_script_sha256"] = (file_identity(self.setup_script)["sha256"] if self.setup_script else None)
        names = sorted(set(self.env_required) | {v for t in self.tools.values() for v in (t.get("env_required") or [])})
        dump = " ".join(f'printf "%s=%s\\0" {shlex.quote(n)} "${{{n}:-}}";' for n in names)
        p = procmod.run(["bash", "-c", self.prefix() + f'\nprintf "PATH=%s\\0" "$PATH"; {dump}'], timeout=timeout)
        ident["environment_sha256"] = (hashlib.sha256((p.stdout or "").encode()).hexdigest()
                                       if p.returncode == 0 else f"<setup {('timeout' if p.timed_out else p.returncode)}>")
        ident["required_variables"] = names
        return ident

    def run(self, stage: str, args: Sequence[str], cwd: Path, log: Path, timeout: Optional[int] = None,
            stdin_text: Optional[str] = None, env_extra: Optional[Dict[str, str]] = None,
            label: Optional[str] = None) -> StageResult:
        name = label or stage
        if stage not in self.tools:
            return StageResult(name, "unavailable", detail=f"{stage} tool not configured in profile {self.profile.data['name']}")
        t = self.tools[stage]
        timeout = int(timeout or t.get("timeout_s") or 3600)
        cwd.mkdir(parents=True, exist_ok=True)
        script = self.prefix(t.get("env_required") or []) + "\n" + self.command(stage, args)
        env = dict(os.environ)
        env.update(env_extra or {})
        env.setdefault("OMP_NUM_THREADS", str(self.threads))
        with open(log, "w") as fh:
            p = procmod.run(["bash", "-c", script], cwd=str(cwd), env=env, timeout=timeout, stdin_text=stdin_text,
                            stdout=fh, stderr=subprocess.STDOUT)
        if p.timed_out:
            why = "run budget expired" if p.budget_expired else f"exceeded {timeout}s"
            return StageResult(name, "timeout", p.seconds, str(log), None, why,
                               evidence={"budget_expired": p.budget_expired})
        rc = p.returncode
        secs = p.seconds
        if rc in (97, 98):
            tail = _tail(log, 400)
            return StageResult(name, "unavailable", secs, str(log), rc, tail)
        if rc == 127:
            return StageResult(name, "unavailable", secs, str(log), rc, f"{t['executable']}: command not found")
        return StageResult(name, "ok" if rc == 0 else "failed", secs, str(log), rc,
                           "" if rc == 0 else _tail(log, 600))


def _tail(path: Path, n: int) -> str:
    try:
        return Path(path).read_text(errors="replace")[-n:]
    except OSError:
        return ""
