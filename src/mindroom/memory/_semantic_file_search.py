"""Semantic search for file-backed memory roots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agno.knowledge.knowledge import Knowledge
from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader
from agno.vectordb.chroma import ChromaDb

from mindroom.chunking import SafeFixedSizeChunking
from mindroom.embedding_factory import create_configured_embedder
from mindroom.logging_config import get_logger
from mindroom.path_globs import matches_root_glob

if TYPE_CHECKING:
    from agno.knowledge.document.base import Document
    from agno.knowledge.reader.base import Reader

    from mindroom.config.main import Config
    from mindroom.config.memory import MemorySearchConfig
    from mindroom.constants import RuntimePaths
    from mindroom.memory._shared import MemoryResult

logger = get_logger(__name__)
_COLLECTION_PREFIX = "mindroom_memory"
_SOURCE_PATH_KEY = "source_path"
_CHUNK_SIZE = 5000
_CHUNK_OVERLAP = 0


@dataclass(frozen=True)
class _IndexedFile:
    path: Path
    relative_path: str
    mtime_ns: int
    size: int


def _safe_identifier(value: str) -> str:
    sanitized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return sanitized or "default"


def _path_is_symlink_or_under_symlink(root: Path, path: Path) -> bool:
    try:
        relative_path = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _include_relative_path(relative_path: str, search_config: MemorySearchConfig) -> bool:
    if relative_path == "MEMORY.md":
        return search_config.include_entrypoint
    return any(matches_root_glob(relative_path, pattern) for pattern in search_config.include)


def _list_indexed_files(root: Path, search_config: MemorySearchConfig) -> list[_IndexedFile]:
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    files: list[_IndexedFile] = []
    for dirpath, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        current_dir = Path(dirpath)
        dirnames[:] = [dirname for dirname in dirnames if not (current_dir / dirname).is_symlink()]
        for filename in filenames:
            path = current_dir / filename
            if path.suffix.lower() != ".md" or _path_is_symlink_or_under_symlink(resolved_root, path):
                continue
            try:
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(resolved_root)
                relative_path = resolved_path.relative_to(resolved_root).as_posix()
                if not _include_relative_path(relative_path, search_config):
                    continue
                stat = resolved_path.stat()
                files.append(
                    _IndexedFile(
                        path=resolved_path,
                        relative_path=relative_path,
                        mtime_ns=stat.st_mtime_ns,
                        size=stat.st_size,
                    ),
                )
            except (OSError, ValueError):
                continue
    return sorted(files, key=lambda item: item.relative_path)


def _settings_signature(config: Config, search_config: MemorySearchConfig, root: Path) -> str:
    embedder_config = config.memory.embedder.config
    payload = repr(
        (
            str(root.resolve()),
            config.memory.embedder.provider,
            embedder_config.model,
            embedder_config.host,
            embedder_config.dimensions,
            tuple(search_config.include),
            search_config.include_entrypoint,
        ),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scope_digest(root: Path, scope_user_id: str) -> str:
    return hashlib.sha256(f"{scope_user_id}:{root.resolve()}".encode()).hexdigest()[:16]


def _index_storage_path(runtime_paths: RuntimePaths, root: Path, scope_user_id: str) -> Path:
    name = f"{_safe_identifier(scope_user_id)}_{_scope_digest(root, scope_user_id)}"
    return runtime_paths.storage_root / "memory_search_db" / name


def _collection_name(root: Path, scope_user_id: str) -> str:
    return f"{_COLLECTION_PREFIX}_{_safe_identifier(scope_user_id)}_{_scope_digest(root, scope_user_id)}"


def _state_path(index_path: Path) -> Path:
    return index_path / "index_state.json"


def _file_state(files: list[_IndexedFile]) -> dict[str, dict[str, int]]:
    return {file.relative_path: {"mtime_ns": file.mtime_ns, "size": file.size} for file in files}


def _load_state(index_path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(_state_path(index_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_state(
    index_path: Path,
    *,
    settings_signature: str,
    collection_name: str,
    files: list[_IndexedFile],
) -> None:
    _state_path(index_path).write_text(
        json.dumps(
            {
                "settings_signature": settings_signature,
                "collection": collection_name,
                "files": _file_state(files),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _build_reader(file_path: Path) -> Reader:
    reader = ReaderFactory.get_reader_for_extension(file_path.suffix.lower())
    if not isinstance(reader, (TextReader, MarkdownReader)):
        return reader
    configured_reader = deepcopy(reader)
    configured_reader.chunk = True
    configured_reader.chunk_size = _CHUNK_SIZE
    configured_reader.chunking_strategy = SafeFixedSizeChunking(chunk_size=_CHUNK_SIZE, overlap=_CHUNK_OVERLAP)
    return configured_reader


def _indexed_file_changed(saved: object, current: _IndexedFile) -> bool:
    if not isinstance(saved, dict):
        return True
    saved_file = cast("dict[str, object]", saved)
    return saved_file.get("mtime_ns") != current.mtime_ns or saved_file.get("size") != current.size


def _vector_db(knowledge: Knowledge) -> ChromaDb:
    return cast("ChromaDb", knowledge.vector_db)


def _reset_collection(knowledge: Knowledge) -> None:
    vector_db = _vector_db(knowledge)
    if vector_db.exists():
        vector_db.delete()
    vector_db.create()


def _insert_file(knowledge: Knowledge, indexed_file: _IndexedFile) -> None:
    knowledge.insert(
        path=str(indexed_file.path),
        metadata={_SOURCE_PATH_KEY: indexed_file.relative_path},
        upsert=True,
        reader=_build_reader(indexed_file.path),
    )


def _ensure_index_current(
    knowledge: Knowledge,
    files: list[_IndexedFile],
    index_path: Path,
    collection_name: str,
    settings_signature: str,
) -> None:
    state = _load_state(index_path)
    saved_files = state.get("files") if isinstance(state, dict) else None
    needs_reset = (
        not _vector_db(knowledge).exists()
        or state is None
        or state.get("settings_signature") != settings_signature
        or state.get("collection") != collection_name
        or not isinstance(saved_files, dict)
    )

    if needs_reset:
        _reset_collection(knowledge)
        for indexed_file in files:
            _insert_file(knowledge, indexed_file)
    else:
        current_by_path = {file.relative_path: file for file in files}
        saved_by_path = cast("dict[str, object]", saved_files)
        for relative_path in sorted(set(saved_by_path) - set(current_by_path)):
            knowledge.remove_vectors_by_metadata({_SOURCE_PATH_KEY: relative_path})
        for relative_path, indexed_file in current_by_path.items():
            if _indexed_file_changed(saved_by_path.get(relative_path), indexed_file):
                knowledge.remove_vectors_by_metadata({_SOURCE_PATH_KEY: relative_path})
                _insert_file(knowledge, indexed_file)

    _write_state(index_path, settings_signature=settings_signature, collection_name=collection_name, files=files)


async def search_semantic_file_memories(
    query: str,
    *,
    scope_user_id: str,
    root: Path,
    config: Config,
    runtime_paths: RuntimePaths,
    search_config: MemorySearchConfig,
    limit: int,
) -> list[MemoryResult]:
    """Search one file-memory scope with an embedding-backed index."""
    index_path = _index_storage_path(runtime_paths, root, scope_user_id)
    index_path.mkdir(parents=True, exist_ok=True)
    collection_name = _collection_name(root, scope_user_id)
    files = await asyncio.to_thread(_list_indexed_files, root, search_config)
    if not files:
        return []

    knowledge = Knowledge(
        vector_db=ChromaDb(
            collection=collection_name,
            path=str(index_path),
            persistent_client=True,
            embedder=create_configured_embedder(config, runtime_paths),
        ),
    )
    await asyncio.to_thread(
        _ensure_index_current,
        knowledge,
        files,
        index_path,
        collection_name,
        _settings_signature(config, search_config, root),
    )
    documents: list[Document] = await asyncio.to_thread(knowledge.search, query=query, max_results=limit)
    results: list[MemoryResult] = []
    for rank, document in enumerate(documents, start=1):
        metadata = dict(document.meta_data)
        source_file = metadata.get(_SOURCE_PATH_KEY)
        if not isinstance(source_file, str):
            source_file = "memory"
        content = " ".join(document.content.split())
        if not content:
            continue
        score = document.reranking_score
        results.append(
            cast(
                "MemoryResult",
                {
                    "id": f"semantic:{source_file}:{rank}",
                    "memory": content,
                    "user_id": scope_user_id,
                    "score": float(score) if score is not None else 1.0 - (rank * 0.000001),
                    "metadata": {"source_file": source_file, "semantic": True, "search_mode": "semantic"},
                },
            ),
        )
    return results
