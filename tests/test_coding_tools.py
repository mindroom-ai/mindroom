"""Tests for the ergonomic coding tools."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from mindroom.custom_tools.coding import (
    CodingTools,
    _find_all_matches,
    _normalize_for_fuzzy,
    _run_ripgrep,
    _truncate_head,
    _truncate_line,
)
from mindroom.tools.file import file_tools

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
        matches = _find_all_matches("hello world", "hello")
        assert len(matches) == 1
        assert not matches[0].was_fuzzy
        assert matches[0].matched_text == "hello"

    def test_fuzzy_match_trailing_whitespace(self) -> None:
        """Fuzzy match handles trailing whitespace differences."""
        content = "def foo():   \n    pass\n"
        old = "def foo():\n    pass"
        matches = _find_all_matches(content, old)
        assert len(matches) == 1
        assert matches[0].was_fuzzy

    def test_no_match(self) -> None:
        """Returns empty list when text is not found."""
        assert _find_all_matches("hello world", "nonexistent") == []

    def test_count_exact(self) -> None:
        """Finds all exact occurrences."""
        assert len(_find_all_matches("aaa", "a")) == 3

    def test_count_fuzzy(self) -> None:
        """Finds fuzzy occurrences when exact match fails."""
        content = "hello   \nhello   \n"
        assert len(_find_all_matches(content, "hello\nhello")) == 1

    def test_empty_old_text_returns_no_matches(self) -> None:
        """Empty old_text should return no matches instead of hanging."""
        assert _find_all_matches("hello world", "") == []

    def test_old_text_normalized_to_empty_returns_no_matches(self) -> None:
        """Inputs that normalize to empty should return no matches."""
        assert _find_all_matches("hello world", "\n") == []


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

    def test_read_empty_file(self, tools: CodingTools, tmp_base: Path) -> None:
        """Reading an existing empty file should succeed with empty output."""
        (tmp_base / "empty.txt").write_text("")
        result = tools.read_file("empty.txt")
        assert result == ""

    def test_read_rejects_non_positive_limit(self, tools: CodingTools) -> None:
        """Rejects invalid non-positive line limits."""
        result = tools.read_file("hello.py", limit=-1)
        assert "Error" in result
        assert "limit" in result

    def test_read_rejects_non_positive_offset(self, tools: CodingTools) -> None:
        """Rejects invalid non-positive offsets."""
        result = tools.read_file("hello.py", offset=0)
        assert "Error" in result
        assert "offset" in result

    def test_read_long_single_line_is_still_readable(self, tools: CodingTools, tmp_base: Path) -> None:
        """Very long single-line files should return partial content, not an empty page."""
        (tmp_base / "single_long.txt").write_text("x" * 70_000)
        result = tools.read_file("single_long.txt")
        assert "1| " in result
        assert "[truncated]" in result


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

    def test_fuzzy_edit_multiline_preserves_line_boundaries(self, tools: CodingTools, tmp_base: Path) -> None:
        """Fuzzy multiline edits should consume trailing spaces and line ending of matched lines."""
        (tmp_base / "fuzzy_multiline.py").write_text("foo   \nbar\n")
        result = tools.edit_file("fuzzy_multiline.py", "foo\n", "X\n")
        assert "Applied edit" in result
        assert "fuzzy" in result
        assert (tmp_base / "fuzzy_multiline.py").read_text() == "X\nbar\n"

    def test_fuzzy_edit_single_line_consumes_trailing_whitespace_and_newline(
        self,
        tools: CodingTools,
        tmp_base: Path,
    ) -> None:
        """Fuzzy single-line edits should not leave trailing whitespace artifacts."""
        (tmp_base / "fuzzy_single_line.py").write_text("foo   \n")
        result = tools.edit_file("fuzzy_single_line.py", "foo\n", "X\n")
        assert "Applied edit" in result
        assert "fuzzy" in result
        assert (tmp_base / "fuzzy_single_line.py").read_text() == "X\n"

    def test_fuzzy_edit_handles_composed_vs_decomposed_unicode(self, tools: CodingTools, tmp_base: Path) -> None:
        """Fuzzy edits should replace full graphemes across NFC differences."""
        (tmp_base / "unicode.py").write_text("Cafe\u0301")
        result = tools.edit_file("unicode.py", "Café", "Tea")
        assert "Applied edit" in result
        assert "fuzzy" in result
        assert (tmp_base / "unicode.py").read_text() == "Tea"

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

    def test_edit_rejects_empty_old_text(self, tools: CodingTools) -> None:
        """Rejects empty old_text instead of attempting a replacement."""
        result = tools.edit_file("hello.py", "", "replacement")
        assert "Error" in result
        assert "non-empty" in result


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

    def test_grep_with_glob_python_fallback(self, tools: CodingTools, monkeypatch: pytest.MonkeyPatch) -> None:
        """Python fallback should treat bare '*.ext' as recursive like ripgrep."""
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("content", glob="*.txt")
        assert "nested" in result

    def test_grep_with_absolute_glob_returns_error_in_fallback(
        self,
        tools: CodingTools,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Invalid absolute glob patterns should return an error, not raise."""
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("content", glob="/absolute/path/*.txt")
        assert "Error: Invalid glob pattern" in result

    def test_grep_with_absolute_glob_returns_error_with_rg(self, tools: CodingTools) -> None:
        """Absolute glob validation should not depend on ripgrep availability."""
        result = tools.grep("content", glob="/absolute/path/*.txt")
        assert "Error: Invalid glob pattern" in result

    def test_grep_python_fallback_respects_gitignore(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Python fallback should skip hidden and gitignored files for ripgrep parity."""
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
        (tmp_path / ".gitignore").write_text("ignored.txt\n")
        (tmp_path / "visible.txt").write_text("match me\n")
        (tmp_path / "ignored.txt").write_text("match me\n")
        (tmp_path / ".hidden.txt").write_text("match me\n")
        (tmp_path / ".hidden").mkdir()
        (tmp_path / ".hidden" / "inside.txt").write_text("match me\n")

        tools = CodingTools(base_dir=str(tmp_path))
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("match")

        assert "visible.txt:1:match me" in result
        assert "ignored.txt" not in result
        assert ".hidden.txt" not in result
        assert ".hidden/inside.txt" not in result

    def test_grep_python_fallback_batches_gitignore_checks(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Python fallback should use one batched git check-ignore invocation."""
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
        (tmp_path / ".gitignore").write_text("ignored.txt\n")
        for i in range(6):
            (tmp_path / f"visible{i}.txt").write_text("needle\n")
        (tmp_path / "ignored.txt").write_text("needle\n")

        tools = CodingTools(base_dir=str(tmp_path))
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)

        run_calls = 0
        original_run = subprocess.run

        def counting_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
            nonlocal run_calls
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd[:2] == ["git", "check-ignore"]:
                run_calls += 1
            return original_run(*args, **kwargs)

        monkeypatch.setattr("mindroom.custom_tools.coding.subprocess.run", counting_run)
        result = tools.grep("needle")

        assert "visible0.txt" in result
        assert run_calls == 1

    def test_grep_python_fallback_does_not_follow_symlink_outside_base(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Fallback grep should ignore files reached via symlinks escaping base_dir."""
        outside_dir = tmp_path.parent / f"{tmp_path.name}_outside"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "secret.txt").write_text("needle\n")
        try:
            (tmp_path / "link").symlink_to(outside_dir, target_is_directory=True)
        except (NotImplementedError, OSError):
            pytest.skip("Symlinks not supported on this platform")

        tools = CodingTools(base_dir=str(tmp_path))
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("needle", glob="link/*.txt")
        assert "secret.txt" not in result
        assert "No matches found." in result

    def test_run_ripgrep_enforces_global_match_limit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Ripgrep path should enforce a true global limit across files."""
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "a.txt"},
                            "lines": {"text": "match one\n"},
                            "line_number": 1,
                        },
                    },
                ),
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "b.txt"},
                            "lines": {"text": "match two\n"},
                            "line_number": 1,
                        },
                    },
                ),
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "c.txt"},
                            "lines": {"text": "match three\n"},
                            "line_number": 1,
                        },
                    },
                ),
            ],
        )

        monkeypatch.setattr("mindroom.custom_tools.coding.shutil.which", lambda _name: "rg")
        monkeypatch.setattr(
            "mindroom.custom_tools.coding.subprocess.run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=["rg"],
                returncode=0,
                stdout=stdout,
                stderr="",
            ),
        )

        result = _run_ripgrep(
            "match",
            tmp_path,
            tmp_path,
            None,
            ignore_case=False,
            literal=False,
            context=0,
            limit=2,
        )

        assert result is not None
        assert "a.txt:1:match one" in result
        assert "b.txt:1:match two" in result
        assert "c.txt:1:match three" not in result
        assert "Results limited to 2 matches" in result

    def test_run_ripgrep_limit_does_not_leak_context_of_excluded_match(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Context from matches beyond limit should not appear in output."""
        stdout = "\n".join(
            [
                json.dumps(
                    {
                        "type": "context",
                        "data": {
                            "path": {"text": "a.txt"},
                            "lines": {"text": "before first\n"},
                            "line_number": 1,
                        },
                    },
                ),
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "a.txt"},
                            "lines": {"text": "first\n"},
                            "line_number": 2,
                        },
                    },
                ),
                json.dumps(
                    {
                        "type": "context",
                        "data": {
                            "path": {"text": "a.txt"},
                            "lines": {"text": "after first\n"},
                            "line_number": 3,
                        },
                    },
                ),
                json.dumps(
                    {
                        "type": "context",
                        "data": {
                            "path": {"text": "a.txt"},
                            "lines": {"text": "before second\n"},
                            "line_number": 6,
                        },
                    },
                ),
                json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "a.txt"},
                            "lines": {"text": "second\n"},
                            "line_number": 7,
                        },
                    },
                ),
            ],
        )

        monkeypatch.setattr("mindroom.custom_tools.coding.shutil.which", lambda _name: "rg")
        monkeypatch.setattr(
            "mindroom.custom_tools.coding.subprocess.run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=["rg"],
                returncode=0,
                stdout=stdout,
                stderr="",
            ),
        )

        result = _run_ripgrep(
            "match",
            tmp_path,
            tmp_path,
            None,
            ignore_case=False,
            literal=False,
            context=1,
            limit=1,
        )

        assert result is not None
        assert "a.txt-1-before first" in result
        assert "a.txt:2:first" in result
        assert "a.txt-3-after first" in result
        assert "before second" not in result
        assert "a.txt:7:second" not in result
        assert "Results limited to 1 matches" in result

    def test_grep_context_deduplicates_overlapping_lines(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nearby matches with overlapping context should not duplicate shared lines."""
        (tmp_base / "overlap.txt").write_text("a\nmatch1\nb\nmatch2\nc\n")
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("match", path="overlap.txt", context=1)
        lines = result.strip().splitlines()
        # Line "b" (line 3) is shared context — must appear exactly once.
        b_lines = [line for line in lines if "-3-b" in line or ":3:b" in line]
        assert len(b_lines) == 1

    def test_grep_context_no_separator_lines(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Python fallback context output should not contain '--' separator lines."""
        (tmp_base / "ctx.txt").write_text("a\nb\nmatch\nc\nd\n")
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("match", path="ctx.txt", context=1)
        assert "match" in result
        assert "\n--\n" not in result
        assert "-2-b" in result
        assert ":3:match" in result
        assert "-4-c" in result
        lines = result.strip().splitlines()
        assert all(line != "--" for line in lines)

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

    def test_grep_python_fallback_limit_does_not_emit_extra_match(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Python fallback should not emit the (limit+1)th match while detecting truncation."""
        (tmp_base / "limit.txt").write_text("match1\nmatch2\nmatch3\n")
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("match", path="limit.txt", limit=2)

        match_lines = [line for line in result.splitlines() if line.startswith("limit.txt:")]
        assert len(match_lines) == 2
        assert "limit.txt:3:match3" not in result
        assert "Results limited to 2 matches" in result

    def test_grep_limit_banner_not_shown_at_exact_count(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Limit banner should not appear when total matches exactly equal the limit."""
        (tmp_base / "exact.txt").write_text("match\nmatch\nmatch\n")
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("match", path="exact.txt", limit=3)
        assert "match" in result
        assert "limited" not in result.lower()

    def test_grep_literal(self, tools: CodingTools, tmp_base: Path) -> None:
        """Literal mode escapes regex special chars."""
        (tmp_base / "regex.txt").write_text("foo[bar]\nfoo.bar\n")
        result = tools.grep("[bar]", path="regex.txt", literal=True)
        assert "foo[bar]" in result

    def test_grep_dash_prefixed_pattern(self, tools: CodingTools, tmp_base: Path) -> None:
        """Patterns starting with '-' should not be parsed as ripgrep flags."""
        (tmp_base / "flags.txt").write_text("--files\n--color\nregular\n")
        result = tools.grep("--files", path="flags.txt")
        assert "--files" in result
        assert "No matches" not in result

    def test_grep_line_truncation(self, tools: CodingTools, tmp_base: Path) -> None:
        """Long match lines are truncated at 500 chars."""
        long_line = "x" * 600
        (tmp_base / "long.txt").write_text(long_line + "\n")
        result = tools.grep("x", path="long.txt")
        assert "[truncated]" in result

    def test_grep_python_fallback_single_file_shows_filename(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Python fallback should show the filename, not '.', when path points to a single file."""
        (tmp_base / "a.txt").write_text("needle\n")
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        result = tools.grep("needle", path="a.txt")
        assert "a.txt:1:" in result
        assert ".:1:" not in result

    def test_grep_python_fallback_applies_global_output_truncation(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Python fallback should enforce the same global output truncation limits."""
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)
        (tmp_base / "huge.txt").write_text("\n".join(f"match line {i}" for i in range(7000)))
        result = tools.grep("match", path="huge.txt", limit=7000)
        assert "[Output truncated." in result

    def test_grep_explicit_hidden_path_returns_matches(self, tools: CodingTools, tmp_base: Path) -> None:
        """Explicit hidden path targets are not filtered — only recursive discovery excludes dotfiles."""
        (tmp_base / ".hidden_dir").mkdir()
        (tmp_base / ".hidden_dir" / "secret.txt").write_text("needle\n")
        result = tools.grep("needle", path=".hidden_dir")
        assert "needle" in result
        assert "No matches" not in result

    def test_grep_rg_and_fallback_emit_same_relative_paths(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Ripgrep and Python fallback must emit the same relative path format."""
        (tmp_base / "parity.txt").write_text("needle\n")

        # Capture rg output (mocked with absolute paths like real rg)
        abs_path = str((tmp_base / "parity.txt").resolve())
        rg_stdout = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": abs_path},
                    "lines": {"text": "needle\n"},
                    "line_number": 1,
                },
            },
        )
        monkeypatch.setattr("mindroom.custom_tools.coding.shutil.which", lambda _name: "rg")
        monkeypatch.setattr(
            "mindroom.custom_tools.coding.subprocess.run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=["rg"],
                returncode=0,
                stdout=rg_stdout,
                stderr="",
            ),
        )
        rg_result = tools.grep("needle", path="parity.txt")

        # Capture fallback output
        monkeypatch.setattr("mindroom.custom_tools.coding.shutil.which", lambda _name: None)
        fallback_result = tools.grep("needle", path="parity.txt")

        assert rg_result == fallback_result

    def test_grep_rg_and_fallback_parity_subdirectory(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parity for subdirectory targets: paths must include the subdirectory prefix."""
        abs_path = str((tmp_base / "sub" / "nested.txt").resolve())
        rg_stdout = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": abs_path},
                    "lines": {"text": "nested content\n"},
                    "line_number": 1,
                },
            },
        )
        monkeypatch.setattr("mindroom.custom_tools.coding.shutil.which", lambda _name: "rg")
        monkeypatch.setattr(
            "mindroom.custom_tools.coding.subprocess.run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=["rg"],
                returncode=0,
                stdout=rg_stdout,
                stderr="",
            ),
        )
        rg_result = tools.grep("nested", path="sub")

        monkeypatch.setattr("mindroom.custom_tools.coding.shutil.which", lambda _name: None)
        fallback_result = tools.grep("nested", path="sub")

        assert rg_result == fallback_result
        assert "sub/nested.txt" in rg_result

    def test_grep_rg_and_fallback_parity_nested_single_file(
        self,
        tools: CodingTools,
        tmp_base: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parity for single-file targets in subdirectories."""
        abs_path = str((tmp_base / "sub" / "nested.txt").resolve())
        rg_stdout = json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": abs_path},
                    "lines": {"text": "nested content\n"},
                    "line_number": 1,
                },
            },
        )
        monkeypatch.setattr("mindroom.custom_tools.coding.shutil.which", lambda _name: "rg")
        monkeypatch.setattr(
            "mindroom.custom_tools.coding.subprocess.run",
            lambda *_args, **_kwargs: subprocess.CompletedProcess(
                args=["rg"],
                returncode=0,
                stdout=rg_stdout,
                stderr="",
            ),
        )
        rg_result = tools.grep("nested", path="sub/nested.txt")

        monkeypatch.setattr("mindroom.custom_tools.coding.shutil.which", lambda _name: None)
        fallback_result = tools.grep("nested", path="sub/nested.txt")

        assert rg_result == fallback_result
        assert "sub/nested.txt" in rg_result


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

    def test_find_limit_banner_not_shown_at_exact_count(self, tmp_path: Path) -> None:
        """Limit banner should not appear when total files exactly equal the limit."""
        for i in range(3):
            (tmp_path / f"file{i}.txt").write_text("")
        tools = CodingTools(base_dir=str(tmp_path))
        result = tools.find_files("*.txt", limit=3)
        assert "limited" not in result.lower()

    def test_find_with_absolute_glob_returns_error(self, tools: CodingTools) -> None:
        """Invalid absolute glob patterns should return an error, not raise."""
        result = tools.find_files("/absolute/path/*.txt")
        assert "Error: Invalid glob pattern" in result

    def test_find_filters_dotfiles(self, tools: CodingTools, tmp_base: Path) -> None:
        """find_files should exclude dotfiles and files inside dot-directories."""
        (tmp_base / ".hidden_file.txt").write_text("hidden")
        (tmp_base / ".hidden_dir").mkdir()
        (tmp_base / ".hidden_dir" / "inside.txt").write_text("inside hidden dir")
        result = tools.find_files("**/*.txt")
        assert "nested.txt" in result
        assert ".hidden_file.txt" not in result
        assert ".hidden_dir" not in result

    def test_find_batches_gitignore_checks(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """find_files should use one batched git check-ignore invocation."""
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
        (tmp_path / ".gitignore").write_text("ignored*.txt\n")
        for i in range(8):
            (tmp_path / f"visible{i}.txt").write_text("x")
        for i in range(4):
            (tmp_path / f"ignored{i}.txt").write_text("x")

        tools = CodingTools(base_dir=str(tmp_path))
        run_calls = 0
        original_run = subprocess.run

        def counting_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
            nonlocal run_calls
            cmd = args[0] if args else kwargs.get("args")
            if isinstance(cmd, list) and cmd[:2] == ["git", "check-ignore"]:
                run_calls += 1
            return original_run(*args, **kwargs)

        monkeypatch.setattr("mindroom.custom_tools.coding.subprocess.run", counting_run)
        result = tools.find_files("*.txt")

        assert "visible0.txt" in result
        assert "ignored0.txt" not in result
        assert run_calls == 1

    def test_find_does_not_follow_symlink_outside_base(self, tools: CodingTools, tmp_base: Path) -> None:
        """find_files should ignore matches that resolve outside base_dir."""
        outside_dir = tmp_base.parent / "outside"
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("hidden")
        try:
            (tmp_base / "link").symlink_to(outside_dir, target_is_directory=True)
        except (NotImplementedError, OSError):
            pytest.skip("Symlinks not supported on this platform")

        result = tools.find_files("link/*.txt")
        assert "secret.txt" not in result
        assert "No files matching" in result

    def test_find_follows_symlink_inside_base_when_restricted(self, tmp_path: Path) -> None:
        """Restricted find_files should allow symlinks whose targets stay inside base_dir."""
        base_dir = tmp_path / "base"
        src_dir = base_dir / "src"
        target_dir = base_dir / "shared"
        base_dir.mkdir()
        src_dir.mkdir()
        target_dir.mkdir()
        (target_dir / "match.txt").write_text("visible")
        try:
            (src_dir / "link").symlink_to(target_dir, target_is_directory=True)
        except (NotImplementedError, OSError):
            pytest.skip("Symlinks not supported on this platform")

        tools = CodingTools(base_dir=str(base_dir))
        result = tools.find_files("link/*.txt", path="src")

        assert "src/link/match.txt" in result
        assert "No files matching" not in result


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

    def test_ls_limit_banner_not_shown_at_exact_count(self, tmp_path: Path) -> None:
        """Limit banner should not appear when total entries exactly equal the limit."""
        for i in range(3):
            (tmp_path / f"file{i}.txt").write_text("")
        tools = CodingTools(base_dir=str(tmp_path))
        result = tools.ls(limit=3)
        assert "limited" not in result.lower()

    def test_ls_rejects_non_positive_limit(self, tools: CodingTools) -> None:
        """Rejects invalid non-positive entry limits."""
        result = tools.ls(limit=0)
        assert "Error" in result
        assert "limit" in result


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

    def test_tilde_not_expanded(self, tools: CodingTools) -> None:
        """Tilde paths are not expanded and treated as literal relative paths."""
        result = tools.read_file("~/../../etc/passwd")
        assert "Error" in result


class TestRestrictToBaseDir:
    """Tests for the restrict_to_base_dir toggle."""

    def test_restrict_to_base_dir_default_true(self, tmp_path: Path) -> None:
        """Default behavior should still block outside-base paths."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret\n")

        tools = CodingTools(base_dir=str(base_dir))
        result = tools.read_file(str(outside_dir / "secret.txt"))

        assert "Error" in result
        assert "outside base_dir" in result
        assert "restrict_to_base_dir=false" in result

    def test_restrict_to_base_dir_false_allows_outside(self, tmp_path: Path) -> None:
        """Disabling the restriction should allow absolute paths outside base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"

        tools = CodingTools(base_dir=str(base_dir), restrict_to_base_dir=False)
        write_result = tools.write_file(str(outside_file), "outside\n")
        read_result = tools.read_file(str(outside_file))

        assert "Wrote" in write_result
        assert outside_file.read_text() == "outside\n"
        assert "outside" in read_result

    def test_restrict_to_base_dir_false_relative_paths_still_work(self, tmp_path: Path) -> None:
        """Relative paths should still resolve from base_dir when unrestricted."""
        base_dir = tmp_path / "base"
        base_dir.mkdir()

        tools = CodingTools(base_dir=str(base_dir), restrict_to_base_dir=False)
        write_result = tools.write_file("nested/file.txt", "workspace\n")
        read_result = tools.read_file("nested/file.txt")

        assert "Wrote" in write_result
        assert (base_dir / "nested" / "file.txt").read_text() == "workspace\n"
        assert "workspace" in read_result

    def test_restrict_to_base_dir_default_true_blocks_edit_outside(self, tmp_path: Path) -> None:
        """Restricted edit_file should reject files outside base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("hello\n")

        tools = CodingTools(base_dir=str(base_dir))
        result = tools.edit_file(str(outside_file), "hello", "goodbye")

        assert "Error" in result
        assert "outside base_dir" in result
        assert outside_file.read_text() == "hello\n"

    def test_restrict_to_base_dir_false_allows_edit_outside(self, tmp_path: Path) -> None:
        """Unrestricted edit_file should work on files outside base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("hello\n")

        tools = CodingTools(base_dir=str(base_dir), restrict_to_base_dir=False)
        result = tools.edit_file(str(outside_file), "hello", "goodbye")

        assert "Applied edit" in result
        assert outside_file.read_text() == "goodbye\n"

    def test_restrict_to_base_dir_false_finds_files_outside(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """find_files and grep should work outside base_dir when unrestricted."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "match.txt"
        outside_file.write_text("needle\n")

        tools = CodingTools(base_dir=str(base_dir), restrict_to_base_dir=False)
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)

        find_result = tools.find_files("*.txt", path=str(outside_dir))
        grep_result = tools.grep("needle", path=str(outside_dir), glob="*.txt")

        assert str(outside_file) in find_result
        assert f"{outside_file}:1:needle" in grep_result

    def test_restrict_to_base_dir_false_supports_parent_traversal_patterns(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unrestricted grep and find should honor ../ patterns relative to base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "match.txt"
        outside_file.write_text("needle\n")

        tools = CodingTools(base_dir=str(base_dir), restrict_to_base_dir=False)
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)

        find_result = tools.find_files("../outside/*.txt")
        grep_result = tools.grep("needle", glob="../outside/*.txt")

        assert str(outside_file) in find_result
        assert f"{outside_file}:1:needle" in grep_result


class TestFileToolRestrictToBaseDir:
    """Tests for the file tool restrict_to_base_dir toggle."""

    def test_file_tool_default_restrict_to_base_dir_true_blocks_outside(self, tmp_path: Path) -> None:
        """File tools should still block outside-base reads by default."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir)
        result = tool.read_file(str(outside_file))

        assert "outside base_dir" in result
        assert "restrict_to_base_dir=false" in result

    def test_file_tool_search_files_blocks_parent_traversal_when_restricted(self, tmp_path: Path) -> None:
        """Restricted search_files should block ../ traversal before globbing."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir)
        result = tool.search_files("../outside/*.txt")

        assert "outside base_dir" in result
        assert "searching files" in result

    def test_file_tool_search_files_blocks_symlink_escape_when_restricted(self, tmp_path: Path) -> None:
        """Restricted search_files should reject symlinked directories escaping base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("secret\n")
        try:
            (base_dir / "link").symlink_to(outside_dir, target_is_directory=True)
        except (NotImplementedError, OSError):
            pytest.skip("Symlinks not supported on this platform")

        cls = file_tools()
        tool = cls(base_dir=base_dir)
        result = tool.search_files("link/*.txt")

        assert "outside base_dir" in result
        assert "searching files" in result

    def test_file_tool_restrict_to_base_dir_false_allows_outside_and_relative_paths(self, tmp_path: Path) -> None:
        """File tools should allow outside absolute paths while keeping relative paths anchored."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"

        cls = file_tools()
        tool = cls(base_dir=base_dir, restrict_to_base_dir=False)

        outside_write = tool.save_file("outside\n", str(outside_file))
        relative_write = tool.save_file("workspace\n", "note.txt")

        assert outside_write == str(outside_file)
        assert outside_file.read_text() == "outside\n"
        assert relative_write == "note.txt"
        assert (base_dir / "note.txt").read_text() == "workspace\n"

    def test_file_tool_delete_file_blocked_when_restricted(self, tmp_path: Path) -> None:
        """Restricted delete_file should reject outside-base paths."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir)
        result = tool.delete_file(str(outside_file))

        assert "outside base_dir" in result
        assert outside_file.exists()

    def test_file_tool_delete_file_allowed_when_unrestricted(self, tmp_path: Path) -> None:
        """Unrestricted delete_file should allow outside-base paths."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, restrict_to_base_dir=False)
        result = tool.delete_file(str(outside_file))

        assert result == ""
        assert not outside_file.exists()

    def test_file_tool_read_file_chunk_blocked_when_restricted(self, tmp_path: Path) -> None:
        """Restricted read_file_chunk should reject outside-base paths."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("alpha\nbeta\ngamma\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir)
        result = tool.read_file_chunk(str(outside_file), 0, 1)

        assert "outside base_dir" in result
        assert "reading file" in result

    def test_file_tool_read_file_chunk_allowed_when_unrestricted(self, tmp_path: Path) -> None:
        """Unrestricted read_file_chunk should allow outside-base paths."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("alpha\nbeta\ngamma\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, restrict_to_base_dir=False)
        result = tool.read_file_chunk(str(outside_file), 1, 2)

        assert result == "beta\ngamma"

    def test_file_tool_replace_file_chunk_blocked_when_restricted(self, tmp_path: Path) -> None:
        """Restricted replace_file_chunk should use the correct blocked-path message."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("alpha\nbeta\ngamma\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir)
        result = tool.replace_file_chunk(str(outside_file), 1, 1, "updated")

        assert "outside base_dir" in result
        assert "replacing file chunk" in result
        assert outside_file.read_text() == "alpha\nbeta\ngamma\n"

    def test_file_tool_replace_file_chunk_allowed_when_unrestricted(self, tmp_path: Path) -> None:
        """Unrestricted replace_file_chunk should allow outside-base paths."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("alpha\nbeta\ngamma\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, restrict_to_base_dir=False)
        result = tool.replace_file_chunk(str(outside_file), 1, 1, "updated")

        assert result == str(outside_file)
        assert outside_file.read_text() == "alpha\nupdated\ngamma\n"

    def test_file_tool_list_files_outside_base_dir_formats_absolute_paths(self, tmp_path: Path) -> None:
        """Listing an outside directory should return absolute paths instead of crashing."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, restrict_to_base_dir=False)
        result = tool.list_files(directory=str(outside_dir))

        assert json.loads(result) == [str(outside_file)]

    def test_file_tool_search_files_supports_absolute_patterns_when_unrestricted(self, tmp_path: Path) -> None:
        """Absolute search patterns should work when the restriction is disabled."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, restrict_to_base_dir=False)
        result = json.loads(tool.search_files(str(outside_dir / "*.txt")))

        assert result["files"] == [str(outside_file)]
        assert result["matches_found"] == 1

    def test_file_tool_search_files_supports_parent_traversal_when_unrestricted(self, tmp_path: Path) -> None:
        """Relative ../ search patterns should work when the restriction is disabled."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, restrict_to_base_dir=False)
        result = json.loads(tool.search_files("../outside/*.txt"))

        assert result["files"] == [str(outside_file)]
        assert result["matches_found"] == 1


class TestRegistration:
    """Tests for tool registration."""

    def test_coding_tool_registered(self) -> None:
        """Coding tool is in the metadata registry."""
        from mindroom.tool_system.metadata import TOOL_METADATA  # noqa: PLC0415

        assert "coding" in TOOL_METADATA

    def test_config_field_registered(self) -> None:
        """Coding and file tools should expose restrict_to_base_dir in metadata."""
        from mindroom.tool_system.metadata import TOOL_METADATA  # noqa: PLC0415

        coding_field = next(
            field for field in TOOL_METADATA["coding"].config_fields if field.name == "restrict_to_base_dir"
        )
        file_field = next(
            field for field in TOOL_METADATA["file"].config_fields if field.name == "restrict_to_base_dir"
        )
        description = "Whether file access must stay under base_dir. Relative paths still resolve from base_dir."

        assert coding_field.type == "boolean"
        assert coding_field.default is True
        assert coding_field.description == description
        assert file_field.type == "boolean"
        assert file_field.default is True
        assert file_field.description == description

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
