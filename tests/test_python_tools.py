"""Tests for the MindRoom Python tool wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import RuntimePaths, resolve_primary_runtime_paths
from mindroom.tools import python as python_tools_module

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeLogger:
    exceptions: list[str] = field(default_factory=list)

    def exception(self, message: str) -> None:
        self.exceptions.append(message)


class _DummyPythonTools:
    def __init__(self, **kwargs: object) -> None:
        self.init_kwargs = kwargs


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def test_python_tools_preserve_both_install_entrypoints(tmp_path: Path) -> None:
    """MindRoom should keep both installer names available for compatibility."""
    tool = python_tools_module.python_tools()(runtime_paths=_runtime_paths(tmp_path))

    assert "pip_install_package" in tool.functions
    assert "uv_pip_install_package" in tool.functions


@pytest.mark.parametrize("installer_name", ["pip_install_package", "uv_pip_install_package"])
def test_python_tools_respect_include_tools_for_installers(installer_name: str, tmp_path: Path) -> None:
    """Toolkit include filters should still expose whichever installer was requested."""
    tool = python_tools_module.python_tools()(
        include_tools=[installer_name],
        runtime_paths=_runtime_paths(tmp_path),
    )

    assert sorted(tool.functions) == [installer_name]


@pytest.mark.parametrize("installer_name", ["pip_install_package", "uv_pip_install_package"])
def test_python_tool_installers_use_shared_install_command_and_warn(
    monkeypatch: pytest.MonkeyPatch,
    installer_name: str,
    tmp_path: Path,
) -> None:
    """Both installer names should reuse the shared command builder and warnings."""
    commands: list[list[str]] = []
    calls: list[str] = []
    logger = _FakeLogger()

    monkeypatch.setattr(python_tools_module.subprocess, "check_call", lambda cmd: commands.append(cmd))
    monkeypatch.setattr(
        python_tools_module,
        "install_command_for_current_python",
        lambda: ["/worker/python", "-m", "uv", "pip", "install", "--python", "/worker/python", "--system"],
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
    result = getattr(tool_cls(runtime_paths=_runtime_paths(tmp_path)), installer_name)("pyfiglet")

    assert result == "successfully installed package pyfiglet"
    assert commands == [
        ["/worker/python", "-m", "uv", "pip", "install", "--python", "/worker/python", "--system", "pyfiglet"],
    ]
    assert calls == ["warn", "debug:Installing package pyfiglet"]
    assert logger.exceptions == []


@pytest.mark.parametrize("installer_name", ["pip_install_package", "uv_pip_install_package"])
def test_python_tool_installers_log_errors(
    monkeypatch: pytest.MonkeyPatch,
    installer_name: str,
    tmp_path: Path,
) -> None:
    """Install failures should be logged and returned for both installer entrypoints."""
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
    result = getattr(tool_cls(runtime_paths=_runtime_paths(tmp_path)), installer_name)("pyfiglet")

    assert result == "Error installing package pyfiglet: boom"
    assert calls == ["warn", "debug:Installing package pyfiglet"]
    assert logger.exceptions == ["Error installing package pyfiglet"]
