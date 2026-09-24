"""File-backed memory implementation."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast
from zoneinfo import ZoneInfo

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.constants import resolve_config_relative_path
from mindroom.embedding_errors import classified_embedder_error
from mindroom.logging_config import get_logger
from mindroom.memory_scope_ids import agent_name_from_scope_user_id, agent_scope_user_id
from mindroom.path_confinement import open_directory_within_root, open_regular_file_within_root
from mindroom.timing import timed

from ._policy import (
    allowed_scope_storage_paths,
    build_team_user_id,
    effective_storage_paths_for_context,
    get_team_ids_for_agent,
    resolve_file_memory_resolution,
    storage_paths_for_scope_user_id,
)
from ._semantic_file_search import (
    SemanticFileMemoryIndexUnavailableError,
    schedule_semantic_file_memory_refresh,
    search_semantic_file_memories,
)
from ._shared import (
    FILE_MEMORY_DAILY_DIR,
    FILE_MEMORY_DEFAULT_DIRNAME,
    FILE_MEMORY_ENTRY_PATTERN,
    FILE_MEMORY_ENTRYPOINT,
    FILE_MEMORY_PATH_ID_PATTERN,
    FileMemoryResolution,
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
    relative_path: str
    line_no: int
    raw_line: str
    memory: str
    lines: tuple[str, ...]


def _tag_keyword_mode(result: MemoryResult) -> None:
    metadata = result.get("metadata")
    result["metadata"] = (
        {**metadata, "search_mode": "keyword"} if isinstance(metadata, dict) else {"search_mode": "keyword"}
    )


def _file_memory_root(
    storage_path: Path,
    resolution: FileMemoryResolution,
    config: Config,
    *,
    use_configured_path: bool,
) -> Path:
    configured_path = config.memory.file.path if use_configured_path else None
    if configured_path:
        return resolve_config_relative_path(
            configured_path,
            runtime_paths=resolution.runtime_paths,
        )
    return (storage_path.expanduser().resolve() / FILE_MEMORY_DEFAULT_DIRNAME).resolve()


def _scope_dir_name(scope_user_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._+-]+", "_", scope_user_id).strip("_") or "default"


def _scope_entrypoint_path(scope_path: Path) -> Path:
    return scope_path / FILE_MEMORY_ENTRYPOINT


def _scope_relative_markdown_path(relative_path: str) -> Path | None:
    """Return the lexically in-scope markdown path, never resolving a link."""
    candidate = Path(relative_path)
    if candidate.is_absolute() or not candidate.parts or ".." in candidate.parts:
        return None
    if candidate.suffix.lower() != ".md":
        return None
    return candidate


def _scope_dir(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
    *,
    create: bool,
) -> Path:
    if resolution.agent_memory_scope_path is not None:
        scope_path = resolution.agent_memory_scope_path
        if create:
            scope_path.mkdir(parents=True, exist_ok=True)
        return scope_path

    scope_path = _file_memory_root(
        resolution.storage_path,
        resolution,
        config,
        use_configured_path=resolution.use_configured_path,
    ) / _scope_dir_name(scope_user_id)
    if create:
        scope_path.mkdir(parents=True, exist_ok=True)
    return scope_path


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


def _read_scope_markdown_files(scope_path: Path) -> list[_ScopeMemoryFile]:
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


def _existing_file_mode(directory_fd: int, name: str) -> int | None:
    """Return a regular entry's permission bits so a rewrite keeps them."""
    try:
        file_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return stat.S_IMODE(file_stat.st_mode) & 0o777 if stat.S_ISREG(file_stat.st_mode) else None


def _require_rewritable(memory_file: _ScopeMemoryFile) -> None:
    if not memory_file.rewritable:
        msg = (
            f"File memory entry {memory_file.relative_path} is oversized or not valid UTF-8, "
            "so it cannot be rewritten safely; edit the file directly."
        )
        raise ValueError(msg)


