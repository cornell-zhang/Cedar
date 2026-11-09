# SPDX-License-Identifier: Apache-2.0
"""Overall run budget and process-group cleanup.

* Every subprocess runs in its own process group; on timeout (stage limit or
  run budget) the whole group is terminated and reaped -- verified with real
  spawned children.
* The run budget is persisted, bounds every subprocess, survives resumes, and
  an expired run ends as failed: it can never complete, keep a report that
  claims success, or earn successful-iteration credit.
* An unattended agent's timeout or nonzero exit is handled before its output
  files are considered.
"""
import functools
import json
import os
import sys
import time
from pathlib import Path

import pytest
import yaml

from cedar import config as C
from cedar import proc
from cedar.eda import flow
from cedar.eda.runner import ToolEnv

CHILD = "sleep 30 & echo $! > child.pid; wait"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                              # a not-yet-reaped zombie is dead
        return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
    except OSError:
        return False


def _gone(pid: int, wait: float = 5.0) -> bool:
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return False


def _child(tmp_path: Path) -> int:
    for _ in range(100):
        f = tmp_path / "child.pid"
        if f.is_file() and f.read_text().strip():
            return int(f.read_text())
        time.sleep(0.02)
    raise AssertionError("child pid not written")


@pytest.fixture(autouse=True)
def _no_deadline():
    proc.set_deadline(None)
    yield
    proc.set_deadline(None)


def _ctx(tmp_path: Path) -> flow.FlowContext:
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump({"schema": "cedar/tool-profile@1", "name": "t", "tools": {
        "simulation": {"adapter": "synopsys-vcs", "executable": "bash"},
        "synthesis": {"adapter": "synopsys-dc", "executable": "bash"}}}))
    (tmp_path / "tech.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/technology@1", "name": "x", "units": {"time": "ps"}, "default_corner": "tt",
        "corners": {"tt": {"db": ["a.db"], "liberty": ["a.lib"], "sim_models": ["a.v"], "rc_tech": "rc"}},
        "lef": {"tech": "tech.lef", "cells": ["cells.lef"]}, "place_route": {"site": "core"}}))
    return flow.FlowContext(ToolEnv(C.load_tool_profile(str(tmp_path / "tools.yaml"))),
                            C.load_technology(str(tmp_path / "tech.yaml")), "tt", "m",
                            [{"port": "clk", "period_ns_resolved": 1.0}], 0.1)


# ----------------------------------------------------------- process groups

def test_shell_timeout_kills_spawned_child(tmp_path):
    """The reviewer's reproduction: a backgrounded child must not survive."""
    r = flow._shell(_ctx(tmp_path), tmp_path, f"bash -c '{CHILD}'", tmp_path / "s.log", 1, "probe", "simulation")
    assert r.status == "timeout"
    assert _gone(_child(tmp_path))


def test_tool_run_timeout_kills_spawned_child(tmp_path):
    r = _ctx(tmp_path).tools.run("synthesis", ["-c", CHILD], tmp_path, tmp_path / "t.log", timeout=1)
    assert r.status == "timeout"
    assert _gone(_child(tmp_path))


def test_normal_exit_leaves_no_descendants(tmp_path):
    with open(tmp_path / "o.log", "w") as fh:
        r = proc.run(["bash", "-c", "sleep 30 & echo $! > child.pid; exit 0"], cwd=str(tmp_path), timeout=20,
                     stdout=fh, stderr=fh)
    assert r.returncode == 0 and not r.timed_out and r.leftovers_killed
    assert _gone(_child(tmp_path))


def test_run_budget_caps_every_subprocess(tmp_path):
    proc.set_deadline(time.time() + 1.0)
    t0 = time.monotonic()
    with open(tmp_path / "o.log", "w") as fh:
        r = proc.run(["bash", "-c", CHILD], cwd=str(tmp_path), timeout=600, stdout=fh, stderr=fh)
    assert r.timed_out and r.budget_expired and time.monotonic() - t0 < 10
    assert _gone(_child(tmp_path))
    r2 = proc.run(["true"], timeout=600)                 # nothing starts after expiry
    assert r2.timed_out and r2.budget_expired and r2.returncode is None


