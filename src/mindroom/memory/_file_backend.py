"""File-backed memory implementation."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast
from zoneinfo import ZoneInfo

from mindroom.atomic_file import atomic_write_bytes_at, existing_file_mode
from mindroom.config.resolution import entity_timezone, memory_search
from mindroom.embeddings.errors import classified_embedder_error
from mindroom.logging_config import get_logger
from mindroom.memory_scope_ids import agent_scope_user_id
from mindroom.path_confinement import open_directory_within_root, open_regular_file_within_root
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.timing import timed
from mindroom.token_counting.counts import count

from ._policy import MemoryScope, team_scope, team_scopes, team_user_id
from ._prompting import redact_memory_text
from ._semantic_file_search import (
    SemanticFileMemoryIndexUnavailableError,
    schedule_semantic_file_memory_refresh,
    search_semantic_file_memories,
)
from ._shared import (
    FILE_MEMORY_DAILY_DIR,
    FILE_MEMORY_ENTRY_PATTERN,
    FILE_MEMORY_ENTRYPOINT,
    FILE_MEMORY_ENTRYPOINT_HEADER,
    FILE_MEMORY_PATH_ID_PATTERN,
    MemoryEntrypointContext,
    MemoryNotFoundError,
    MemoryResult,
    MemorySearchOutcome,
    new_memory_id,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import TokenCounter
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

# The file-memory scope directory is the agent's tool workspace, so code running
# in a sandboxed shell/python tool can plant symlinks, FIFOs and huge files in it.
# Every access below stays descriptor-relative, refuses non-regular entries, and
# reads under a byte cap so the primary process never resolves a planted entry.
_MAX_MEMORY_FILE_BYTES = 1 << 20
_MAX_MEMORY_SCAN_BYTES = 16 << 20
_MAX_MEMORY_SCAN_ENTRIES = 4096
_MAX_MEMORY_DIR_DEPTH = 8
_READ_CHUNK_BYTES = 1 << 16


@dataclass(frozen=True)
class _CappedPayload:
    """Bytes read from one memory file, plus whether the cap withheld content."""

    payload: bytes
    truncated: bool


@dataclass(frozen=True)
class _ScopeMemoryFile:
    """One memory file's scope-relative path and its capped text.

    ``rewritable`` is false when the loaded text is not the file's exact
    content, so rewriting the file from it would destroy the rest.
    """

    relative_path: str
    text: str
    rewritable: bool


@dataclass(frozen=True)
class _PathMemoryLine:
    memory_file: _ScopeMemoryFile
    line_no: int
    raw_line: str
    memory: str


def _tag_keyword_mode(result: MemoryResult) -> None:
    metadata = result.get("metadata")
    result["metadata"] = (
        {**metadata, "search_mode": "keyword"} if isinstance(metadata, dict) else {"search_mode": "keyword"}
    )


def _is_regular_file_at(directory_fd: int, name: str) -> bool:
    try:
        return stat.S_ISREG(os.lstat(name, dir_fd=directory_fd).st_mode)
    except OSError:
        return False


def _collect_daily_markdown_paths(daily_fd: int) -> list[str]:
    """List markdown files below the pinned daily directory, never crossing a link.

    Every directory is re-opened by a no-follow walk from ``daily_fd``, and one
    entry budget bounds files and directories alike.
    """
    found: list[str] = []
    pending: list[tuple[str, ...]] = [()]
    examined = 0
    skipped_deep = False
    while pending and examined <= _MAX_MEMORY_SCAN_ENTRIES:
        parts = pending.pop()
        with (
            suppress(OSError, ValueError),
            open_directory_within_root(daily_fd, Path(*parts)) as directory_fd,
            os.scandir(directory_fd) as entries,
        ):
            for entry in entries:
                examined += 1
                if examined > _MAX_MEMORY_SCAN_ENTRIES:
                    break
                if entry.is_dir(follow_symlinks=False):
                    if len(parts) < _MAX_MEMORY_DIR_DEPTH:
                        pending.append((*parts, entry.name))
                    else:
                        skipped_deep = True
                elif entry.name.endswith(".md") and entry.is_file(follow_symlinks=False):
                    found.append("/".join((FILE_MEMORY_DAILY_DIR, *parts, entry.name)))
    if examined > _MAX_MEMORY_SCAN_ENTRIES or skipped_deep:
        logger.warning(
            "Stopped file memory listing at its entry or depth limit",
            max_entries=_MAX_MEMORY_SCAN_ENTRIES,
            max_depth=_MAX_MEMORY_DIR_DEPTH,
        )
    return found


def _scope_markdown_relative_paths(scope_fd: int) -> list[str]:
    paths: list[str] = []
    if _is_regular_file_at(scope_fd, FILE_MEMORY_ENTRYPOINT):
        paths.append(FILE_MEMORY_ENTRYPOINT)
    with suppress(OSError, ValueError), open_directory_within_root(scope_fd, FILE_MEMORY_DAILY_DIR) as daily_fd:
        paths.extend(sorted(_collect_daily_markdown_paths(daily_fd)))
    return paths


def _read_capped_payload(descriptor: int, max_bytes: int) -> _CappedPayload:
    chunks: list[bytes] = []
    remaining = max_bytes
    while remaining > 0:
        chunk = os.read(descriptor, min(remaining, _READ_CHUNK_BYTES))
        if not chunk:
            return _CappedPayload(payload=b"".join(chunks), truncated=False)
        chunks.append(chunk)
        remaining -= len(chunk)
    return _CappedPayload(payload=b"".join(chunks), truncated=bool(os.read(descriptor, 1)))


def _validate_memory_descriptor(descriptor: int) -> os.stat_result:
    """Reject anything but a regular file behind an opened memory entry."""
    file_stat = os.fstat(descriptor)
    if not stat.S_ISREG(file_stat.st_mode):
        msg = "File memory entries must be regular files."
        raise ValueError(msg)
    return file_stat


def _read_scope_file_at(scope_fd: int, relative_path: str | Path, *, max_bytes: int) -> _CappedPayload | None:
    """Read one scope file through descriptors; ``None`` only when it is absent.

    Any other failure is raised so a caller never mistakes an unreadable entry
    for an empty one.
    """
    try:
        with open_regular_file_within_root(scope_fd, relative_path) as descriptor:
            _validate_memory_descriptor(descriptor)
            return _read_capped_payload(descriptor, max_bytes)
    except FileNotFoundError:
        return None


def _decode_capped_text(payload: _CappedPayload) -> str:
    """Decode capped bytes, dropping the partial line a truncated read left."""
    text = payload.payload.decode("utf-8", errors="replace")
    if not payload.truncated:
        return text
    newline_index = text.rfind("\n")
    return text[: newline_index + 1] if newline_index >= 0 else ""


def _scope_memory_file(relative_path: str, payload: _CappedPayload) -> _ScopeMemoryFile:
    text = _decode_capped_text(payload)
    # Replacement characters and dropped bytes must never be written back.
    lossy = text.encode("utf-8") != payload.payload
    return _ScopeMemoryFile(
        relative_path=relative_path,
        text=text,
        rewritable=not payload.truncated and not lossy,
    )


def _read_scope_markdown_files_at(scope_fd: int, scope_path: Path) -> list[_ScopeMemoryFile]:
    files: list[_ScopeMemoryFile] = []
    skipped: list[str] = []
    budget = _MAX_MEMORY_SCAN_BYTES
    for relative_path in _scope_markdown_relative_paths(scope_fd):
        if budget <= 0:
            logger.warning("File memory scan stopped at its byte budget", scope_path=str(scope_path))
            break
        try:
            payload = _read_scope_file_at(scope_fd, relative_path, max_bytes=min(_MAX_MEMORY_FILE_BYTES, budget))
        except (OSError, ValueError):
            skipped.append(relative_path)
            continue
        if payload is None:
            continue
        budget -= len(payload.payload)
        if payload.truncated:
            logger.warning(
                "Truncated oversized file memory entry",
                path=relative_path,
                max_bytes=_MAX_MEMORY_FILE_BYTES,
            )
        files.append(_scope_memory_file(relative_path, payload))
    if skipped:
        # One notice per scan, so planted entries cannot flood the log.
        logger.warning("Skipped unreadable file memory entries", count=len(skipped), first_path=skipped[0])
    return files


def _read_listed_memory_file(scope_path: Path, relative_path: str) -> _ScopeMemoryFile | None:
    """Read one listed non-entrypoint memory file without reading the rest of the scope."""
    if relative_path == FILE_MEMORY_ENTRYPOINT:
        return None
    try:
        with open_directory_within_root(scope_path) as scope_fd:
            if relative_path not in _scope_markdown_relative_paths(scope_fd):
                return None
            payload = _read_scope_file_at(scope_fd, relative_path, max_bytes=_MAX_MEMORY_FILE_BYTES)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("Skipped unreadable file memory entry", path=relative_path, error_type=type(exc).__name__)
        return None
    return _scope_memory_file(relative_path, payload) if payload is not None else None


def read_scope_memory_files(scope_path: Path) -> list[_ScopeMemoryFile]:
    """Read every in-scope memory file under the per-file and per-scan byte caps."""
    try:
        with open_directory_within_root(scope_path) as scope_fd:
            return _read_scope_markdown_files_at(scope_fd, scope_path)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        logger.warning(
            "Skipped file memory scope that is not a usable directory",
            scope_path=str(scope_path),
            error_type=type(exc).__name__,
        )
        return []


def _require_rewritable(memory_file: _ScopeMemoryFile) -> None:
    if not memory_file.rewritable:
        msg = (
            f"File memory entry {memory_file.relative_path} is oversized or not valid UTF-8, "
            "so it cannot be rewritten safely; edit the file directly."
        )
        raise ValueError(msg)


def write_scope_markdown_file(scope_path: Path, relative_path: Path, payload: bytes) -> None:
    """Publish one memory file descriptor-relative, never through a planted entry, keeping its permissions."""
    with (
        open_directory_within_root(scope_path) as scope_fd,
        open_directory_within_root(scope_fd, relative_path.parent, create=True) as directory_fd,
    ):
        mode = existing_file_mode(directory_fd, relative_path.name)
        # A new file is readable like a hand-written one, not left at the temp file's 0o600.
        atomic_write_bytes_at(directory_fd, relative_path.name, payload, file_mode=0o644 if mode is None else mode)


def _append_scope_markdown_line(scope_path: Path, relative_path: Path, line: str, *, initial_text: bytes) -> None:
    """Append one entry line through an append-only descriptor that follows no link."""
    with (
        open_directory_within_root(scope_path) as scope_fd,
        open_directory_within_root(scope_fd, relative_path.parent, create=True) as directory_fd,
    ):
        # A new file gets 0o666 less the umask, like an ordinary file write.
        descriptor = os.open(
            relative_path.name,
            os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
            0o666,
            dir_fd=directory_fd,
        )
        try:
            file_stat = _validate_memory_descriptor(descriptor)
            payload = _appended_payload(descriptor, file_stat, line, initial_text=initial_text)
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
        finally:
            os.close(descriptor)


def _appended_payload(descriptor: int, file_stat: os.stat_result, line: str, *, initial_text: bytes) -> bytes:
    if file_stat.st_size == 0:
        prefix = initial_text
    elif os.pread(descriptor, 1, file_stat.st_size - 1) != b"\n":
        prefix = b"\n"
    else:
        prefix = b""
    return prefix + line.encode("utf-8") + b"\n"


def _memory_lines_payload(lines: list[str]) -> bytes:
    return f"{'\n'.join(lines)}\n".encode() if lines else b""


def _is_unstructured_memory_line(snippet: str) -> bool:
    return bool(snippet) and not snippet.startswith("#") and FILE_MEMORY_ENTRY_PATTERN.match(snippet) is None


def _normalize_memory_text_for_dedup(text: str) -> str:
    return " ".join(text.lower().split())


def _load_scope_id_entries(scope: MemoryScope) -> tuple[list[MemoryResult], dict[str, _ScopeMemoryFile]]:
    results: list[MemoryResult] = []
    id_to_file: dict[str, _ScopeMemoryFile] = {}
    for memory_file in read_scope_memory_files(scope.root):
        for line_no, raw_line in enumerate(memory_file.text.splitlines(), 1):
            match = FILE_MEMORY_ENTRY_PATTERN.match(raw_line.strip())
            if not match:
                continue
            memory_id = match.group("id").strip()
            memory_text = match.group("memory").strip()
            if not memory_id or not memory_text:
                continue
            result: MemoryResult = {
                "id": memory_id,
                "memory": memory_text,
                "user_id": scope.user_id,
                "metadata": {"source_file": memory_file.relative_path, "line": line_no},
            }
            results.append(result)
            id_to_file.setdefault(memory_id, memory_file)

    return results, id_to_file


def _iter_scope_unstructured_lines(scope_path: Path) -> Iterator[tuple[str, int, str]]:
    """Yield relative paths, line numbers, and eligible snippets in file order."""
    for memory_file in read_scope_memory_files(scope_path):
        if memory_file.relative_path == FILE_MEMORY_ENTRYPOINT:
            continue
        for line_no, raw_line in enumerate(memory_file.text.splitlines(), 1):
            snippet = raw_line.strip()
            if _is_unstructured_memory_line(snippet):
                yield memory_file.relative_path, line_no, snippet


def _load_scope_unstructured_entries(scope: MemoryScope, existing_memory_text: set[str]) -> list[MemoryResult]:
    results: list[MemoryResult] = []
    seen_memory_text = set(existing_memory_text)
    for relative_path, line_no, snippet in _iter_scope_unstructured_lines(scope.root):
        normalized_snippet = _normalize_memory_text_for_dedup(snippet)
        if normalized_snippet in seen_memory_text:
            continue
        seen_memory_text.add(normalized_snippet)
        results.append(
            {
                "id": f"file:{relative_path}:{line_no}",
                "memory": snippet,
                "user_id": scope.user_id,
                "metadata": {"source_file": relative_path, "line": line_no},
            },
        )
    return results


@timed("system_prompt_assembly.memory_search.file.id_entries_load")
def _load_scope_entries_for_search(scope: MemoryScope) -> tuple[list[MemoryResult], dict[str, _ScopeMemoryFile]]:
    return _load_scope_id_entries(scope)


def _extract_query_tokens(query: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) > 1}


def _match_score(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    lowered = text.lower()
    return sum(1 for token in query_tokens if token in lowered) / len(query_tokens)


def _format_entry_line(memory_id: str, content: str) -> str:
    normalized_content = " ".join(content.strip().split())
    return f"- [id={memory_id}] {normalized_content}"


def _agent_scope(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> MemoryScope:
    """Return an agent's file-memory scope, its workspace, recording a private instance's owner first."""
    runtime = resolve_agent_runtime(
        agent_name,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        create=config.get_agent(agent_name).private is not None,
    )
    if runtime.workspace.file_memory_path is None:
        msg = f"File-memory agent '{agent_name}' did not resolve a file-memory root"
        raise ValueError(msg)
    return MemoryScope(agent_scope_user_id(agent_name), runtime.workspace.file_memory_path)


def _visible_scopes(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> list[MemoryScope]:
    """Return the agent's scope, then the scopes of its teams."""
    return [
        _agent_scope(agent_name, config, runtime_paths, execution_identity),
        *team_scopes(agent_name, config, runtime_paths),
    ]


