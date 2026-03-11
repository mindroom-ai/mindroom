"""File-backed memory implementation."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from mindroom.constants import resolve_config_relative_path
from mindroom.logging_config import get_logger

from ._policy import (
    agent_name_from_scope_user_id,
    agent_scope_user_id,
    agent_uses_worker_scoped_memory,
    build_team_user_id,
    effective_storage_paths_for_context,
    file_memory_resolution_from_paths,
    get_allowed_memory_user_ids,
    get_team_ids_for_agent,
    mutation_target_storage_paths,
    resolve_file_memory_resolution,
    room_scope_user_id,
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
    from pathlib import Path

    from mindroom.config.main import Config

logger = get_logger(__name__)


def _file_memory_root(
    storage_path: Path,
    config: Config,
    *,
    use_configured_path: bool,
) -> Path:
    configured_path = config.memory.file.path if use_configured_path else None
    if configured_path:
        return resolve_config_relative_path(configured_path)
    return (storage_path.expanduser().resolve() / FILE_MEMORY_DEFAULT_DIRNAME).resolve()


def _scope_dir_name(scope_user_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._+-]+", "_", scope_user_id).strip("_") or "default"


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
    agent_name = agent_name_from_scope_user_id(scope_user_id)
    if agent_name is not None:
        agent_config = config.agents.get(agent_name)
        if (
            resolution.allow_agent_memory_file_path_override
            and agent_config is not None
            and agent_config.memory_file_path is not None
            and not agent_uses_worker_scoped_memory(agent_name, config)
        ):
            scope_path = resolve_config_relative_path(agent_config.memory_file_path)
            if create:
                scope_path.mkdir(parents=True, exist_ok=True)
            return scope_path

    scope_path = _file_memory_root(
        resolution.storage_path,
        config,
        use_configured_path=resolution.use_configured_path,
    ) / _scope_dir_name(scope_user_id)
    if create:
        scope_path.mkdir(parents=True, exist_ok=True)
    return scope_path


def _scope_markdown_files(scope_path: Path) -> list[Path]:
    return sorted(
        (path for path in scope_path.rglob("*.md") if path.is_file()),
        key=lambda path: path.relative_to(scope_path).as_posix(),
    )


def load_scope_id_entries(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> tuple[list[MemoryResult], dict[str, Path]]:
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    if not scope_path.exists():
        return [], {}

    markdown_files = _scope_markdown_files(scope_path)
    entrypoint_path = scope_path / FILE_MEMORY_ENTRYPOINT
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


def append_scope_memory_entry(
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


def search_scope_memory_entries(  # noqa: C901
    scope_user_id: str,
    query: str,
    resolution: FileMemoryResolution,
    config: Config,
    *,
    limit: int,
) -> list[MemoryResult]:
    id_entries, _ = load_scope_id_entries(scope_user_id, resolution, config)
    query_tokens = _extract_query_tokens(query)

    scored_entries: list[MemoryResult] = []
    seen_scored_text: set[str] = set()
    for entry in id_entries:
        text = entry.get("memory", "")
        normalized_text = text.strip().lower()
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
    entrypoint_path = scope_path / FILE_MEMORY_ENTRYPOINT
    snippet_results: list[MemoryResult] = []
    existing_memory_text = {
        memory_text for entry in scored_entries if (memory_text := entry.get("memory", "").strip().lower())
    }
    for file_path in _scope_markdown_files(scope_path):
        if file_path == entrypoint_path:
            continue
        relative_path = file_path.relative_to(scope_path).as_posix()
        for line_no, raw_line in enumerate(file_path.read_text(encoding="utf-8").splitlines(), 1):
            snippet = raw_line.strip()
            if not snippet or snippet.startswith("#"):
                continue
            if FILE_MEMORY_ENTRY_PATTERN.match(snippet):
                continue
            normalized_snippet = snippet.lower()
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

    snippet_results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    return scored_entries + snippet_results[:remaining_limit]


def _get_scope_memory_by_path_id(
    scope_user_id: str,
    memory_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> MemoryResult | None:
    match = FILE_MEMORY_PATH_ID_PATTERN.match(memory_id)
    if match is None:
        return None
    line_no = int(match.group("line"))
    relative_path = match.group("path")
    scope_path = _scope_dir(scope_user_id, resolution, config, create=False)
    target_path = _resolve_scope_markdown_path(scope_path, relative_path)
    if target_path is None or not target_path.exists():
        return None
    lines = target_path.read_text(encoding="utf-8").splitlines()
    if line_no <= 0 or line_no > len(lines):
        return None
    snippet = lines[line_no - 1].strip()
    if not snippet:
        return None
    return {
        "id": memory_id,
        "memory": snippet,
        "user_id": scope_user_id,
        "metadata": {"source_file": relative_path, "line": line_no},
    }


def get_scope_memory_by_id(
    scope_user_id: str,
    memory_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> MemoryResult | None:
    if path_result := _get_scope_memory_by_path_id(scope_user_id, memory_id, resolution, config):
        return path_result
    entries, _ = load_scope_id_entries(scope_user_id, resolution, config)
    for entry in entries:
        if entry["id"] == memory_id:
            return entry
    return None


def replace_scope_memory_entry(
    scope_user_id: str,
    memory_id: str,
    content: str | None,
    resolution: FileMemoryResolution,
    config: Config,
) -> bool:
    _entries, id_to_file = load_scope_id_entries(scope_user_id, resolution, config)
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


def load_scope_entrypoint_context(
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
) -> str:
    entrypoint_path = _scope_dir(scope_user_id, resolution, config, create=False) / FILE_MEMORY_ENTRYPOINT
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

    entries, _ = load_scope_id_entries(scope_user_id, resolution, config)
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
) -> MemoryResult | None:
    for target_storage_path in effective_storage_paths_for_context(caller_context, storage_path, config):
        resolution = file_memory_resolution_from_paths(
            original_storage_path=storage_path,
            resolved_storage_path=target_storage_path,
        )
        for scope_user_id in sorted(get_allowed_memory_user_ids(caller_context, config)):
            if result := get_scope_memory_by_id(scope_user_id, memory_id, resolution, config):
                return result
    return None


def _file_mutation_target_ids(
    scope_user_id: str,
    memory_id: str,
    anchor_result: MemoryResult,
    resolution: FileMemoryResolution,
    config: Config,
) -> list[str]:
    if get_scope_memory_by_id(scope_user_id, memory_id, resolution, config) is not None:
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
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
    anchor_result: MemoryResult,
) -> tuple[str, int]:
    updated_targets = 0
    scope_user_id = anchor_result["user_id"]
    for target_storage_path in mutation_target_storage_paths(scope_user_id, caller_context, storage_path, config):
        resolution = file_memory_resolution_from_paths(
            original_storage_path=storage_path,
            resolved_storage_path=target_storage_path,
        )
        for target_id in dict.fromkeys(
            _file_mutation_target_ids(scope_user_id, memory_id, anchor_result, resolution, config),
        ):
            if replace_scope_memory_entry(scope_user_id, target_id, content, resolution, config):
                updated_targets += 1
    return scope_user_id, updated_targets


def add_file_agent_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
) -> None:
    resolution = resolve_file_memory_resolution(storage_path, config, agent_name=agent_name)
    append_scope_memory_entry(agent_scope_user_id(agent_name), content, resolution, config)
    logger.info("File memory added", agent=agent_name)


def append_agent_daily_file_memory(
    content: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    preserve_resolved_storage_path: bool = False,
) -> MemoryResult:
    resolution = resolve_file_memory_resolution(
        storage_path,
        config,
        agent_name=agent_name,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
    )
    current_date = datetime.now(ZoneInfo(config.timezone)).date().isoformat()
    daily_relative_path = f"{FILE_MEMORY_DAILY_DIR}/{current_date}.md"
    result = append_scope_memory_entry(
        agent_scope_user_id(agent_name),
        content,
        resolution,
        config,
        target_relative_path=daily_relative_path,
    )
    logger.info("File daily memory added", agent=agent_name, date=current_date)
    return result


def search_file_agent_memories(
    query: str,
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    limit: int,
) -> list[MemoryResult]:
    resolution = resolve_file_memory_resolution(storage_path, config, agent_name=agent_name)
    results = search_scope_memory_entries(agent_scope_user_id(agent_name), query, resolution, config, limit=limit)
    existing_memories = {result.get("memory", "") for result in results}
    for team_id in get_team_ids_for_agent(agent_name, config):
        team_results = search_scope_memory_entries(team_id, query, resolution, config, limit=limit)
        for memory in team_results:
            memory_text = memory.get("memory", "")
            if memory_text in existing_memories:
                continue
            existing_memories.add(memory_text)
            results.append(memory)
    results.sort(key=lambda item: cast("float", item.get("score", 0.0)), reverse=True)
    return results[:limit]


def list_file_agent_memories(
    agent_name: str,
    storage_path: Path,
    config: Config,
    *,
    limit: int,
    preserve_resolved_storage_path: bool = False,
) -> list[MemoryResult]:
    resolution = resolve_file_memory_resolution(
        storage_path,
        config,
        agent_name=agent_name,
        preserve_resolved_storage_path=preserve_resolved_storage_path,
    )
    results, _ = load_scope_id_entries(agent_scope_user_id(agent_name), resolution, config)
    return results[:limit]


def get_file_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> MemoryResult | None:
    for target_storage_path in effective_storage_paths_for_context(caller_context, storage_path, config):
        resolution = file_memory_resolution_from_paths(
            original_storage_path=storage_path,
            resolved_storage_path=target_storage_path,
        )
        for scope_user_id in sorted(get_allowed_memory_user_ids(caller_context, config)):
            result = get_scope_memory_by_id(scope_user_id, memory_id, resolution, config)
            if result is not None:
                return result
    return None


def update_file_agent_memory(
    memory_id: str,
    content: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> None:
    if (anchor_result := _find_file_anchor_memory_result(memory_id, caller_context, storage_path, config)) is None:
        raise MemoryNotFoundError(memory_id)

    scope_user_id, updated_targets = _mutate_file_memory_targets(
        memory_id=memory_id,
        content=content,
        caller_context=caller_context,
        storage_path=storage_path,
        config=config,
        anchor_result=anchor_result,
    )
    if updated_targets > 0:
        logger.info(
            "File memory updated",
            memory_id=memory_id,
            scope=scope_user_id,
            storage_targets=updated_targets,
        )
        return
    raise MemoryNotFoundError(memory_id)


def delete_file_agent_memory(
    memory_id: str,
    caller_context: str | list[str],
    storage_path: Path,
    config: Config,
) -> None:
    if (anchor_result := _find_file_anchor_memory_result(memory_id, caller_context, storage_path, config)) is None:
        raise MemoryNotFoundError(memory_id)

    scope_user_id, deleted_targets = _mutate_file_memory_targets(
        memory_id=memory_id,
        content=None,
        caller_context=caller_context,
        storage_path=storage_path,
        config=config,
        anchor_result=anchor_result,
    )
    if deleted_targets > 0:
        logger.info(
            "File memory deleted",
            memory_id=memory_id,
            scope=scope_user_id,
            storage_targets=deleted_targets,
        )
        return
    raise MemoryNotFoundError(memory_id)


def add_file_room_memory(
    content: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    *,
    agent_name: str | None,
) -> None:
    resolution = resolve_file_memory_resolution(storage_path, config, agent_name=agent_name)
    append_scope_memory_entry(room_scope_user_id(room_id), content, resolution, config)
    logger.debug("File room memory added", room_id=room_id)


def search_file_room_memories(
    query: str,
    room_id: str,
    storage_path: Path,
    config: Config,
    *,
    agent_name: str | None,
    limit: int,
) -> list[MemoryResult]:
    resolution = resolve_file_memory_resolution(storage_path, config, agent_name=agent_name)
    return search_scope_memory_entries(room_scope_user_id(room_id), query, resolution, config, limit=limit)


def store_file_conversation_memory(
    prompt: str,
    agent_name: str | list[str],
    storage_path: Path,
    config: Config,
    room_id: str | None,
) -> None:
    condensed_prompt = " ".join(prompt.strip().split())
    if not condensed_prompt:
        return

    target_storage_paths = effective_storage_paths_for_context(agent_name, storage_path, config)
    scope_user_id = agent_scope_user_id(agent_name) if isinstance(agent_name, str) else build_team_user_id(agent_name)
    team_memory_id = new_memory_id() if isinstance(agent_name, list) else None
    room_user_id = room_scope_user_id(room_id) if room_id else None

    for target_storage_path in target_storage_paths:
        resolution = file_memory_resolution_from_paths(
            original_storage_path=storage_path,
            resolved_storage_path=target_storage_path,
        )
        append_scope_memory_entry(
            scope_user_id,
            condensed_prompt,
            resolution,
            config,
            memory_id=team_memory_id,
        )
        if room_user_id is not None:
            append_scope_memory_entry(
                room_user_id,
                condensed_prompt,
                resolution,
                config,
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

    if room_id:
        logger.debug("File room memory added", room_id=room_id, storage_targets=len(target_storage_paths))