def test_unattended_agent_timeout_kills_its_children(tmp_path):
    from cedar.workflow.tasks import Task, run_agent_command
    pf = tmp_path / "task.md"
    pf.write_text("prompt")
    t = Task(id="t1", kind="direct_rewrite", prompt_file=str(pf), outputs=[])
    info = run_agent_command(f"bash -c '{CHILD}'", t, tmp_path, timeout=1)
    assert info["timed_out"] and info["returncode"] is None
    assert _gone(_child(tmp_path))


# ------------------------------------------------------------- run budget

RTL = "module m(input clk, input [3:0] a, b, output reg [3:0] y); always @(posedge clk) y <= a * b + a; endmodule\n"
VARIANT = "module m(input clk, input [3:0] a, b, output reg [3:0] y); always @(posedge clk) y <= a * (b + 4'd1); endmodule\n"


def _fake_measure(self, name, rtl):
    from cedar.metrics import PPA, Metric
    ppa = PPA({"area": Metric.measured("area", 90.0 if name != "baseline" else 100.0, "um2", "post-synthesis", "stub"),
               "delay": Metric.measured("delay", 1.0, "ns", "post-synthesis", "stub"),
               "slack": Metric.measured("slack", 0.1, "ns", "post-synthesis", "stub"),
               "power": Metric.measured("power", 80.0, "uW", "post-route", "stub")},
              True, self.s["constraints_id"], "full-ppa")
    return {"ppa": ppa.to_dict(), "stages": [], "pruned": False, "complete": True, "info": {}}


def _run(tmp_path, toolchain, monkeypatch, measure=_fake_measure, **create):
    from cedar.workflow import engine as E
    monkeypatch.setenv("CEDAR_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(E.Run, "_measure", measure)
    monkeypatch.setattr(E.Run, "_verify", lambda self, n, r: {"status": "passed", "backend": "stub", "detail": "stub",
                                                               "seconds": 0.0, "evidence": ""})
    monkeypatch.setattr(E.flow, "synthesize", lambda *a, **k: type("S", (), {"ok": False, "status": "skipped",
                                                                             "detail": "stub", "outputs": {}})())
    monkeypatch.setattr(E.flow, "synth_metrics", lambda *a, **k: {})
    monkeypatch.setattr(E.Run, "phase_attribution", lambda self: "next")
    monkeypatch.setattr(E.Run, "_attribute", lambda self, name, rec: None)
    for n in ("a.db", "a.lib", "a.v", "rc", "tech.lef", "cells.lef"):
        (tmp_path / n).write_text("")
    (tmp_path / "m.v").write_text(RTL)
    stages = {"synthesis": "synopsys-dc", "place_route": "cadence-innovus", "simulation": "synopsys-vcs",
              "saif": "synopsys-vcd2saif", "power": "synopsys-primetime", "equivalence": "cadence-conformal"}
    (tmp_path / "tools.yaml").write_text(yaml.safe_dump({"schema": "cedar/tool-profile@1", "name": "t", "tools": {
        k: {"adapter": v, "executable": "true"} for k, v in stages.items()}}))
    (tmp_path / "tech.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/technology@1", "name": "x", "units": {"time": "ps"}, "default_corner": "tt",
        "corners": {"tt": {"db": ["a.db"], "liberty": ["a.lib"], "sim_models": ["a.v"], "rc_tech": "rc"}},
        "lef": {"tech": "tech.lef", "cells": ["cells.lef"]}, "place_route": {"site": "core"}}))
    (tmp_path / "design.yaml").write_text(yaml.safe_dump({
        "schema": "cedar/design@1", "name": "m", "top": "m", "rtl": ["m.v"],
        "profiles": {"tools": "tools.yaml", "technology": "tech.yaml"},
        "clocks": [{"port": "clk", "period_ns": 1.0}], "activity": {"mode": "testbench", "testbench": "m.v"},
        "verification": {"mode": "conformal"}, "objective": {"preset": "balanced"},
        "search": {"preset": "quick", "rules_to_propose": 0}}))
    sel = str(toolchain.bin_dir.parent) if toolchain.bin_dir.name == "bin" else None
    return E.Run.create(str(tmp_path / "design.yaml"), str(tmp_path / "runs"), toolchain_prefix=sel, **create)


