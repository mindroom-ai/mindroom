"""Tests for the ergonomic coding tools."""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom.custom_tools import coding as coding_module
from mindroom.custom_tools.coding import (
    CodingTools,
    _find_all_matches,
    _normalize_for_fuzzy,
    _run_ripgrep,
    _truncate_head,
    _truncate_line,
)
from mindroom.path_safety import (
    _blocked_base_dir_message,
    format_path_for_output,
    resolve_base_dir_path,
    split_search_pattern,
)
from mindroom.tool_system.registry import builtin_tool_registry
from mindroom.tool_system.tool_schema import tool_parameters, validated_entrypoint
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
        assert len(result.content.splitlines()) == 10
        assert result.total_lines == 1000

    def test_truncation_by_bytes(self) -> None:
        """Content exceeding max_bytes is truncated."""
        content = "x" * 200 + "\n" + "y" * 200 + "\n"
        result = _truncate_head(content, max_lines=1000, max_bytes=250)
        assert result.was_truncated
        assert len(result.content.splitlines()) == 1


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
        assert (matches[0].start, matches[0].end) == (0, 5)

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


def _one(old_text: str, new_text: str) -> list[dict[str, str]]:
    """Return the edits of a call that makes one replacement."""
    return [{"old_text": old_text, "new_text": new_text}]


