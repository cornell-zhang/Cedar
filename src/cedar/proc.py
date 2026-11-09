# SPDX-License-Identifier: Apache-2.0
"""External processes: own process group, bounded time, nothing left behind.

Every command Cedar starts (EDA tools, compiled simulators, the compiler
toolchain, unattended agents) runs through :func:`run`:

* it starts in a new session/process group;
* its timeout is capped by the *run budget* deadline (:func:`set_deadline`),
  so no subprocess can outlive the overall execution budget;
* on timeout the whole group is terminated (SIGTERM, then SIGKILL) and the
  leader is reaped; after a normal exit, any descendants still in the group
  (background jobs, daemons a tool forked) are terminated the same way.

The deadline is a wall-clock (``time.time()``) instant shared by all threads of
the process; the workflow engine sets it while it advances a run.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import IO, Mapping, Optional, Sequence, Union

_lock = threading.Lock()
_deadline: Optional[float] = None
GRACE_S = 3.0


def set_deadline(epoch: Optional[float]) -> None:
    global _deadline
    with _lock:
        _deadline = epoch


def deadline() -> Optional[float]:
    return _deadline


def remaining() -> Optional[float]:
    d = _deadline
    return None if d is None else d - time.time()


def expired() -> bool:
    r = remaining()
    return r is not None and r <= 0


class DeadlineExpired(Exception):
    """The run budget ran out; in-process work (Z3) must stop immediately."""


def check() -> None:
    if expired():
        raise DeadlineExpired("run budget expired")


def bounded_ms(timeout_ms: int) -> int:
    """A Z3 timeout (ms) capped by the remaining run budget.

    Never returns 0 (which Z3 treats as *no* limit): if less than 1 ms of the
    budget remains, DeadlineExpired is raised instead.
    """
    ms = max(1, int(timeout_ms))
    r = remaining()
    if r is None:
        return ms
    left = int(r * 1000)
    if left < 1:
        raise DeadlineExpired("run budget expired")
    return min(ms, left)


def bounded(timeout: Optional[float]) -> Optional[float]:
    """``timeout`` capped by the remaining run budget (never below 0)."""
    r = remaining()
    if r is None:
        return timeout
    r = max(0.0, r)
    return r if timeout is None else min(float(timeout), r)


@dataclass
class Result:
    returncode: Optional[int]          # None if killed on timeout
    seconds: float
    timed_out: bool = False
    budget_expired: bool = False       # the timeout was the run budget, not the stage limit
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    leftovers_killed: bool = False     # descendants were still alive after the leader exited


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def kill_group(pgid: int, grace: float = GRACE_S, leader: Optional[subprocess.Popen] = None) -> bool:
    """Terminate every process of group ``pgid``; True if any was alive.

    ``leader`` (our own child) is reaped while waiting, so its zombie does not
    keep the group "alive"; other members are reparented and reaped by init.
    """
    def alive() -> bool:
        if leader is not None:
            leader.poll()
        return _group_alive(pgid)

    if not alive():
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    t_end = time.monotonic() + grace
    while time.monotonic() < t_end and alive():
        time.sleep(0.02)
    if alive():
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        t_end = time.monotonic() + grace
        while time.monotonic() < t_end and alive():
            time.sleep(0.02)
    return True


Stream = Union[int, IO, None]


def run(argv: Sequence[str], *, cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None, stdin_text: Optional[str] = None,
        stdout: Stream = subprocess.PIPE, stderr: Stream = subprocess.PIPE, text: bool = True) -> Result:
    """Run ``argv`` in its own process group with group-wide cleanup."""
    eff = bounded(timeout)
    budget_limited = eff is not None and (timeout is None or eff < float(timeout))
    t0 = time.monotonic()
    if eff is not None and eff <= 0:
        return Result(None, 0.0, timed_out=True, budget_expired=True)
    proc = subprocess.Popen(list(argv), cwd=cwd, env=dict(env) if env is not None else None, stdout=stdout,
                            stderr=stderr, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                            text=text, start_new_session=True)
    try:
        out, err = proc.communicate(input=stdin_text, timeout=eff)
    except subprocess.TimeoutExpired:
        kill_group(proc.pid, leader=proc)
        try:
            out, err = proc.communicate(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        return Result(None, round(time.monotonic() - t0, 2), timed_out=True, budget_expired=budget_limited,
                      stdout=out, stderr=err)
    except BaseException:
        kill_group(proc.pid, leader=proc)
        proc.wait()
        raise
    leftovers = kill_group(proc.pid)
    return Result(proc.returncode, round(time.monotonic() - t0, 2), stdout=out, stderr=err,
                  leftovers_killed=leftovers)
