"""Ergonomic coding tools for LLM agents.

Provides file read/write/edit, grep, find, and ls operations with
smart truncation, fuzzy matching, and actionable pagination hints.

Unlike the generic ``file`` tool (agno's FileTools), this toolkit is
optimised for coding agents: line-numbered reads with pagination,
search-and-replace edits (several per call) with fuzzy matching, ripgrep-backed grep,
and gitignore-aware file discovery. Prefer this tool over ``file`` for
coding-heavy agents; keep ``file`` for backward compatibility.
"""

from __future__ import annotations

import bisect
import difflib
import fcntl
import itertools
import json
import os
import re
import shutil
import subprocess
import unicodedata
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from glob import has_magic
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Optional

from pydantic import BaseModel, ConfigDict, Field, SkipValidation, TypeAdapter, ValidationError

from mindroom.git_invocation import hardened_git_command, hardened_git_env
from mindroom.path_confinement import is_git_metadata_path
from mindroom.path_safety import (
    blocked_git_metadata_message,
    format_path_for_output,
    is_within_base_dir,
    read_resolved_file,
    resolve_base_dir_path,
    resolve_tool_base_dir,
    spelled_output_path,
    split_search_pattern,
    write_resolved_file,
)
from mindroom.shared_directories.tool_roots import no_shared_directory_roots, shared_directory_aware
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from mindroom.config.models import FileAccess
    from mindroom.shared_directories.tool_roots import SharedDirectoryRoot

_MAX_LINES = 2000
_MAX_BYTES = 50 * 1024  # 50KB
_MAX_LINE_CHARS = 500  # Per-line truncation for grep output
_DEFAULT_GREP_LIMIT = 100
_DEFAULT_FIND_LIMIT = 1000
_DEFAULT_LS_LIMIT = 500
_MAX_HINT_CHARS = 200
_MAX_LISTED_RANGES = 10
_EDITS_SHAPE = 'Pass edits=[{"old_text": "...", "new_text": "..."}].'
_NO_CHANGES = "\nNo changes written."


@dataclass
class _TruncateResult:
    """Result of a truncation operation."""

    content: str
    was_truncated: bool
    total_lines: int


