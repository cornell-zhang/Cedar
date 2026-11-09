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
    from cedar.rules import certify
    return certify.signatures_from_template((ROOT / "toolchain" / "templates" / "circt.egg").read_text())
