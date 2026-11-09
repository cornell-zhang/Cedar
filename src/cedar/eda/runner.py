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
import shlex
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

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

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict:
        return asdict(self)


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
            lines.append(f"source {shlex.quote(self.setup_script)} >/dev/null 2>&1 || "
                         f"{{ echo 'CEDAR: setup script failed: {self.setup_script}' >&2; exit 97; }}")
        for p in self.path_prepend:
            lines.append(f"export PATH={shlex.quote(p)}:\"$PATH\"")
        req = list(dict.fromkeys(list(self.env_required) + list(extra_required)))
        for v in req:
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
        try:
            p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return StageResult(stage, "timeout", time.monotonic() - t0, detail="environment setup timed out")
        out = (p.stdout.strip().splitlines() or [""])[-1]
        if p.returncode != 0 or not out:
            return StageResult(stage, "unavailable", time.monotonic() - t0, returncode=p.returncode,
                               detail=(p.stderr.strip()[-300:] or f"{t['executable']} not found"))
        return StageResult(stage, "ok", round(time.monotonic() - t0, 2), detail=out)

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
        t0 = time.monotonic()
        with open(log, "w") as fh:
            proc = subprocess.Popen(["bash", "-c", script], cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT,
                                    stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                                    env=env, text=True, start_new_session=True)
            try:
                proc.communicate(input=stdin_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(3)
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                return StageResult(name, "timeout", round(time.monotonic() - t0, 2), str(log), None,
                                   f"exceeded {timeout}s")
        rc = proc.returncode
        secs = round(time.monotonic() - t0, 2)
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
