"""Shared resolution of model-supplied file paths under the agent file_access setting."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.file_access import AuthorizedFile, resolve_agent_file

if TYPE_CHECKING:
    from pathlib import Path


def test_workspace_mode_accepts_relative_and_absolute_paths_inside_workspace(tmp_path: Path) -> None:
    """Workspace mode accepts relative and absolute paths that stay inside the workspace."""
    workspace = tmp_path / "ws"
    (workspace / "docs").mkdir(parents=True)
    report = workspace / "docs" / "report.pdf"
    report.write_bytes(b"pdf")
    for raw in ("docs/report.pdf", str(report)):
        authorized = resolve_agent_file(raw, workspace_root=workspace, file_access="workspace", field_name="attachment")
        assert authorized == AuthorizedFile(root=workspace.resolve(), path=report.resolve())


def test_workspace_mode_accepts_symlinks_that_stay_inside_workspace(tmp_path: Path) -> None:
    """Workspace mode follows symlinks whose target stays inside the workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "real.txt"
    target.write_text("r")
    (workspace / "alias.txt").symlink_to(target)
    authorized = resolve_agent_file(
        "alias.txt",
        workspace_root=workspace,
        file_access="workspace",
        field_name="attachment",
    )
    assert authorized.path == target.resolve()


def test_workspace_mode_rejects_paths_outside_workspace_and_escaping_symlinks(tmp_path: Path) -> None:
    """Workspace mode rejects outside paths, parent traversal, and escaping symlinks."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    (workspace / "link.txt").symlink_to(secret)
    for raw in (str(secret), "../secret.txt", "link.txt"):
        with pytest.raises(ValueError, match="attachment"):
            resolve_agent_file(raw, workspace_root=workspace, file_access="workspace", field_name="attachment")


def test_workspace_mode_without_workspace_refuses_paths(tmp_path: Path) -> None:
    """Workspace mode refuses every path when the agent has no workspace."""
    target = tmp_path / "a.txt"
    target.write_text("a")
    with pytest.raises(ValueError, match="workspace"):
        resolve_agent_file(str(target), workspace_root=None, file_access="workspace", field_name="attachment")


def test_unrestricted_mode_accepts_any_existing_regular_file(tmp_path: Path) -> None:
    """Unrestricted mode accepts any existing regular file, with or without a workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("o")
    authorized = resolve_agent_file(
        str(outside),
        workspace_root=workspace,
        file_access="unrestricted",
        field_name="attachment",
    )
    assert authorized.path == outside.resolve()
    assert authorized.path.is_relative_to(authorized.root)
    no_workspace = resolve_agent_file(
        str(outside),
        workspace_root=None,
        file_access="unrestricted",
        field_name="attachment",
    )
    assert no_workspace.path == outside.resolve()


def test_unrestricted_mode_resolves_relative_paths_against_workspace(tmp_path: Path) -> None:
    """Unrestricted mode resolves relative paths against the workspace."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_text("a")
    authorized = resolve_agent_file(
        "a.txt",
        workspace_root=workspace,
        file_access="unrestricted",
        field_name="attachment",
    )
    assert authorized.path == (workspace / "a.txt").resolve()


def test_unrestricted_mode_expands_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unrestricted mode expands a leading tilde."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "notes.txt").write_text("n")
    authorized = resolve_agent_file(
        "~/notes.txt",
        workspace_root=None,
        file_access="unrestricted",
        field_name="attachment",
    )
    assert authorized.path == (tmp_path / "notes.txt").resolve()


def test_both_modes_reject_missing_files_and_directories(tmp_path: Path) -> None:
    """Both modes reject missing paths and directories."""
    workspace = tmp_path / "ws"
    (workspace / "dir").mkdir(parents=True)
    for mode in ("workspace", "unrestricted"):
        for raw in ("missing.txt", "dir"):
            with pytest.raises(ValueError, match="attachment"):
                resolve_agent_file(raw, workspace_root=workspace, file_access=mode, field_name="attachment")
