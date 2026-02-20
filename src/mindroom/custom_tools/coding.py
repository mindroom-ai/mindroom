"""Ergonomic coding tools for LLM agents.

Provides file read/write/edit, grep, find, and ls operations with
smart truncation, fuzzy matching, and actionable pagination hints.
Inspired by the PI coding editor approach to LLM-friendly file operations.
"""

from __future__ import annotations

import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from agno.tools import Toolkit

MAX_LINES = 500
MAX_BYTES = 50 * 1024  # 50KB
DEFAULT_GREP_LIMIT = 100
DEFAULT_FIND_LIMIT = 1000
DEFAULT_LS_LIMIT = 500


@dataclass
class TruncateResult:
    """Result of a truncation operation."""

    content: str
    was_truncated: bool
    total_lines: int
    shown_lines: int


def _truncate_head(
    content: str,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
) -> TruncateResult:
    """Keep the first N lines / max_bytes of content."""
    lines = content.splitlines(keepends=True)
    total = len(lines)
    kept: list[str] = []
    byte_count = 0
    for line in lines:
        line_bytes = len(line.encode("utf-8", errors="replace"))
        if len(kept) >= max_lines or byte_count + line_bytes > max_bytes:
            break
        kept.append(line)
        byte_count += line_bytes
    result = "".join(kept)
    return TruncateResult(
        content=result,
        was_truncated=len(kept) < total,
        total_lines=total,
        shown_lines=len(kept),
    )


def _truncate_tail(
    content: str,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
) -> TruncateResult:
    """Keep the last N lines / max_bytes of content."""
    lines = content.splitlines(keepends=True)
    total = len(lines)
    kept: list[str] = []
    byte_count = 0
    for line in reversed(lines):
        line_bytes = len(line.encode("utf-8", errors="replace"))
        if len(kept) >= max_lines or byte_count + line_bytes > max_bytes:
            break
        kept.append(line)
        byte_count += line_bytes
    kept.reverse()
    result = "".join(kept)
    return TruncateResult(
        content=result,
        was_truncated=len(kept) < total,
        total_lines=total,
        shown_lines=len(kept),
    )


# ── Fuzzy matching helpers ──────────────────────────────────────────

# Smart quotes / dashes / special spaces → ASCII equivalents
_QUOTE_MAP = str.maketrans(
    {
        "\u2018": "'",  # '
        "\u2019": "'",  # '
        "\u201c": '"',  # "
        "\u201d": '"',  # "
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u00a0": " ",  # non-breaking space
        "\u2002": " ",  # en space
        "\u2003": " ",  # em space
        "\u2009": " ",  # thin space
        "\ufeff": "",  # BOM
    },
)


def _normalize_for_fuzzy(text: str) -> str:
    """Normalize text for fuzzy matching.

    - Strip trailing whitespace per line
    - Normalize Unicode quotes/dashes/spaces to ASCII
    - Normalize NFC
    """
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_QUOTE_MAP)
    lines = text.splitlines()
    return "\n".join(line.rstrip() for line in lines)


@dataclass
class MatchResult:
    """Result of a fuzzy find operation."""

    start: int
    end: int
    matched_text: str
    was_fuzzy: bool


def _fuzzy_find(content: str, old_text: str) -> MatchResult | None:
    """Try exact match first, then fuzzy match."""
    # Exact match
    idx = content.find(old_text)
    if idx != -1:
        return MatchResult(
            start=idx,
            end=idx + len(old_text),
            matched_text=old_text,
            was_fuzzy=False,
        )

    # Fuzzy match: normalize both sides
    norm_content = _normalize_for_fuzzy(content)
    norm_old = _normalize_for_fuzzy(old_text)

    idx = norm_content.find(norm_old)
    if idx == -1:
        return None

    # Map normalized index back to original content.
    # We need to find the corresponding region in the original.
    # Walk through original lines to find the matching region.
    orig_lines = content.splitlines(keepends=True)
    norm_lines = norm_content.splitlines(keepends=True)

    # Build a mapping from normalized offset to original offset
    norm_offset = 0
    orig_offset = 0
    start_orig = None
    end_orig = None
    norm_end = idx + len(norm_old)

    for orig_line, norm_line in zip(orig_lines, norm_lines):
        if start_orig is None and norm_offset + len(norm_line) > idx:
            # Start is in this line
            line_delta = idx - norm_offset
            start_orig = orig_offset + line_delta
        if start_orig is not None and norm_offset + len(norm_line) >= norm_end:
            line_delta = norm_end - norm_offset
            end_orig = orig_offset + line_delta
            break
        norm_offset += len(norm_line)
        orig_offset += len(orig_line)

    if start_orig is not None and end_orig is not None:
        return MatchResult(
            start=start_orig,
            end=end_orig,
            matched_text=content[start_orig:end_orig],
            was_fuzzy=True,
        )
    return None


