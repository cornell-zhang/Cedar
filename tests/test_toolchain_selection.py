# SPDX-License-Identifier: Apache-2.0
import os
import stat

import pytest

from cedar import toolchain as T


def test_no_path_fallback(tmp_path, monkeypatch):
    # Put fake tools on PATH; resolution must still refuse to use them.
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    for b in T.REQUIRED_BINARIES:
        p = bin_ / b
        p.write_text("#!/bin/sh\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("CEDAR_TOOLCHAIN_PREFIX", raising=False)
    monkeypatch.setitem(__import__("sys").modules, "cedar_toolchain", None)
    with pytest.raises(T.ToolchainError):
        T.resolve()


def test_incomplete_prefix_rejected(tmp_path):
    (tmp_path / "bin").mkdir()
    (tmp_path / "share" / "cedar").mkdir(parents=True)
    with pytest.raises(T.ToolchainError) as e:
        T.resolve(str(tmp_path))
    assert "missing" in str(e.value)


def test_selected_toolchain_identity(toolchain):
    ident = toolchain.identity()
    assert set(T.REQUIRED_BINARIES) <= set(ident["binaries"])
    assert toolchain.template("circt.egg").is_file()
