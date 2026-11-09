# SPDX-License-Identifier: Apache-2.0
"""A small declarative schema checker for Cedar's YAML configuration.

Features: required/optional fields, defaults, unknown-key rejection, enums,
numeric bounds, nested objects, lists and maps, and *path* fields that are
resolved relative to the configuration file's directory (never the shell's
cwd). ``${VAR}`` references in paths are expanded at resolution time; the
unexpanded text is kept for provenance so resolved secrets are never echoed.
Errors carry a dotted path to the offending field.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(ValueError):
    def __init__(self, errors: Sequence[str], source: str = ""):
        self.errors = list(errors)
        self.source = source
        head = f"{source}: " if source else ""
        super().__init__(head + "; ".join(self.errors))


@dataclass
class F:
    """Field specification."""

    kind: str                                  # str int float bool path enum obj list map any
    required: bool = False
    default: Any = None
    doc: str = ""
    unit: str = ""
    choices: Sequence[str] = ()
    min: Optional[float] = None
    max: Optional[float] = None
    fields: Optional[Dict[str, "F"]] = None    # for obj
    item: Optional["F"] = None                 # for list / map values
    must_exist: bool = True                    # for path
    pattern: Optional[str] = None              # regex for str
    glob: bool = False                         # path may be a glob pattern


def expand_env(text: str, missing: List[str]) -> str:
    def rep(m: "re.Match[str]") -> str:
        v = os.environ.get(m.group(1))
        if v is None:
            missing.append(m.group(1))
            return m.group(0)
        return v

    return _ENV_RE.sub(rep, text)


def resolve_path(raw: str, base: Path, missing_env: List[str]) -> Path:
    s = expand_env(os.path.expanduser(str(raw)), missing_env)
    p = Path(s)
    return p if p.is_absolute() else (base / p)


def check(spec: Dict[str, F], data: Any, base: Path, where: str = "") -> (Dict[str, Any], List[str]):
    """Validate ``data`` against an object spec. Returns (normalized, errors)."""
    errors: List[str] = []
    out: Dict[str, Any] = {}
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return {}, [f"{where or '<root>'}: expected a mapping"]
    for key in data:
        if key not in spec:
            near = ", ".join(sorted(spec))
            errors.append(f"{_j(where, key)}: unknown key (allowed: {near})")
    for key, f in spec.items():
        here = _j(where, key)
        if key not in data or data[key] is None:
            if f.required:
                errors.append(f"{here}: required" + (f" — {f.doc}" if f.doc else ""))
            elif f.default is not None:
                out[key] = _copy(f.default)
            elif f.kind == "obj" and f.fields is not None:
                # An absent optional object is only materialized (with its
                # defaults) if it has no required members; otherwise it stays
                # unset so that e.g. an unconfigured tool stage is not mistaken
                # for a configured one.
                if not any(sf.required for sf in f.fields.values()):
                    sub, errs = check(f.fields, {}, base, here)
                    out[key] = sub
                    errors += errs
            continue
        val, errs = _check_value(f, data[key], base, here)
        out[key] = val
        errors += errs
    return out, errors


def _copy(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_copy(x) for x in v]
    return v


def _j(where: str, key: Any) -> str:
    return f"{where}.{key}" if where else str(key)


def _check_value(f: F, v: Any, base: Path, here: str):
    errs: List[str] = []
    k = f.kind
    if k == "any":
        return v, errs
    if k == "str":
        if not isinstance(v, str):
            return v, [f"{here}: expected a string"]
        if f.pattern and not re.fullmatch(f.pattern, v):
            return v, [f"{here}: {v!r} does not match {f.pattern}"]
        return v, errs
    if k == "bool":
        if not isinstance(v, bool):
            return v, [f"{here}: expected true/false"]
        return v, errs
    if k in ("int", "float"):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or (k == "int" and not isinstance(v, int)):
            return v, [f"{here}: expected {'an integer' if k == 'int' else 'a number'}" + (f" in {f.unit}" if f.unit else "")]
        if f.min is not None and v < f.min:
            errs.append(f"{here}: must be >= {f.min}{(' ' + f.unit) if f.unit else ''}")
        if f.max is not None and v > f.max:
            errs.append(f"{here}: must be <= {f.max}{(' ' + f.unit) if f.unit else ''}")
        return (float(v) if k == "float" else v), errs
    if k == "enum":
        if v not in f.choices:
            return v, [f"{here}: must be one of {list(f.choices)} (got {v!r})"]
        return v, errs
    if k == "path":
        if not isinstance(v, str) or not v:
            return v, [f"{here}: expected a path string"]
        missing: List[str] = []
        p = resolve_path(v, base, missing)
        if missing:
            return {"raw": v, "path": None}, [f"{here}: environment variable(s) not set: {', '.join(missing)}"]
        if f.glob and any(ch in str(p) for ch in "*?["):
            hits = sorted(Path(p.anchor).glob(str(p.relative_to(p.anchor)))) if p.is_absolute() else []
            if f.must_exist and not hits:
                errs.append(f"{here}: glob {v!r} matched no files")
            return {"raw": v, "path": str(p), "matches": [str(h) for h in hits]}, errs
        if f.must_exist and not p.exists():
            errs.append(f"{here}: path does not exist: {v!r} (resolved {p})")
        return {"raw": v, "path": str(p)}, errs
    if k == "obj":
        return check(f.fields or {}, v, base, here)
    if k == "list":
        if not isinstance(v, list):
            return v, [f"{here}: expected a list"]
        out = []
        for i, item in enumerate(v):
            val, e = _check_value(f.item or F("any"), item, base, f"{here}[{i}]")
            out.append(val)
            errs += e
        return out, errs
    if k == "map":
        if not isinstance(v, dict):
            return v, [f"{here}: expected a mapping"]
        out = {}
        for key, item in v.items():
            if not isinstance(key, str):
                errs.append(f"{here}: keys must be strings")
                continue
            val, e = _check_value(f.item or F("any"), item, base, _j(here, key))
            out[key] = val
            errs += e
        return out, errs
    raise AssertionError(f"unknown field kind {k}")


def describe(spec: Dict[str, F], indent: int = 0) -> List[str]:
    """Human-readable schema documentation (used by `cedar setup questions`)."""
    lines: List[str] = []
    pad = "  " * indent
    for key, f in spec.items():
        bits = [f.kind]
        if f.required:
            bits.append("required")
        if f.unit:
            bits.append(f"unit={f.unit}")
        if f.choices:
            bits.append("one of " + "|".join(f.choices))
        if f.default is not None:
            bits.append(f"default={f.default!r}")
        lines.append(f"{pad}{key}: ({', '.join(bits)}) {f.doc}".rstrip())
        if f.kind == "obj" and f.fields:
            lines += describe(f.fields, indent + 1)
        if f.kind in ("list", "map") and f.item is not None and f.item.kind == "obj" and f.item.fields:
            lines += describe(f.item.fields, indent + 1)
    return lines


def to_json_schema(spec: Dict[str, F]) -> dict:
    """Export a JSON-Schema (draft 2020-12) rendering for editors/tools."""
    def conv(f: F) -> dict:
        d: dict = {"description": (f.doc + (f" [{f.unit}]" if f.unit else "")).strip()}
        if f.kind == "str":
            d["type"] = "string"
        elif f.kind == "path":
            d["type"] = "string"
            d["format"] = "path (relative to this file)"
        elif f.kind == "bool":
            d["type"] = "boolean"
        elif f.kind == "int":
            d["type"] = "integer"
        elif f.kind == "float":
            d["type"] = "number"
        elif f.kind == "enum":
            d["enum"] = list(f.choices)
        elif f.kind == "obj":
            d.update(obj(f.fields or {}))
        elif f.kind == "list":
            d["type"] = "array"
            d["items"] = conv(f.item or F("any"))
        elif f.kind == "map":
            d["type"] = "object"
            d["additionalProperties"] = conv(f.item or F("any"))
        if f.min is not None:
            d["minimum"] = f.min
        if f.max is not None:
            d["maximum"] = f.max
        if f.default is not None:
            d["default"] = f.default
        return d

    def obj(fields: Dict[str, F]) -> dict:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {k: conv(v) for k, v in fields.items()},
            "required": [k for k, v in fields.items() if v.required],
        }

    out = obj(spec)
    out["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return out
