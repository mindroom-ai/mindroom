"""Tests for the ergonomic coding tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.custom_tools.coding import (
    CodingTools,
    _count_occurrences,
    _fuzzy_find,
    _normalize_for_fuzzy,
    _truncate_head,
    _truncate_line,
    _truncate_tail,
)

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def tmp_base(tmp_path: Path) -> Path:
    """Create a temporary base directory with some test files."""
    (tmp_path / "hello.py").write_text("print('hello')\nprint('world')\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nested.txt").write_text("nested content\nline two\n")
    return tmp_path


@pytest.fixture
def tools(tmp_base: Path) -> CodingTools:
    """Create CodingTools with the tmp base."""
    return CodingTools(base_dir=str(tmp_base))


class TestTruncateHead:
    """Tests for _truncate_head."""

    def test_no_truncation_needed(self) -> None:
        """Small content is returned as-is."""
        result = _truncate_head("line1\nline2\nline3\n")
        assert not result.was_truncated
        assert result.total_lines == 3
        assert result.content == "line1\nline2\nline3\n"

    def test_truncation_by_lines(self) -> None:
        """Content exceeding max_lines is truncated."""
        content = "\n".join(f"line{i}" for i in range(1000))
        result = _truncate_head(content, max_lines=10, max_bytes=1_000_000)
        assert result.was_truncated
        assert result.shown_lines == 10
        assert result.total_lines == 1000

    def test_truncation_by_bytes(self) -> None:
        """Content exceeding max_bytes is truncated."""
        content = "x" * 200 + "\n" + "y" * 200 + "\n"
        result = _truncate_head(content, max_lines=1000, max_bytes=250)
        assert result.was_truncated
        assert result.shown_lines == 1


class TestTruncateTail:
    """Tests for _truncate_tail."""

    def test_keeps_last_lines(self) -> None:
        """Tail truncation keeps the last N lines."""
        content = "\n".join(f"line{i}" for i in range(100))
        result = _truncate_tail(content, max_lines=5, max_bytes=1_000_000)
        assert result.was_truncated
        assert result.shown_lines == 5
        assert "line99" in result.content
        assert "line0" not in result.content


class TestFuzzyMatching:
    """Tests for fuzzy text matching helpers."""

    def test_normalize_strips_trailing_whitespace(self) -> None:
        """Trailing whitespace is stripped per line."""
        assert _normalize_for_fuzzy("hello   \nworld  ") == "hello\nworld"

    def test_normalize_smart_quotes(self) -> None:
        """Smart quotes are normalized to ASCII."""
        result = _normalize_for_fuzzy("\u201chello\u201d")
        assert result == '"hello"'

    def test_normalize_dashes(self) -> None:
        """Unicode dashes (U+2010-2015, U+2212) are normalized to hyphens."""
        result = _normalize_for_fuzzy("a\u2013b\u2014c\u2010d\u2212e")
        assert result == "a-b-c-d-e"

    def test_normalize_extended_spaces(self) -> None:
        """Extended Unicode spaces are normalized to regular spaces."""
        result = _normalize_for_fuzzy("a\u202fb\u205fc\u3000d")
        assert result == "a b c d"

    def test_exact_match_preferred(self) -> None:
        """Exact match is preferred over fuzzy."""
        match = _fuzzy_find("hello world", "hello")
        assert match is not None
        assert not match.was_fuzzy
        assert match.matched_text == "hello"

    def test_fuzzy_match_trailing_whitespace(self) -> None:
        """Fuzzy match handles trailing whitespace differences."""
        content = "def foo():   \n    pass\n"
        old = "def foo():\n    pass"
        match = _fuzzy_find(content, old)
        assert match is not None
        assert match.was_fuzzy

    def test_no_match(self) -> None:
        """Returns None when text is not found."""
        assert _fuzzy_find("hello world", "nonexistent") is None

    def test_count_exact(self) -> None:
        """Counts exact occurrences."""
        assert _count_occurrences("aaa", "a") == 3

    def test_count_fuzzy(self) -> None:
        """Counts fuzzy occurrences when exact match fails."""
        content = "hello   \nhello   \n"
        assert _count_occurrences(content, "hello\nhello") == 1


class TestReadFile:
    """Tests for CodingTools.read_file."""

    def test_read_simple(self, tools: CodingTools) -> None:
        """Reads a simple file with line numbers."""
        result = tools.read_file("hello.py")
        assert "print('hello')" in result
        assert "print('world')" in result
        assert "1|" in result

    def test_read_nested(self, tools: CodingTools) -> None:
        """Reads a file in a subdirectory."""
        result = tools.read_file("sub/nested.txt")
        assert "nested content" in result

    def test_read_missing(self, tools: CodingTools) -> None:
        """Returns error for missing files."""
        result = tools.read_file("nonexistent.py")
        assert "Error" in result

    def test_read_with_offset(self, tools: CodingTools, tmp_base: Path) -> None:
        """Reads from a given offset."""
        content = "\n".join(f"line {i}" for i in range(1, 21))
        (tmp_base / "many.txt").write_text(content)
        result = tools.read_file("many.txt", offset=10)
        assert "line 10" in result
        assert "line 1|" not in result

    def test_read_with_limit(self, tools: CodingTools, tmp_base: Path) -> None:
        """Respects line limit and shows pagination hint."""
        content = "\n".join(f"line {i}" for i in range(1, 21))
        (tmp_base / "many.txt").write_text(content)
        result = tools.read_file("many.txt", limit=5)
        assert "line 1" in result
        assert "offset=6" in result

    def test_read_offset_past_end(self, tools: CodingTools) -> None:
        """Returns error when offset exceeds file length."""
        result = tools.read_file("hello.py", offset=9999)
        assert "Error" in result
        assert "exceeds" in result

    def test_read_truncation_hint(self, tools: CodingTools, tmp_base: Path) -> None:
        """Shows pagination hint for large files (>2000 lines)."""
        content = "\n".join(f"line {i}" for i in range(1, 3001))
        (tmp_base / "big.txt").write_text(content)
        result = tools.read_file("big.txt")
        assert "offset=" in result

    def test_read_not_a_file(self, tools: CodingTools) -> None:
        """Returns error when path is a directory."""
        result = tools.read_file("sub")
        assert "Error" in result
        assert "Not a file" in result


class TestEditFile:
    """Tests for CodingTools.edit_file."""

    def test_exact_edit(self, tools: CodingTools, tmp_base: Path) -> None:
        """Performs exact text replacement with context diff."""
        result = tools.edit_file("hello.py", "print('hello')", "print('hi')")
        assert "Applied edit" in result
        assert "print('hello')" in result  # old line shown in diff
        assert "print('hi')" in result  # new line shown in diff
        # Context line should be present
        assert "print('world')" in result
        content = (tmp_base / "hello.py").read_text()
        assert "print('hi')" in content
        assert "print('hello')" not in content

    def test_fuzzy_edit_whitespace(self, tools: CodingTools, tmp_base: Path) -> None:
        """Handles trailing whitespace differences via fuzzy matching."""
        (tmp_base / "ws.py").write_text("def foo():   \n    pass\n")
        result = tools.edit_file("ws.py", "def foo():\n    pass", "def bar():\n    pass")
        assert "Applied edit" in result
        assert "fuzzy" in result

    def test_edit_not_found(self, tools: CodingTools) -> None:
        """Returns error when old_text is not found."""
        result = tools.edit_file("hello.py", "nonexistent text", "replacement")
        assert "Error" in result
        assert "not found" in result

    def test_edit_multiple_matches(self, tools: CodingTools, tmp_base: Path) -> None:
        """Returns error when old_text matches multiple locations."""
        (tmp_base / "dup.py").write_text("foo\nfoo\nfoo\n")
        result = tools.edit_file("dup.py", "foo", "bar")
        assert "Error" in result
        assert "3 locations" in result

    def test_edit_missing_file(self, tools: CodingTools) -> None:
        """Returns error for missing files."""
        result = tools.edit_file("missing.py", "old", "new")
        assert "Error" in result
        assert "not found" in result


class TestWriteFile:
    """Tests for CodingTools.write_file."""

    def test_write_new_file(self, tools: CodingTools, tmp_base: Path) -> None:
        """Creates a new file."""
        result = tools.write_file("new.py", "content here\n")
        assert "Wrote" in result
        assert (tmp_base / "new.py").read_text() == "content here\n"

    def test_write_creates_dirs(self, tools: CodingTools, tmp_base: Path) -> None:
        """Auto-creates parent directories."""
        result = tools.write_file("a/b/c.txt", "deep\n")
        assert "Wrote" in result
        assert (tmp_base / "a" / "b" / "c.txt").read_text() == "deep\n"

    def test_write_overwrite(self, tools: CodingTools, tmp_base: Path) -> None:
        """Overwrites an existing file."""
        result = tools.write_file("hello.py", "overwritten\n")
        assert "Wrote" in result
        assert (tmp_base / "hello.py").read_text() == "overwritten\n"


class TestGrep:
    """Tests for CodingTools.grep."""

    def test_grep_simple(self, tools: CodingTools) -> None:
        """Finds a simple pattern."""
        result = tools.grep("hello")
        assert "hello" in result

    def test_grep_no_match(self, tools: CodingTools) -> None:
        """Returns 'No matches' when nothing matches."""
        result = tools.grep("nonexistent_string_xyz")
        assert "No matches" in result

    def test_grep_with_glob(self, tools: CodingTools) -> None:
        """Filters by glob pattern."""
        result = tools.grep("content", glob="*.txt")
        assert "nested" in result

    def test_grep_ignore_case(self, tools: CodingTools, tmp_base: Path) -> None:
        """Supports case-insensitive search."""
        (tmp_base / "case.txt").write_text("Hello World\n")
        result = tools.grep("hello world", path="case.txt", ignore_case=True)
        assert "Hello World" in result

    def test_grep_invalid_regex(self, tools: CodingTools) -> None:
        """Handles invalid regex gracefully."""
        result = tools.grep("[invalid")
        assert isinstance(result, str)

    def test_grep_missing_path(self, tools: CodingTools) -> None:
        """Returns error for missing paths."""
        result = tools.grep("hello", path="nonexistent_dir")
        assert "Error" in result

    def test_grep_with_limit(self, tools: CodingTools, tmp_base: Path) -> None:
        """Respects match limit."""
        (tmp_base / "repeat.txt").write_text("\n".join(f"match{i}" for i in range(50)))
        result = tools.grep("match", path="repeat.txt", limit=5)
        assert isinstance(result, str)

    def test_grep_literal(self, tools: CodingTools, tmp_base: Path) -> None:
        """Literal mode escapes regex special chars."""
        (tmp_base / "regex.txt").write_text("foo[bar]\nfoo.bar\n")
        result = tools.grep("[bar]", path="regex.txt", literal=True)
        assert "foo[bar]" in result

    def test_grep_line_truncation(self, tools: CodingTools, tmp_base: Path) -> None:
        """Long match lines are truncated at 500 chars."""
        long_line = "x" * 600
        (tmp_base / "long.txt").write_text(long_line + "\n")
        result = tools.grep("x", path="long.txt")
        assert "[truncated]" in result


class TestLineTruncation:
    """Tests for per-line truncation in grep output."""

    def test_short_line_unchanged(self) -> None:
        """Lines under 500 chars are not modified."""
        assert _truncate_line("short") == "short"

    def test_long_line_truncated(self) -> None:
        """Lines over 500 chars get truncated with marker."""
        line = "a" * 600
        result = _truncate_line(line)
        assert len(result) < 600
        assert result.endswith("[truncated]")


class TestFindFiles:
    """Tests for CodingTools.find_files."""

    def test_find_py_files(self, tools: CodingTools) -> None:
        """Finds Python files."""
        result = tools.find_files("**/*.py")
        assert "hello.py" in result

    def test_find_txt_files(self, tools: CodingTools) -> None:
        """Finds text files in subdirectories."""
        result = tools.find_files("**/*.txt")
        assert "nested.txt" in result

    def test_find_no_match(self, tools: CodingTools) -> None:
        """Returns message when no files match."""
        result = tools.find_files("**/*.rs")
        assert "No files" in result

    def test_find_with_limit(self, tools: CodingTools, tmp_base: Path) -> None:
        """Respects result limit."""
        for i in range(20):
            (tmp_base / f"file{i}.txt").write_text(f"content{i}")
        result = tools.find_files("*.txt", limit=5)
        assert "limited" in result.lower() or result.count("\n") <= 5


class TestLs:
    """Tests for CodingTools.ls."""

    def test_ls_root(self, tools: CodingTools) -> None:
        """Lists root directory with directory indicators."""
        result = tools.ls()
        assert "hello.py" in result
        assert "sub/" in result

    def test_ls_subdir(self, tools: CodingTools) -> None:
        """Lists a subdirectory."""
        result = tools.ls("sub")
        assert "nested.txt" in result

    def test_ls_missing(self, tools: CodingTools) -> None:
        """Returns error for missing paths."""
        result = tools.ls("nonexistent")
        assert "Error" in result

    def test_ls_not_dir(self, tools: CodingTools) -> None:
        """Returns error when path is not a directory."""
        result = tools.ls("hello.py")
        assert "Error" in result
        assert "Not a directory" in result

    def test_ls_includes_dotfiles(self, tools: CodingTools, tmp_base: Path) -> None:
        """Includes dotfiles like PI's ls tool."""
        (tmp_base / ".hidden").write_text("secret")
        result = tools.ls()
        assert ".hidden" in result

    def test_ls_with_limit(self, tools: CodingTools, tmp_base: Path) -> None:
        """Respects entry limit."""
        for i in range(20):
            (tmp_base / f"item{i}.txt").write_text("")
        result = tools.ls(limit=5)
        assert "limited" in result.lower()