class TestEditFile:
    """Tests for CodingTools.edit_file."""

    def test_exact_edit(self, tools: CodingTools, tmp_base: Path) -> None:
        """Replaces exact text and reports where the new text landed, without a diff."""
        result = tools.edit_file("hello.py", _one("print('hello')", "print('hi')"))
        assert result == "Applied 1 edit to hello.py: line 1."
        assert (tmp_base / "hello.py").read_text() == "print('hi')\nprint('world')\n"

    def test_edits_match_the_original_file_not_each_other(self, tools: CodingTools, tmp_base: Path) -> None:
        """Every old_text is matched against the file as it was, so two edits can swap lines."""
        (tmp_base / "swap.py").write_text("a = 1\nb = 2\nc = 3\n")
        result = tools.edit_file(
            "swap.py",
            [{"old_text": "a = 1", "new_text": "b = 2"}, {"old_text": "b = 2", "new_text": "a = 1"}],
        )
        assert result == "Applied 2 edits to swap.py: lines 1, 2."
        assert (tmp_base / "swap.py").read_text() == "b = 2\na = 1\nc = 3\n"

    def test_reported_lines_are_in_the_new_file(self, tools: CodingTools, tmp_base: Path) -> None:
        """Line ranges count the replaced text where it sits after all edits."""
        (tmp_base / "lines.py").write_text("one\ntwo\nthree\nfour\n")
        result = tools.edit_file(
            "lines.py",
            [{"old_text": "four\n", "new_text": "4\n"}, {"old_text": "one\n", "new_text": "1\n1.1\n1.2\n"}],
        )
        assert result == "Applied 2 edits to lines.py: lines 1-3, 6."
        assert (tmp_base / "lines.py").read_text() == "1\n1.1\n1.2\ntwo\nthree\n4\n"

    def test_replace_all(self, tools: CodingTools, tmp_base: Path) -> None:
        """replace_all replaces every match."""
        (tmp_base / "dup.py").write_text("foo\nbar\nfoo\n")
        result = tools.edit_file("dup.py", [{"old_text": "foo", "new_text": "baz", "replace_all": True}])
        assert result == "Applied 1 edit to dup.py: lines 1, 3."
        assert (tmp_base / "dup.py").read_text() == "baz\nbar\nbaz\n"

    def test_overlapping_edits_are_refused(self, tools: CodingTools, tmp_base: Path) -> None:
        """Edits whose matches overlap are refused and nothing is written."""
        (tmp_base / "overlap.py").write_text("alpha beta gamma\n")
        result = tools.edit_file(
            "overlap.py",
            [
                {"old_text": "alpha", "new_text": "A"},
                {"old_text": "gamma", "new_text": "G"},
                {"old_text": "beta gamma", "new_text": "B"},
            ],
        )
        assert result == "Error: edits[1] and edits[2] overlap. Merge them into one edit.\nNo changes written."
        assert (tmp_base / "overlap.py").read_text() == "alpha beta gamma\n"

    def test_one_failing_edit_writes_nothing(self, tools: CodingTools, tmp_base: Path) -> None:
        """All edits apply or none does."""
        result = tools.edit_file(
            "hello.py",
            [{"old_text": "print('hello')", "new_text": "print('hi')"}, {"old_text": "missing", "new_text": "x"}],
        )
        assert result.startswith("Error: edits[1] not found in the file.")
        assert result.endswith("No changes written.")
        assert (tmp_base / "hello.py").read_text() == "print('hello')\nprint('world')\n"

    def test_not_found_names_the_closest_line(self, tools: CodingTools, tmp_base: Path) -> None:
        """A missed old_text points at the line it most likely meant."""
        (tmp_base / "calc.py").write_text("import math\n\n\ndef compute_total(items):\n    return sum(items)\n")
        result = tools.edit_file("calc.py", _one("\ndef compute_totals(items):\n    pass\n", "x"))
        assert result == (
            "Error: old_text not found in the file.\nClosest line 4: def compute_total(items):\nNo changes written."
        )

    def test_not_found_without_a_close_line(self, tools: CodingTools) -> None:
        """No hint when nothing in the file resembles old_text."""
        result = tools.edit_file("hello.py", _one("nonexistent text", "replacement"))
        assert result == "Error: old_text not found in the file.\nNo changes written."

    def test_closest_line_is_cut(self, tools: CodingTools, tmp_base: Path) -> None:
        """A long closest line is cut to 200 characters."""
        (tmp_base / "long.py").write_text("x" * 500 + "\n")
        result = tools.edit_file("long.py", _one("x" * 499 + "y", "z"))
        assert f"Closest line 1: {'x' * 200}\nNo changes written." in result

    def test_several_matches_lists_their_lines(self, tools: CodingTools, tmp_base: Path) -> None:
        """An ambiguous old_text names every line it matches and how to pick one."""
        (tmp_base / "dup.py").write_text("foo\nx\nfoo\nfoo\n")
        result = tools.edit_file("dup.py", _one("foo", "bar"))
        assert result == (
            "Error: old_text matches 3 places (lines 1, 3, 4). Add nearby lines to old_text to pick one, "
            "or set replace_all.\nNo changes written."
        )
        assert (tmp_base / "dup.py").read_text() == "foo\nx\nfoo\nfoo\n"

    def test_edits_as_a_json_string(self, tools: CodingTools, tmp_base: Path) -> None:
        """Edits a model sent as a JSON string still apply, through the validated tool call too."""
        edits = json.dumps(_one("print('hello')", "print('hi')"))
        assert (
            validated_entrypoint(tools.edit_file)(path="hello.py", edits=edits) == "Applied 1 edit to hello.py: line 1."
        )
        assert tools.edit_file("hello.py", json.dumps(_one("print('hi')", "print('hey')"))).startswith("Applied 1 edit")
        assert (tmp_base / "hello.py").read_text() == "print('hey')\nprint('world')\n"

    def test_legacy_flat_call_still_applies(self, tools: CodingTools, tmp_base: Path) -> None:
        """A call with the old flat old_text and new_text arguments works, though the schema no longer shows them."""
        result = validated_entrypoint(tools.edit_file)(
            path="hello.py",
            old_text="print('hello')",
            new_text="print('hi')",
        )
        assert result == "Applied 1 edit to hello.py: line 1."
        assert (tmp_base / "hello.py").read_text() == "print('hi')\nprint('world')\n"

    def test_unknown_arguments_show_the_shape(self, tools: CodingTools, tmp_base: Path) -> None:
        """A call without edits gets an error that shows how to call it."""
        result = validated_entrypoint(tools.edit_file)(path="hello.py", old_str="print('hello')", new_str="x")
        assert result.startswith("Error:")
        assert '"old_text"' in result
        assert (tmp_base / "hello.py").read_text() == "print('hello')\nprint('world')\n"

    def test_schema_offers_only_path_and_edits(self, tools: CodingTools) -> None:
        """The model sees edits items with old_text, new_text and replace_all, and no top-level old_text."""
        parameters = tool_parameters(tools.edit_file)
        assert list(parameters["properties"]) == ["path", "edits"]
        items = parameters["properties"]["edits"]["items"]
        assert list(items["properties"]) == ["old_text", "new_text", "replace_all"]
        assert items["required"] == ["old_text", "new_text"]

    def test_crlf_and_bom_survive(self, tools: CodingTools, tmp_base: Path) -> None:
        """Line endings and a byte order mark stay as they were."""
        (tmp_base / "win.txt").write_bytes(b"\xef\xbb\xbfline one\r\nline two\r\nline three\r\n")
        result = tools.edit_file(
            "win.txt",
            [{"old_text": "line one", "new_text": "first"}, {"old_text": "line three", "new_text": "third"}],
        )
        assert result == "Applied 2 edits to win.txt: lines 1, 3."
        assert (tmp_base / "win.txt").read_bytes() == b"\xef\xbb\xbffirst\r\nline two\r\nthird\r\n"

    def test_lf_edits_of_a_crlf_file_keep_crlf(self, tools: CodingTools, tmp_base: Path) -> None:
        """An old_text written with LF matches a CRLF file, and its new_text lands with CRLF."""
        (tmp_base / "win.txt").write_bytes(b"line one\r\nline two\r\nline three\r\n")
        result = tools.edit_file("win.txt", _one("line one\nline two\n", "first\nsecond\nthird\n"))
        assert result == "Applied 1 edit to win.txt: lines 1-3."
        assert (tmp_base / "win.txt").read_bytes() == b"first\r\nsecond\r\nthird\r\nline three\r\n"

    def test_fuzzy_edit_whitespace(self, tools: CodingTools, tmp_base: Path) -> None:
        """Handles trailing whitespace differences via fuzzy matching."""
        (tmp_base / "ws.py").write_text("def foo():   \n    pass\n")
        result = tools.edit_file("ws.py", _one("def foo():\n    pass", "def bar():\n    pass"))
        assert result == "Applied 1 edit to ws.py: lines 1-2 (matched after normalizing whitespace/quotes)."
        assert (tmp_base / "ws.py").read_text() == "def bar():\n    pass\n"

    def test_fuzzy_note_names_the_edit(self, tools: CodingTools, tmp_base: Path) -> None:
        """With several edits, the fuzzy note says which one matched loosely."""
        (tmp_base / "q.py").write_text("a = 1\nsay(\u201chi\u201d)\n")
        result = tools.edit_file(
            "q.py",
            [{"old_text": "a = 1", "new_text": "a = 2"}, {"old_text": 'say("hi")', "new_text": 'say("bye")'}],
        )
        assert result == "Applied 2 edits to q.py: lines 1, 2 (edits[1] matched after normalizing whitespace/quotes)."
        assert (tmp_base / "q.py").read_text() == 'a = 2\nsay("bye")\n'

    def test_fuzzy_edit_multiline_preserves_line_boundaries(self, tools: CodingTools, tmp_base: Path) -> None:
        """Fuzzy multiline edits should consume trailing spaces and line ending of matched lines."""
        (tmp_base / "fuzzy_multiline.py").write_text("foo   \nbar\n")
        result = tools.edit_file("fuzzy_multiline.py", _one("foo\n", "X\n"))
        assert "normalizing" in result
        assert (tmp_base / "fuzzy_multiline.py").read_text() == "X\nbar\n"

    def test_fuzzy_edit_single_line_consumes_trailing_whitespace_and_newline(
        self,
        tools: CodingTools,
        tmp_base: Path,
    ) -> None:
        """Fuzzy single-line edits should not leave trailing whitespace artifacts."""
        (tmp_base / "fuzzy_single_line.py").write_text("foo   \n")
        result = tools.edit_file("fuzzy_single_line.py", _one("foo\n", "X\n"))
        assert "normalizing" in result
        assert (tmp_base / "fuzzy_single_line.py").read_text() == "X\n"

    def test_fuzzy_edit_handles_composed_vs_decomposed_unicode(self, tools: CodingTools, tmp_base: Path) -> None:
        """Fuzzy edits should replace full graphemes across NFC differences."""
        (tmp_base / "unicode.py").write_text("Cafe\u0301")
        result = tools.edit_file("unicode.py", _one("Café", "Tea"))
        assert "normalizing" in result
        assert (tmp_base / "unicode.py").read_text() == "Tea"

    def test_edit_missing_file(self, tools: CodingTools) -> None:
        """Returns error for missing files."""
        result = tools.edit_file("missing.py", _one("old", "new"))
        assert "Error" in result
        assert "not found" in result

    def test_edit_rejects_empty_old_text(self, tools: CodingTools, tmp_base: Path) -> None:
        """Rejects empty old_text instead of attempting a replacement."""
        result = tools.edit_file("hello.py", [*_one("print('hello')", "x"), *_one("", "replacement")])
        assert result == "Error: edits[1].old_text is empty.\nNo changes written."
        assert (tmp_base / "hello.py").read_text() == "print('hello')\nprint('world')\n"

    def test_edit_rejects_empty_edits(self, tools: CodingTools) -> None:
        """An empty edits list is refused with the shape to use."""
        result = tools.edit_file("hello.py", [])
        assert result.startswith("Error: edits is empty.")
        assert '"old_text"' in result

    def test_edit_rejects_a_no_op(self, tools: CodingTools) -> None:
        """Edits that leave the file as it was are refused."""
        result = tools.edit_file("hello.py", _one("print('hello')", "print('hello')"))
        assert result.startswith("Error:")
        assert "identical" in result
        assert result.endswith("No changes written.")


