"""Tests for skill reference generator scripts."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from scripts.generate_llms_txt import (
    _collect_entries,
    _extract_description,
    _md_path_to_url,
    _read_full_content,
    _walk_nav,
    generate_llms_full_txt,
    generate_llms_txt,
)
from scripts.generate_source_index import (
    _collect_python_files,
    _extract_module_docstring,
    generate_source_index,
    generate_source_map,
)

# ---------------------------------------------------------------------------
# generate_llms_txt.py tests
# ---------------------------------------------------------------------------


class TestMdPathToUrl:
    """Test markdown path to URL conversion."""

    def test_index_md(self) -> None:
        """Root index.md maps to site root."""
        assert _md_path_to_url("index.md") == "https://docs.mindroom.chat/"

    def test_nested_index_md(self) -> None:
        """Nested index.md maps to parent directory URL."""
        assert _md_path_to_url("configuration/index.md") == "https://docs.mindroom.chat/configuration/"

    def test_regular_page(self) -> None:
        """Regular page strips .md and adds trailing slash."""
        assert _md_path_to_url("getting-started.md") == "https://docs.mindroom.chat/getting-started/"

    def test_nested_page(self) -> None:
        """Nested page preserves directory structure."""
        assert _md_path_to_url("configuration/agents.md") == "https://docs.mindroom.chat/configuration/agents/"


class TestExtractDescription:
    """Test first-paragraph extraction from markdown files."""

    def test_extracts_first_paragraph(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Extract the first non-heading paragraph line."""
        doc = tmp_path / "test.md"
        doc.write_text("# Title\n\nFirst paragraph line.\n\nSecond paragraph.\n")
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        assert _extract_description("test.md") == "First paragraph line."

    def test_skips_frontmatter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """YAML frontmatter is stripped before extracting description."""
        doc = tmp_path / "test.md"
        doc.write_text("---\ntitle: Test\n---\n\n# Title\n\nContent here.\n")
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        assert _extract_description("test.md") == "Content here."

    def test_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing file returns empty string."""
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        assert _extract_description("nonexistent.md") == ""

    def test_skips_headings_and_blockquotes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Headings and blockquotes are skipped."""
        doc = tmp_path / "test.md"
        doc.write_text("# Heading\n> Quote\n\nActual content.\n")
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        assert _extract_description("test.md") == "Actual content."

    def test_empty_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """File with only headings returns empty string."""
        doc = tmp_path / "test.md"
        doc.write_text("# Only a heading\n")
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        assert _extract_description("test.md") == ""