def _truncate_head(
    content: str,
    max_lines: int = _MAX_LINES,
    max_bytes: int = _MAX_BYTES,
) -> _TruncateResult:
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
    return _TruncateResult(
        content=result,
        was_truncated=len(kept) < total,
        total_lines=total,
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
class _MatchResult:
    """Result of a fuzzy find operation."""

    start: int
    end: int
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


def _find_all_matches(content: str, old_text: str) -> list[_MatchResult]:
    """Find all non-overlapping occurrences, trying exact match first then fuzzy.

    Unified function: both counting and locating use the same code path,
    preventing disagreements between separate count and find operations.
    """
    if not old_text:
        return []

    # Try exact matches first
    matches: list[_MatchResult] = []
    pos = 0
    while True:
        idx = content.find(old_text, pos)
        if idx == -1:
            break
        matches.append(_MatchResult(start=idx, end=idx + len(old_text), was_fuzzy=False))
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
            _MatchResult(
                start=orig_start,
                end=orig_end,
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
    if char_in_norm_line <= 0:
        # A line boundary maps to itself, never past the stripped whitespace of the line it starts.
        return orig_offsets[line_idx]
    line_map = line_maps[line_idx]
    char_in_norm_line = max(0, min(char_in_norm_line, line_map.norm_len))
    orig_in_line = line_map.norm_to_orig[char_in_norm_line]

    return min(orig_offsets[line_idx] + orig_in_line, content_len)


def _model_facing_schema(schema: dict[str, Any]) -> None:
    """Drop the titles and class docstring pydantic adds, which the model does not need."""
    schema.pop("description", None)
    for node in (schema, *schema["properties"].values()):
        node.pop("title", None)


class _Edit(BaseModel):
    """One replacement of edit_file."""

    model_config = ConfigDict(extra="forbid", json_schema_extra=_model_facing_schema)

    old_text: str = Field(description="Text to replace, copied exactly from the file.")
    new_text: str = Field(description="Replacement text.")
    replace_all: bool = Field(default=False, description="Replace every match, not just one.")


# edit_file validates its edits itself, so a malformed call gets one short line rather than pydantic's report.
_Edits = Annotated[list[_Edit], SkipValidation]
_EDITS = TypeAdapter(list[_Edit])


@contextmanager
def _directory_lock(resolved: Path) -> Iterator[None]:
    """Hold an exclusive ``flock`` on the directory of ``resolved`` (its nearest existing ancestor).

    Each call opens its own descriptor, so calls in other threads and in other processes, such as each worker
    request's own, wait alike; the lock survives the file's atomic replacement and leaves no lock file behind.
    """
    directory = resolved.parent
    while not directory.is_dir():
        directory = directory.parent
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _line_ranges(text: str, spans: list[tuple[int, int]]) -> str:
    """Name the lines of ``text`` the ordered ``spans`` cover, as read_file numbers them, merged and capped."""
    lines = text.splitlines(keepends=True)
    offsets = _cumulative_offsets(lines)
    open_last_line = bool(lines) and lines[-1].splitlines() == [lines[-1]]

    def line_at(offset: int) -> int | None:
        line = bisect.bisect_right(offsets, offset)
        return line if line < len(offsets) else (len(lines) if open_last_line else None)

    merged: list[list[int | None]] = []
    for start, end in spans:
        first, last = line_at(start), line_at(max(start, end - 1))
        previous = merged[-1][1] if merged else None
        if first is not None and previous is not None and first <= previous + 1:
            merged[-1][1] = max(previous, last or first)
        else:
            merged.append([first, last])
    words = [
        "end of file" if first is None else str(first) if first == last else f"{first}-{last}"
        for first, last in merged[:_MAX_LISTED_RANGES]
    ]
    if len(merged) > _MAX_LISTED_RANGES:
        words.append(f"and {len(merged) - _MAX_LISTED_RANGES} more")
    label = "" if words == ["end of file"] else "line " if len(words) == 1 and words[0].isdigit() else "lines "
    return label + ", ".join(words)


def _closest_line_hint(content: str, old_text: str) -> str:
    """Name the file's line most like old_text's first non-blank line, or return nothing."""
    first = next((line.strip() for line in old_text.splitlines() if line.strip()), "")
    lines = content.splitlines()
    stripped = [line.strip() for line in lines]
    close = difflib.get_close_matches(first, stripped, n=1, cutoff=0.6) if first else []
    if not close:
        return ""
    index = stripped.index(close[0])
    return f"\nClosest line {index + 1}: {lines[index].rstrip()[:_MAX_HINT_CHARS]}"


def _edits_argument(edits: object, legacy: dict[str, object]) -> list[_Edit] | str:
    """Return the edits a call asked for, a call in the old flat shape as one edit, or why not."""
    if legacy:
        # Calls written for the old flat shape send one old_text and new_text, which the schema no longer shows.
        if edits is not None or set(legacy) != {"old_text", "new_text"}:
            return f"edit_file takes path and edits, not {', '.join(sorted(legacy))}. {_EDITS_SHAPE}"
        edits = [legacy]
    if isinstance(edits, str):
        with suppress(ValueError):
            edits = json.loads(edits)
    if not edits:
        return f"edits is empty. {_EDITS_SHAPE}"
    try:
        return _EDITS.validate_python(edits)
    except ValidationError as e:
        error = e.errors()[0]
        where = "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in error["loc"])
        return f"edits{where}: {error['msg']}. {_EDITS_SHAPE}"


def _splits_crlf(content: str, offset: int) -> bool:
    return 0 < offset < len(content) and content[offset - 1 : offset + 1] == "\r\n"


def _matched_spans(content: str, edits: list[_Edit]) -> list[tuple[_MatchResult, int]] | str:
    """Match every edit against ``content`` and return its spans in file order, each with its edit's index, or why not.

    A match that would start or end between the CR and LF of a line ending is not taken.
    """
    many = len(edits) > 1
    spans: list[tuple[_MatchResult, int]] = []
    for index, edit in enumerate(edits):
        who = f"edits[{index}]" if many else "old_text"
        if not edit.old_text:
            return f"{f'edits[{index}].' if many else ''}old_text is empty."
        matches = [
            match
            for match in _find_all_matches(content, edit.old_text)
            if not _splits_crlf(content, match.start) and not _splits_crlf(content, match.end)
        ]
        if not matches:
            return f"{who} not found in the file.{_closest_line_hint(content, edit.old_text)}"
        if len(matches) > 1 and not edit.replace_all:
            lines = _line_ranges(content, [(match.start, match.end) for match in matches])
            return (
                f"{who} matches {len(matches)} places ({lines}). "
                "Add nearby lines to old_text to pick one, or set replace_all."
            )
        spans.extend((match, index) for match in matches)
    spans.sort(key=lambda span: span[0].start)
    for (first, first_index), (second, second_index) in itertools.pairwise(spans):
        if second.start < first.end:
            low, high = sorted((first_index, second_index))
            return f"edits[{low}] and edits[{high}] overlap. Merge them into one edit."
    return spans


def _apply_edits(content: str, edits: list[_Edit]) -> tuple[str, int, str] | str:
    """Apply every edit to ``content`` as it is, returning the new content, its replacements and their lines, or why not.

    A byte order mark is kept out of matching and always kept. A file whose every line ends in CRLF is edited with
    LF endings, which old_text and new_text may use either way, and gets its CRLF back; a file with mixed endings is
    edited as it is.
    """
    bom = "\ufeff" if content.startswith("\ufeff") else ""
    text = content.removeprefix(bom)
    crlf = text.count("\r\n") > 0 and text.count("\r") == text.count("\n") == text.count("\r\n")
    if crlf:
        text = text.replace("\r\n", "\n")
        edits = [
            edit.model_copy(
                update={
                    "old_text": edit.old_text.replace("\r\n", "\n"),
                    "new_text": edit.new_text.replace("\r\n", "\n"),
                },
            )
            for edit in edits
        ]
    spans = _matched_spans(text, edits)
    if isinstance(spans, str):
        return spans
    parts: list[str] = []
    landed: list[tuple[int, int]] = []
    position = shift = 0
    for match, index in spans:
        new_text = edits[index].new_text
        parts += [text[position : match.start], new_text]
        landed.append((match.start + shift, match.start + shift + len(new_text)))
        shift += len(new_text) - (match.end - match.start)
        position = match.end
    parts.append(text[position:])
    edited = "".join(parts)
    new_content = bom + (edited.replace("\n", "\r\n") if crlf else edited)
    if new_content == content:
        return "the edits leave the file identical to what it was; new_text must differ from the text it replaces."

    report = _line_ranges(edited, landed)
    fuzzy = [f"edits[{index}]" for index in sorted({index for match, index in spans if match.was_fuzzy})]
    if fuzzy:
        report += f" ({', '.join(fuzzy) + ' ' if len(edits) > 1 else ''}matched after normalizing whitespace/quotes)"
    return new_content, len(spans), report


def _edit_resolved(base_dir: Path, resolved: Path, path: str, edits: list[_Edit]) -> str:
    """Apply ``edits`` to the file at ``resolved`` under its directory's lock, and return the result or the error."""
    with _directory_lock(resolved):
        raw = _read_resolved(base_dir, resolved, path)
        if isinstance(raw, str):
            return raw + _NO_CHANGES
        applied = _apply_edits(raw.decode("utf-8", errors="replace"), edits)
        if isinstance(applied, str):
            return f"Error: {applied}{_NO_CHANGES}"
        new_content, replacements, report = applied
        try:
            write_resolved_file(base_dir, resolved, new_content.encode("utf-8"))
        except OSError as e:
            return f"Error writing file: {e}"
    counted = f" ({replacements} replacements)" if replacements > len(edits) else ""
    return f"Applied {len(edits)} edit{'s' if len(edits) > 1 else ''}{counted} to {path}: {report}."


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
            hardened_git_command(["check-ignore", "--stdin", "-z"]),
            check=False,
            cwd=str(base_dir),
            env=hardened_git_env(),
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

    max_lines = limit if limit is not None else _MAX_LINES
    end = min(start + max_lines - 1, total_lines)
    selected = all_lines[start - 1 : end]

    selected, end = _apply_byte_limit(selected, start, end)

    width = len(str(start + len(selected) - 1))
    numbered = "\n".join(f"{start + i:>{width}}| {line}" for i, line in enumerate(selected))
    return numbered + _pagination_hint(start, end, total_lines)


def _apply_byte_limit(selected: list[str], start: int, end: int) -> tuple[list[str], int]:
    """Re-truncate selected lines if they exceed the max byte limit."""
    selected_text = "\n".join(selected)
    if len(selected_text.encode("utf-8", errors="replace")) <= _MAX_BYTES:
        return selected, end

    trunc = _truncate_head(selected_text, max_lines=len(selected), max_bytes=_MAX_BYTES)
    selected = trunc.content.splitlines()
    if not selected and selected_text:
        partial = _truncate_to_max_bytes(selected_text, _MAX_BYTES).rstrip("\n")
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
            name = (spelled_output_path(item) or item.name) + ("/" if item.is_dir() else "")
            entries.append(name)
            if len(entries) > limit:
                break
    except OSError as e:
        return f"Error listing directory: {e}"

    if not entries:
        return "Directory is empty."

    was_limited = len(entries) > limit
    if was_limited:
        entries = entries[:limit]

    result = "\n".join(entries)
    if was_limited:
        result += f"\n\n[Listing limited to {limit} entries.]"
    return result


def _find_files_in(
    search_path: Path,
    base_dir: Path,
    pattern: str,
    limit: int,
    restrict_to_base_dir: bool = True,
) -> str:
    """Glob for files, filter gitignored, and format results."""
    glob_error = _validate_glob_pattern(pattern)
    if glob_error:
        return glob_error

    try:
        candidates = [p for p in sorted(search_path.glob(pattern)) if p.is_file()]
    except (NotImplementedError, ValueError) as e:
        return f"Error: Invalid glob pattern '{pattern}': {e}"

    filter_root = base_dir if restrict_to_base_dir else search_path
    # A dot directory named in `path` or in the pattern's literal prefix was asked for, so only dot paths below it hide.
    parts = Path(pattern).parts
    named = parts[: next((index for index, part in enumerate(parts) if has_magic(part)), len(parts))]
    filtered = _filter_hidden_and_ignored(candidates, filter_root, search_path.joinpath(*named))
    matches: list[str] = []
    for candidate in filtered:
        matches.append(format_path_for_output(candidate, base_dir))
        if len(matches) > limit:
            break

    if not matches:
        return f"No files matching '{pattern}' found."

    was_limited = len(matches) > limit
    if was_limited:
        matches = matches[:limit]

    result = "\n".join(matches)
    if was_limited:
        result += f"\n\n[Results limited to {limit}. Narrow the pattern to see more.]"
    return result


def _read_resolved(base_dir: Path, resolved: Path, path: str) -> bytes | str:
    """Read the resolved file ``path`` names, or return the error."""
    if not resolved.exists():
        return f"Error: File not found: {path}"
    if not resolved.is_file():
        return f"Error: Not a file: {path}"
    try:
        return read_resolved_file(base_dir, resolved)
    except (OSError, ValueError) as e:
        return f"Error reading file: {e}"


class CodingTools(Toolkit):
    """Ergonomic coding tools for LLM agents.

    Provides file read/write/edit, grep, find, and ls operations with
    smart truncation, fuzzy matching, and actionable pagination hints.
    """

    def __init__(
        self,
        base_dir: str | None = None,
        file_access: FileAccess = "workspace",
        shared_directory_roots: Callable[[], tuple[SharedDirectoryRoot, ...]] = no_shared_directory_roots,
    ) -> None:
        self.base_dir = resolve_tool_base_dir(base_dir)
        # Without a workspace the base dir is only the process working directory, which workspace access refuses.
        self.has_workspace = bool(base_dir)
        self.restrict_to_base_dir = file_access == "workspace"
        self.shared_directory_roots = shared_directory_roots
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

    @shared_directory_aware("path", writes=False)
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
        try:
            resolved = resolve_base_dir_path(self.base_dir, path, self.restrict_to_base_dir)
        except ValueError as e:
            return f"Error: {e}"
        raw = _read_resolved(self.base_dir, resolved, path)
        if isinstance(raw, str):
            return raw
        return _format_read_output(raw.decode("utf-8", errors="replace"), offset, limit)

    @shared_directory_aware("path", writes=True)
    def edit_file(
        self,
        path: str,
        edits: Optional[_Edits] = None,  # noqa: UP045 - Optional, unlike `| None`, keeps null out of the schema.
        **legacy: object,
    ) -> str:
        """Replace exact text in one file. Read it first.

        Copy each old_text from the current file; edits match the original, not each other's results.
        Without replace_all, old_text must match one place. Whitespace/quote differences are tolerated.
        Edits must not overlap; merge nearby ones. If one fails, nothing is written.

        Args:
            path: File path, relative to working directory or absolute.
            edits: Non-empty list of replacements.
            **legacy: The old flat shape's old_text and new_text, taken as one edit.

        Returns:
            The lines each edit changed, or an error.

        """
        parsed = _edits_argument(edits, legacy)
        if isinstance(parsed, str):
            return f"Error: {parsed}{_NO_CHANGES}"
        try:
            resolved = resolve_base_dir_path(self.base_dir, path, self.restrict_to_base_dir)
        except ValueError as e:
            return f"Error: {e}{_NO_CHANGES}"
        if is_git_metadata_path(resolved):
            return blocked_git_metadata_message("writing file", path)
        try:
            return _edit_resolved(self.base_dir, resolved, path, parsed)
        except OSError as e:
            return f"Error: {e}{_NO_CHANGES}"

    @shared_directory_aware("path", writes=True)
    def write_file(self, path: str, content: str) -> str:
        """Write content to a file, creating parent directories if needed.

        Args:
            path: File path (relative to working directory or absolute).
            content: The full file content to write.

        Returns:
            Confirmation with byte count written.

        """
        try:
            resolved = resolve_base_dir_path(self.base_dir, path, self.restrict_to_base_dir)
        except ValueError as e:
            return f"Error: {e}"
        if is_git_metadata_path(resolved):
            return blocked_git_metadata_message("writing file", path)

        try:
            with _directory_lock(resolved):
                write_resolved_file(self.base_dir, resolved, content.encode("utf-8"))
        except OSError as e:
            return f"Error writing file: {e}"

        byte_count = len(content.encode("utf-8"))
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"Wrote {byte_count} bytes ({lines} lines) to {path}"

    @shared_directory_aware("path", writes=False)
    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_case: bool = False,
        literal: bool = False,
        context: int = 0,
        limit: int = _DEFAULT_GREP_LIMIT,
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
            search_path = resolve_base_dir_path(self.base_dir, path or ".", self.restrict_to_base_dir)
        except ValueError as e:
            return f"Error: {e}"
        effective_glob = glob
        if not self.restrict_to_base_dir and glob is not None:
            normalized_path, normalized_glob = split_search_pattern(search_path, glob)
            if normalized_path.exists():
                search_path = normalized_path
                effective_glob = normalized_glob

        validation_error = _validate_grep_request(search_path, path, effective_glob, limit, context)
        if validation_error:
            return validation_error

        # Try ripgrep first
        rg_result = _run_ripgrep(
            pattern,
            search_path,
            self.base_dir,
            effective_glob,
            ignore_case,
            literal,
            context,
            limit,
        )
        if rg_result is None:
            # Python fallback
            return _python_grep_fallback(
                pattern,
                search_path,
                self.base_dir,
                effective_glob,
                ignore_case,
                literal,
                context,
                limit,
            )
        return rg_result

    @shared_directory_aware("path", writes=False)
    def find_files(
        self,
        pattern: str,
        path: str | None = None,
        limit: int = _DEFAULT_FIND_LIMIT,
    ) -> str:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g., "*.py", "**/*.ts", "src/**/*.jsx").
            path: Directory to search in. Defaults to working directory.
            limit: Maximum number of results. Defaults to 1000.

        Returns:
            List of matching file paths, one per line.
            Hidden files/directories below the searched directory and gitignored
            files are automatically excluded from results; a hidden directory
            named in path or in the pattern's leading directories is searched.

        """
        try:
            search_path = resolve_base_dir_path(self.base_dir, path or ".", self.restrict_to_base_dir)
        except ValueError as e:
            return f"Error: {e}"
        search_pattern = pattern
        if not self.restrict_to_base_dir:
            normalized_path, normalized_pattern = split_search_pattern(search_path, pattern)
            if normalized_path.exists():
                search_path = normalized_path
                search_pattern = normalized_pattern

        if not search_path.exists():
            return f"Error: Path not found: {path or '.'}"
        if limit < 1:
            return "Error: limit must be >= 1."

        return _find_files_in(
            search_path,
            self.base_dir,
            search_pattern,
            limit,
            restrict_to_base_dir=self.restrict_to_base_dir,
        )

    @shared_directory_aware("path", writes=False)
    def ls(self, path: str | None = None, limit: int = _DEFAULT_LS_LIMIT) -> str:
        """List directory contents with directory indicators.

        Args:
            path: Directory to list. Defaults to working directory.
            limit: Maximum number of entries. Defaults to 500.

        Returns:
            Sorted directory listing with '/' suffix on directories.
            Dotfiles are intentionally included.

        """
        try:
            target = resolve_base_dir_path(self.base_dir, path or ".", self.restrict_to_base_dir)
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


def _truncate_line(line: str, max_chars: int = _MAX_LINE_CHARS) -> str:
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


def _format_rg_line(event: _RgEvent, base_dir: Path) -> str:
    """Format a ripgrep event as one output line."""
    marker = ":" if event.event_type == "match" else "-"
    rel_path = format_path_for_output(event.path_text, base_dir)
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

    rel = format_path_for_output(filepath, base_dir)

    lines = text.splitlines()
    emitted_lines: set[int] = set()
    for i, line in enumerate(lines):
        if match_count > limit:
            break
        if not regex.search(line):
            continue
        if match_count >= limit:
            # Signal truncation without emitting output for the extra match.
            return match_count + 1
        match_count += 1
        if context > 0:
            start = max(0, i - context)
            end = min(len(lines), i + context + 1)
            for j in range(start, end):
                if j in emitted_lines:
                    continue
                emitted_lines.add(j)
                marker = ":" if j == i else "-"
                results.append(f"{rel}{marker}{j + 1}{marker}{_truncate_line(lines[j])}")
        else:
            results.append(f"{rel}:{i + 1}:{_truncate_line(line)}")
    return match_count


def _filter_hidden_and_ignored(files: list[Path], search_path: Path, visible_root: Path | None = None) -> list[Path]:
    """Filter out files outside ``search_path``, gitignored files, and dotfiles below ``visible_root``."""
    search_root = search_path.resolve()
    hidden_root = search_path if visible_root is None else visible_root
    visible: list[Path] = []
    for filepath in files:
        if not is_within_base_dir(filepath, search_root):
            continue
        if not filepath.is_file():
            continue
        try:
            rel = filepath.relative_to(hidden_root)
        except ValueError:
            rel = filepath.resolve()
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
            # applies recursively; include both so the fallback matches what ripgrep would.
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
        if match_count > limit:
            break

    if not results:
        return "No matches found."

    was_limited = match_count > limit
    output = "\n".join(results)
    if was_limited:
        output += f"\n\n[Results limited to {limit} matches.]"
    trunc = _truncate_head(output)
    if trunc.was_truncated:
        return trunc.content + f"\n\n[Output truncated. {trunc.total_lines} total lines.]"
    return output.rstrip()
