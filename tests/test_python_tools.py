"""Tests for the MindRoom Python tool wrapper."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from mindroom.tools import python as python_tools_module

if TYPE_CHECKING:
    import pytest


def test_uv_pip_install_package_uses_uv_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the uv CLI when it is available in the image."""
    commands: list[list[str]] = []

    monkeypatch.setattr(python_tools_module.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
    monkeypatch.setattr(python_tools_module.subprocess, "check_call", lambda cmd: commands.append(cmd))

    tool_cls = python_tools_module.python_tools()
    result = tool_cls().uv_pip_install_package("pyfiglet")

    assert result == "successfully installed package pyfiglet"
    assert commands == [["/usr/bin/uv", "pip", "install", "--python", sys.executable, "pyfiglet"]]


def test_uv_pip_install_package_falls_back_to_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fall back to pip when the uv binary is unavailable."""
    commands: list[list[str]] = []

    monkeypatch.setattr(python_tools_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(python_tools_module.subprocess, "check_call", lambda cmd: commands.append(cmd))

    tool_cls = python_tools_module.python_tools()
    result = tool_cls().uv_pip_install_package("pyfiglet")

    assert result == "successfully installed package pyfiglet"
    assert commands == [[sys.executable, "-m", "pip", "install", "pyfiglet"]]
