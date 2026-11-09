# SPDX-License-Identifier: Apache-2.0
"""Locate the binaries and templates bundled in the cedar-toolchain wheel."""

import json
from pathlib import Path

__version__ = "0.1.0"
_HERE = Path(__file__).resolve().parent


def bin_dir() -> Path:
    return _HERE / "_bin"


def share_dir() -> Path:
    return _HERE / "_share"


def licenses_dir() -> Path:
    return _HERE / "_licenses"


def manifest() -> dict:
    p = _HERE / "cedar-toolchain-manifest.json"
    return json.loads(p.read_text()) if p.is_file() else {}