def _answer(run, text=VARIANT):
    t = run.pending_task()
    Path(t.outputs[0].path).write_text(text)
    Path(t.outputs[-1].path).write_text('{"variants": []}')
    return run.submit(t.id, {"host": "test"})


def test_budget_is_persisted_and_configurable(tmp_path, toolchain, monkeypatch):
    run = _run(tmp_path, toolchain, monkeypatch, run_timeout_s=120)
    b = json.loads((run.dir / "run.json").read_text())["budget"]
    assert b["seconds"] == 120 and abs(b["deadline_epoch"] - b["started_at_epoch"] - 120) < 1
    assert run.settings["run_timeout_s"] == 120
    from cedar.workflow import presets
    assert presets.resolve({"preset": "quick"})["run_timeout_s"] == 3600
    with pytest.raises(ValueError):
        presets.resolve({"preset": "quick", "run_timeout_s": 5})


def test_expired_budget_stops_across_resume_and_rejects_work(tmp_path, toolchain, monkeypatch):
    from cedar.workflow import engine as E
    run = _run(tmp_path, toolchain, monkeypatch)
    assert run.advance() == "awaiting_agent"
    run.s["budget"]["deadline_epoch"] = time.time() - 1          # the budget runs out while the agent works
    run.save()
    r2 = E.Run(run.dir)
    ok, msg = _answer(r2)
    assert not ok and "budget" in msg
    assert r2.advance() == "failed" and r2.s["expired"] and "budget" in r2.s["error"]
    r3 = E.Run(run.dir)                                          # `cedar run continue --retry`
    r3.s["status"] = "running"
    assert r3.advance() == "failed" and r3.s["status"] == "failed"
    assert not (run.dir / "report.json").exists()
    assert not any(it.get("successful") for it in r3.s["iterations"])


def test_budget_expiring_during_measurement_gets_no_credit(tmp_path, toolchain, monkeypatch):
    def measure(self, name, rtl):
        if name == "iter01":
            # the budget runs out while a measurement subprocess is running
            self.s["budget"]["deadline_epoch"] = time.time() + 1.0
            proc.set_deadline(self.s["budget"]["deadline_epoch"])
            work = self.dir / "measure" / name
            work.mkdir(parents=True, exist_ok=True)
            st = flow._shell(self.ctx, work, f"bash -c '{CHILD}'", work / "sim.log", 600, "activity_sim", "simulation")
            assert st.status == "timeout" and st.evidence["budget_expired"]
            assert _gone(_child(work))
            md = _fake_measure(self, name, rtl)
            md["stages"], md["complete"] = [st.to_dict()], False
            return md
        return _fake_measure(self, name, rtl)

    run = _run(tmp_path, toolchain, monkeypatch, measure)
    assert run.advance() == "awaiting_agent"
    assert _answer(run)[0]
    assert run.advance() == "failed"
    assert run.s["status"] == "failed" and run.s["expired"]["during"].startswith("iter")
    assert not any(it.get("successful") for it in run.s["iterations"])
    assert not (run.dir / "report.json").exists()


def test_report_written_as_budget_expires_is_withdrawn(tmp_path, toolchain, monkeypatch):
    from cedar.workflow import engine as E
    orig = E.Run.phase_report

    def late_report(self):
        res = orig(self)
        self.s["budget"]["deadline_epoch"] = time.time() - 1
        return res

    monkeypatch.setattr(E.Run, "phase_report", late_report)
    run = _run(tmp_path, toolchain, monkeypatch)
    assert run.advance() == "awaiting_agent"
    assert _answer(run)[0]
    assert run.advance() == "failed"
    rep = json.loads((run.dir / "report.json").read_text())
    assert rep["final_status"] == "failed" and rep["budget"]["expired"] is True
    assert rep["execution_contract"]["status"] == "incomplete"
    assert E.Run(run.dir).advance() == "failed"                  # never flips to completed