class TestEditFileConcurrency:
    """Concurrent edit_file calls on one file serialize; calls on different files still run in parallel."""

    @staticmethod
    def _run_concurrently(
        tools: CodingTools,
        calls: list[tuple[str, list[dict[str, str]]]],
        monkeypatch: pytest.MonkeyPatch,
        barrier_timeout: float,
    ) -> threading.Barrier:
        """Run edit_file calls in threads that wait for each other right after reading the file."""
        barrier = threading.Barrier(len(calls))
        read = coding_module.read_resolved_file

        def read_then_wait(base_dir: Path, resolved: Path) -> bytes:
            content = read(base_dir, resolved)
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=barrier_timeout)
            return content

        monkeypatch.setattr(coding_module, "read_resolved_file", read_then_wait)
        results: list[str] = []
        threads = [threading.Thread(target=lambda c=call: results.append(tools.edit_file(*c))) for call in calls]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(result.startswith("Applied") for result in results), results
        return barrier

    @pytest.mark.parametrize("where", ["inside", "outside"])
    def test_concurrent_edits_of_one_file_both_land(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        where: str,
    ) -> None:
        """Two racing edits of one file both land, and no tail of a longer write is left behind."""
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        target = base_dir / "race.txt" if where == "inside" else tmp_path / "race.txt"
        target.write_text("AAAA\nBBBB\n")
        tools = CodingTools(base_dir=str(base_dir), file_access="workspace" if where == "inside" else "unrestricted")

        self._run_concurrently(
            tools,
            [(str(target), _one("AAAA", "A" * 50)), (str(target), _one("BBBB", "b"))],
            monkeypatch,
            barrier_timeout=0.5,
        )

        assert target.read_text() == "A" * 50 + "\nb\n"

    def test_edits_of_different_files_run_in_parallel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Calls on different files are not serialized: both reach the barrier together."""
        (tmp_path / "one.txt").write_text("one\n")
        (tmp_path / "two.txt").write_text("two\n")
        tools = CodingTools(base_dir=str(tmp_path))

        barrier = self._run_concurrently(
            tools,
            [("one.txt", _one("one", "1")), ("two.txt", _one("two", "2"))],
            monkeypatch,
            barrier_timeout=10,
        )

        assert not barrier.broken
        assert (tmp_path / "one.txt").read_text() == "1\n"
        assert (tmp_path / "two.txt").read_text() == "2\n"


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

    def test_gitignore_checks_never_run_workspace_git_programs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker code writes the workspace .git, so the primary's ignore check runs Git hardened."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True, text=True)
        marker = tmp_path / "fsmonitor-ran"
        hook = tmp_path / "fsmonitor-hook"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        subprocess.run(["git", "config", "core.fsmonitor", str(hook)], cwd=workspace, check=True, capture_output=True)
        (workspace / ".gitignore").write_text("ignored.txt\n")
        (workspace / "visible.txt").write_text("match me\n")
        (workspace / "ignored.txt").write_text("match me\n")
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)

        result = CodingTools(base_dir=str(workspace)).grep("match")

        assert "visible.txt:1:match me" in result
        assert "ignored.txt" not in result
        assert not marker.exists()

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
            if isinstance(cmd, list) and "check-ignore" in cmd:
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

    def test_find_searches_a_named_dot_directory(self, tools: CodingTools, tmp_base: Path) -> None:
        """A dot directory named in path or in the pattern's leading directories is searched, not hidden."""
        staging = tmp_base / ".mindroom" / "runs" / "r1" / "memory"
        staging.mkdir(parents=True)
        (staging / "people.md").write_text("x")
        (staging / ".draft.md").write_text("x")

        by_path = tools.find_files("*.md", path=".mindroom/runs/r1/memory")
        by_pattern = tools.find_files(".mindroom/runs/*/memory/*.md")

        for result in (by_path, by_pattern):
            assert ".mindroom/runs/r1/memory/people.md" in result
            assert ".draft.md" not in result
        assert ".mindroom" not in tools.find_files("**/*.md")

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
            if isinstance(cmd, list) and "check-ignore" in cmd:
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
        result = tools.edit_file("../../etc/passwd", _one("root", "hacked"))
        assert "Error" in result

    def test_absolute_outside_base(self, tools: CodingTools) -> None:
        """Blocks absolute paths outside base directory."""
        result = tools.read_file("/etc/hostname")
        assert "Error" in result

    def test_tilde_not_expanded(self, tools: CodingTools) -> None:
        """Tilde paths are not expanded and treated as literal relative paths."""
        result = tools.read_file("~/../../etc/passwd")
        assert "Error" in result


