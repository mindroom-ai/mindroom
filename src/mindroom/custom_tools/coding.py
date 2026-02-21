"""Ergonomic coding tools for LLM agents.

Provides file read/write/edit, grep, find, and ls operations with
smart truncation, fuzzy matching, and actionable pagination hints.

Unlike the generic ``file`` tool (agno's FileTools), this toolkit is
optimised for coding agents: line-numbered reads with pagination,
search-and-replace edits with fuzzy matching, ripgrep-backed grep,
and gitignore-aware file discovery. Prefer this tool over ``file`` for
coding-heavy agents; keep ``file`` for backward compatibility.
"""

from __future__ import annotations

import bisect
import difflib
import json
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from agno.tools import Toolkit

MAX_LINES = 2000
MAX_BYTES = 50 * 1024  # 50KB
MAX_LINE_CHARS = 500  # Per-line truncation for grep output
DEFAULT_GREP_LIMIT = 100
DEFAULT_FIND_LIMIT = 1000
DEFAULT_LS_LIMIT = 500
DIFF_CONTEXT_LINES = 4


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


def _truncate_to_max_bytes(text: str, max_bytes: int) -> str:
    """Truncate text to a maximum encoded byte length."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return text
    truncated = encoded[:max_bytes]
    return truncated.decode("utf-8", errors="ignore")


# ── Fuzzy matching helpers ──────────────────────────────────────────

# Smart quotes / dashes / special spaces -> ASCII equivalents
# Matches PI coding editor's normalization (edit.ts)
_NORMALIZE_MAP = str.maketrans(
    {
        # Quotes
        "\u2018": "'",  # left single curly
        "\u2019": "'",  # right single curly
        "\u201c": '"',  # left double curly
        "\u201d": '"',  # right double curly
        "\u201e": '"',  # double low-9
        "\u201f": '"',  # double high-reversed-9
        # Dashes
        "\u2010": "-",  # hyphen
        "\u2011": "-",  # non-breaking hyphen
        "\u2012": "-",  # figure dash
        "\u2013": "-",  # en dash
        "\u2014": "-",  # em dash
        "\u2015": "-",  # horizontal bar
        "\u2212": "-",  # minus sign
        # Special spaces
        "\u00a0": " ",  # non-breaking space
        "\u2002": " ",  # en space
        "\u2003": " ",  # em space
        "\u2004": " ",  # three-per-em space
        "\u2005": " ",  # four-per-em space
        "\u2006": " ",  # six-per-em space
        "\u2007": " ",  # figure space
        "\u2008": " ",  # punctuation space
        "\u2009": " ",  # thin space
        "\u200a": " ",  # hair space
        "\u202f": " ",  # narrow no-break space
        "\u205f": " ",  # medium mathematical space
        "\u3000": " ",  # ideographic space
        # BOM
        "\ufeff": "",
    },
)


def _normalize_for_fuzzy(text: str) -> str:
    """Normalize text for fuzzy matching.

    - Strip trailing whitespace per line
    - Normalize Unicode quotes/dashes/spaces to ASCII
    - Normalize NFC
    """
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_NORMALIZE_MAP)
    normalized_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        body, line_ending = _split_line_body_ending(line)
        normalized_lines.append(body.rstrip() + line_ending)
    return "".join(normalized_lines)


@dataclass
class MatchResult:
    """Result of a fuzzy find operation."""

    start: int
    end: int
    matched_text: str
    was_fuzzy: bool


@dataclass
class _NormalizedLineMap:
    """Per-line normalized-to-original offset mapping for fuzzy matches."""

    norm_to_orig: list[int]
    norm_len: int


def _split_line_body_ending(line: str) -> tuple[str, str]:
    """Split a line into body and line ending, preserving CRLF."""
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith(("\n", "\r")):
        return line[:-1], line[-1]
    return line, ""


def _normalize_prefix_map(text: str) -> tuple[str, list[int]]:
    """Return normalized text and a map from normalized offsets to original offsets.

    Fast path is O(n): when input is already NFC (common for source files), translation
    is applied per character and offsets are accumulated linearly.
    Slow path keeps the exact prefix-based behavior for non-NFC inputs.
    """
    if unicodedata.is_normalized("NFC", text):
        return _normalize_prefix_map_linear(text)
    return _normalize_prefix_map_slow(text)


def _normalize_prefix_map_linear(text: str) -> tuple[str, list[int]]:
    """Build normalized text and offset map in O(n) for NFC input."""
    normalized_parts: list[str] = []
    offset_map = [0]

    for i, ch in enumerate(text, 1):
        mapped = ch.translate(_NORMALIZE_MAP)
        if not mapped:
            continue
        normalized_parts.append(mapped)
        offset_map.extend([i] * len(mapped))

    return "".join(normalized_parts), offset_map


def _normalize_prefix_map_slow(text: str) -> tuple[str, list[int]]:
    """Exact prefix-based mapping for non-NFC input."""
    normalized = unicodedata.normalize("NFC", text).translate(_NORMALIZE_MAP)
    offset_map = [0] * (len(normalized) + 1)
    prev_len = 0

    for i in range(1, len(text) + 1):
        current_len = len(unicodedata.normalize("NFC", text[:i]).translate(_NORMALIZE_MAP))
        current_len = min(current_len, len(normalized))
        prev_len = min(prev_len, current_len)
        for offset in range(prev_len + 1, current_len + 1):
            offset_map[offset] = i
        offset_map[current_len] = i
        prev_len = current_len

    for offset in range(1, len(offset_map)):
        offset_map[offset] = max(offset_map[offset], offset_map[offset - 1])

    return normalized, offset_map


def _build_normalized_line_maps(orig_lines: list[str]) -> list[_NormalizedLineMap]:
    """Build per-line maps from normalized offsets to original offsets."""
    line_maps: list[_NormalizedLineMap] = []
    for line in orig_lines:
        body, line_ending = _split_line_body_ending(line)
        normalized_body_full, body_offset_map = _normalize_prefix_map(body)
        normalized_body = normalized_body_full.rstrip()
        normalized_body_len = len(normalized_body)

        norm_to_orig = body_offset_map[: normalized_body_len + 1]
        # Consume stripped trailing whitespace in fuzzy replacements.
        norm_to_orig[normalized_body_len] = len(body)
        for i in range(1, len(line_ending) + 1):
            norm_to_orig.append(len(body) + i)

        line_maps.append(
            _NormalizedLineMap(
                norm_to_orig=norm_to_orig,
                norm_len=normalized_body_len + len(line_ending),
            ),
        )
    return line_maps


def _find_all_matches(content: str, old_text: str) -> list[MatchResult]:
    """Find all non-overlapping occurrences, trying exact match first then fuzzy.

    Unified function: both counting and locating use the same code path,
    preventing disagreements between separate count and find operations.
    """
    if not old_text:
        return []

    # Try exact matches first
    matches: list[MatchResult] = []
    pos = 0
    while True:
        idx = content.find(old_text, pos)
        if idx == -1:
            break
        matches.append(MatchResult(start=idx, end=idx + len(old_text), matched_text=old_text, was_fuzzy=False))
        pos = idx + len(old_text)
    if matches:
        return matches

    # Fuzzy: normalize both sides and find in normalized space
    norm_content = _normalize_for_fuzzy(content)
    norm_old = _normalize_for_fuzzy(old_text)
    if not norm_old or not norm_old.strip():
        return []

    orig_lines = content.splitlines(keepends=True)
    norm_lines = norm_content.splitlines(keepends=True)
    orig_offsets = _cumulative_offsets(orig_lines)
    norm_offsets = _cumulative_offsets(norm_lines)
    line_maps = _build_normalized_line_maps(orig_lines)

    pos = 0
    while True:
        idx = norm_content.find(norm_old, pos)
        if idx == -1:
            break
        norm_end = idx + len(norm_old)
        orig_start = _norm_to_orig_offset(idx, norm_offsets, orig_offsets, line_maps, len(content))
        orig_end = _norm_to_orig_offset(norm_end, norm_offsets, orig_offsets, line_maps, len(content))
        matches.append(
            MatchResult(
                start=orig_start,
                end=orig_end,
                matched_text=content[orig_start:orig_end],
                was_fuzzy=True,
            ),
        )
        pos = norm_end

    return matches


def _cumulative_offsets(lines: list[str]) -> list[int]:
    """Return cumulative character offsets for line boundaries."""
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def _norm_to_orig_offset(
    norm_offset: int,
    norm_offsets: list[int],
    orig_offsets: list[int],
    line_maps: list[_NormalizedLineMap],
    content_len: int,
) -> int:
    """Map a character offset in normalized text to the original text.

    Uses line boundaries and per-line maps that account for NFC normalization,
    quote/dash translation, BOM removal, and trailing-whitespace stripping.
    """
    if not line_maps:
        return 0

    line_idx = bisect.bisect_right(norm_offsets, norm_offset) - 1
    line_idx = max(0, min(line_idx, len(line_maps) - 1))

    char_in_norm_line = norm_offset - norm_offsets[line_idx]
    line_map = line_maps[line_idx]
    char_in_norm_line = max(0, min(char_in_norm_line, line_map.norm_len))
    orig_in_line = line_map.norm_to_orig[char_in_norm_line]

    return min(orig_offsets[line_idx] + orig_in_line, content_len)


def _make_diff(
    old_content: str,
    new_content: str,
    context: int = DIFF_CONTEXT_LINES,
) -> str:
    """Create a unified diff between old and new content."""
    diff_lines = difflib.unified_diff(
        old_content.splitlines(),
        new_content.splitlines(),
        fromfile="before",
        tofile="after",
        lineterm="",
        n=context,
    )
    return "\n".join(diff_lines)


def _resolve_path(base_dir: Path, path: str) -> Path:
    """Resolve a path relative to base_dir, preventing traversal."""
    p = Path(path)
    if not p.is_absolute():
        p = base_dir / p
    resolved = p.resolve()
    base_resolved = base_dir.resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError:
        msg = f"Path '{path}' resolves outside the base directory."
        raise ValueError(msg) from None
    return resolved


def _is_git_repo(base_dir: Path) -> bool:
    """Check whether base_dir is inside a git working tree."""
    current = base_dir.resolve()
    return any((parent / ".git").exists() for parent in (current, *current.parents))


def _gitignored_paths(paths: list[Path], base_dir: Path) -> set[Path]:
    """Return the subset of paths ignored by git using a single batched call."""
    if not paths or not _is_git_repo(base_dir):
        return set()

    base_resolved = base_dir.resolve()
    path_map: dict[str, list[Path]] = {}
    for candidate in paths:
        try:
            token = str(candidate.resolve().relative_to(base_resolved))
        except ValueError:
            token = str(candidate)
        path_map.setdefault(token, []).append(candidate)

    payload = "\0".join(path_map.keys()) + "\0"
    try:
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            check=False,
            cwd=str(base_dir),
            input=payload.encode("utf-8"),
            capture_output=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return set()

    if result.returncode not in (0, 1):
        return set()

    ignored: set[Path] = set()
    for token in result.stdout.decode("utf-8", errors="replace").split("\0"):
        if not token:
            continue
        ignored.update(path_map.get(token, []))
    return ignored


def _format_read_output(content: str, offset: int | None, limit: int | None) -> str:
    """Format file content with line numbers and pagination hints."""
    all_lines = content.splitlines()
    total_lines = len(all_lines)

    if offset is not None and offset < 1:
        return "Error: offset must be >= 1."
    if limit is not None and limit < 1:
        return "Error: limit must be >= 1."

    start = offset or 1
    if total_lines == 0:
        if start > 1:
            return f"Error: offset {start} exceeds file length (0 lines)."
        return ""
    if start > total_lines:
        return f"Error: offset {start} exceeds file length ({total_lines} lines)."

    max_lines = limit if limit is not None else MAX_LINES
    end = min(start + max_lines - 1, total_lines)
    selected = all_lines[start - 1 : end]

    selected, end = _apply_byte_limit(selected, start, end)

    width = len(str(start + len(selected) - 1))
    numbered = "\n".join(f"{start + i:>{width}}| {line}" for i, line in enumerate(selected))
    return numbered + _pagination_hint(start, end, total_lines)


def _apply_byte_limit(selected: list[str], start: int, end: int) -> tuple[list[str], int]:
    """Re-truncate selected lines if they exceed MAX_BYTES."""
    selected_text = "\n".join(selected)
    if len(selected_text.encode("utf-8", errors="replace")) <= MAX_BYTES:
        return selected, end

    trunc = _truncate_head(selected_text, max_lines=len(selected), max_bytes=MAX_BYTES)
    selected = trunc.content.splitlines()
    if not selected and selected_text:
        partial = _truncate_to_max_bytes(selected_text, MAX_BYTES).rstrip("\n")
        selected = [f"{partial} [truncated]"]
    return selected, start + len(selected) - 1


def _pagination_hint(start: int, end: int, total: int) -> str:
    """Build a pagination hint suffix for truncated file reads."""
    if end < total:
        return f"\n\n[Showing lines {start}-{end} of {total}. Use offset={end + 1} to continue.]"
    if start > 1:
        return f"\n\n[Showing lines {start}-{end} of {total}.]"
    return ""


def _list_directory(target: Path, limit: int) -> str:
    """List directory contents with directory indicators."""
    entries: list[str] = []
    try:
        for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
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


def _find_files_in(search_path: Path, base_dir: Path, pattern: str, limit: int) -> str:
    """Glob for files, filter gitignored, and format results."""
    glob_error = _validate_glob_pattern(pattern)
    if glob_error:
        return glob_error

    try:
        candidates = [p for p in sorted(search_path.glob(pattern)) if p.is_file()]
    except (NotImplementedError, ValueError) as e:
        return f"Error: Invalid glob pattern '{pattern}': {e}"

    filtered = _filter_hidden_and_ignored(candidates, base_dir)
    matches: list[str] = []
    for candidate in filtered:
        try:
            rel = candidate.relative_to(base_dir)
        except ValueError:
            rel = candidate
        matches.append(str(rel))
        if len(matches) >= limit:
            break

    if not matches:
        return f"No files matching '{pattern}' found."

    result = "\n".join(matches)
    if len(matches) >= limit:
        result += f"\n\n[Results limited to {limit}. Narrow the pattern to see more.]"
    return result


def _resolve_and_read(base_dir: Path, path: str) -> tuple[Path, str] | str:
    """Resolve path and read file content. Returns (resolved, content) or error string."""
    try:
        resolved = _resolve_path(base_dir, path)
    except ValueError as e:
        return f"Error: {e}"

    if not resolved.exists():
        return f"Error: File not found: {path}"
    if not resolved.is_file():
        return f"Error: Not a file: {path}"

    try:
        return resolved, resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"Error reading file: {e}"


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
            limit: Maximum number of lines to return. Defaults to 2000.

        Returns:
            Line-numbered file content with pagination hints if truncated.

        """
        result = _resolve_and_read(self.base_dir, path)
        if isinstance(result, str):
            return result
        return _format_read_output(result[1], offset, limit)

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
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
        if not old_text:
            return "Error: old_text must be non-empty."

        result = _resolve_and_read(self.base_dir, path)
        if isinstance(result, str):
            return result
        resolved, content = result

        matches = _find_all_matches(content, old_text)
        if len(matches) == 0:
            return "Error: old_text not found in file."
        if len(matches) > 1:
            return f"Error: old_text matches {len(matches)} locations. Provide more context to make the match unique."

        match = matches[0]
        new_content = content[: match.start] + new_text + content[match.end :]

        try:
            resolved.write_text(new_content, encoding="utf-8")
        except OSError as e:
            return f"Error writing file: {e}"

        diff = _make_diff(content, new_content)
        fuzzy_note = " (fuzzy match: whitespace/Unicode normalized)" if match.was_fuzzy else ""
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
        literal: bool = False,
        context: int = 0,
        limit: int = DEFAULT_GREP_LIMIT,
    ) -> str:
        """Search file contents for a pattern. Uses ripgrep if available, falls back to Python re.

        Args:
            pattern: Regex pattern (or literal string if literal=True) to search for.
            path: Directory or file to search in. Defaults to working directory.
            glob: File glob pattern to filter (e.g., "*.py", "*.ts").
            ignore_case: Whether to ignore case in matching.
            literal: Treat pattern as a literal string instead of regex.
            context: Number of context lines before and after each match.
            limit: Maximum number of matches to return. Defaults to 100.

        Returns:
            Matching lines with file paths and line numbers.
            During recursive search, hidden files/directories and gitignored
            files are automatically excluded. Explicit path targets are not
            filtered.

        """
        try:
            search_path = _resolve_path(self.base_dir, path) if path else self.base_dir
        except ValueError as e:
            return f"Error: {e}"

        validation_error = _validate_grep_request(search_path, path, glob, limit, context)
        if validation_error:
            return validation_error

        # Try ripgrep first
        rg_result = _run_ripgrep(pattern, search_path, self.base_dir, glob, ignore_case, literal, context, limit)
        if rg_result is None:
            # Python fallback
            return _python_grep_fallback(
                pattern,
                search_path,
                self.base_dir,
                glob,
                ignore_case,
                literal,
                context,
                limit,
            )
        return rg_result

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
            Hidden files/directories and gitignored files are automatically
            excluded from results.

        """
        try:
            search_path = _resolve_path(self.base_dir, path) if path else self.base_dir
        except ValueError as e:
            return f"Error: {e}"

        if not search_path.exists():
            return f"Error: Path not found: {path or '.'}"
        if limit < 1:
            return "Error: limit must be >= 1."

        return _find_files_in(search_path, self.base_dir, pattern, limit)

    def ls(self, path: str | None = None, limit: int = DEFAULT_LS_LIMIT) -> str:
        """List directory contents with directory indicators.

        Args:
            path: Directory to list. Defaults to working directory.
            limit: Maximum number of entries. Defaults to 500.

        Returns:
            Sorted directory listing with '/' suffix on directories.
            Dotfiles are intentionally included.

        """
        try:
            target = _resolve_path(self.base_dir, path) if path else self.base_dir
        except ValueError as e:
            return f"Error: {e}"

        if not target.exists():
            return f"Error: Path not found: {path or '.'}"
        if not target.is_dir():
            return f"Error: Not a directory: {path}"
        if limit < 1:
            return "Error: limit must be >= 1."

        return _list_directory(target, limit)


# ── Grep helpers ────────────────────────────────────────────────────


def _truncate_line(line: str, max_chars: int = MAX_LINE_CHARS) -> str:
    """Truncate a single line if it exceeds max_chars."""
    if len(line) <= max_chars:
        return line
    return line[:max_chars] + " [truncated]"


@dataclass
class _RgEvent:
    """Parsed ripgrep JSON event."""

    event_type: str
    path_text: str
    line_text: str
    line_number: int


def _parse_rg_event(raw: str) -> _RgEvent | None:
    """Parse a single ripgrep JSON event line. Returns None for non-match/context events."""
    try:
        event = json.loads(raw)
    except json.JSONDecodeError:
        return None

    event_type = event.get("type")
    if event_type not in {"match", "context"}:
        return None

    data = event.get("data")
    if not isinstance(data, dict):
        return None
    path_data = data.get("path")
    lines_data = data.get("lines")
    line_number = data.get("line_number")
    if not isinstance(path_data, dict) or not isinstance(lines_data, dict) or not isinstance(line_number, int):
        return None
    path_text = path_data.get("text")
    line_text = lines_data.get("text")
    if not isinstance(path_text, str) or not isinstance(line_text, str):
        return None

    return _RgEvent(event_type=event_type, path_text=path_text, line_text=line_text, line_number=line_number)


def _run_ripgrep(
    pattern: str,
    search_path: Path,
    base_dir: Path,
    glob_filter: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
) -> str | None:
    """Run ripgrep and return formatted results. Returns None if rg is not available."""
    rg_binary = shutil.which("rg")
    if rg_binary is None:
        return None

    args = [rg_binary, "--json", "--color=never"]
    if ignore_case:
        args.append("-i")
    if literal:
        args.append("-F")
    if context > 0:
        args.extend(["-C", str(context)])
    if glob_filter:
        args.extend(["--glob", glob_filter])
    args.extend(["--", pattern, str(search_path)])

    try:
        result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return "Error: grep timed out after 30 seconds."

    if result.returncode == 1:
        return "No matches found."
    if result.returncode not in (0, 1):
        stderr = result.stderr.strip()
        return f"Error running grep: {stderr}" if stderr else "Error running grep."

    return _format_rg_output(result.stdout, limit, context, base_dir)


def _relativize_path(path_text: str, base_dir: Path) -> str:
    """Make an absolute path relative to base_dir for consistent output."""
    try:
        return str(Path(path_text).relative_to(base_dir))
    except ValueError:
        return path_text


def _format_rg_line(event: _RgEvent, base_dir: Path) -> str:
    """Format a ripgrep event as one output line."""
    marker = ":" if event.event_type == "match" else "-"
    rel_path = _relativize_path(event.path_text, base_dir)
    return f"{rel_path}{marker}{event.line_number}{marker}{_truncate_line(event.line_text.rstrip())}"


def _append_trailing_context(
    output_lines: list[str],
    pending_context: list[_RgEvent],
    last_match: _RgEvent | None,
    context: int,
    base_dir: Path,
) -> None:
    """Append only after-context for the last accepted match."""
    if context <= 0 or last_match is None:
        pending_context.clear()
        return

    for event in pending_context:
        if event.path_text != last_match.path_text:
            continue
        if last_match.line_number < event.line_number <= last_match.line_number + context:
            output_lines.append(_format_rg_line(event, base_dir))
    pending_context.clear()


def _format_rg_output(stdout: str, limit: int, context: int, base_dir: Path) -> str:
    """Parse ripgrep JSON output and format as text lines."""
    output_lines: list[str] = []
    pending_context: list[_RgEvent] = []
    match_count = 0
    was_limited = False
    last_match: _RgEvent | None = None

    for raw_line in stdout.splitlines():
        event = _parse_rg_event(raw_line)
        if event is None:
            continue

        if event.event_type == "context":
            pending_context.append(event)
            continue

        if match_count >= limit:
            was_limited = True
            _append_trailing_context(output_lines, pending_context, last_match, context, base_dir)
            break

        if pending_context:
            output_lines.extend(_format_rg_line(context_event, base_dir) for context_event in pending_context)
            pending_context.clear()

        match_count += 1
        last_match = event
        output_lines.append(_format_rg_line(event, base_dir))

    if not was_limited:
        _append_trailing_context(output_lines, pending_context, last_match, context, base_dir)

    if not output_lines:
        return "No matches found."

    output = "\n".join(output_lines)
    if was_limited:
        output += f"\n\n[Results limited to {limit} matches.]"

    trunc = _truncate_head(output)
    if trunc.was_truncated:
        return trunc.content.rstrip() + f"\n\n[Output truncated. {trunc.total_lines} total lines.]"
    return output.rstrip()


def _validate_glob_pattern(glob_filter: str | None) -> str | None:
    """Validate optional glob filter and return an error string when invalid."""
    if glob_filter is None:
        return None
    if Path(glob_filter).is_absolute():
        return f"Error: Invalid glob pattern '{glob_filter}': absolute paths are not allowed."
    return None


def _validate_grep_request(
    search_path: Path,
    path: str | None,
    glob_filter: str | None,
    limit: int,
    context: int,
) -> str | None:
    """Validate grep request arguments and return an error string when invalid."""
    if not search_path.exists():
        return f"Error: Path not found: {path or '.'}"
    if limit < 1:
        return "Error: limit must be >= 1."
    if context < 0:
        return "Error: context must be >= 0."
    return _validate_glob_pattern(glob_filter)


def _grep_file(
    filepath: Path,
    base_dir: Path,
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
        rel = filepath.relative_to(base_dir)
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
                results.append(f"{rel}{marker}{j + 1}{marker}{_truncate_line(lines[j])}")
        else:
            results.append(f"{rel}:{i + 1}:{_truncate_line(line)}")
    return match_count


def _filter_hidden_and_ignored(files: list[Path], search_path: Path) -> list[Path]:
    """Filter out hidden files (dotfiles) and gitignored files."""
    search_root = search_path.resolve()
    visible: list[Path] = []
    for filepath in files:
        try:
            resolved = filepath.resolve()
        except OSError:
            continue
        try:
            resolved.relative_to(search_root)
        except ValueError:
            # Ignore files reached through symlinks that escape the search root.
            continue
        if not filepath.is_file():
            continue
        try:
            rel = filepath.relative_to(search_path)
        except ValueError:
            rel = resolved
        if any(part.startswith(".") for part in rel.parts):
            continue
        visible.append(filepath)

    ignored = _gitignored_paths(visible, search_path)
    return [f for f in visible if f not in ignored]


def _collect_grep_files(search_path: Path, glob_filter: str | None) -> list[Path] | str:
    """Collect and filter files for grep. Returns file list or error string."""
    if search_path.is_file():
        return [search_path]

    if glob_filter:
        patterns = [glob_filter]
        if "/" not in glob_filter and "\\" not in glob_filter and not glob_filter.startswith("**/"):
            # pathlib's "*.ext" only matches the top level, while ripgrep's --glob
            # applies recursively; include both patterns for parity in fallback mode.
            patterns.append(f"**/{glob_filter}")
    else:
        patterns = ["**/*"]

    files: list[Path] = []
    seen: set[Path] = set()
    for glob_pat in patterns:
        try:
            glob_matches = sorted(search_path.glob(glob_pat))
        except (NotImplementedError, ValueError) as e:
            return f"Error: Invalid glob pattern '{glob_pat}': {e}"
        for match in glob_matches:
            if match not in seen:
                seen.add(match)
                files.append(match)

    return _filter_hidden_and_ignored(files, search_path)


def _python_grep_fallback(
    pattern: str,
    search_path: Path,
    base_dir: Path,
    glob_filter: str | None,
    ignore_case: bool,
    literal: bool,
    context: int,
    limit: int,
) -> str:
    """Pure Python grep fallback when ripgrep is not available."""
    if literal:
        pattern = re.escape(pattern)
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"

    files_or_error = _collect_grep_files(search_path, glob_filter)
    if isinstance(files_or_error, str):
        return files_or_error

    results: list[str] = []
    match_count = 0
    for filepath in files_or_error:
        match_count = _grep_file(filepath, base_dir, regex, context, limit, results, match_count)
        if match_count >= limit:
            break

    if not results:
        return "No matches found."

    output = "\n".join(results)
    if match_count >= limit:
        output += f"\n\n[Results limited to {limit} matches.]"
    trunc = _truncate_head(output)
    if trunc.was_truncated:
        return trunc.content + f"\n\n[Output truncated. {trunc.total_lines} total lines.]"
    return output.rstrip()
