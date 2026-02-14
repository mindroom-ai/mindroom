"""Tests for container sandbox runtime behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

import mindroom.tools  # noqa: F401
import mindroom.tools_metadata as tools_metadata_module
from tests.conftest import FakeCredentialsManager

if TYPE_CHECKING:
    from pathlib import Path


def test_container_sandbox_overrides_local_execution_tools(tmp_path: Path, monkeypatch: object) -> None:
    """Container sandbox mode should force local execution tools into workspace."""
    workspace = tmp_path / "workspace"
    fake_credentials = FakeCredentialsManager(
        {
            "file": {"base_dir": tmp_path / "file-creds"},
            "shell": {"base_dir": str(tmp_path / "shell-creds")},
            "python": {"base_dir": tmp_path / "python-creds", "restrict_to_base_dir": False},
        },
    )

    monkeypatch.setattr(tools_metadata_module, "MINDROOM_CONTAINER_SANDBOX", True)
    monkeypatch.setattr(tools_metadata_module, "MINDROOM_SANDBOX_WORKSPACE", workspace)
    monkeypatch.setattr(tools_metadata_module, "get_credentials_manager", lambda: fake_credentials)

    file_tool = tools_metadata_module.get_tool_by_name("file")
    shell_tool = tools_metadata_module.get_tool_by_name("shell")
    python_tool = tools_metadata_module.get_tool_by_name("python")

    assert file_tool.base_dir == workspace
    assert shell_tool.base_dir == workspace
    assert python_tool.base_dir == workspace
    assert python_tool.restrict_to_base_dir is True
    assert workspace.exists()


def test_non_sandbox_uses_tool_credentials(tmp_path: Path, monkeypatch: object) -> None:
    """When sandbox mode is disabled, tools should use configured credentials."""
    file_base_dir = tmp_path / "file-base"
    shell_base_dir = tmp_path / "shell-base"
    python_base_dir = tmp_path / "python-base"

    fake_credentials = FakeCredentialsManager(
        {
            "file": {"base_dir": file_base_dir},
            "shell": {"base_dir": str(shell_base_dir)},
            "python": {"base_dir": python_base_dir, "restrict_to_base_dir": False},
        },
    )

    monkeypatch.setattr(tools_metadata_module, "MINDROOM_CONTAINER_SANDBOX", False)
    monkeypatch.setattr(tools_metadata_module, "get_credentials_manager", lambda: fake_credentials)

    file_tool = tools_metadata_module.get_tool_by_name("file")
    shell_tool = tools_metadata_module.get_tool_by_name("shell")
    python_tool = tools_metadata_module.get_tool_by_name("python")

    assert file_tool.base_dir == file_base_dir.resolve()
    assert shell_tool.base_dir == shell_base_dir
    assert python_tool.base_dir == python_base_dir.resolve()
    assert python_tool.restrict_to_base_dir is False


def test_credential_overrides_take_precedence(tmp_path: Path, monkeypatch: object) -> None:
    """Per-call credential overrides should override stored credentials."""
    stored_base_dir = tmp_path / "stored-base"
    override_base_dir = tmp_path / "override-base"
    fake_credentials = FakeCredentialsManager({"file": {"base_dir": stored_base_dir}})

    monkeypatch.setattr(tools_metadata_module, "MINDROOM_CONTAINER_SANDBOX", False)
    monkeypatch.setattr(tools_metadata_module, "get_credentials_manager", lambda: fake_credentials)

    file_tool = tools_metadata_module.get_tool_by_name(
        "file",
        credential_overrides={"base_dir": override_base_dir},
    )

    assert file_tool.base_dir == override_base_dir.resolve()