def _count_occurrences(content: str, old_text: str) -> int:
    """Count non-overlapping occurrences, trying exact then fuzzy."""
    # First try exact
    count = content.count(old_text)
    if count > 0:
        return count

    # Fuzzy count
    norm_content = _normalize_for_fuzzy(content)
    norm_old = _normalize_for_fuzzy(old_text)
    return norm_content.count(norm_old)


def _make_diff(old_text: str, new_text: str) -> str:
    """Create a simple diff showing what changed."""
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    result = [f"- {line}" for line in old_lines]
    result.extend(f"+ {line}" for line in new_lines)
    return "\n".join(result)


def _number_lines(content: str, start: int = 1) -> str:
    """Add line numbers to content."""
    lines = content.splitlines()
    if not lines:
        return ""
    width = len(str(start + len(lines) - 1))
    numbered = []
    for i, line in enumerate(lines):
        numbered.append(f"{start + i:>{width}}| {line}")
    return "\n".join(numbered)


def _resolve_path(base_dir: Path, path: str) -> Path:
    """Resolve a path relative to base_dir, preventing traversal."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    resolved = p.resolve()
    base_resolved = base_dir.resolve()
    # Allow the base_dir itself
    if resolved != base_resolved and not str(resolved).startswith(str(base_resolved) + os.sep):
        msg = f"Path '{path}' resolves outside the base directory."
        raise ValueError(msg)
    return resolved


def _is_gitignored(path: Path, base_dir: Path) -> bool:
    """Check if a path is gitignored using git check-ignore."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            check=False,
            cwd=str(base_dir),
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    else:
        return result.returncode == 0