def _write_scope_markdown_file(scope_path: Path, relative_path: Path, payload: bytes) -> None:
    """Publish one memory file descriptor-relative, never through a planted entry."""
    with (
        open_directory_within_root(scope_path) as scope_fd,
        open_directory_within_root(scope_fd, relative_path.parent, create=True) as directory_fd,
    ):
        atomic_write_bytes_at(
            directory_fd,
            relative_path.name,
            payload,
            file_mode=_existing_file_mode(directory_fd, relative_path.name),
        )


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


def _load_scope_id_entries(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> tuple[list[MemoryResult], dict[str, _ScopeMemoryFile]]:
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)

    results: list[MemoryResult] = []
    id_to_file: dict[str, _ScopeMemoryFile] = {}
    for memory_file in _read_scope_markdown_files(scope_path):
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
                "user_id": scope_user_id,
                "metadata": {"source_file": memory_file.relative_path, "line": line_no},
            }
            results.append(result)
            id_to_file.setdefault(memory_id, memory_file)

    return results, id_to_file


def _iter_scope_unstructured_lines(scope_path: Path) -> Iterator[tuple[str, int, str]]:
    """Yield relative paths, line numbers, and eligible snippets in file order."""
    for memory_file in _read_scope_markdown_files(scope_path):
        if memory_file.relative_path == FILE_MEMORY_ENTRYPOINT:
            continue
        for line_no, raw_line in enumerate(memory_file.text.splitlines(), 1):
            snippet = raw_line.strip()
            if _is_unstructured_memory_line(snippet):
                yield memory_file.relative_path, line_no, snippet


def _load_scope_unstructured_entries(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
    existing_memory_text: set[str],
) -> list[MemoryResult]:
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    if not scope_path.exists():
        return []

    results: list[MemoryResult] = []
    seen_memory_text = set(existing_memory_text)
    for relative_path, line_no, snippet in _iter_scope_unstructured_lines(scope_path):
        normalized_snippet = _normalize_memory_text_for_dedup(snippet)
        if normalized_snippet in seen_memory_text:
            continue
        seen_memory_text.add(normalized_snippet)
        results.append(
            {
                "id": f"file:{relative_path}:{line_no}",
                "memory": snippet,
                "user_id": scope_user_id,
                "metadata": {"source_file": relative_path, "line": line_no},
            },
        )
    return results


