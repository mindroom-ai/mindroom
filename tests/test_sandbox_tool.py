"""Tests for sandbox workspace tool behavior."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import mindroom.tools  # noqa: F401
import mindroom.tools_metadata as tools_metadata_module
from mindroom.custom_tools.sandbox import SandboxTools

if TYPE_CHECKING:
    from pathlib import Path


def test_sandbox_tool_resets_workspace(tmp_path: Path, monkeypatch: object) -> None:
    """Reset requires confirmation and clears all persisted workspace files."""
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("mindroom.custom_tools.sandbox.MINDROOM_SANDBOX_WORKSPACE", workspace)

    tool = SandboxTools()
    (workspace / "keep.txt").write_text("hello", encoding="utf-8")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "data.txt").write_text("world", encoding="utf-8")

    rejected = tool.reset_workspace("nope")
    assert "Refusing reset" in rejected
    assert (workspace / "keep.txt").exists()

    accepted = tool.reset_workspace("RESET WORKSPACE")
    assert "Sandbox workspace reset" in accepted
    assert workspace.exists()
    assert list(workspace.iterdir()) == []


def test_sandbox_tool_resets_workspace_with_symlinks(tmp_path: Path, monkeypatch: object) -> None:
    """Reset should handle directory symlinks without crashing."""
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("mindroom.custom_tools.sandbox.MINDROOM_SANDBOX_WORKSPACE", workspace)

    tool = SandboxTools()

    # Create a real directory, a file, and a directory symlink.
    real_dir = tmp_path / "real_target"
    real_dir.mkdir()
    (real_dir / "target_file.txt").write_text("target", encoding="utf-8")

    (workspace / "regular_file.txt").write_text("hello", encoding="utf-8")
    (workspace / "regular_dir").mkdir()
    (workspace / "regular_dir" / "inner.txt").write_text("inner", encoding="utf-8")
    os.symlink(real_dir, workspace / "symlinked_dir")
    os.symlink(workspace / "regular_file.txt", workspace / "symlinked_file")

    result = tool.reset_workspace("RESET WORKSPACE")
    assert "Sandbox workspace reset" in result
    assert workspace.exists()
    assert list(workspace.iterdir()) == []
    # Symlink target should be untouched.
    assert real_dir.exists()
    assert (real_dir / "target_file.txt").exists()


def test_sandbox_tool_registration(tmp_path: Path, monkeypatch: object) -> None:
    """Tool registry should expose the sandbox tool and workspace metadata."""
    workspace = tmp_path / "workspace"
    monkeypatch.setattr("mindroom.custom_tools.sandbox.MINDROOM_SANDBOX_WORKSPACE", workspace)
    monkeypatch.setattr(tools_metadata_module, "MINDROOM_CONTAINER_SANDBOX", True)
    monkeypatch.setattr(tools_metadata_module, "MINDROOM_SANDBOX_WORKSPACE", workspace)

    tool = tools_metadata_module.get_tool_by_name("sandbox")
    assert isinstance(tool, SandboxTools)
    info = tool.workspace_info()
    assert str(workspace) in info