AGENT = r'''
import json, sys, time
from pathlib import Path
s = json.loads(Path("run.json").read_text())
t = next(v for v in s["tasks"].values() if v["status"] == "pending")
Path(t["outputs"][0]["path"]).write_text(sys.argv[1].replace("\\n", "\n"))
Path(t["outputs"][-1]["path"]).write_text('{"variants": []}')
mode = sys.argv[2]
if mode == "fail":
    sys.exit(3)
if mode == "hang":
    time.sleep(60)
'''


@pytest.mark.parametrize("mode, why", [("fail", "exited with status 3"), ("hang", "timed out")])
def test_unattended_agent_failure_is_not_accepted(mode, why, tmp_path, toolchain, monkeypatch):
    from cedar.workflow import engine as E
    script = tmp_path / "agent.py"
    script.write_text(AGENT)
    monkeypatch.setattr(E, "run_agent_command", functools.partial(E.run_agent_command, timeout=3))
    run = _run(tmp_path, toolchain, monkeypatch)
    cmd = f"{sys.executable} {script} {json.dumps(VARIANT.replace(chr(10), chr(92) + 'n'))} {mode}"
    assert run.advance(agent_cmd=cmd) == "failed"
    t = next(v for v in run.s["tasks"].values() if v["kind"] == "direct_rewrite")
    assert t["status"] == "pending" and t["attempts"] == 2 and why in t["feedback"]
    assert why in run.s["error"]
    assert "direct_1" not in run.s["candidates"]                 # the written files were never accepted


# ------------------------------------------------------- in-process Z3 work

HARD_RULE = """(rule ((= ?m (comb_mux ?s (comb_mul ?a ?ops ?t) ?z ?t))
       (= ?z (hw_constant (NamedAttr "value" (IntegerAttr 0 ?t)) ?t))
       (= 1 (vec-length ?ops)))
      ((union ?m (comb_mul (comb_mux ?s ?a ?z ?t) (vec-push (vec-empty) (vec-get ?ops 0)) ?t)))
      :ruleset rules)"""                       # Z3 needs well over a minute at 16 bits


def test_budget_cuts_a_running_proof(template_sigs):
    from cedar.rules import certify as Cert
    proc.set_deadline(time.time() + 0.3)
    t0 = time.monotonic()
    with pytest.raises(proc.DeadlineExpired):
        Cert.prove_instance("hard", HARD_RULE, 16, template_sigs, timeout_ms=60000)
    assert time.monotonic() - t0 < 3.0          # not the 60 s solver allowance


def test_no_proof_starts_after_expiry(template_sigs, monkeypatch):
    from cedar.rules import certify as Cert
    from cedar.rules import z3verify as zv
    calls = []
    real = zv.verify_rule

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(zv, "verify_rule", counting)
    from importlib import resources
    seeds = {f.name[:-4]: f.read_text() for f in resources.files("cedar").joinpath("data/rules").iterdir()
             if f.name.endswith(".egg")}
    rules = {"a_hard": HARD_RULE, **seeds}       # sorted first: the hard proof runs into the deadline
    proc.set_deadline(time.time() + 0.3)
    with pytest.raises(proc.DeadlineExpired):
        Cert.admit(rules, [16, 8], None, template_sigs, timeout_ms=60000)
    assert len(calls) == 1                       # nothing after the interrupted proof
    calls.clear()
    proc.set_deadline(time.time() - 1)
    with pytest.raises(proc.DeadlineExpired):
        Cert.admit(seeds, [8], None, template_sigs)
    assert calls == []                           # and nothing at all once expired
    with pytest.raises(proc.DeadlineExpired):    # re-certification goes through prove_instance
        Cert.prove_instance("seed", next(iter(seeds.values())), 8, template_sigs)


