# SPDX-License-Identifier: Apache-2.0
"""Guided setup backend shared by the Claude Code and Codex ``cedar-setup`` skills.

The agent asks the questions from :data:`QUESTIONS` (``cedar setup questions``),
collects answers into a YAML/JSON file and calls ``cedar setup write-profile``;
this module validates the answers against the same schema used at run time
and persists the profile under the config home. Users can also skip the agent
and write the YAML files directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from . import config as cfgmod
from .config import specs
from .config.schema import ConfigError, check, describe

DEFAULT_EXECUTABLES = {
    "synthesis": ("synopsys-dc", ["dc_shell", "dc_shell-xg-t", "dcnxt_shell"]),
    "place_route": ("cadence-innovus", ["innovus"]),
    "simulation": ("synopsys-vcs", ["vcs"]),
    "saif": ("synopsys-vcd2saif", ["vcd2saif"]),
    "power": ("synopsys-primetime", ["pt_shell"]),
    "equivalence": ("cadence-conformal", ["lec"]),
}

QUESTIONS: List[Dict] = [
    {"id": "profile_name", "layer": "tools", "ask": "Name for this tool profile (e.g. 'my-site')?"},
    {"id": "setup_script", "layer": "tools",
     "ask": "Path to a bash file that prepares the EDA environment (module loads, license variables)? "
            "It is sourced before every tool call. Leave empty if tools are already on PATH."},
    {"id": "env_required", "layer": "tools",
     "ask": "Names (not values) of environment variables the tools need, e.g. LM_LICENSE_FILE, CDS_LIC_FILE?"},
    {"id": "tools", "layer": "tools",
     "ask": "For each stage confirm the executable (run `cedar setup discover` first): synthesis (DC), "
            "place_route (Innovus), simulation (VCS), saif (vcd2saif), power (PrimeTime), "
            "equivalence (Conformal). Also ask for per-stage timeouts if the defaults (3600 s) are unsuitable."},
    {"id": "toolchain_prefix", "layer": "tools",
     "ask": "Use the installed cedar-toolchain wheel (default) or a native build prefix (path with bin/ and share/cedar/)?"},
    {"id": "technology_name", "layer": "technology", "ask": "Name for the technology profile (e.g. 'mypdk-tt')?"},
    {"id": "time_unit", "layer": "technology", "ask": "Library time unit used in SDC and timing reports: ps or ns?"},
    {"id": "corners", "layer": "technology",
     "ask": "For each PVT corner: Synopsys .db files, Liberty .lib files, Verilog cell models for gate-level "
            "simulation, and the QRC/RC tech file (globs allowed). Which corner is the default?"},
    {"id": "lef", "layer": "technology", "ask": "Technology LEF and standard-cell LEF files for P&R?"},
    {"id": "place_route", "layer": "technology",
     "ask": "Placement site name, target utilization, filler cells, routing layer range, power/ground net names; "
            "any site Tcl hooks to source after init_design or before routing?"},
    {"id": "synthesis", "layer": "technology",
     "ask": "DC compile command (default: compile -map_effort high -area_effort high -no_design_rule -power_effort high), "
            "max transition / input transition in library time units, dont_use cells, optional pre/post compile Tcl hooks?"},
    {"id": "equivalence", "layer": "technology",
     "ask": "Extra Conformal dofile commands needed at your site (e.g. library or black-box settings)?"},
    {"id": "design", "layer": "design",
     "ask": "Per design: RTL files, top module, include dirs/defines, clock port and the TIMING GOAL as a period in ns "
            "(or a unit-qualified frequency such as '1.25 GHz'), resets (port, active level, sync/async), activity "
            "testbench (or generated stimulus), verification backend (conformal | simulation with a self-checking "
            "testbench | circt-lec for combinational designs), objective preset or explicit area/delay/power weights. "
            "Never pick the timing goal by frequency search; ask the user."},
]


def discover() -> Dict[str, Dict[str, Optional[str]]]:
    """Find candidate executables on the current PATH (no environment changes)."""
    out: Dict[str, Dict[str, Optional[str]]] = {}
    for stage, (adapter, names) in DEFAULT_EXECUTABLES.items():
        hits = {n: shutil.which(n) for n in names}
        found = [n for n, p in hits.items() if p]
        out[stage] = {"adapter": adapter, "candidates": hits, "suggested": found[0] if found else None}
    return out


def questions_text() -> str:
    lines = ["Cedar guided setup — ask the user these, then write YAML answers and run "
             "`cedar setup write-profile --kind tools|technology --answers FILE`.", ""]
    for q in QUESTIONS:
        lines.append(f"[{q['layer']}] {q['id']}: {q['ask']}")
    lines += ["", "Tool profile schema (cedar/tool-profile@1):"] + describe(specs.TOOL_PROFILE, 1)
    lines += ["", "Technology profile schema (cedar/technology@1):"] + describe(specs.TECHNOLOGY, 1)
    lines += ["", "Design spec schema (cedar/design@1):"] + describe(specs.DESIGN, 1)
    return "\n".join(lines)


def write_profile(kind: str, answers_path: Path, name: Optional[str] = None, force: bool = False,
                  dest_dir: Optional[Path] = None) -> Path:
    """Validate answers (a profile document) and save it under the config home."""
    data = yaml.safe_load(Path(answers_path).read_text())
    if not isinstance(data, dict):
        raise ConfigError(["answers must be a mapping"], str(answers_path))
    spec = {"tools": specs.TOOL_PROFILE, "technology": specs.TECHNOLOGY}[kind]
    data.setdefault("schema", "cedar/tool-profile@1" if kind == "tools" else "cedar/technology@1")
    if name:
        data["name"] = name
    sub = "profiles" if kind == "tools" else "technologies"
    dest_dir = dest_dir or (cfgmod.config_home() / sub)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{data.get('name', 'unnamed')}.yaml"
    # Relative paths in answers are resolved against the answers file's directory;
    # make them absolute so the saved profile is location independent.
    base = Path(answers_path).resolve().parent
    data = _absolutize(data, spec, base)
    _, errors = check(spec, data, dest_dir)
    if errors:
        raise ConfigError(errors, str(answers_path))
    if dest.exists() and not force:
        raise ConfigError([f"{dest} exists; pass --force to overwrite"])
    dest.write_text("# Written by `cedar setup write-profile` — private, do not commit.\n" +
                    yaml.safe_dump(data, sort_keys=False))
    os.chmod(dest, 0o600)
    return dest


def _absolutize(data, spec, base: Path):
    from .config.schema import F
    if not isinstance(data, dict):
        return data
    out = {}
    for k, v in data.items():
        f: Optional[F] = spec.get(k) if isinstance(spec, dict) else None
        out[k] = _abs_val(v, f, base) if f is not None else v
    return out


def _abs_val(v, f, base: Path):
    if v is None:
        return v
    if f.kind == "path" and isinstance(v, str) and "${" not in v and not v.startswith(("/", "~")):
        return str((base / v).resolve())
    if f.kind == "obj" and isinstance(v, dict):
        return _absolutize(v, f.fields or {}, base)
    if f.kind == "list" and isinstance(v, list):
        return [_abs_val(x, f.item, base) if f.item else x for x in v]
    if f.kind == "map" and isinstance(v, dict):
        return {k: (_abs_val(x, f.item, base) if f.item else x) for k, x in v.items()}
    return v


# ---------------------------------------------------------------- validation records

def tool_fingerprint(tools: cfgmod.Loaded) -> Dict[str, str]:
    """Resolved executables for every configured stage (identity for staleness)."""
    from .eda.runner import ToolEnv
    env = ToolEnv(tools)
    fp = {}
    for st in env.tools:
        r = env.which(st)
        fp[st] = r.detail if r.ok else f"<{r.status}>"
    return fp


def validation_key(tools: cfgmod.Loaded, tech: cfgmod.Loaded) -> str:
    return hashlib.sha256((tools.sha256 + tech.sha256).encode()).hexdigest()[:16]


def validation_path(tools: cfgmod.Loaded, tech: cfgmod.Loaded) -> Path:
    return cfgmod.state_home() / "validations" / f"{tools.data['name']}--{tech.data['name']}--{validation_key(tools, tech)}.json"


def save_validation(tools: cfgmod.Loaded, tech: cfgmod.Loaded, record: dict) -> Path:
    p = validation_path(tools, tech)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(record)
    rec.update({"tools_sha256": tools.sha256, "technology_sha256": tech.sha256,
                "fingerprint": tool_fingerprint(tools), "validated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    p.write_text(json.dumps(rec, indent=2))
    return p


def load_validation(tools: cfgmod.Loaded, tech: cfgmod.Loaded) -> Dict:
    p = validation_path(tools, tech)
    if not p.is_file():
        return {"state": "not-validated"}
    rec = json.loads(p.read_text())
    if rec.get("fingerprint") != tool_fingerprint(tools):
        return {"state": "stale", "reason": "tool executables changed since validation", "record": str(p)}
    return {"state": "validated" if rec.get("passed") else "validation-failed", "record": str(p),
            "validated_at": rec.get("validated_at"), "stages": rec.get("stages")}
