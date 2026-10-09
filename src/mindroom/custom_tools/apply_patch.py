"""Parse Codex ``apply_patch`` patches and compute the file contents they produce.

Ported from OpenAI Codex, https://github.com/openai/codex, ``codex-rs/apply-patch`` (``parser.rs``,
``file_update.rs``, ``seek_sequence.rs``, ``text_file.rs``) at commit 7f2f4fd46e0f50798fd8d3436a8e14a5386d253b.
Copyright 2025 OpenAI, licensed under the Apache License, Version 2.0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_BEGIN_PATCH = "*** Begin Patch"
_END_PATCH = "*** End Patch"
_ADD_FILE = "*** Add File: "
_DELETE_FILE = "*** Delete File: "
_UPDATE_FILE = "*** Update File: "
_MOVE_TO = "*** Move to: "
_END_OF_FILE = "*** End of File"
_CHANGE_CONTEXT = "@@ "
_EMPTY_CHANGE_CONTEXT = "@@"
_HEREDOC_STARTS = frozenset({"<<EOF", "<<'EOF'", '<<"EOF"'})
_SOURCE_LINE = re.compile(r"(?P<text>[^\r\n]*)(?P<ending>\r\n|\n|\r)|(?P<last>[^\r\n]+)\Z")
# Typographic dashes, quotes, and spaces that Codex's last matching pass reads as ASCII.
_PUNCTUATION = str.maketrans(
    {
        **dict.fromkeys(map(chr, (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212)), "-"),
        **dict.fromkeys(map(chr, (0x2018, 0x2019, 0x201A, 0x201B)), "'"),
        **dict.fromkeys(map(chr, (0x201C, 0x201D, 0x201E, 0x201F)), '"'),
        **dict.fromkeys(
            map(chr, (0x00A0, *range(0x2002, 0x200B), 0x202F, 0x205F, 0x3000)),
            " ",
        ),
    },
)


class PatchError(ValueError):
    """A patch that cannot be parsed or applied; the message is what the model sees."""


@dataclass(frozen=True)
class AddFile:
    """Create or overwrite a file with *contents*."""

    path: str
    contents: str


@dataclass(frozen=True)
class DeleteFile:
    """Delete an existing file."""

    path: str


@dataclass(frozen=True)
class _UpdateChunk:
    """Replace *old_lines* with *new_lines*, after the line *change_context* when given.

    *context_line_indices* pairs the old and new indices of lines parsed as context, which keep their
    original text and line ending.
    """

    change_context: str | None
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]
    context_line_indices: tuple[tuple[int, int], ...]
    is_end_of_file: bool


@dataclass(frozen=True)
class _UpdateFile:
    """Update an existing file in place, or at *move_to* when it is renamed."""

    path: str
    move_to: str | None
    chunks: tuple[_UpdateChunk, ...]


type PatchHunk = AddFile | DeleteFile | _UpdateFile


def _hunk_error(line_number: int, message: str) -> PatchError:
    return PatchError(f"invalid hunk at line {line_number}, {message}")


def _patch_body(lines: list[str]) -> list[str]:
    first = lines[0].strip() if lines else None
    last = lines[-1].strip() if lines else None
    if first == _BEGIN_PATCH and last == _END_PATCH and len(lines) >= 2:
        return lines[1:-1]
    if lines and lines[0] in _HEREDOC_STARTS and lines[-1].endswith("EOF") and len(lines) >= 4:
        return _patch_body(lines[1:-1])
    if first != _BEGIN_PATCH:
        msg = "invalid patch: The first line of the patch must be '*** Begin Patch'"
        raise PatchError(msg)
    msg = "invalid patch: The last line of the patch must be '*** End Patch'"
    raise PatchError(msg)


def _chunk_header(line: str, line_number: int, *, allow_missing_context: bool) -> tuple[str | None, int]:
    """Return a chunk's @@ context and how many header lines it has; trailing whitespace is ignored, as in Codex."""
    header = line.rstrip()
    if header == _EMPTY_CHANGE_CONTEXT:
        return None, 1
    if header.startswith(_CHANGE_CONTEXT):
        return header.removeprefix(_CHANGE_CONTEXT), 1
    if allow_missing_context:
        return None, 0
    raise _hunk_error(line_number, f"Expected update hunk to start with a @@ context marker, got: '{line}'")


