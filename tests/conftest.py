# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def toolchain():
    """The selected Cedar toolchain, or skip (never falls back to PATH)."""
    from cedar.toolchain import ToolchainError, resolve
    try:
        return resolve(os.environ.get("CEDAR_TOOLCHAIN_PREFIX"))
    except ToolchainError as exc:
        pytest.skip(f"no Cedar toolchain selected: {exc}")


@pytest.fixture(scope="session")
def template_sigs():
    """Operator signatures from the repo template, else the selected toolchain."""
    from cedar.rules import certify
    from cedar.toolchain import ToolchainError, resolve
    tmpl = ROOT / "toolchain" / "templates" / "circt.egg"
    if tmpl.is_file():
        return certify.signatures_from_template(tmpl.read_text())
    try:
        return certify.signatures_from_template(resolve().template("circt.egg").read_text())
    except ToolchainError:
        return certify.signatures_from_template(None)
