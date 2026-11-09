# SPDX-License-Identifier: Apache-2.0
"""Agent task contracts.

Python never calls a model directly. When the workflow needs reasoning (RTL
rewrites, rule proposals, rule/cost selection, extraction fallback, stop
decisions) it writes a *task*: a prompt file plus a list of required output
files. The active agent (Claude Code or Codex, via the shared Cedar skill)
performs the task and runs ``cedar task submit``; Python validates the outputs
deterministically before the run continues. An optional unattended adapter
(``--agent-cmd``) can execute tasks in batch mode instead.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Callable, Dict, List, Optional

TASK_KINDS = ("direct_rewrite", "propose_rules", "select_rules", "extraction_fallback", "stop_decision")


class _SafeDict(dict):
    def __missing__(self, key):  # leave unknown placeholders visible
        return "{" + key + "}"


def render(name: str, **kw) -> str:
    text = resources.files("cedar").joinpath(f"data/prompts/{name}.md").read_text()
    return text.format_map(_SafeDict(**{k: v for k, v in kw.items()}))


@dataclass
class TaskOutput:
    path: str
    kind: str              # verilog | egg | json
    required: bool = True


@dataclass
class Task:
    id: str
    kind: str
    prompt_file: str
    outputs: List[TaskOutput]
    status: str = "pending"          # pending | accepted | rejected
    created_at: str = ""
    submitted_at: str = ""
    attempts: int = 0
    feedback: str = ""
    agent: Dict[str, str] = field(default_factory=dict)
    stage_model: str = ""
    meta: Dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        d = dict(d)
        d["outputs"] = [TaskOutput(**o) for o in d.get("outputs", [])]
        return cls(**d)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_outputs(task: Task, verilog_check: Callable[[Path], Optional[str]],
                     egg_check: Callable[[Path], Optional[str]]) -> List[str]:
    problems: List[str] = []
    for o in task.outputs:
        p = Path(o.path)
        if not p.is_file() or p.stat().st_size == 0:
            if o.required:
                problems.append(f"missing output {p}")
            continue
        if o.kind == "verilog":
            err = verilog_check(p)
            if err:
                problems.append(f"{p.name}: {err}")
        elif o.kind == "egg":
            err = egg_check(p)
            if err:
                problems.append(f"{p.name}: {err}")
        elif o.kind == "json":
            try:
                json.loads(p.read_text())
            except json.JSONDecodeError as exc:
                problems.append(f"{p.name}: invalid JSON ({exc})")
    return problems


def run_agent_command(cmd: str, task: Task, cwd: Path, timeout: int = 1800) -> Dict[str, object]:
    """Unattended adapter: run ``cmd`` with the task prompt appended as one argument.

    Examples: ``claude -p --permission-mode acceptEdits`` or
    ``codex exec --full-auto``. The command's own model/host identity is
    recorded by the caller.
    """
    prompt = Path(task.prompt_file).read_text()
    prompt += ("\n\nYou are running non-interactively: write the required output files directly. "
               "Do not run `cedar task submit`; the caller validates the outputs.\n")
    argv = shlex.split(cmd) + [prompt]
    t0 = time.monotonic()
    log = Path(task.prompt_file).with_suffix(".agent.log")
    with open(log, "w") as fh:
        try:
            p = subprocess.run(argv, cwd=str(cwd), stdout=fh, stderr=subprocess.STDOUT, timeout=timeout,
                               stdin=subprocess.DEVNULL)
            rc = p.returncode
        except subprocess.TimeoutExpired:
            rc = None
    return {"returncode": rc, "seconds": round(time.monotonic() - t0, 2), "log": str(log)}


_MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)")