def _parse_chunk(
    lines: list[str],
    line_number: int,
    *,
    allow_missing_context: bool,
) -> tuple[_UpdateChunk, int]:
    change_context, start = _chunk_header(lines[0], line_number, allow_missing_context=allow_missing_context)
    if start >= len(lines):
        raise _hunk_error(line_number + 1, "Update hunk does not contain any lines")
    old_lines: list[str] = []
    new_lines: list[str] = []
    context_line_indices: list[tuple[int, int]] = []
    is_end_of_file = False
    parsed = 0
    for line in lines[start:]:
        if line.rstrip() == _END_OF_FILE:
            if parsed == 0:
                raise _hunk_error(line_number + 1, "Update hunk does not contain any lines")
            is_end_of_file = True
            parsed += 1
            break
        marker, text = line[:1], line[1:]
        if marker in {"", " "}:
            context_line_indices.append((len(old_lines), len(new_lines)))
            old_lines.append(text)
            new_lines.append(text)
        elif marker == "+":
            new_lines.append(text)
        elif marker == "-":
            old_lines.append(text)
        elif parsed == 0:
            raise _hunk_error(
                line_number + 1,
                f"Unexpected line found in update hunk: '{line}'. Every line should start with ' ' (context line), "
                "'+' (added line), or '-' (removed line)",
            )
        else:
            break
        parsed += 1
    chunk = _UpdateChunk(
        change_context=change_context,
        old_lines=tuple(old_lines),
        new_lines=tuple(new_lines),
        context_line_indices=tuple(context_line_indices),
        is_end_of_file=is_end_of_file,
    )
    return chunk, parsed + start


def _parse_update(lines: list[str], line_number: int, path: str) -> tuple[_UpdateFile, int]:
    remaining = lines[1:]
    parsed = 1
    move_to = remaining[0].rstrip().removeprefix(_MOVE_TO) if remaining and remaining[0].startswith(_MOVE_TO) else None
    if move_to is not None:
        remaining = remaining[1:]
        parsed += 1
    chunks: list[_UpdateChunk] = []
    while remaining:
        # Like Codex, a blank line here is an empty context line, which the chunk parser reads, except after an
        # end-of-file hunk, where it is skipped.
        if chunks and chunks[-1].is_end_of_file and not remaining[0].strip():
            remaining = remaining[1:]
            parsed += 1
            continue
        if remaining[0].startswith("*"):
            break
        chunk, chunk_lines = _parse_chunk(remaining, line_number + parsed, allow_missing_context=not chunks)
        chunks.append(chunk)
        parsed += chunk_lines
        remaining = remaining[chunk_lines:]
    if not chunks:
        raise _hunk_error(line_number, f"Update file hunk for path '{path}' is empty")
    return _UpdateFile(path=path, move_to=move_to, chunks=tuple(chunks)), parsed


def _parse_hunk(lines: list[str], line_number: int) -> tuple[PatchHunk, int]:
    header = lines[0].strip()
    if header.startswith(_ADD_FILE):
        added = []
        for line in lines[1:]:
            if not line.startswith("+"):
                break
            added.append(line[1:])
        return AddFile(path=header.removeprefix(_ADD_FILE), contents="".join(f"{line}\n" for line in added)), len(
            added,
        ) + 1
    if header.startswith(_DELETE_FILE):
        return DeleteFile(path=header.removeprefix(_DELETE_FILE)), 1
    if header.startswith(_UPDATE_FILE):
        return _parse_update(lines, line_number, header.removeprefix(_UPDATE_FILE))
    raise _hunk_error(
        line_number,
        f"'{header}' is not a valid hunk header. Valid hunk headers: '*** Add File: {{path}}', "
        "'*** Delete File: {path}', '*** Update File: {path}'",
    )


def parse_patch(text: str) -> list[PatchHunk]:
    """Return the hunks of a patch, accepting Codex's lenient marker whitespace and heredoc wrapping."""
    # Like Rust's str::lines: split on LF and drop one CR before it, never on other separators.
    remaining = _patch_body([line.removesuffix("\r") for line in text.strip().split("\n")])
    hunks: list[PatchHunk] = []
    line_number = 2
    while remaining:
        hunk, parsed = _parse_hunk(remaining, line_number)
        hunks.append(hunk)
        line_number += parsed
        remaining = remaining[parsed:]
    return hunks