class CodingTools(Toolkit):
    """Ergonomic coding tools for LLM agents.

    Provides file read/write/edit, grep, find, and ls operations with
    smart truncation, fuzzy matching, and actionable pagination hints.
    """

    def __init__(self, base_dir: str | None = None) -> None:
        self.base_dir = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
        super().__init__(
            name="coding",
            tools=[
                self.read_file,
                self.edit_file,
                self.write_file,
                self.grep,
                self.find_files,
                self.ls,
            ],
        )

    def read_file(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        """Read a file with line numbers. Large files are automatically truncated with pagination hints.

        Args:
            path: File path (relative to working directory or absolute).
            offset: Starting line number (1-based). Use this to paginate through large files.
            limit: Maximum number of lines to return. Defaults to 500.

        Returns:
            Line-numbered file content with pagination hints if truncated.

        """
        try:
            resolved = _resolve_path(self.base_dir, path)
        except ValueError as e:
            return f"Error: {e}"

        if not resolved.exists():
            return f"Error: File not found: {path}"
        if not resolved.is_file():
            return f"Error: Not a file: {path}"

        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"Error reading file: {e}"

        all_lines = content.splitlines()
        total_lines = len(all_lines)

        # Apply offset (1-based)
        start = max(1, offset or 1)
        if start > total_lines:
            return f"Error: offset {start} exceeds file length ({total_lines} lines)."

        max_lines = limit or MAX_LINES
        end = min(start + max_lines - 1, total_lines)
        selected = all_lines[start - 1 : end]

        # Also check byte limit
        selected_text = "\n".join(selected)
        if len(selected_text.encode("utf-8", errors="replace")) > MAX_BYTES:
            # Re-truncate by bytes
            trunc = _truncate_head(selected_text, max_lines=len(selected), max_bytes=MAX_BYTES)
            selected = trunc.content.splitlines()
            end = start + len(selected) - 1

        numbered = _number_lines("\n".join(selected), start=start)

        if end < total_lines:
            hint = f"\n\n[Showing lines {start}-{end} of {total_lines}. Use offset={end + 1} to continue.]"
        elif start > 1:
            hint = f"\n\n[Showing lines {start}-{end} of {total_lines}.]"
        else:
            hint = ""

        return numbered + hint

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:  # noqa: PLR0911
        """Replace a specific text occurrence in a file. Uses fuzzy matching to handle whitespace/Unicode differences.

        The old_text must match exactly one location in the file. If it matches
        zero or more than one location, an error is returned.

        Args:
            path: File path (relative to working directory or absolute).
            old_text: The text to find and replace. Must be unique in the file.
            new_text: The replacement text.

        Returns:
            A diff showing the change, or an error message.

        """
        try:
            resolved = _resolve_path(self.base_dir, path)
        except ValueError as e:
            return f"Error: {e}"

        if not resolved.exists():
            return f"Error: File not found: {path}"
        if not resolved.is_file():
            return f"Error: Not a file: {path}"

        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return f"Error reading file: {e}"

        # Check uniqueness
        occurrences = _count_occurrences(content, old_text)
        if occurrences == 0:
            return "Error: old_text not found in file."
        if occurrences > 1:
            return f"Error: old_text matches {occurrences} locations. Provide more context to make the match unique."

        match = _fuzzy_find(content, old_text)
        if match is None:
            return "Error: old_text not found in file."

        # Perform replacement
        new_content = content[: match.start] + new_text + content[match.end :]

        try:
            resolved.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return f"Error writing file: {e}"

        # Build result
        diff = _make_diff(match.matched_text, new_text)
        fuzzy_note = " (fuzzy match: whitespace/Unicode normalized)" if match.was_fuzzy else ""
        # Find the line number of the edit
        line_num = content[: match.start].count("\n") + 1
        return f"Applied edit at line {line_num}{fuzzy_note}:\n\n{diff}"

    def write_file(self, path: str, content: str) -> str:
        """Write content to a file, creating parent directories if needed.

        Args:
            path: File path (relative to working directory or absolute).
            content: The full file content to write.

        Returns:
            Confirmation with byte count written.

        """
        try:
            resolved = _resolve_path(self.base_dir, path)
        except ValueError as e:
            return f"Error: {e}"

        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content, encoding="utf-8")
        except OSError as e:
            return f"Error writing file: {e}"

        byte_count = len(content.encode("utf-8"))
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Wrote {byte_count} bytes ({lines} lines) to {path}"

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_case: bool = False,
        context: int = 0,
        limit: int = DEFAULT_GREP_LIMIT,
    ) -> str:
        """Search file contents using regex patterns. Uses ripgrep if available, falls back to Python re.

        Args:
            pattern: Regex pattern to search for.
            path: Directory or file to search in. Defaults to working directory.
            glob: File glob pattern to filter (e.g., "*.py", "*.ts").
            ignore_case: Whether to ignore case in matching.
            context: Number of context lines before and after each match.
            limit: Maximum number of matches to return. Defaults to 100.

        Returns:
            Matching lines with file paths and line numbers.

        """
        try:
            search_path = _resolve_path(self.base_dir, path) if path else self.base_dir
        except ValueError as e:
            return f"Error: {e}"

        if not search_path.exists():
            return f"Error: Path not found: {path or '.'}"

        # Try ripgrep first
        rg_result = _run_ripgrep(pattern, search_path, glob, ignore_case, context, limit)
        if rg_result is not None:
            return rg_result

        # Python fallback
        return _python_grep_fallback(pattern, search_path, glob, ignore_case, context, limit)

    def find_files(
        self,
        pattern: str,
        path: str | None = None,
        limit: int = DEFAULT_FIND_LIMIT,
    ) -> str:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g., "*.py", "**/*.ts", "src/**/*.jsx").
            path: Directory to search in. Defaults to working directory.
            limit: Maximum number of results. Defaults to 1000.

        Returns:
            List of matching file paths, one per line.

        """
        try:
            search_path = _resolve_path(self.base_dir, path) if path else self.base_dir
        except ValueError as e:
            return f"Error: {e}"

        if not search_path.exists():
            return f"Error: Path not found: {path or '.'}"

        matches: list[str] = []
        for p in sorted(search_path.glob(pattern)):
            if p.is_file() and not _is_gitignored(p, self.base_dir):
                try:
                    rel = p.relative_to(self.base_dir)
                except ValueError:
                    rel = p
                matches.append(str(rel))
                if len(matches) >= limit:
                    break

        if not matches:
            return f"No files matching '{pattern}' found."

        result = "\n".join(matches)
        if len(matches) >= limit:
            result += f"\n\n[Results limited to {limit}. Narrow the pattern to see more.]"
        return result

    def ls(self, path: str | None = None, limit: int = DEFAULT_LS_LIMIT) -> str:
        """List directory contents with directory indicators.

        Args:
            path: Directory to list. Defaults to working directory.
            limit: Maximum number of entries. Defaults to 500.

        Returns:
            Sorted directory listing with '/' suffix on directories.

        """
        try:
            target = _resolve_path(self.base_dir, path) if path else self.base_dir
        except ValueError as e:
            return f"Error: {e}"

        if not target.exists():
            return f"Error: Path not found: {path or '.'}"
        if not target.is_dir():
            return f"Error: Not a directory: {path}"

        entries: list[str] = []
        try:
            for item in sorted(target.iterdir()):
                if item.name.startswith("."):
                    continue
                name = item.name + ("/" if item.is_dir() else "")
                entries.append(name)
                if len(entries) >= limit:
                    break
        except OSError as e:
            return f"Error listing directory: {e}"

        if not entries:
            return "Directory is empty."

        result = "\n".join(entries)
        if len(entries) >= limit:
            result += f"\n\n[Listing limited to {limit} entries.]"
        return result


# ── Grep helpers ────────────────────────────────────────────────────


def _run_ripgrep(
    pattern: str,
    search_path: Path,
    glob_filter: str | None,
    ignore_case: bool,
    context: int,
    limit: int,
) -> str | None:
    """Run ripgrep and return formatted results. Returns None if rg is not available."""
    args = ["rg", "--no-heading", "--line-number", "--color=never"]
    if ignore_case:
        args.append("-i")
    if context > 0:
        args.extend(["-C", str(context)])
    if glob_filter:
        args.extend(["--glob", glob_filter])
    args.extend(["-m", str(limit), pattern, str(search_path)])

    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return "Error: grep timed out after 30 seconds."

    if result.returncode == 1:
        return "No matches found."
    if result.returncode not in (0, 1):
        stderr = result.stderr.strip()
        return f"Error running grep: {stderr}" if stderr else "Error running grep."

    output = result.stdout
    trunc = _truncate_tail(output)
    if trunc.was_truncated:
        return trunc.content + f"\n\n[Output truncated. {trunc.total_lines} total lines.]"
    return output.rstrip()


def _grep_file(
    filepath: Path,
    search_path: Path,
    regex: re.Pattern[str],
    context: int,
    limit: int,
    results: list[str],
    match_count: int,
) -> int:
    """Search a single file for regex matches. Returns updated match_count."""
    try:
        text = filepath.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return match_count

    try:
        rel = filepath.relative_to(search_path)
    except ValueError:
        rel = filepath

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if match_count >= limit:
            break
        if not regex.search(line):
            continue
        match_count += 1
        if context > 0:
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            for j in range(start, end):
                marker = ":" if j == i else "-"
                results.append(f"{rel}{marker}{j + 1}{marker}{lines[j]}")
            results.append("--")
        else:
            results.append(f"{rel}:{i + 1}:{line}")
    return match_count


def _python_grep_fallback(
    pattern: str,
    search_path: Path,
    glob_filter: str | None,
    ignore_case: bool,
    context: int,
    limit: int,
) -> str:
    """Pure Python grep fallback when ripgrep is not available."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"

    results: list[str] = []
    match_count = 0

    if search_path.is_file():
        files: list[Path] = [search_path]
    else:
        file_glob = glob_filter or "**/*"
        files = sorted(search_path.glob(file_glob))

    for filepath in files:
        if not filepath.is_file():
            continue
        match_count = _grep_file(filepath, search_path, regex, context, limit, results, match_count)
        if match_count >= limit:
            break

    if not results:
        return "No matches found."

    output = "\n".join(results)
    if match_count >= limit:
        output += f"\n\n[Results limited to {limit} matches.]"
    return output
