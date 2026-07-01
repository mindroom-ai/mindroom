"""File-backed memory implementation."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, cast
from zoneinfo import ZoneInfo

from mindroom.constants import resolve_config_relative_path
from mindroom.logging_config import get_logger
from mindroom.timing import timed

from ._policy import (
    agent_name_from_scope_user_id,
    agent_scope_user_id,
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
    MemoryNotFoundError,
    MemoryResult,
    new_memory_id,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)


@dataclass(frozen=True)
class _PathMemoryLine:
    target_path: Path
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


def _scope_daily_memory_dir(scope_path: Path) -> Path:
    return scope_path / FILE_MEMORY_DAILY_DIR


def _resolve_scope_markdown_path(scope_path: Path, relative_path: str) -> Path | None:
    candidate = (scope_path / relative_path).resolve()
    resolved_scope = scope_path.resolve()
    try:
        candidate.relative_to(resolved_scope)
    except ValueError:
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


def _scope_markdown_files(scope_path: Path) -> list[Path]:
    files: list[Path] = []
    entrypoint_path = _scope_entrypoint_path(scope_path)
    if entrypoint_path.is_file():
        files.append(entrypoint_path)
    daily_dir = _scope_daily_memory_dir(scope_path)
    if daily_dir.is_dir():
        files.extend(
            sorted(
                (path for path in daily_dir.rglob("*.md") if path.is_file()),
                key=lambda path: path.relative_to(scope_path).as_posix(),
            ),
        )
    return files


def _is_unstructured_memory_line(snippet: str) -> bool:
    return bool(snippet) and not snippet.startswith("#") and FILE_MEMORY_ENTRY_PATTERN.match(snippet) is None


def _normalize_memory_text_for_dedup(text: str) -> str:
    return " ".join(text.lower().split())


def _load_scope_id_entries(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> tuple[list[MemoryResult], dict[str, Path]]:
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    if not scope_path.exists():
        return [], {}

    markdown_files = _scope_markdown_files(scope_path)
    entrypoint_path = _scope_entrypoint_path(scope_path)
    ordered_files = ([entrypoint_path] if entrypoint_path in markdown_files else []) + [
        path for path in markdown_files if path != entrypoint_path
    ]

    results: list[MemoryResult] = []
    id_to_file: dict[str, Path] = {}
    for file_path in ordered_files:
        relative_path = file_path.relative_to(scope_path).as_posix()
        for line_no, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
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
                "metadata": {"source_file": relative_path, "line": line_no},
            }
            results.append(result)
            id_to_file.setdefault(memory_id, file_path)

    return results, id_to_file


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
    entrypoint_path = _scope_entrypoint_path(scope_path)
    for file_path in _scope_markdown_files(scope_path):
        if file_path == entrypoint_path:
            continue
        relative_path = file_path.relative_to(scope_path).as_posix()
        for line_no, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
            snippet = raw_line.strip()
            if not _is_unstructured_memory_line(snippet):
                continue
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
) -> tuple[list[MemoryResult], dict[str, Path]]:
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
    search_config = config.get_agent_memory_search(agent_name)
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
        target_path = scope_path / FILE_MEMORY_ENTRYPOINT
        if not target_path.exists():
            target_path.write_text("# Memory\n\n", encoding="utf-8")
    else:
        target_path = _resolve_scope_markdown_path(scope_path, target_relative_path)
        if target_path is None:
            msg = f"Invalid markdown memory path: {target_relative_path}"
            raise ValueError(msg)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            target_path.touch()

    relative_path = target_path.relative_to(scope_path).as_posix()
    memory_id = memory_id or new_memory_id()
    line = _format_entry_line(memory_id, content)

    text = target_path.read_text(encoding="utf-8")
    needs_separator = bool(text) and not text.endswith("\n")
    separator = "\n" if needs_separator else ""
    target_path.write_text(f"{text}{separator}{line}\n", encoding="utf-8")

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
    entrypoint_path = _scope_entrypoint_path(scope_path)
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
        entrypoint_path,
        existing_memory_text,
    )

    snippet_results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    return scored_entries + snippet_results[:remaining_limit]


@timed("system_prompt_assembly.memory_search.file.snippet_scan")
def _scan_scope_memory_snippets(
    scope_user_id: str,
    query_tokens: set[str],
    scope_path: Path,
    entrypoint_path: Path,
    existing_memory_text: set[str],
) -> list[MemoryResult]:
    snippet_results: list[MemoryResult] = []
    for file_path in _scope_markdown_files(scope_path):
        if file_path == entrypoint_path:
            continue
        relative_path = file_path.relative_to(scope_path).as_posix()
        for line_no, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
            snippet = raw_line.strip()
            if not _is_unstructured_memory_line(snippet):
                continue
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
    target_path = _resolve_scope_markdown_path(scope_path, relative_path)
    if target_path is None or not target_path.exists():
        return None

    entrypoint_path = _scope_entrypoint_path(scope_path)
    eligible_paths = {path.resolve() for path in _scope_markdown_files(scope_path) if path != entrypoint_path}
    if target_path not in eligible_paths:
        return None

    lines = target_path.read_text(encoding="utf-8").splitlines()
    if line_no <= 0 or line_no > len(lines):
        return None

    raw_line = lines[line_no - 1]
    snippet = raw_line.strip()
    if not _is_unstructured_memory_line(snippet):
        return None
    return _PathMemoryLine(
        target_path=target_path,
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
    if (file_path := id_to_file.get(memory_id)) is None:
        return False

    lines = file_path.read_text(encoding="utf-8").splitlines()
    changed = False
    new_lines: list[str] = []
    for raw_line in lines:
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

    file_path.write_text(f"{'\n'.join(new_lines)}\n" if new_lines else "", encoding="utf-8")
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

    lines = list(path_memory_line.lines)

    if content is None or not content.strip():
        lines[path_memory_line.line_no - 1] = ""
    else:
        prefix_len = len(path_memory_line.raw_line) - len(path_memory_line.raw_line.lstrip(" "))
        lines[path_memory_line.line_no - 1] = (
            f"{path_memory_line.raw_line[:prefix_len]}{' '.join(content.strip().split())}"
        )
    path_memory_line.target_path.write_text(f"{'\n'.join(lines)}\n" if lines else "", encoding="utf-8")
    return True


@timed("system_prompt_assembly.memory_file_entrypoint_read")
def _load_scope_entrypoint_context(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> str:
    """Load the scoped `MEMORY.md` entrypoint text."""
    entrypoint_path = _scope_entrypoint_path(_scope_dir(scope_user_id, resolution, config, create=False))
    if not entrypoint_path.exists():
        return ""
    max_lines = config.memory.file.max_entrypoint_lines
    lines = entrypoint_path.read_text(encoding="utf-8").splitlines()
    if max_lines < len(lines):
        lines = lines[:max_lines]
    return "\n".join(lines).strip()


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
    ) -> list[MemoryResult]:
        """Search file-backed memories visible to an agent.

        Keyword scans read and score every memory file in the scope, so all
        file-reading paths run in worker threads (#1260); only the semantic
        index query stays natively async.
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

        search_config = config.get_agent_memory_search(agent_name)
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
            except SemanticFileMemoryIndexUnavailableError:
                logger.debug(
                    "File-memory semantic index unavailable; falling back to keyword search",
                    agent=agent_name,
                )
                results = await asyncio.to_thread(keyword_results)
            except Exception:
                logger.exception(
                    "File-memory semantic search failed; falling back to keyword search",
                    agent=agent_name,
                )
                results = await asyncio.to_thread(keyword_results)
        else:
            results = await asyncio.to_thread(keyword_results)

        return await asyncio.to_thread(
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
    ) -> str:
        """Load the stable scoped `MEMORY.md` entrypoint text for one agent."""
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