@timed("system_prompt_assembly.memory_search.file.id_entries_load")
def _load_scope_entries_for_search(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> tuple[list[MemoryResult], dict[str, _ScopeMemoryFile]]:
    return _load_scope_id_entries(scope_user_id, resolution, config)


def _extract_query_tokens(query: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9_]+", query.lower()) if len(token) > 1}


def _match_score(query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    lowered = text.lower()
    overlap = sum(1 for token in query_tokens if token in lowered)
    if overlap == 0:
        return 0.0
    return overlap / len(query_tokens)


def _format_entry_line(memory_id: str, content: str) -> str:
    normalized_content = " ".join(content.strip().split())
    return f"- [id={memory_id}] {normalized_content}"


def _schedule_agent_semantic_refresh(
    agent_name: str,
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    search_config = config.resolve_entity(agent_name).memory_search
    if search_config.mode != "semantic":
        return
    schedule_semantic_file_memory_refresh(
        scope_user_id=scope_user_id,
        root=_scope_dir(scope_user_id, resolution, config, create=False),
        config=config,
        runtime_paths=runtime_paths,
        search_config=search_config,
        execution_identity=execution_identity,
    )


def _schedule_scope_semantic_refresh(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
) -> None:
    if (agent_name := agent_name_from_scope_user_id(scope_user_id)) is None:
        return
    _schedule_agent_semantic_refresh(
        agent_name,
        scope_user_id,
        resolution,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )


def _append_scope_memory_entry(
    scope_user_id: str,
    content: str,
    resolution: FileMemoryResolution,
    config: Config,
    *,
    memory_id: str | None = None,
    target_relative_path: str | None = None,
) -> MemoryResult:
    scope_path = _scope_dir(scope_user_id, resolution, config, create=True)
    if target_relative_path is None:
        target_path = Path(FILE_MEMORY_ENTRYPOINT)
        initial_text = b"# Memory\n\n"
    else:
        validated_path = _scope_relative_markdown_path(target_relative_path)
        if validated_path is None:
            msg = f"Invalid markdown memory path: {target_relative_path}"
            raise ValueError(msg)
        target_path = validated_path
        initial_text = b""

    relative_path = target_path.as_posix()
    memory_id = memory_id or new_memory_id()
    # Appending keeps the existing bytes untouched, so a planted entry can neither
    # redirect the write nor cost the file its content; it raises instead.
    _append_scope_markdown_line(
        scope_path,
        target_path,
        _format_entry_line(memory_id, content),
        initial_text=initial_text,
    )

    return {
        "id": memory_id,
        "memory": " ".join(content.strip().split()),
        "user_id": scope_user_id,
        "metadata": {"source_file": relative_path},
    }


def _search_scope_memory_entries(
    scope_user_id: str,
    query: str,
    resolution: FileMemoryResolution,
    config: Config,
    *,
    limit: int,
) -> list[MemoryResult]:
    id_entries, _ = _load_scope_entries_for_search(scope_user_id, resolution, config)
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
        enriched = dict(entry)
        enriched["score"] = score
        scored_entries.append(cast("MemoryResult", enriched))
        if normalized_text:
            seen_scored_text.add(normalized_text)

    scored_entries.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    scored_entries = scored_entries[:limit]

    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    if not scope_path.exists() or limit <= len(scored_entries):
        return scored_entries

    remaining_limit = limit - len(scored_entries)
    snippet_results: list[MemoryResult] = []
    existing_memory_text = {
        memory_text
        for entry in scored_entries
        if (memory_text := _normalize_memory_text_for_dedup(entry.get("memory", "")))
    }
    snippet_results = _scan_scope_memory_snippets(
        scope_user_id,
        query_tokens,
        scope_path,
        existing_memory_text,
    )

    snippet_results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    return scored_entries + snippet_results[:remaining_limit]


@timed("system_prompt_assembly.memory_search.file.snippet_scan")
def _scan_scope_memory_snippets(
    scope_user_id: str,
    query_tokens: set[str],
    scope_path: Path,
    existing_memory_text: set[str],
) -> list[MemoryResult]:
    snippet_results: list[MemoryResult] = []
    for relative_path, line_no, snippet in _iter_scope_unstructured_lines(scope_path):
        normalized_snippet = _normalize_memory_text_for_dedup(snippet)
        if normalized_snippet in existing_memory_text:
            continue
        score = _match_score(query_tokens, snippet)
        if score <= 0:
            continue
        existing_memory_text.add(normalized_snippet)
        snippet_results.append(
            {
                "id": f"file:{relative_path}:{line_no}",
                "memory": snippet,
                "user_id": scope_user_id,
                "metadata": {"source_file": relative_path, "line": line_no},
                "score": score,
            },
        )
    return snippet_results


def _load_scope_path_memory_line(
    scope_user_id: str,
    memory_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> _PathMemoryLine | None:
    match = FILE_MEMORY_PATH_ID_PATTERN.match(memory_id)
    if match is None:
        return None

    line_no = int(match.group("line"))
    relative_path = match.group("path")
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    memory_file = _read_listed_memory_file(scope_path, relative_path)
    if memory_file is None:
        return None

    lines = memory_file.text.splitlines()
    if line_no <= 0 or line_no > len(lines):
        return None

    raw_line = lines[line_no - 1]
    snippet = raw_line.strip()
    if not _is_unstructured_memory_line(snippet):
        return None
    return _PathMemoryLine(
        memory_file=memory_file,
        relative_path=relative_path,
        line_no=line_no,
        raw_line=raw_line,
        memory=snippet,
        lines=tuple(lines),
    )


def _get_scope_memory_by_path_id(
    scope_user_id: str,
    memory_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> MemoryResult | None:
    path_memory_line = _load_scope_path_memory_line(scope_user_id, memory_id, resolution, config)
    if path_memory_line is None:
        return None
    return {
        "id": memory_id,
        "memory": path_memory_line.memory,
        "user_id": scope_user_id,
        "metadata": {"source_file": path_memory_line.relative_path, "line": path_memory_line.line_no},
    }


def _get_scope_memory_by_id(
    scope_user_id: str,
    memory_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> MemoryResult | None:
    if path_result := _get_scope_memory_by_path_id(scope_user_id, memory_id, resolution, config):
        return path_result
    entries, _ = _load_scope_id_entries(scope_user_id, resolution, config)
    for entry in entries:
        if entry["id"] == memory_id:
            return entry
    return None


def _replace_scope_memory_entry(
    scope_user_id: str,
    memory_id: str,
    content: str | None,
    resolution: FileMemoryResolution,
    config: Config,
) -> bool:
    if FILE_MEMORY_PATH_ID_PATTERN.match(memory_id):
        return _replace_scope_path_memory_entry(scope_user_id, memory_id, content, resolution, config)

    _entries, id_to_file = _load_scope_id_entries(scope_user_id, resolution, config)
    if (memory_file := id_to_file.get(memory_id)) is None:
        return False

    changed = False
    new_lines: list[str] = []
    for raw_line in memory_file.text.splitlines():
        stripped = raw_line.strip()
        match = FILE_MEMORY_ENTRY_PATTERN.match(stripped)
        if match is None or match.group("id").strip() != memory_id:
            new_lines.append(raw_line)
            continue

        changed = True
        if content is None:
            continue

        updated_line = _format_entry_line(memory_id, content)
        prefix_len = len(raw_line) - len(raw_line.lstrip(" "))
        new_lines.append(f"{raw_line[:prefix_len]}{updated_line}")

    if not changed:
        return False

    _require_rewritable(memory_file)
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    _write_scope_markdown_file(scope_path, Path(memory_file.relative_path), _memory_lines_payload(new_lines))
    return True


def _replace_scope_path_memory_entry(
    scope_user_id: str,
    memory_id: str,
    content: str | None,
    resolution: FileMemoryResolution,
    config: Config,
) -> bool:
    path_memory_line = _load_scope_path_memory_line(scope_user_id, memory_id, resolution, config)
    if path_memory_line is None:
        return False
    _require_rewritable(path_memory_line.memory_file)

    lines = list(path_memory_line.lines)

    if content is None or not content.strip():
        lines[path_memory_line.line_no - 1] = ""
    else:
        prefix_len = len(path_memory_line.raw_line) - len(path_memory_line.raw_line.lstrip(" "))
        lines[path_memory_line.line_no - 1] = (
            f"{path_memory_line.raw_line[:prefix_len]}{' '.join(content.strip().split())}"
        )
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    _write_scope_markdown_file(
        scope_path,
        Path(path_memory_line.memory_file.relative_path),
        _memory_lines_payload(lines),
    )
    return True


@timed("system_prompt_assembly.memory_file_entrypoint_read")
def _load_scope_entrypoint_context(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> MemoryEntrypointContext:
    """Load the scoped `MEMORY.md` entrypoint text and what the cap withheld."""
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    payload = _read_scope_entrypoint_payload(scope_path)
    if payload is None:
        return MemoryEntrypointContext()
    entrypoint_path = _scope_entrypoint_path(scope_path)
    max_lines = config.memory.file.max_entrypoint_lines
    lines = _decode_capped_text(payload).splitlines()
    # An oversized entrypoint still reports at least one withheld line, so the
    # preamble tells the model to read the file instead of claiming completeness.
    total_lines = len(lines) + 1 if payload.truncated else len(lines)
    if payload.truncated:
        logger.warning(
            "Truncated oversized file memory entrypoint",
            path=str(entrypoint_path),
            max_bytes=_MAX_MEMORY_FILE_BYTES,
        )
    if max_lines < total_lines:
        lines = lines[:max_lines]
    return MemoryEntrypointContext(
        text="\n".join(lines).strip(),
        source_path=entrypoint_path,
        included_lines=len(lines),
        total_lines=total_lines,
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


def _find_file_replica_memory_ids(
    *,
    scope_user_id: str,
    anchor_result: MemoryResult,
    resolution: FileMemoryResolution,
    config: Config,
) -> list[str]:
    anchor_memory = anchor_result.get("memory")
    anchor_metadata = anchor_result.get("metadata")
    anchor_source_file = anchor_metadata.get("source_file") if isinstance(anchor_metadata, dict) else None
    if not isinstance(anchor_memory, str):
        return []

    entries, _ = _load_scope_id_entries(scope_user_id, resolution, config)
    matches: list[str] = []
    for entry in entries:
        if entry.get("memory") != anchor_memory:
            continue
        metadata = entry.get("metadata")
        if anchor_source_file is not None and (
            not isinstance(metadata, dict) or metadata.get("source_file") != anchor_source_file
        ):
            continue
        matches.append(entry["id"])
    return matches if len(matches) == 1 else []


def _find_file_anchor_memory_result(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> MemoryResult | None:
    for scope_user_id, target_storage_path in allowed_scope_storage_paths(
        caller_context,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ):
        resolution = resolve_file_memory_resolution(
            target_storage_path,
            config,
            runtime_paths,
            agent_name=agent_name_from_scope_user_id(scope_user_id),
            original_storage_path=storage_path,
            execution_identity=execution_identity,
        )
        if result := _get_scope_memory_by_id(scope_user_id, memory_id, resolution, config):
            return result
    return None


def _file_mutation_target_ids(
    scope_user_id: str,
    memory_id: str,
    anchor_result: MemoryResult,
    resolution: FileMemoryResolution,
    config: Config,
) -> list[str]:
    if _get_scope_memory_by_id(scope_user_id, memory_id, resolution, config) is not None:
        return [memory_id]
    return _find_file_replica_memory_ids(
        scope_user_id=scope_user_id,
        anchor_result=anchor_result,
        resolution=resolution,
        config=config,
    )


def _mutate_file_memory_targets(
    *,
    memory_id: str,
    content: str | None,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    anchor_result: MemoryResult,
    execution_identity: ToolExecutionIdentity | None = None,
) -> tuple[str, int, list[FileMemoryResolution]]:
    updated_targets = 0
    updated_resolutions: list[FileMemoryResolution] = []
    scope_user_id = anchor_result["user_id"]
    for target_storage_path in storage_paths_for_scope_user_id(
        scope_user_id,
        storage_path,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    ):
        resolution = resolve_file_memory_resolution(
            target_storage_path,
            config,
            runtime_paths,
            agent_name=agent_name_from_scope_user_id(scope_user_id),
            original_storage_path=storage_path,
            execution_identity=execution_identity,
        )
        for target_id in dict.fromkeys(
            _file_mutation_target_ids(scope_user_id, memory_id, anchor_result, resolution, config),
        ):
            if _replace_scope_memory_entry(scope_user_id, target_id, content, resolution, config):
                updated_targets += 1
                updated_resolutions.append(resolution)
    return scope_user_id, updated_targets, updated_resolutions


def append_agent_daily_file_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    *,
    preserve_resolved_storage_path: bool = False,
) -> MemoryResult:
    """Append one memory entry to the agent's daily file."""
    resolution = resolve_file_memory_resolution(
        storage_path,
        config,
        runtime_paths,
        agent_name=agent_name,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
        execution_identity=execution_identity,
    )
    current_date = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    daily_relative_path = f"{FILE_MEMORY_DAILY_DIR}/{current_date}.md"
    scope_user_id = agent_scope_user_id(agent_name)
    result = _append_scope_memory_entry(
        scope_user_id,
        content,
        resolution,
        config,
        target_relative_path=daily_relative_path,
    )
    _schedule_agent_semantic_refresh(
        agent_name,
        scope_user_id,
        resolution,
        config,
        runtime_paths,
        execution_identity=execution_identity,
    )
    logger.info("File daily memory added", agent=agent_name, date=current_date)
    return result


@timed("system_prompt_assembly.memory_search.file.agent_scope")
def _search_agent_file_scope_memories(
    query: str,
    agent_name: str,
    resolution: FileMemoryResolution,
    config: Config,
    limit: int,
) -> list[MemoryResult]:
    return _search_scope_memory_entries(
        agent_scope_user_id(agent_name),
        query,
        resolution,
        config,
        limit=limit,
    )


@timed("system_prompt_assembly.memory_search.file.team_scope")
def _search_team_file_scope_memories(
    team_id: str,
    query: str,
    resolution: FileMemoryResolution,
    config: Config,
    limit: int,
) -> list[MemoryResult]:
    return _search_scope_memory_entries(
        team_id,
        query,
        resolution,
        config,
        limit=limit,
    )


def _merge_team_scope_results(
    results: list[MemoryResult],
    *,
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    limit: int,
    execution_identity: ToolExecutionIdentity | None,
) -> list[MemoryResult]:
    """Merge keyword team-scope matches into one ranked result list (filesystem-bound)."""
    existing_memories = {result.get("memory", "") for result in results}
    for team_id in get_team_ids_for_agent(agent_name, config):
        for target_storage_path in storage_paths_for_scope_user_id(
            team_id,
            storage_path,
            config,
            runtime_paths,
            execution_identity=execution_identity,
        ):
            team_resolution = resolve_file_memory_resolution(
                target_storage_path,
                config,
                runtime_paths,
                original_storage_path=storage_path,
                execution_identity=execution_identity,
            )
            for memory in _search_team_file_scope_memories(
                team_id,
                query,
                team_resolution,
                config,
                limit,
            ):
                memory_text = memory.get("memory", "")
                if memory_text in existing_memories:
                    continue
                existing_memories.add(memory_text)
                _tag_keyword_mode(memory)
                results.append(memory)
    results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    return results[:limit]


@dataclass(frozen=True)
class FileMemoryBackend:
    """File-backed adapter implementing the shared memory backend surface."""

    runtime_paths: RuntimePaths
    context_label: ClassVar[str] = "agent file"

    async def add(
        self,
        content: str,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        metadata: dict | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Append one file-backed memory for an agent scope."""
        del metadata  # File memory entries persist plain text only.
        resolution = await asyncio.to_thread(
            resolve_file_memory_resolution,
            storage_path,
            config,
            self.runtime_paths,
            agent_name=agent_name,
            execution_identity=execution_identity,
        )
        scope_user_id = agent_scope_user_id(agent_name)
        await asyncio.to_thread(_append_scope_memory_entry, scope_user_id, content, resolution, config)
        _schedule_agent_semantic_refresh(
            agent_name,
            scope_user_id,
            resolution,
            config,
            self.runtime_paths,
            execution_identity=execution_identity,
        )
        logger.info("File memory added", agent=agent_name)

    @timed("system_prompt_assembly.memory_search.file_backend")
    async def search(
        self,
        query: str,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        limit: int,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemorySearchOutcome:
        """Search file-backed memories visible to an agent.

        Keyword scans read and score every memory file in the scope, so all
        file-reading paths run in worker threads (#1260); only the semantic
        index query stays natively async. A semantic-path failure falls back
        to keyword matches and surfaces its classified cause in the outcome.
        """
        agent_resolution = await asyncio.to_thread(
            resolve_file_memory_resolution,
            storage_path,
            config,
            self.runtime_paths,
            agent_name=agent_name,
            execution_identity=execution_identity,
        )

        def keyword_results() -> list[MemoryResult]:
            results = _search_agent_file_scope_memories(
                query,
                agent_name,
                agent_resolution,
                config,
                limit,
            )
            for result in results:
                _tag_keyword_mode(result)
            return results

        degraded_reason: str | None = None
        search_config = config.resolve_entity(agent_name).memory_search
        if search_config.mode == "semantic":
            scope_user_id = agent_scope_user_id(agent_name)
            try:
                results = await search_semantic_file_memories(
                    query,
                    scope_user_id=scope_user_id,
                    root=_scope_dir(scope_user_id, agent_resolution, config, create=False),
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
                results = await asyncio.to_thread(keyword_results)
            except Exception as exc:
                degraded_reason = (
                    classified_embedder_error(exc) or f"semantic memory search failed ({type(exc).__name__})"
                )
                logger.exception(
                    "File-memory semantic search failed; falling back to keyword search",
                    agent=agent_name,
                )
                results = await asyncio.to_thread(keyword_results)
        else:
            results = await asyncio.to_thread(keyword_results)

        merged = await asyncio.to_thread(
            _merge_team_scope_results,
            results,
            query=query,
            agent_name=agent_name,
            storage_path=storage_path,
            config=config,
            runtime_paths=self.runtime_paths,
            limit=limit,
            execution_identity=execution_identity,
        )
        return MemorySearchOutcome(results=merged, degraded_reason=degraded_reason)

    async def list_all(
        self,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        limit: int,
        preserve_resolved_storage_path: bool = False,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> list[MemoryResult]:
        """List file-backed memories stored for an agent."""
        resolution = await asyncio.to_thread(
            resolve_file_memory_resolution,
            storage_path,
            config,
            self.runtime_paths,
            agent_name=agent_name,
            preserve_resolved_storage_path=preserve_resolved_storage_path,
            execution_identity=execution_identity,
        )
        results, _ = await asyncio.to_thread(
            _load_scope_id_entries,
            agent_scope_user_id(agent_name),
            resolution,
            config,
        )
        if len(results) >= limit:
            return results[:limit]

        existing_memory_text = {
            _normalize_memory_text_for_dedup(memory_text) for entry in results if (memory_text := entry.get("memory"))
        }
        unstructured_results = await asyncio.to_thread(
            _load_scope_unstructured_entries,
            agent_scope_user_id(agent_name),
            resolution,
            config,
            existing_memory_text,
        )
        return (results + unstructured_results)[:limit]

    async def get(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryResult | None:
        """Return one file-backed memory visible to the caller."""
        return await asyncio.to_thread(
            _find_file_anchor_memory_result,
            memory_id,
            caller_context,
            storage_path,
            config,
            self.runtime_paths,
            execution_identity=execution_identity,
        )

    async def update(
        self,
        memory_id: str,
        content: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Update one file-backed memory across its replica targets."""
        if (
            anchor_result := await asyncio.to_thread(
                _find_file_anchor_memory_result,
                memory_id,
                caller_context,
                storage_path,
                config,
                self.runtime_paths,
                execution_identity=execution_identity,
            )
        ) is None:
            raise MemoryNotFoundError(memory_id)

        scope_user_id, updated_targets, updated_resolutions = await asyncio.to_thread(
            _mutate_file_memory_targets,
            memory_id=memory_id,
            content=content,
            storage_path=storage_path,
            config=config,
            runtime_paths=self.runtime_paths,
            anchor_result=anchor_result,
            execution_identity=execution_identity,
        )
        if updated_targets > 0:
            for resolution in updated_resolutions:
                _schedule_scope_semantic_refresh(
                    scope_user_id,
                    resolution,
                    config,
                    self.runtime_paths,
                    execution_identity=execution_identity,
                )
            logger.info(
                "File memory updated",
                memory_id=memory_id,
                scope=scope_user_id,
                storage_targets=updated_targets,
            )
            return
        raise MemoryNotFoundError(memory_id)

    async def delete(
        self,
        memory_id: str,
        caller_context: str | list[str],
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Delete one file-backed memory across its replica targets."""
        if (
            anchor_result := await asyncio.to_thread(
                _find_file_anchor_memory_result,
                memory_id,
                caller_context,
                storage_path,
                config,
                self.runtime_paths,
                execution_identity=execution_identity,
            )
        ) is None:
            raise MemoryNotFoundError(memory_id)

        scope_user_id, deleted_targets, deleted_resolutions = await asyncio.to_thread(
            _mutate_file_memory_targets,
            memory_id=memory_id,
            content=None,
            storage_path=storage_path,
            config=config,
            runtime_paths=self.runtime_paths,
            anchor_result=anchor_result,
            execution_identity=execution_identity,
        )
        if deleted_targets > 0:
            for resolution in deleted_resolutions:
                _schedule_scope_semantic_refresh(
                    scope_user_id,
                    resolution,
                    config,
                    self.runtime_paths,
                    execution_identity=execution_identity,
                )
            logger.info(
                "File memory deleted",
                memory_id=memory_id,
                scope=scope_user_id,
                storage_targets=deleted_targets,
            )
            return
        raise MemoryNotFoundError(memory_id)

    async def store_conversation(
        self,
        prompt: str,
        agent_name: str | list[str],
        storage_path: Path,
        session_id: str,
        config: Config,
        *,
        thread_history: Sequence[ResolvedVisibleMessage] | None = None,
        user_id: str | None = None,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Persist condensed conversation text to file-backed memory scopes."""
        del session_id, thread_history, user_id  # File conversation memory stores condensed text only.
        condensed_prompt = " ".join(prompt.strip().split())
        if not condensed_prompt:
            return

        target_storage_paths = await asyncio.to_thread(
            effective_storage_paths_for_context,
            agent_name,
            storage_path,
            config,
            self.runtime_paths,
            execution_identity=execution_identity,
        )
        scope_user_id = (
            agent_scope_user_id(agent_name) if isinstance(agent_name, str) else build_team_user_id(agent_name)
        )
        team_memory_id = new_memory_id() if isinstance(agent_name, list) else None

        def _persist_to_targets() -> list[FileMemoryResolution]:
            resolutions: list[FileMemoryResolution] = []
            for target_storage_path in target_storage_paths:
                resolution = resolve_file_memory_resolution(
                    target_storage_path,
                    config,
                    self.runtime_paths,
                    agent_name=agent_name_from_scope_user_id(scope_user_id),
                    original_storage_path=storage_path,
                    execution_identity=execution_identity,
                )
                _append_scope_memory_entry(
                    scope_user_id,
                    condensed_prompt,
                    resolution,
                    config,
                    memory_id=team_memory_id,
                )
                resolutions.append(resolution)
            return resolutions

        resolutions = await asyncio.to_thread(_persist_to_targets)
        if isinstance(agent_name, str):
            # Semantic refresh scheduling creates loop tasks, so it stays on the loop.
            for resolution in resolutions:
                _schedule_agent_semantic_refresh(
                    agent_name,
                    scope_user_id,
                    resolution,
                    config,
                    self.runtime_paths,
                    execution_identity=execution_identity,
                )

        if isinstance(agent_name, list):
            logger.info(
                "File team memory added",
                team_id=scope_user_id,
                members=agent_name,
                storage_targets=len(target_storage_paths),
            )
        else:
            logger.info("File memory added", agent=agent_name)

    @timed("system_prompt_assembly.memory_file_entrypoint_load")
    def load_entrypoint_context(
        self,
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> MemoryEntrypointContext:
        """Load the stable scoped `MEMORY.md` entrypoint context for one agent."""
        resolution = resolve_file_memory_resolution(
            storage_path,
            config,
            self.runtime_paths,
            agent_name=agent_name,
            execution_identity=execution_identity,
        )
        return _load_scope_entrypoint_context(
            agent_scope_user_id(agent_name),
            resolution,
            config,
        )