def _schedule_semantic_refresh(
    agent_name: str,
    scope: MemoryScope,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None,
) -> None:
    search_config = memory_search(config, agent_name)
    if search_config.mode != "semantic":
        return
    schedule_semantic_file_memory_refresh(
        scope_user_id=scope.user_id,
        root=scope.root,
        config=config,
        runtime_paths=runtime_paths,
        search_config=search_config,
        execution_identity=execution_identity,
    )


def refresh_agent_memory_search(
    agent_name: str,
    root: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    """Re-index an agent's file memory at ``root`` after its files changed outside the memory tool.

    A private agent's memory is one requester's, so ``execution_identity`` names that requester.
    """
    _schedule_semantic_refresh(
        agent_name,
        MemoryScope(agent_scope_user_id(agent_name), root),
        config,
        runtime_paths,
        execution_identity,
    )


def _append_scope_memory_entry(
    scope: MemoryScope,
    content: str,
    relative_path: str = FILE_MEMORY_ENTRYPOINT,
) -> MemoryResult:
    scope.root.mkdir(parents=True, exist_ok=True)
    initial_text = f"{FILE_MEMORY_ENTRYPOINT_HEADER}\n\n".encode() if relative_path == FILE_MEMORY_ENTRYPOINT else b""
    memory_id = new_memory_id()
    # Appending keeps the existing bytes untouched, so a planted entry can neither
    # redirect the write nor cost the file its content; it raises instead.
    _append_scope_markdown_line(
        scope.root,
        Path(relative_path),
        _format_entry_line(memory_id, content),
        initial_text=initial_text,
    )

    return {
        "id": memory_id,
        "memory": " ".join(content.strip().split()),
        "user_id": scope.user_id,
        "metadata": {"source_file": relative_path},
    }


def _search_scope_memory_entries(scope: MemoryScope, query: str, *, limit: int) -> list[MemoryResult]:
    id_entries, _ = _load_scope_entries_for_search(scope)
    query_tokens = _extract_query_tokens(query)

    scored_entries: list[MemoryResult] = []
    seen_scored_text: set[str] = set()
    for entry in id_entries:
        text = entry.get("memory", "")
        normalized_text = _normalize_memory_text_for_dedup(text)
        if normalized_text in seen_scored_text:
            continue
        score = _match_score(query_tokens, text)
        if score <= 0:
            continue
        scored_entries.append({**entry, "score": score})
        seen_scored_text.add(normalized_text)

    scored_entries.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    scored_entries = scored_entries[:limit]

    if limit <= len(scored_entries):
        return scored_entries

    remaining_limit = limit - len(scored_entries)
    snippet_results = _scan_scope_memory_snippets(scope, query_tokens, seen_scored_text)

    snippet_results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    return scored_entries + snippet_results[:remaining_limit]


@timed("system_prompt_assembly.memory_search.file.snippet_scan")
def _scan_scope_memory_snippets(
    scope: MemoryScope,
    query_tokens: set[str],
    existing_memory_text: set[str],
) -> list[MemoryResult]:
    return [
        {**entry, "score": score}
        for entry in _load_scope_unstructured_entries(scope, existing_memory_text)
        if (score := _match_score(query_tokens, entry["memory"])) > 0
    ]


def _load_scope_path_memory_line(scope: MemoryScope, memory_id: str) -> _PathMemoryLine | None:
    match = FILE_MEMORY_PATH_ID_PATTERN.match(memory_id)
    if match is None:
        return None

    line_no = int(match.group("line"))
    relative_path = match.group("path")
    memory_file = _read_listed_memory_file(scope.root, relative_path)
    if memory_file is None:
        return None

    lines = memory_file.text.splitlines()
    if line_no <= 0 or line_no > len(lines):
        return None

    raw_line = lines[line_no - 1]
    snippet = raw_line.strip()
    if not _is_unstructured_memory_line(snippet):
        return None
    return _PathMemoryLine(memory_file=memory_file, line_no=line_no, raw_line=raw_line, memory=snippet)


def _get_scope_memory_by_path_id(scope: MemoryScope, memory_id: str) -> MemoryResult | None:
    path_memory_line = _load_scope_path_memory_line(scope, memory_id)
    if path_memory_line is None:
        return None
    return {
        "id": memory_id,
        "memory": path_memory_line.memory,
        "user_id": scope.user_id,
        "metadata": {"source_file": path_memory_line.memory_file.relative_path, "line": path_memory_line.line_no},
    }


def _get_scope_memory_by_id(scope: MemoryScope, memory_id: str) -> MemoryResult | None:
    if path_result := _get_scope_memory_by_path_id(scope, memory_id):
        return path_result
    entries, _ = _load_scope_id_entries(scope)
    for entry in entries:
        if entry["id"] == memory_id:
            return entry
    return None


def _replace_scope_memory_entry(scope: MemoryScope, memory_id: str, content: str | None) -> bool:
    if FILE_MEMORY_PATH_ID_PATTERN.match(memory_id):
        return _replace_scope_path_memory_entry(scope, memory_id, content)

    _entries, id_to_file = _load_scope_id_entries(scope)
    if (memory_file := id_to_file.get(memory_id)) is None:
        return False

    new_lines: list[str] = []
    for raw_line in memory_file.text.splitlines():
        stripped = raw_line.strip()
        match = FILE_MEMORY_ENTRY_PATTERN.match(stripped)
        if match is None or match.group("id").strip() != memory_id:
            new_lines.append(raw_line)
            continue
        if content is None:
            continue

        updated_line = _format_entry_line(memory_id, content)
        prefix_len = len(raw_line) - len(raw_line.lstrip(" "))
        new_lines.append(f"{raw_line[:prefix_len]}{updated_line}")

    _require_rewritable(memory_file)
    write_scope_markdown_file(scope.root, Path(memory_file.relative_path), _memory_lines_payload(new_lines))
    return True


def _replace_scope_path_memory_entry(scope: MemoryScope, memory_id: str, content: str | None) -> bool:
    path_memory_line = _load_scope_path_memory_line(scope, memory_id)
    if path_memory_line is None:
        return False
    _require_rewritable(path_memory_line.memory_file)

    lines = path_memory_line.memory_file.text.splitlines()

    if content is None or not content.strip():
        lines[path_memory_line.line_no - 1] = ""
    else:
        prefix_len = len(path_memory_line.raw_line) - len(path_memory_line.raw_line.lstrip(" "))
        lines[path_memory_line.line_no - 1] = (
            f"{path_memory_line.raw_line[:prefix_len]}{' '.join(content.strip().split())}"
        )
    write_scope_markdown_file(
        scope.root,
        Path(path_memory_line.memory_file.relative_path),
        _memory_lines_payload(lines),
    )
    return True


def _selected_lines(costs: list[int], first_entry: int, max_lines: int, budget: int) -> list[int]:
    """Return the indexes of the whole lines within both caps, withholding the oldest structured entries first.

    ``costs`` holds each line's tokens with its newline, and ``budget`` is the token cap.
    `add_memory` appends entries, so a capped preload keeps the curated head before the first entry and fills the rest
    with the newest lines; a file without entries, or whose head alone reaches a cap, keeps its leading lines. A long
    line is withheld whole rather than cut, so the preload never shows a partial fact and the truncation marker
    accounts for every withheld line.
    """
    leading = 0
    kept = 0
    for cost in costs[:max_lines]:
        if kept + cost > budget:
            break
        kept += cost
        leading += 1
    if leading == len(costs) or leading < first_entry:
        return list(range(leading))
    kept = sum(costs[:first_entry])
    newest: list[int] = []
    for index in range(len(costs) - 1, first_entry - 1, -1):
        if first_entry + len(newest) >= max_lines or kept + costs[index] > budget:
            break
        kept += costs[index]
        newest.append(index)
    return [*range(first_entry), *reversed(newest)]


def _entrypoint_text(lines: Sequence[str]) -> str:
    """Return the text of ``lines`` as the preamble inlines it."""
    return "\n".join(lines).strip()


def _line_tokens(counter: TokenCounter, line: str) -> int:
    """Count one line with its newline as an upper bound: before the encoder loads, its UTF-8 bytes."""
    return count(counter, line + "\n", upper_bound=True)


def _lines_within_caps(lines: Sequence[str], config: Config, counter: TokenCounter) -> list[str]:
    """Keep the whole lines within both caps, as ``counter`` counts them, withholding the oldest entries first."""
    file_config = config.memory.file
    first_entry = next(
        (index for index, line in enumerate(lines) if FILE_MEMORY_ENTRY_PATTERN.match(line.strip())),
        len(lines),
    )
    costs = [_line_tokens(counter, line) for line in lines]
    kept = _selected_lines(costs, first_entry, file_config.max_entrypoint_lines, file_config.max_entrypoint_tokens)
    return [lines[index] for index in kept]


def cap_entrypoint(
    entrypoint: MemoryEntrypointContext,
    config: Config,
    counter: TokenCounter,
) -> MemoryEntrypointContext:
    """Cut the preload to both caps, as the model the preload goes to counts it."""
    kept = _lines_within_caps(entrypoint.lines, config, counter)
    return replace(entrypoint, text=_entrypoint_text(kept), included_lines=len(kept))


def entrypoint_tokens(text: str, counter: TokenCounter) -> int:
    """Return the tokens of the MEMORY.md ``text`` as the preload's token cap counts them."""
    return sum(_line_tokens(counter, line) for line in text.splitlines())


def entrypoint_withholds_lines(text: str, config: Config, counter: TokenCounter) -> bool:
    """Return whether the MEMORY.md preload would leave out any line of ``text``, its size counted with ``counter``."""
    lines = text.splitlines()
    return len(_lines_within_caps(lines, config, counter)) < len(lines)


@timed("system_prompt_assembly.memory_file_entrypoint_read")
def _load_scope_entrypoint_context(scope: MemoryScope) -> MemoryEntrypointContext:
    """Load the scoped `MEMORY.md` entrypoint whole; ``cap_entrypoint`` cuts its preload."""
    payload = _read_scope_entrypoint_payload(scope.root)
    if payload is None:
        return MemoryEntrypointContext()
    entrypoint_path = scope.root / FILE_MEMORY_ENTRYPOINT
    lines = _decode_capped_text(payload).splitlines()
    if payload.truncated:
        logger.warning(
            "Truncated oversized file memory entrypoint",
            path=str(entrypoint_path),
            max_bytes=_MAX_MEMORY_FILE_BYTES,
        )
    return MemoryEntrypointContext(
        source_path=entrypoint_path,
        # An oversized entrypoint still reports at least one withheld line, so the
        # preamble tells the model to read the file instead of claiming completeness.
        total_lines=len(lines) + 1 if payload.truncated else len(lines),
        lines=tuple(lines),
    )


def _read_scope_entrypoint_payload(scope_path: Path) -> _CappedPayload | None:
    """Read `MEMORY.md` for the per-turn preload, degrading instead of raising."""
    try:
        with open_directory_within_root(scope_path) as scope_fd:
            return _read_scope_file_at(scope_fd, FILE_MEMORY_ENTRYPOINT, max_bytes=_MAX_MEMORY_FILE_BYTES)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning(
            "Skipped unreadable file memory entrypoint",
            scope_path=str(scope_path),
            error_type=type(exc).__name__,
        )
        return None


def _first_scope_memory(scopes: list[MemoryScope], memory_id: str) -> MemoryResult | None:
    """Return one memory from the first scope holding it."""
    return next(filter(None, (_get_scope_memory_by_id(scope, memory_id) for scope in scopes)), None)


def _replace_in_first_scope(scopes: list[MemoryScope], memory_id: str, content: str | None) -> MemoryScope | None:
    """Rewrite one memory in the first scope holding it, or remove it when ``content`` is None; return that scope."""
    return next((scope for scope in scopes if _replace_scope_memory_entry(scope, memory_id, content)), None)


def append_agent_daily_file_memory(
    content: str,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> MemoryResult:
    """Append one memory entry to the agent's daily file."""
    scope = _agent_scope(agent_name, config, runtime_paths, execution_identity)
    current_date = datetime.now(ZoneInfo(entity_timezone(config, agent_name))).date().isoformat()
    result = _append_scope_memory_entry(scope, content, f"{FILE_MEMORY_DAILY_DIR}/{current_date}.md")
    _schedule_semantic_refresh(agent_name, scope, config, runtime_paths, execution_identity)
    logger.info("File daily memory added", agent=agent_name, date=current_date)
    return result


def _keyword_results(scope: MemoryScope, query: str, limit: int) -> list[MemoryResult]:
    results = _search_scope_memory_entries(scope, query, limit=limit)
    for result in results:
        _tag_keyword_mode(result)
    return results


@timed("system_prompt_assembly.memory_search.file.agent_scope")
def _agent_keyword_results(scope: MemoryScope, query: str, limit: int) -> list[MemoryResult]:
    return _keyword_results(scope, query, limit)


@timed("system_prompt_assembly.memory_search.file.team_scope")
def _team_keyword_results(scopes: list[MemoryScope], query: str, limit: int) -> list[MemoryResult]:
    return [result for scope in scopes for result in _keyword_results(scope, query, limit)]


@dataclass(frozen=True)
class FileMemoryBackend:
    """File-backed adapter implementing the shared memory backend surface."""

    runtime_paths: RuntimePaths
    context_label: ClassVar[str] = "agent file"

    async def add(
        self,
        content: str,
        agent_name: str,
        config: Config,
        *,
        metadata: dict | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Append one file-backed memory for an agent scope."""
        del metadata  # File memory entries persist plain text only.
        scope = await asyncio.to_thread(_agent_scope, agent_name, config, self.runtime_paths, execution_identity)
        await asyncio.to_thread(_append_scope_memory_entry, scope, content)
        _schedule_semantic_refresh(agent_name, scope, config, self.runtime_paths, execution_identity)
        logger.info("File memory added", agent=agent_name)

    @timed("system_prompt_assembly.memory_search.file_backend")
    async def search(
        self,
        query: str,
        agent_name: str,
        config: Config,
        *,
        limit: int,
        execution_identity: ToolExecutionIdentity | None = None,
        editable: bool = False,
    ) -> MemorySearchOutcome:
        """Search file-backed memories visible to an agent: its own scope, then its teams' scopes by keyword.

        Keyword scans read and score every memory file in the scope, so all
        file-reading paths run in worker threads (#1260); only the semantic
        index query stays natively async. A semantic-path failure falls back
        to keyword matches and surfaces its classified cause in the outcome.
        """
        agent_scope, *teams = await asyncio.to_thread(
            _visible_scopes,
            agent_name,
            config,
            self.runtime_paths,
            execution_identity,
        )

        degraded_reason: str | None = None
        results: list[MemoryResult] | None = None
        search_config = memory_search(config, agent_name)
        # Semantic matches are read-only locators, so an editable search matches by keyword.
        if search_config.mode == "semantic" and not editable:
            try:
                results = await search_semantic_file_memories(
                    query,
                    scope_user_id=agent_scope.user_id,
                    root=agent_scope.root,
                    config=config,
                    runtime_paths=self.runtime_paths,
                    search_config=search_config,
                    limit=limit,
                    execution_identity=execution_identity,
                )
            except SemanticFileMemoryIndexUnavailableError as exc:
                # A cold index kept unpublished by a classified embedder
                # failure degrades loudly; plain warm-up stays silent.
                degraded_reason = exc.degraded_reason
                logger.debug(
                    "File-memory semantic index unavailable; falling back to keyword search",
                    agent=agent_name,
                    degraded_reason=degraded_reason,
                )
            except Exception as exc:
                degraded_reason = (
                    classified_embedder_error(exc) or f"semantic memory search failed ({type(exc).__name__})"
                )
                logger.exception(
                    "File-memory semantic search failed; falling back to keyword search",
                    agent=agent_name,
                )
        if results is None:
            results = await asyncio.to_thread(_agent_keyword_results, agent_scope, query, limit)

        results.extend(await asyncio.to_thread(_team_keyword_results, teams, query, limit))
        results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
        return MemorySearchOutcome(results=results[:limit], degraded_reason=degraded_reason)

    async def list_all(
        self,
        agent_name: str,
        config: Config,
        *,
        limit: int,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> list[MemoryResult]:
        """List file-backed memories stored for an agent."""
        scope = await asyncio.to_thread(_agent_scope, agent_name, config, self.runtime_paths, execution_identity)
        results, _ = await asyncio.to_thread(_load_scope_id_entries, scope)
        if len(results) >= limit:
            return results[:limit]

        existing_memory_text = {
            _normalize_memory_text_for_dedup(memory_text) for entry in results if (memory_text := entry.get("memory"))
        }
        unstructured_results = await asyncio.to_thread(_load_scope_unstructured_entries, scope, existing_memory_text)
        return (results + unstructured_results)[:limit]

    async def get(
        self,
        memory_id: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryResult | None:
        """Return one file-backed memory of a scope the agent reads."""
        scopes = await asyncio.to_thread(_visible_scopes, agent_name, config, self.runtime_paths, execution_identity)
        return await asyncio.to_thread(_first_scope_memory, scopes, memory_id)

    async def update(
        self,
        memory_id: str,
        content: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Update one file-backed memory of a scope the agent reads."""
        await self._replace(memory_id, content, agent_name, config, execution_identity)
        logger.info("File memory updated", memory_id=memory_id)

    async def delete(
        self,
        memory_id: str,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Delete one file-backed memory of a scope the agent reads."""
        await self._replace(memory_id, None, agent_name, config, execution_identity)
        logger.info("File memory deleted", memory_id=memory_id)

    async def _replace(
        self,
        memory_id: str,
        content: str | None,
        agent_name: str,
        config: Config,
        execution_identity: ToolExecutionIdentity | None,
    ) -> None:
        """Rewrite one memory of the first scope holding it, or remove it when ``content`` is None."""
        scopes = await asyncio.to_thread(_visible_scopes, agent_name, config, self.runtime_paths, execution_identity)
        if (scope := await asyncio.to_thread(_replace_in_first_scope, scopes, memory_id, content)) is None:
            raise MemoryNotFoundError(memory_id)
        if scope == scopes[0]:
            # Semantic search indexes the agent's own scope only.
            _schedule_semantic_refresh(agent_name, scope, config, self.runtime_paths, execution_identity)

    async def store_conversation(
        self,
        prompt: str,
        caller: str | Sequence[str],
        session_id: str,
        config: Config,
        *,
        thread_history: Sequence[ResolvedVisibleMessage] | None = None,
        user_id: str | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Append condensed conversation text to the agent's scope or the team's."""
        del session_id, thread_history, user_id  # File conversation memory stores condensed text only.
        condensed_prompt = " ".join(redact_memory_text(prompt).split())
        if not condensed_prompt:
            return
        if isinstance(caller, str):
            await self.add(condensed_prompt, caller, config, execution_identity=execution_identity)
            return
        scope = team_scope(team_user_id(caller), self.runtime_paths)
        await asyncio.to_thread(_append_scope_memory_entry, scope, condensed_prompt)
        logger.info("File team memory added", team_id=scope.user_id)

    @timed("system_prompt_assembly.memory_file_entrypoint_load")
    def load_entrypoint_context(
        self,
        agent_name: str,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryEntrypointContext:
        """Load the stable scoped `MEMORY.md` entrypoint of one agent whole, before ``cap_entrypoint`` cuts it."""
        return _load_scope_entrypoint_context(_agent_scope(agent_name, config, self.runtime_paths, execution_identity))