def test_cached_admission_also_stops_at_expiry(tmp_path, template_sigs):
    """Even certificate reuse (no Z3 work) stops iterating once the budget expired."""
    from importlib import resources

    from cedar.rules import certify as Cert
    seeds = {f.name[:-4]: f.read_text() for f in resources.files("cedar").joinpath("data/rules").iterdir()
             if f.name.endswith(".egg")}
    store = Cert.CertificateStore(tmp_path / "certs", template_sigs)
    assert Cert.admit(seeds, [8], store, template_sigs).admitted       # warm cache, no deadline
    proc.set_deadline(time.time() - 1)
    with pytest.raises(proc.DeadlineExpired):
        Cert.admit(seeds, [8], store, template_sigs)


def test_solver_timeout_is_never_zero():
    assert proc.bounded_ms(0) == 1               # 0 would mean "no limit" to Z3
    proc.set_deadline(time.time() + 0.0005)
    time.sleep(0.002)
    with pytest.raises(proc.DeadlineExpired):
        proc.bounded_ms(60000)
    proc.set_deadline(time.time() + 2.0)
    assert 1 <= proc.bounded_ms(60000) <= 2000


def test_budget_caps_the_miter_solver(tmp_path, toolchain, monkeypatch):
    """The circt-lec miter solve gets the remaining budget as its Z3 timeout,
    and a solve that outlasts the budget yields no verdict."""
    import z3

    from cedar import verification as V
    a, b = tmp_path / "a.v", tmp_path / "b.v"
    a.write_text("module m(input [7:0] x, y, output [7:0] o); assign o = x * y + x; endmodule\n")
    b.write_text("module m(input [7:0] x, y, output [7:0] o); assign o = x * (y + 8'd1); endmodule\n")
    asked, given = [], []
    real_bounded = proc.bounded_ms

    def at_solve(ms):                           # the budget is nearly spent when the miter is solved
        asked.append(ms)
        proc.set_deadline(time.time() + 0.4)
        return real_bounded(ms)

    class SlowSolver(z3.Solver):                # a real solver whose solve outlasts the budget
        def set(self, *a, **k):
            if a and a[0] == "timeout":
                given.append(a[1])
            return super().set(*a, **k)

        def check(self, *a):
            time.sleep(0.6)
            return super().check(*a)

    monkeypatch.setattr(V.proc, "bounded_ms", at_solve)
    monkeypatch.setattr(z3, "Solver", SlowSolver)
    with pytest.raises(proc.DeadlineExpired):
        V.check("circt-lec", [str(a)], [str(b)], "m", tmp_path / "lec", toolchain=toolchain)
    assert asked == [120000] and given and 1 <= given[0] <= 400


def test_run_expiring_during_rule_admission_fails(tmp_path, toolchain, monkeypatch):
    from cedar.workflow import engine as E
    real = E.Run.phase_rules_admit

    def admit_with_hard_rule(self):
        self.s["budget"]["deadline_epoch"] = time.time() + 0.5
        proc.set_deadline(self.s["budget"]["deadline_epoch"])
        from cedar.rules import certify as Cert
        Cert.admit({"hard": HARD_RULE}, [16], None, Cert.signatures_from_template(
            self.tc.template("circt.egg").read_text()), timeout_ms=60000)
        return real(self)

    monkeypatch.setattr(E.Run, "phase_rules_admit", admit_with_hard_rule)
    run = _run(tmp_path, toolchain, monkeypatch)
    assert run.advance() == "awaiting_agent"
    assert _answer(run)[0]
    t0 = time.monotonic()
    assert run.advance() == "failed"
    assert time.monotonic() - t0 < 30
    assert run.s["expired"]["during"] == "rules_admit"
    assert E.Run(run.dir).advance() == "failed"                  # sticky


# ------------------------------------------- standalone `cedar task submit`

def _slow_frontend_prefix(tmp_path, toolchain, body):
    """A toolchain prefix whose circt-verilog runs `body` first (real tool after)."""
    import shlex

    from cedar.toolchain import resolve
    prefix = tmp_path / "slow-prefix"                 # works for a prefix and for the wheel toolchain
    (prefix / "bin").mkdir(parents=True)
    (prefix / "share").mkdir()
    (prefix / "share" / "cedar").symlink_to(toolchain.share_dir)
    for p in toolchain.bin_dir.iterdir():
        if p.name != "circt-verilog":
            (prefix / "bin" / p.name).symlink_to(p)
    w = prefix / "bin" / "circt-verilog"
    w.write_text(f"#!/bin/sh\n{body}\nexec {shlex.quote(toolchain.tool('circt-verilog'))} \"$@\"\n")
    w.chmod(0o755)
    return resolve(str(prefix))