def _seek(lines: list[str], pattern: tuple[str, ...], start: int, *, eof: bool) -> int | None:
    """Return where *pattern* starts at or after *start*, trying exact, then progressively looser matches."""
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    search_start = max(len(lines) - len(pattern), start) if eof else start
    candidates = range(search_start, len(lines) - len(pattern) + 1)
    for normalize in (
        lambda line: line,
        str.rstrip,
        str.strip,
        lambda line: line.strip().translate(_PUNCTUATION),
    ):
        normalized_pattern = [normalize(line) for line in pattern]
        for index in candidates:
            if [normalize(line) for line in lines[index : index + len(pattern)]] == normalized_pattern:
                return index
    return None


def _split_lines(contents: str) -> tuple[list[str], list[str | None], str]:
    """Return line texts, each line's ending, and the first ending, which inserted lines reuse."""
    texts: list[str] = []
    endings: list[str | None] = []
    for match in _SOURCE_LINE.finditer(contents):
        last_line = match["last"]
        texts.append(match["text"] if last_line is None else last_line)
        endings.append(match["ending"])
    preferred = next((ending for ending in endings if ending is not None), "\n")
    return texts, endings, preferred


def _around_context(
    found: int,
    pattern: tuple[str, ...],
    new_slice: tuple[str, ...],
    context_line_indices: tuple[tuple[int, int], ...],
) -> list[tuple[int, int, tuple[str, ...]]]:
    """Return replacements for one matched chunk that leave its context lines in place.

    Context lines keep their exact text and line endings, which matters for files with mixed endings.
    """
    replacements: list[tuple[int, int, tuple[str, ...]]] = []
    old_start = new_start = 0
    for old_context, new_context in context_line_indices:
        if old_context >= len(pattern) or new_context >= len(new_slice):
            break
        if (old_start, new_start) != (old_context, new_context):
            replacements.append((found + old_start, old_context - old_start, new_slice[new_start:new_context]))
        old_start, new_start = old_context + 1, new_context + 1
    if (old_start, new_start) != (len(pattern), len(new_slice)):
        replacements.append((found + old_start, len(pattern) - old_start, new_slice[new_start:]))
    return replacements


def _chunk_replacements(
    lines: list[str],
    path: str,
    chunks: tuple[_UpdateChunk, ...],
) -> list[tuple[int, int, tuple[str, ...]]]:
    replacements: list[tuple[int, int, tuple[str, ...]]] = []
    line_index = 0
    for chunk in chunks:
        if chunk.change_context is not None:
            found = _seek(lines, (chunk.change_context,), line_index, eof=False)
            if found is None:
                msg = f"Failed to find context '{chunk.change_context}' in {path}"
                raise PatchError(msg)
            line_index = found + 1
        if not chunk.old_lines:
            replacements.append((len(lines), 0, chunk.new_lines))
            continue
        pattern, new_slice = chunk.old_lines, chunk.new_lines
        found = _seek(lines, pattern, line_index, eof=chunk.is_end_of_file)
        if found is None and pattern[-1] == "":
            # A trailing empty line stands for the file's final newline, which no line text holds.
            pattern = pattern[:-1]
            new_slice = new_slice[:-1] if new_slice and new_slice[-1] == "" else new_slice
            found = _seek(lines, pattern, line_index, eof=chunk.is_end_of_file)
        if found is None:
            msg = f"Failed to find expected lines in {path}:\n" + "\n".join(chunk.old_lines)
            raise PatchError(msg)
        replacements.extend(_around_context(found, pattern, new_slice, chunk.context_line_indices))
        line_index = found + len(pattern)
    return sorted(replacements, key=lambda replacement: replacement[0])


def updated_contents(contents: str, path: str, chunks: tuple[_UpdateChunk, ...]) -> str:
    """Return *contents* after applying *chunks*; unchanged lines keep their endings and every line ends in one."""
    texts, endings, preferred = _split_lines(contents)
    new_texts: list[str] = []
    new_endings: list[str | None] = []
    source_index = 0
    for start, old_length, new_lines in _chunk_replacements(texts, path, chunks):
        new_texts.extend(texts[source_index:start])
        new_endings.extend(endings[source_index:start])
        new_texts.extend(new_lines)
        new_endings.extend([preferred] * len(new_lines))
        source_index = start + old_length
    new_texts.extend(texts[source_index:])
    new_endings.extend(endings[source_index:])
    return "".join(
        text + (ending if ending is not None else preferred)
        for text, ending in zip(new_texts, new_endings, strict=True)
    )