class TestPathSafetyHelpers:
    """Characterization tests for shared file/coding path helpers."""

    def test_resolve_base_dir_path_preserves_coding_blocked_message(self, tmp_path: Path) -> None:
        """Blocked path resolution should keep the coding-tool error text."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        expected = _blocked_base_dir_message(str(outside_file), outside_file, base_dir)
        with pytest.raises(ValueError, match=re.escape(expected)) as exc_info:
            resolve_base_dir_path(base_dir, str(outside_file))

        assert str(exc_info.value) == expected

    def test_format_path_for_output_prefers_base_relative_paths(self, tmp_path: Path) -> None:
        """Output formatting should use relative paths only inside base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_file = base_dir / "src" / "app.py"
        outside_file = outside_dir / "secret.txt"
        base_file.parent.mkdir(parents=True)
        outside_dir.mkdir()

        assert format_path_for_output(base_file, base_dir) == "src/app.py"
        assert format_path_for_output(outside_file, base_dir) == str(outside_file)

    def test_split_search_pattern_preserves_absolute_and_parent_traversal_patterns(self, tmp_path: Path) -> None:
        """Glob-root splitting should preserve existing absolute and ../ behavior."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()

        absolute_root, absolute_pattern = split_search_pattern(base_dir, str(outside_dir / "*.txt"))
        parent_root, parent_pattern = split_search_pattern(base_dir, "../outside/*.txt")

        assert absolute_root == outside_dir.resolve()
        assert absolute_pattern == "*.txt"
        assert parent_root == outside_dir.resolve()
        assert parent_pattern == "*.txt"


class TestCodingToolsFileAccess:
    """Tests for the coding tool's agent file_access mode."""

    def test_coding_tools_workspace_mode_rejects_outside_paths(self, tmp_path: Path) -> None:
        """Workspace mode refuses reads outside the workspace."""
        base = tmp_path / "ws"
        base.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        tools = CodingTools(base_dir=str(base), file_access="workspace")
        assert tools.read_file(str(outside)).startswith("Error")

    def test_coding_tools_unrestricted_mode_reads_outside_paths(self, tmp_path: Path) -> None:
        """Unrestricted mode reads files outside the workspace."""
        base = tmp_path / "ws"
        base.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("hello")
        tools = CodingTools(base_dir=str(base), file_access="unrestricted")
        assert "hello" in tools.read_file(str(outside))

    def test_default_workspace_mode_blocks_outside(self, tmp_path: Path) -> None:
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
        assert "file_access to 'unrestricted'" in result

    def test_unrestricted_allows_outside(self, tmp_path: Path) -> None:
        """Unrestricted file access should allow absolute paths outside base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"

        tools = CodingTools(base_dir=str(base_dir), file_access="unrestricted")
        write_result = tools.write_file(str(outside_file), "outside\n")
        read_result = tools.read_file(str(outside_file))

        assert "Wrote" in write_result
        assert outside_file.read_text() == "outside\n"
        assert "outside" in read_result

    def test_unrestricted_relative_paths_still_work(self, tmp_path: Path) -> None:
        """Relative paths should still resolve from base_dir when unrestricted."""
        base_dir = tmp_path / "base"
        base_dir.mkdir()

        tools = CodingTools(base_dir=str(base_dir), file_access="unrestricted")
        write_result = tools.write_file("nested/file.txt", "workspace\n")
        read_result = tools.read_file("nested/file.txt")

        assert "Wrote" in write_result
        assert (base_dir / "nested" / "file.txt").read_text() == "workspace\n"
        assert "workspace" in read_result

    def test_workspace_mode_blocks_edit_outside(self, tmp_path: Path) -> None:
        """Restricted edit_file should reject files outside base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("hello\n")

        tools = CodingTools(base_dir=str(base_dir))
        result = tools.edit_file(str(outside_file), _one("hello", "goodbye"))

        assert "Error" in result
        assert "outside base_dir" in result
        assert outside_file.read_text() == "hello\n"

    def test_unrestricted_allows_edit_outside(self, tmp_path: Path) -> None:
        """Unrestricted edit_file should work on files outside base_dir."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("hello\n")

        tools = CodingTools(base_dir=str(base_dir), file_access="unrestricted")
        result = tools.edit_file(str(outside_file), _one("hello", "goodbye"))

        assert result.startswith("Applied 1 edit")
        assert outside_file.read_text() == "goodbye\n"

    def test_unrestricted_finds_files_outside(
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

        tools = CodingTools(base_dir=str(base_dir), file_access="unrestricted")
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)

        find_result = tools.find_files("*.txt", path=str(outside_dir))
        grep_result = tools.grep("needle", path=str(outside_dir), glob="*.txt")

        assert str(outside_file) in find_result
        assert f"{outside_file}:1:needle" in grep_result

    def test_unrestricted_supports_parent_traversal_patterns(
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

        tools = CodingTools(base_dir=str(base_dir), file_access="unrestricted")
        monkeypatch.setattr("mindroom.custom_tools.coding._run_ripgrep", lambda *_args, **_kwargs: None)

        find_result = tools.find_files("../outside/*.txt")
        grep_result = tools.grep("needle", glob="../outside/*.txt")

        assert str(outside_file) in find_result
        assert f"{outside_file}:1:needle" in grep_result


class TestFileToolFileAccess:
    """Tests for the file tool's agent file_access mode."""

    def test_file_tool_workspace_mode_rejects_outside_paths(self, tmp_path: Path) -> None:
        """Workspace mode refuses reads outside the workspace."""
        base = tmp_path / "ws"
        base.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("x")
        tool = file_tools()(base_dir=base, file_access="workspace")
        assert tool.read_file(str(outside)).startswith("Error")

    def test_file_tool_unrestricted_mode_reads_outside_paths(self, tmp_path: Path) -> None:
        """Unrestricted mode reads files outside the workspace."""
        base = tmp_path / "ws"
        base.mkdir()
        outside = tmp_path / "outside.txt"
        outside.write_text("hello")
        tool = file_tools()(base_dir=base, file_access="unrestricted")
        assert "hello" in tool.read_file(str(outside))

    def test_file_tool_default_workspace_mode_blocks_outside(self, tmp_path: Path) -> None:
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
        assert "file_access to 'unrestricted'" in result

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

    def test_file_tool_search_content_blocks_parent_traversal_when_restricted(self, tmp_path: Path) -> None:
        """Restricted search_content refuses a ../ directory instead of searching it."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        (outside_dir / "secret.txt").write_text("needle here\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir)
        result = tool.search_content("needle", "../outside")

        assert result.startswith("Error searching content: path '../outside' is outside base_dir")

    def test_file_tool_search_content_searches_inside_base_dir(self, tmp_path: Path) -> None:
        """Agno's content search runs unchanged inside base_dir, with base-relative paths."""
        base_dir = tmp_path / "base"
        (base_dir / "docs").mkdir(parents=True)
        (base_dir / "docs" / "inside.txt").write_text("needle inside\n")
        (base_dir / "other.txt").write_text("nothing here\n")

        cls = file_tools()
        result = json.loads(cls(base_dir=base_dir).search_content("needle", "docs"))

        assert [match["file"] for match in result["files"]] == ["docs/inside.txt"]

    def test_file_tool_search_content_matches_multiline_queries_in_crlf_and_cr_files(self, tmp_path: Path) -> None:
        """Workspace content search translates line endings as Agno's search does, so multiline queries match."""
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        (base_dir / "crlf.txt").write_bytes(b"first line\r\nsecond line\r\n")
        (base_dir / "cr.txt").write_bytes(b"first line\rsecond line\r")

        cls = file_tools()
        result = json.loads(cls(base_dir=base_dir).search_content("first line\nsecond line"))

        assert sorted(match["file"] for match in result["files"]) == ["cr.txt", "crlf.txt"]
        assert all("\r" not in match["snippet"] for match in result["files"])

    def test_file_tool_search_content_searches_outside_directories_when_unrestricted(self, tmp_path: Path) -> None:
        """Unrestricted content search reaches outside directories and reports absolute paths."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        (outside_dir / "sub").mkdir(parents=True)
        (outside_dir / "sub" / "secret.txt").write_text("needle outside\n")
        (outside_dir / "other.txt").write_text("nothing here\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, file_access="unrestricted")
        result = json.loads(tool.search_content("needle", str(outside_dir)))
        relative_result = json.loads(tool.search_content("needle", "../outside"))

        expected = [str(outside_dir / "sub" / "secret.txt")]
        assert [match["file"] for match in result["files"]] == expected
        assert [match["file"] for match in relative_result["files"]] == expected
        assert "needle outside" in result["files"][0]["snippet"]
        missing = outside_dir / "missing"
        assert tool.search_content("needle", str(missing)) == f"Error: '{missing}' is not a directory"

    def test_file_tool_unrestricted_allows_outside_and_relative_paths(self, tmp_path: Path) -> None:
        """File tools should allow outside absolute paths while keeping relative paths anchored."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"

        cls = file_tools()
        tool = cls(base_dir=base_dir, file_access="unrestricted")

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
        tool = cls(base_dir=base_dir, file_access="unrestricted")
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
        tool = cls(base_dir=base_dir, file_access="unrestricted")
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
        tool = cls(base_dir=base_dir, file_access="unrestricted")
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
        tool = cls(base_dir=base_dir, file_access="unrestricted")
        result = tool.list_files(directory=str(outside_dir))

        assert json.loads(result) == [str(outside_file)]

    def test_file_tool_search_files_supports_absolute_patterns_when_unrestricted(self, tmp_path: Path) -> None:
        """Absolute search patterns should work with unrestricted file access."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, file_access="unrestricted")
        result = json.loads(tool.search_files(str(outside_dir / "*.txt")))

        assert result["files"] == [str(outside_file)]
        assert result["matches_found"] == 1

    def test_file_tool_search_files_supports_parent_traversal_when_unrestricted(self, tmp_path: Path) -> None:
        """Relative ../ search patterns should work with unrestricted file access."""
        base_dir = tmp_path / "base"
        outside_dir = tmp_path / "outside"
        base_dir.mkdir()
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret\n")

        cls = file_tools()
        tool = cls(base_dir=base_dir, file_access="unrestricted")
        result = json.loads(tool.search_files("../outside/*.txt"))

        assert result["files"] == [str(outside_file)]
        assert result["matches_found"] == 1


class TestGitMetadataWrites:
    """Agent file tools must not author Git metadata that MindRoom later runs Git against."""

    _CONFIG = "knowledge/docs/.git/config"
    _ORIGINAL = "[core]\n\tbare = false\n"

    @pytest.fixture
    def git_config(self, tmp_path: Path) -> Path:
        """Create a linked knowledge checkout's Git config inside the workspace."""
        config_path = tmp_path / self._CONFIG
        config_path.parent.mkdir(parents=True)
        config_path.write_text(self._ORIGINAL)
        return config_path

    def test_file_tool_blocks_git_metadata_writes(self, tmp_path: Path, git_config: Path) -> None:
        """save_file, replace_file_chunk, and delete_file refuse .git paths but reads still work."""
        (tmp_path / "linked").symlink_to(git_config.parent, target_is_directory=True)
        tool = file_tools()(base_dir=tmp_path, enable_delete_file=True)

        for result in (
            tool.save_file("[core]\n", self._CONFIG),
            tool.save_file("[core]\n", "linked/config"),
            tool.save_file("gitdir: elsewhere\n", "knowledge/other/.git"),
            tool.save_file("[core]\n", "knowledge/other/.GIT/config"),
            tool.replace_file_chunk(self._CONFIG, 0, 0, "[core]"),
            tool.delete_file(self._CONFIG),
        ):
            assert "Git metadata" in result

        assert git_config.read_text() == self._ORIGINAL
        assert not (tmp_path / "knowledge" / "other").exists()
        assert tool.read_file(self._CONFIG) == self._ORIGINAL

    def test_unrestricted_coding_tools_still_refuse_git_metadata_writes(self, tmp_path: Path) -> None:
        """Unrestricted file access keeps the .git write block."""
        base = tmp_path / "ws"
        (base / ".git").mkdir(parents=True)
        tools = CodingTools(base_dir=str(base), file_access="unrestricted")
        assert "Git metadata" in tools.write_file(".git/config", "[core]")
        assert not (base / ".git" / "config").exists()

    def test_coding_tools_block_git_metadata_writes(self, tmp_path: Path, git_config: Path) -> None:
        """write_file and edit_file refuse .git paths."""
        (tmp_path / "linked").symlink_to(git_config.parent, target_is_directory=True)
        tools = CodingTools(base_dir=str(tmp_path))

        assert "Git metadata" in tools.write_file(self._CONFIG, "[core]\n")
        assert "Git metadata" in tools.write_file("linked/config", "[core]\n")
        assert "Git metadata" in tools.edit_file(self._CONFIG, _one("bare = false", "bare = true"))

        assert git_config.read_text() == self._ORIGINAL


class TestRegistration:
    """Tests for tool registration."""

    def test_coding_tool_registered(self) -> None:
        """Coding tool is in the metadata registry."""
        assert "coding" in builtin_tool_registry()

    def test_restrict_to_base_dir_field_removed(self) -> None:
        """Path tools follow the agent file_access instead of a per-tool restriction field."""
        for tool_name in ("coding", "file", "python"):
            field_names = {field.name for field in builtin_tool_registry().entries[tool_name].config_fields}
            assert "restrict_to_base_dir" not in field_names, tool_name

    def test_coding_tool_factory(self) -> None:
        """Factory returns the CodingTools class."""
        from mindroom.tools.coding import coding_tools  # noqa: PLC0415

        cls = coding_tools()
        assert cls is CodingTools

    def test_toolkit_has_six_methods(self) -> None:
        """Toolkit exposes exactly the 6 expected methods."""
        tools = CodingTools()
        func_names = set(tools.functions)
        expected = {"read_file", "edit_file", "write_file", "grep", "find_files", "ls"}
        assert expected == func_names