def _pending_with_outputs(run):
    t = run.pending_task()
    for o in t.outputs:
        Path(o.path).write_text(VARIANT if o.kind == "verilog" else '{"variants": []}')
    return t


def test_standalone_submit_validation_is_bounded_by_the_budget(tmp_path, toolchain, monkeypatch):
    from cedar.cli import main
    from cedar.workflow import engine as E
    flag, pidf = tmp_path / "slow", tmp_path / "child.pid"
    tc = _slow_frontend_prefix(tmp_path, toolchain,
                               f'if [ -e "{flag}" ]; then sleep 30 & echo $! > "{pidf}"; wait; fi')
    run = _run(tmp_path, tc, monkeypatch)
    assert run.advance() == "awaiting_agent"
    t = _pending_with_outputs(run)
    flag.touch()                                                  # the frontend now needs 30 s
    run.s["budget"]["deadline_epoch"] = time.time() + 1.0
    run.save()
    monkeypatch.delenv("CEDAR_TOOLCHAIN_PREFIX", raising=False)
    t0 = time.monotonic()
    rc = main(["task", "submit", "--run", str(run.dir), t.id])
    assert rc != 0 and time.monotonic() - t0 < 10                 # not the 30 s / 300 s frontend
    assert proc.deadline() is None                                # standalone: nothing left installed
    assert _gone(int(pidf.read_text()))                           # the frontend's child was terminated
    s = E.Run(run.dir).s
    assert s["tasks"][t.id]["status"] == "pending"                # never accepted
    assert s["status"] == "failed" and s["expired"]["during"] == f"task {t.id} validation"
    assert main(["run", "continue", "--run", str(run.dir), "--retry"]) != 0
    assert E.Run(run.dir).s["status"] == "failed"


def test_no_acceptance_once_the_budget_expired(tmp_path, toolchain, monkeypatch):
    from cedar.cli import main
    from cedar.workflow import engine as E
    calls = tmp_path / "frontend-calls"
    tc = _slow_frontend_prefix(tmp_path, toolchain, f'echo x >> "{calls}"')
    run = _run(tmp_path, tc, monkeypatch)
    assert run.advance() == "awaiting_agent"
    t = _pending_with_outputs(run)
    n = calls.read_text().count("x") if calls.exists() else 0
    run.s["budget"]["deadline_epoch"] = time.time() - 1
    run.save()
    monkeypatch.delenv("CEDAR_TOOLCHAIN_PREFIX", raising=False)
    assert main(["task", "submit", "--run", str(run.dir), t.id]) != 0
    assert (calls.read_text().count("x") if calls.exists() else 0) == n   # no validation process started
    s = E.Run(run.dir).s
    assert s["tasks"][t.id]["status"] == "pending" and s["status"] == "failed" and s["expired"]
    assert proc.deadline() is None


def test_submit_restores_an_enclosing_deadline(tmp_path, toolchain, monkeypatch):
    run = _run(tmp_path, toolchain, monkeypatch)
    assert run.advance() == "awaiting_agent"
    t = _pending_with_outputs(run)
    outer = time.time() + 300.0                                   # e.g. advance() running an unattended agent
    proc.set_deadline(outer)
    ok, msg = run.submit(t.id, {"host": "test"})
    assert ok, msg
    assert proc.deadline() == outer                               # restored, not cleared
    # the tighter deadline wins: an enclosing deadline that already passed refuses the submission
    t2 = run.s["tasks"][t.id]
    t2["status"] = "pending"
    run.save()
    proc.set_deadline(time.time() - 1)
    ok, msg = run.submit(t.id, {"host": "test"})
    assert not ok and "budget" in msg and run.s["status"] == "failed"
