# SPDX-License-Identifier: Apache-2.0
"""Configuration loading: tool profiles, technology profiles, design specs.

Name lookup: a profile reference that is not an existing path is looked up as
``<config-home>/profiles/<name>.yaml`` (tool profiles) or
``<config-home>/technologies/<name>.yaml`` (technology profiles), where
``<config-home>`` is ``$CEDAR_CONFIG_HOME`` or ``~/.config/cedar``. Relative
paths inside a file resolve against that file's directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from . import specs
from .schema import ConfigError, check

__all__ = [
    "ConfigError", "Loaded", "config_home", "state_home", "load_tool_profile", "load_technology",
    "load_design", "find_profile", "clock_period_ns", "constraints_id",
]


def config_home() -> Path:
    return Path(os.environ.get("CEDAR_CONFIG_HOME") or Path.home() / ".config" / "cedar")


def state_home() -> Path:
    return Path(os.environ.get("CEDAR_STATE_HOME") or Path.home() / ".local" / "state" / "cedar")


@dataclass
class Loaded:
    kind: str
    path: Path
    raw: Dict[str, Any]
    data: Dict[str, Any]
    sha256: str
    warnings: List[str] = field(default_factory=list)

    def p(self, *keys: str) -> Optional[str]:
        """Resolved path string for a nested path field, or None."""
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return None
            cur = cur[k]
        if isinstance(cur, dict) and "path" in cur:
            return cur["path"]
        return None

    def provenance(self) -> dict:
        """Non-secret snapshot: unexpanded raw config + hash (no env values)."""
        return {"kind": self.kind, "path": str(self.path), "sha256": self.sha256, "raw": self.raw}


def _read_yaml(path: Path) -> dict:
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError([f"cannot read: {exc}"], str(path))
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError([f"YAML syntax error: {exc}"], str(path))
    if not isinstance(data, dict):
        raise ConfigError(["top level must be a mapping"], str(path))
    return data


def _load(kind: str, spec: dict, path: Path) -> Loaded:
    path = path.resolve()
    raw = _read_yaml(path)
    data, errors = check(spec, raw, path.parent)
    if errors:
        raise ConfigError(errors, str(path))
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode()).hexdigest()
    return Loaded(kind, path, raw, data, digest)


def find_profile(ref: str, kind: str, relative_to: Optional[Path] = None) -> Path:
    """Resolve a profile reference (path or name)."""
    cand = Path(os.path.expanduser(ref))
    if not cand.is_absolute() and relative_to is not None:
        rel = (relative_to / cand)
        if rel.exists():
            return rel
    if cand.exists():
        return cand
    sub = {"tools": "profiles", "technology": "technologies"}[kind]
    for name in (ref, f"{ref}.yaml", f"{ref}.yml"):
        p = config_home() / sub / name
        if p.is_file():
            return p
    raise ConfigError([f"{kind} profile {ref!r} not found as a path or in {config_home() / sub}"])


def load_tool_profile(ref: str, relative_to: Optional[Path] = None) -> Loaded:
    return _load("tool-profile", specs.TOOL_PROFILE, find_profile(ref, "tools", relative_to))


def load_technology(ref: str, relative_to: Optional[Path] = None) -> Loaded:
    t = _load("technology", specs.TECHNOLOGY, find_profile(ref, "technology", relative_to))
    if t.data["default_corner"] not in t.data["corners"]:
        raise ConfigError([f"default_corner {t.data['default_corner']!r} is not in corners"], str(t.path))
    return t


_FREQ_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][-+]?\d+)?)\s*(Hz|kHz|MHz|GHz|THz)\s*$")
_FREQ_SCALE = {"Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9, "THz": 1e12}


def clock_period_ns(clock: dict) -> float:
    """Resolve a clock entry to a period in ns (explicit units required)."""
    has_p = clock.get("period_ns") is not None
    has_f = clock.get("frequency") is not None
    if has_p == has_f:
        raise ConfigError(["each clock needs exactly one of period_ns or frequency (e.g. '1 GHz')"])
    if has_p:
        return float(clock["period_ns"])
    m = _FREQ_RE.match(str(clock["frequency"]))
    if not m:
        raise ConfigError([f"frequency {clock['frequency']!r} must be a number with Hz/kHz/MHz/GHz units"])
    hz = float(m.group(1)) * _FREQ_SCALE[m.group(2)]
    if hz <= 0:
        raise ConfigError(["frequency must be positive"])
    return 1e9 / hz


def load_design(path: str) -> Loaded:
    d = _load("design", specs.DESIGN, Path(path))
    errs: List[str] = []
    data = d.data
    if not data["clocks"]:
        errs.append("clocks: at least one clock (the timing goal) is required")
    names = set()
    for i, c in enumerate(data["clocks"]):
        try:
            c["period_ns_resolved"] = clock_period_ns(c)
        except ConfigError as exc:
            errs += [f"clocks[{i}]: {e}" for e in exc.errors]
        if c.get("name") in names:
            errs.append(f"clocks[{i}].name duplicates {c.get('name')!r}")
        names.add(c.get("name"))
    if len(data["clocks"]) > 1 and not data.get("sdc"):
        errs.append("multiple clocks require a user 'sdc' with the clock mapping")
    act = data["activity"]
    if act["mode"] == "testbench" and not act.get("testbench"):
        errs.append("activity.testbench is required when activity.mode is 'testbench'")
    ver = data["verification"]
    if ver["mode"] == "simulation" and not ver.get("testbench"):
        errs.append("verification.testbench (self-checking) is required for simulation mode")
    if ver["mode"] == "simulation" and ver.get("testbench") and act.get("testbench") and \
            ver["testbench"]["path"] == act["testbench"]["path"]:
        errs.append("verification.testbench must be a self-checking testbench distinct from the activity testbench")
    obj = data.get("objective") or {}
    if not obj.get("preset") and not obj.get("weights"):
        errs.append("objective: choose a preset or explicit weights (area/delay/power)")
    search = data.get("search") or {}
    if search.get("opaque_modules") and not search.get("hierarchical"):
        errs.append("search.opaque_modules requires search.hierarchical: true")
    if errs:
        raise ConfigError(errs, str(d.path))
    return d


def constraints_id(design: Loaded, technology: Loaded, corner: str) -> str:
    """Hash of everything that defines the timing/measurement constraints."""
    data = design.data
    payload = {
        "clocks": [{"port": c.get("port"), "name": c.get("name"), "period_ns": c["period_ns_resolved"]} for c in data["clocks"]],
        "sdc": data.get("sdc", {}).get("path") if data.get("sdc") else None,
        "sdc_sha": _file_sha(data["sdc"]["path"]) if data.get("sdc") else None,
        "io_delay_fraction": data["io_delay_fraction"],
        "technology": technology.sha256,
        "corner": corner,
        "measurement": data.get("measurement"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


def _file_sha(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
