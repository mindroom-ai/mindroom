"""Tests for the native File Generation toolkit and workspace-confined saving."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import pytest

from mindroom.providers.openai.media import chat_file_part
from mindroom.tool_system.tool_function import ToolValue
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

    toolkit.generate_file(format="code", content="plugins: [evil]\n", filename="config.yaml")
    toolkit.generate_file(format="txt", content="generated\n", filename="notes.txt")

    assert (working_directory / "config.yaml").read_text(encoding="utf-8") == "agents: {}\n"
    assert (workspace / "config.yaml").read_text(encoding="utf-8") == "plugins: [evil]\n"
    assert outside.read_text(encoding="utf-8") == "untouched\n"
    assert not (workspace / "notes.txt").is_symlink()
    assert (workspace / "notes.txt").read_text(encoding="utf-8") == "generated\n"


def test_saved_file_reaches_the_model_as_the_bytes_the_tool_returned(tmp_path: Path) -> None:
    """Worker code can swap a saved file for a link later, so the model gets the returned bytes, not the path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("primary only\n", encoding="utf-8")
    toolkit = file_generation_tools()(tool_output_workspace_root=workspace, save_files=True)
    value = toolkit.generate_file(format="txt", content="generated\n", filename="notes.txt")
    (workspace / "notes.txt").unlink()
    (workspace / "notes.txt").symlink_to(outside)

    assert isinstance(value, ToolValue)
    file = value.files[0]
    part = chat_file_part(file, provider="OpenAI Chat")

    assert file.filepath is None
    assert part is not None
    assert part["file"]["filename"] == "notes.txt"
    assert base64.b64decode(part["file"]["file_data"].split(",", 1)[1]) == b"generated\n"


def test_file_generation_supports_all_formats_and_enable_flags(tmp_path: Path) -> None:
    """Each supported format produces a native ToolValue File artifact, and disabled formats fail cleanly."""
    toolkit = file_generation_tools()(tool_output_workspace_root=tmp_path)

    json_res = toolkit.generate_file(format="json", content={"a": 1}, filename="data")
    assert json_res.files[0].filename == "data.json"
    assert json.loads(json_res.files[0].content) == {"a": 1}

    csv_res = toolkit.generate_file(format="csv", content=[[1, 2]], headers=["x", "y"], filename="table")
    assert csv_res.files[0].filename == "table.csv"
    assert b"x,y" in csv_res.files[0].content

    pdf_res = toolkit.generate_file(format="pdf", content="Hello & <world>", title="Doc", filename="report")
    assert pdf_res.files[0].filename == "report.pdf"
    assert pdf_res.files[0].content.startswith(b"%PDF")

    docx_res = toolkit.generate_file(format="docx", content="Paragraph 1\n\nParagraph 2", title="Title", filename="doc")
    assert docx_res.files[0].filename == "doc.docx"
    assert docx_res.files[0].content.startswith(b"PK")

    html_res = toolkit.generate_file(format="html", content="<!doctype html><html><body>Hi</body></html>", filename="p")
    assert html_res.files[0].filename == "p.html"
    assert html_res.files[0].mime_type == "text/html"

    code_res = toolkit.generate_file(format="code", content="print(1)\n", language="python", filename="app")
    assert code_res.files[0].filename == "app.py"
    assert code_res.files[0].mime_type == "text/x-python"

    restricted = file_generation_tools()(enable_pdf_generation=False)
    disabled = restricted.generate_file(format="pdf", content="blocked")
    assert not disabled.files
    assert "disabled" in disabled.content


@pytest.mark.parametrize("output_directory", ["/etc", "../outside"])
def test_file_generation_refuses_output_directory_outside_the_workspace(tmp_path: Path, output_directory: str) -> None:
    """An authored output directory must stay inside the workspace."""
    with pytest.raises(ValueError, match="inside the agent workspace"):
        file_generation_tools()(tool_output_workspace_root=tmp_path, output_directory=output_directory)


def test_file_generation_rejects_bare_dict_for_csv_without_touching_existing_file(tmp_path: Path) -> None:
    """Unsupported CSV payloads fail before saving so an existing CSV on disk is never overwritten."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    existing = workspace / "report.csv"
    existing.write_text("name\nGrace\n", encoding="utf-8")
    toolkit = file_generation_tools()(tool_output_workspace_root=workspace, save_files=True)

    rejected = toolkit.generate_file(format="csv", content={"name": "Ada"}, filename="report.csv")

    assert not rejected.files
    assert "Error generating csv file:" in rejected.content
    assert "CSV content must be" in rejected.content
    assert existing.read_text(encoding="utf-8") == "name\nGrace\n"

    valid = toolkit.generate_file(format="csv", content=[{"name": "Ada"}], filename="rows.csv")
    assert len(valid.files) == 1
    assert b"name" in valid.files[0].content
    assert b"Ada" in valid.files[0].content