class TestPathTraversal:
    """Tests for path traversal prevention."""

    def test_read_traversal(self, tools: CodingTools) -> None:
        """Blocks read of files outside base directory."""
        result = tools.read_file("../../etc/passwd")
        assert "Error" in result

    def test_write_traversal(self, tools: CodingTools) -> None:
        """Blocks write of files outside base directory."""
        result = tools.write_file("../../tmp/evil.txt", "evil")
        assert "Error" in result

    def test_edit_traversal(self, tools: CodingTools) -> None:
        """Blocks edit of files outside base directory."""
        result = tools.edit_file("../../etc/passwd", "root", "hacked")
        assert "Error" in result

    def test_absolute_outside_base(self, tools: CodingTools) -> None:
        """Blocks absolute paths outside base directory."""
        result = tools.read_file("/etc/hostname")
        assert "Error" in result


class TestRegistration:
    """Tests for tool registration."""

    def test_coding_tool_registered(self) -> None:
        """Coding tool is in the metadata registry."""
        from mindroom.tools_metadata import TOOL_METADATA  # noqa: PLC0415

        assert "coding" in TOOL_METADATA

    def test_coding_tool_factory(self) -> None:
        """Factory returns the CodingTools class."""
        from mindroom.tools.coding import coding_tools  # noqa: PLC0415

        cls = coding_tools()
        assert cls is CodingTools

    def test_toolkit_has_six_methods(self) -> None:
        """Toolkit exposes exactly the 6 expected methods."""
        tools = CodingTools()
        func_names = {f.name for f in tools.functions.values()}
        expected = {"read_file", "edit_file", "write_file", "grep", "find_files", "ls"}
        assert expected == func_names