class TestReadFullContent:
    """Test full content reading with frontmatter stripping."""

    def test_strips_frontmatter(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """YAML frontmatter is removed from full content."""
        doc = tmp_path / "test.md"
        doc.write_text("---\ntitle: Test\n---\n\n# Hello\n\nWorld.\n")
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        content = _read_full_content("test.md")
        assert content.startswith("# Hello")
        assert "title: Test" not in content

    def test_missing_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing file returns empty string."""
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        assert _read_full_content("nonexistent.md") == ""


class TestCollectEntries:
    """Test nav entry collection from zensical nav structure."""

    def test_flat_entries(self) -> None:
        """Flat nav items produce (title, path) tuples."""
        nav = [{"Home": "index.md"}, {"About": "about.md"}]
        entries = _collect_entries(nav)
        assert entries == [("Home", "index.md"), ("About", "about.md")]

    def test_nested_entries(self) -> None:
        """Nested nav items are flattened."""
        nav = [{"Section": [{"Page": "page.md"}]}]
        entries = _collect_entries(nav)
        assert entries == [("Page", "page.md")]

    def test_skips_dev_paths(self) -> None:
        """Paths starting with dev/ are excluded."""
        nav = [{"Home": "index.md"}, {"Dev": "dev/internal.md"}]
        entries = _collect_entries(nav)
        assert entries == [("Home", "index.md")]


class TestWalkNav:
    """Test nav walking that groups entries by section."""

    def test_groups_sections(self) -> None:
        """Top-level items and nested sections are grouped separately."""
        nav = [
            {"Home": "index.md"},
            {"Config": [{"Overview": "config/index.md"}, {"Agents": "config/agents.md"}]},
        ]
        blocks = _walk_nav(nav)
        assert len(blocks) == 2
        # First block: top-level entries
        assert blocks[0][0] == ""  # no section title
        assert blocks[0][2] == [("Home", "index.md")]
        # Second block: Config section
        assert blocks[1][0] == "Config"
        assert len(blocks[1][2]) == 2


class TestGenerateLlmsTxt:
    """Test llms.txt and llms-full.txt generation."""

    def test_output_structure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Generated llms.txt has header, description, and URLs."""
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        (tmp_path / "index.md").write_text("# Home\n\nWelcome to MindRoom.\n")

        nav = [{"Home": "index.md"}]
        output = generate_llms_txt(nav)
        assert output.startswith("# MindRoom")
        assert "Welcome to MindRoom." in output
        assert "https://docs.mindroom.chat/" in output

    def test_full_txt_includes_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Generated llms-full.txt includes full page content."""
        monkeypatch.setattr("scripts.generate_llms_txt.DOCS_DIR", tmp_path)
        (tmp_path / "page.md").write_text("# Page Title\n\nDetailed content here.\n")

        nav = [{"Page": "page.md"}]
        output = generate_llms_full_txt(nav)
        assert "# MindRoom" in output
        assert "Detailed content here." in output


# ---------------------------------------------------------------------------
# generate_source_index.py tests
# ---------------------------------------------------------------------------


class TestCollectPythonFiles:
    """Test Python file discovery."""

    def test_finds_py_files(self, tmp_path: Path) -> None:
        """Only .py files are collected."""
        (tmp_path / "a.py").write_text("# a")
        (tmp_path / "b.py").write_text("# b")
        (tmp_path / "c.txt").write_text("not python")
        files = _collect_python_files(tmp_path)
        assert len(files) == 2
        assert all(f.suffix == ".py" for f in files)

    def test_skips_pycache(self, tmp_path: Path) -> None:
        """__pycache__ directories are excluded."""
        cache_dir = tmp_path / "__pycache__"
        cache_dir.mkdir()
        (cache_dir / "cached.py").write_text("# cached")
        (tmp_path / "real.py").write_text("# real")
        files = _collect_python_files(tmp_path)
        assert len(files) == 1
        assert files[0].name == "real.py"

    def test_recursive(self, tmp_path: Path) -> None:
        """Files in subdirectories are found."""
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "top.py").write_text("")
        (sub / "nested.py").write_text("")
        files = _collect_python_files(tmp_path)
        assert len(files) == 2


class TestExtractModuleDocstring:
    """Test module docstring extraction via AST."""

    def test_extracts_docstring(self, tmp_path: Path) -> None:
        """First line of module docstring is extracted."""
        f = tmp_path / "mod.py"
        f.write_text('"""Module docstring.\n\nMore details."""\n\nx = 1\n')
        assert _extract_module_docstring(f) == "Module docstring."

    def test_no_docstring(self, tmp_path: Path) -> None:
        """Files without docstrings return empty string."""
        f = tmp_path / "mod.py"
        f.write_text("x = 1\n")
        assert _extract_module_docstring(f) == ""

    def test_syntax_error(self, tmp_path: Path) -> None:
        """Files with syntax errors return empty string."""
        f = tmp_path / "bad.py"
        f.write_text("def broken(\n")
        assert _extract_module_docstring(f) == ""

    def test_multiline_first_line_only(self, tmp_path: Path) -> None:
        """Only the first line of a multiline docstring is returned."""
        f = tmp_path / "mod.py"
        f.write_text('"""First line.\nSecond line."""\n')
        assert _extract_module_docstring(f) == "First line."


class TestGenerateSourceIndex:
    """Test source index generation."""

    def test_output_format(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Source index has header and FILE markers with content."""
        monkeypatch.setattr("scripts.generate_source_index.REPO_ROOT", tmp_path)
        f = tmp_path / "example.py"
        f.write_text('"""Example."""\n\nx = 1\n')
        output = generate_source_index([f])
        assert "# MindRoom Source Code Index" in output
        assert "# FILE: example.py" in output
        assert "x = 1" in output

    def test_multiple_files_separated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Each file gets its own FILE marker."""
        monkeypatch.setattr("scripts.generate_source_index.REPO_ROOT", tmp_path)
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("a = 1\n")
        f2.write_text("b = 2\n")
        output = generate_source_index([f1, f2])
        assert output.count("# FILE:") == 2


class TestGenerateSourceMap:
    """Test source map markdown table generation."""

    def test_markdown_table(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Source map generates a markdown table with file and description."""
        monkeypatch.setattr("scripts.generate_source_index.REPO_ROOT", tmp_path)
        f = tmp_path / "mod.py"
        f.write_text('"""Module description."""\n')
        output = generate_source_map([f])
        assert "| File | Description |" in output
        assert "Module description." in output
        assert "**Total: 1 files**" in output

    def test_no_docstring_shows_dash(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Files without docstrings show a dash in the description column."""
        monkeypatch.setattr("scripts.generate_source_index.REPO_ROOT", tmp_path)
        f = tmp_path / "mod.py"
        f.write_text("x = 1\n")
        output = generate_source_map([f])
        assert "| - |" in output

    def test_pipe_chars_escaped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pipe characters in docstrings are escaped for markdown tables."""
        monkeypatch.setattr("scripts.generate_source_index.REPO_ROOT", tmp_path)
        f = tmp_path / "mod.py"
        f.write_text('"""Has | pipe char."""\n')
        output = generate_source_map([f])
        assert "\\|" in output


# ---------------------------------------------------------------------------
# Integration: verify the actual generators produce valid output
# ---------------------------------------------------------------------------


class TestIntegration:
    """Run the actual generators against the real repo and verify basic invariants."""

    def test_llms_txt_generator_runs(self) -> None:
        """generate_llms_txt produces non-empty output from real zensical.toml."""
        toml_path = Path(__file__).resolve().parent.parent / "zensical.toml"
        if not toml_path.exists():
            pytest.skip("zensical.toml not found")
        with toml_path.open("rb") as f:
            config = tomllib.load(f)
        nav = config["project"]["nav"]
        output = generate_llms_txt(nav)
        assert len(output) > 100
        assert "# MindRoom" in output

    def test_source_index_generator_runs(self) -> None:
        """generate_source_index produces non-empty output from real src/mindroom/."""
        src_dir = Path(__file__).resolve().parent.parent / "src" / "mindroom"
        if not src_dir.exists():
            pytest.skip("src/mindroom/ not found")
        files = _collect_python_files(src_dir)
        assert len(files) > 10
        output = generate_source_index(files)
        assert "# MindRoom Source Code Index" in output
        assert len(output) > 1000
