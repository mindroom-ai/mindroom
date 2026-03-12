"""Tests for the MindRoom Python tool wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.tools import python as python_tools_module

if TYPE_CHECKING:
    import pytest


@dataclass
class _FakeLogger:
    exceptions: list[str] = field(default_factory=list)

    def exception(self, message: str) -> None:
        self.exceptions.append(message)


class _DummyPythonTools:
    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs


def test_python_tools_excludes_plain_pip_install_but_keeps_uv() -> None:
    """MindRoom should only expose the uv-based package installer."""
    tool = python_tools_module.python_tools()()

    assert "pip_install_package" not in tool.functions
    assert "uv_pip_install_package" in tool.functions


def test_uv_pip_install_package_uses_shared_install_command_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Install should reuse the shared command builder and preserve Agno warnings."""
    commands: list[list[str]] = []
    calls: list[str] = []
    logger = _FakeLogger()

    monkeypatch.setattr(python_tools_module.subprocess, "check_call", lambda cmd: commands.append(cmd))
    monkeypatch.setattr(
        python_tools_module,
        "install_command_for_current_python",
        lambda: ["uv", "pip", "install", "--python", "/worker/python", "--system"],
    )
    monkeypatch.setattr(
        python_tools_module,
        "_python_tools_runtime",
        lambda: (
            _DummyPythonTools,
            lambda: calls.append("warn"),
            lambda message: calls.append(f"debug:{message}"),
            logger,
        ),
    )

    tool_cls = python_tools_module.python_tools()
    result = tool_cls().uv_pip_install_package("pyfiglet")

    assert result == "successfully installed package pyfiglet"
    assert commands == [["uv", "pip", "install", "--python", "/worker/python", "--system", "pyfiglet"]]
    assert calls == ["warn", "debug:Installing package pyfiglet"]
    assert logger.exceptions == []


def test_uv_pip_install_package_logs_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install failures should be logged and returned to the caller."""
    calls: list[str] = []
    logger = _FakeLogger()

    monkeypatch.setattr(
        python_tools_module,
        "_install_package_with_current_python",
        lambda _package_name: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        python_tools_module,
        "_python_tools_runtime",
        lambda: (
            _DummyPythonTools,
            lambda: calls.append("warn"),
            lambda message: calls.append(f"debug:{message}"),
            logger,
        ),
    )

    tool_cls = python_tools_module.python_tools()
    result = tool_cls().uv_pip_install_package("pyfiglet")

    assert result == "Error installing package pyfiglet: boom"
    assert calls == ["warn", "debug:Installing package pyfiglet"]
    assert logger.exceptions == ["Error installing package pyfiglet"]
