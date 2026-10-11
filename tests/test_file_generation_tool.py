"""Tests for where the File Generation toolkit saves generated files."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.tools.file_generation import file_generation_tools

if TYPE_CHECKING:
    from pathlib import Path


def test_file_generation_saves_only_inside_the_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model-chosen names land in the workspace, never in the working directory or through planted links."""
    working_directory = tmp_path / "app"
    working_directory.mkdir()
    (working_directory / "config.yaml").write_text("agents: {}\n", encoding="utf-8")
    monkeypatch.chdir(working_directory)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched\n", encoding="utf-8")
    (workspace / "notes.txt").symlink_to(outside)
    toolkit = file_generation_tools()(tool_output_workspace_root=workspace, save_files=True)

    toolkit.generate_code_file(code="plugins: [evil]\n", filename="config.yaml")
    toolkit.generate_text_file(content="generated\n", filename="notes.txt")

    assert (working_directory / "config.yaml").read_text(encoding="utf-8") == "agents: {}\n"
    assert (workspace / "config.yaml").read_text(encoding="utf-8") == "plugins: [evil]\n"
    assert outside.read_text(encoding="utf-8") == "untouched\n"
    assert not (workspace / "notes.txt").is_symlink()
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "generated\n"


@pytest.mark.parametrize("output_directory", ["/etc", "../outside"])
def test_file_generation_refuses_output_directory_outside_the_workspace(tmp_path: Path, output_directory: str) -> None:
    """An authored output directory must stay inside the workspace."""
    with pytest.raises(ValueError, match="inside the agent workspace"):
        file_generation_tools()(tool_output_workspace_root=tmp_path, output_directory=output_directory)
