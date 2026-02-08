"""Knowledge base management for file-backed RAG."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agno.knowledge.embedder.ollama import OllamaEmbedder
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.chroma import ChromaDb
from watchfiles import Change, awatch

from .credentials_sync import get_api_key_for_provider, get_ollama_host
from .logging_config import get_logger

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder

    from .config import Config, KnowledgeBaseConfig

logger = get_logger(__name__)

_COLLECTION_PREFIX = "mindroom_knowledge"


def _resolve_knowledge_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _safe_identifier(value: str) -> str:
    sanitized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return sanitized or "default"


def _base_storage_key(base_id: str) -> str:
    digest = hashlib.sha256(base_id.encode("utf-8")).hexdigest()[:8]
    return f"{_safe_identifier(base_id)}_{digest}"


def _collection_name(base_id: str) -> str:
    return f"{_COLLECTION_PREFIX}_{_base_storage_key(base_id)}"


def _knowledge_base_config(config: Config, base_id: str) -> KnowledgeBaseConfig:
    if base_id not in config.knowledge_bases:
        msg = f"Knowledge base '{base_id}' is not configured"
        raise ValueError(msg)
    return config.knowledge_bases[base_id]


def _settings_key(config: Config, storage_path: Path, base_id: str) -> tuple[str, ...]:
    embedder_config = config.memory.embedder.config
    base_config = _knowledge_base_config(config, base_id)
    knowledge_path = _resolve_knowledge_path(base_config.path)
    return (
        base_id,
        str(storage_path.resolve()),
        str(knowledge_path),
        config.memory.embedder.provider,
        embedder_config.model,
        embedder_config.host or "",
        str(base_config.watch),
    )


def _create_embedder(config: Config) -> Embedder:
    provider = config.memory.embedder.provider
    embedder_config = config.memory.embedder.config

    if provider == "openai":
        return OpenAIEmbedder(
            id=embedder_config.model,
            api_key=get_api_key_for_provider("openai"),
            base_url=embedder_config.host,
        )

    if provider == "ollama":
        host = get_ollama_host() or embedder_config.host or "http://localhost:11434"
        return OllamaEmbedder(id=embedder_config.model, host=host)

    msg = f"Unsupported knowledge embedder provider: {provider}. Supported providers: openai, ollama"
    raise ValueError(msg)


@dataclass
class KnowledgeManager:
    """Manage indexing and watching for one knowledge base folder."""

    base_id: str
    config: Config
    storage_path: Path

    knowledge_path: Path = field(init=False)
    _settings: tuple[str, ...] = field(init=False)
    _knowledge: Knowledge = field(init=False)
    _indexed_files: set[str] = field(default_factory=set, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _watch_task: asyncio.Task[None] | None = field(default=None, init=False)
    _watch_stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        """Initialize filesystem paths and the underlying vector database."""
        base_config = _knowledge_base_config(self.config, self.base_id)
        self.knowledge_path = _resolve_knowledge_path(base_config.path)
        self.knowledge_path.mkdir(parents=True, exist_ok=True)
        self._settings = _settings_key(self.config, self.storage_path, self.base_id)

        vector_db = ChromaDb(
            collection=_collection_name(self.base_id),
            path=str((self.storage_path / "knowledge_db" / _base_storage_key(self.base_id)).resolve()),
            persistent_client=True,
            embedder=_create_embedder(self.config),
        )
        self._knowledge = Knowledge(vector_db=vector_db)

    def matches(self, config: Config, storage_path: Path) -> bool:
        """Return True when manager settings match the provided config."""
        return self._settings == _settings_key(config, storage_path, self.base_id)

    def get_knowledge(self) -> Knowledge:
        """Return the agno Knowledge instance."""
        return self._knowledge

    def list_files(self) -> list[Path]:
        """List all files currently present in the knowledge folder."""
        if not self.knowledge_path.exists():
            return []
        return sorted(path for path in self.knowledge_path.rglob("*") if path.is_file())

    def resolve_file_path(self, file_path: Path | str) -> Path:
        """Resolve a path and ensure it stays inside the knowledge folder."""
        candidate = Path(file_path)
        resolved = (
            candidate.expanduser().resolve() if candidate.is_absolute() else (self.knowledge_path / candidate).resolve()
        )

        try:
            resolved.relative_to(self.knowledge_path)
        except ValueError as exc:
            msg = f"Path {resolved} is outside knowledge folder {self.knowledge_path}"
            raise ValueError(msg) from exc

        return resolved

    def _relative_path(self, file_path: Path) -> str:
        return file_path.relative_to(self.knowledge_path).as_posix()

    def _reset_collection(self) -> None:
        if self._knowledge.vector_db is None:
            return
        self._knowledge.vector_db.delete()
        self._knowledge.vector_db.create()

    def _load_indexed_files_from_vector_db(self) -> set[str]:
        """Load unique source paths currently present in the vector collection."""
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return set()
        if not vector_db.exists():
            return set()

        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        total_count = collection.count()
        if total_count == 0:
            return set()

        indexed_files: set[str] = set()
        offset = 0
        batch_size = 1_000

        while offset < total_count:
            result = collection.get(
                limit=batch_size,
                offset=offset,
                include=["metadatas"],
            )

            metadatas = result.get("metadatas", []) or []
            for metadata in metadatas:
                if not isinstance(metadata, dict):
                    continue
                source_path = metadata.get("source_path")
                if isinstance(source_path, str) and source_path:
                    indexed_files.add(source_path)

            ids = result.get("ids", []) or []
            fetched_count = len(ids)
            if fetched_count == 0:
                break
            offset += fetched_count

        return indexed_files

    async def initialize(self) -> None:
        """Initialize and index all existing knowledge files."""
        indexed_count = await self.reindex_all()
        logger.info(
            "Knowledge base initialized",
            base_id=self.base_id,
            indexed_count=indexed_count,
            path=str(self.knowledge_path),
        )

    async def load_indexed_files(self) -> int:
        """Load in-memory indexed file state from the existing vector DB collection."""
        indexed_files = await asyncio.to_thread(self._load_indexed_files_from_vector_db)
        async with self._lock:
            self._indexed_files = indexed_files
        return len(indexed_files)

    async def start_watcher(self) -> None:
        """Start background file watching if enabled."""
        base_config = _knowledge_base_config(self.config, self.base_id)
        if not base_config.watch:
            return
        if self._watch_task is not None and not self._watch_task.done():
            return

        self._watch_stop_event = asyncio.Event()
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info("Knowledge folder watcher started", base_id=self.base_id, path=str(self.knowledge_path))

    async def stop_watcher(self) -> None:
        """Stop the background file watcher."""
        if self._watch_task is None:
            return

        self._watch_stop_event.set()
        self._watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._watch_task

        self._watch_task = None
        logger.info("Knowledge folder watcher stopped", base_id=self.base_id)

    async def _index_file_locked(self, resolved_path: Path, *, upsert: bool) -> bool:
        """Index one file while holding the manager lock."""
        relative_path = self._relative_path(resolved_path)
        metadata = {"source_path": relative_path}

        try:
            if upsert:
                # Agno/Chroma upsert keys by content hash, so stale chunks from an older
                # version of the same file can remain unless we clear by source metadata first.
                await asyncio.to_thread(self._knowledge.remove_vectors_by_metadata, metadata)
            await asyncio.to_thread(
                self._knowledge.insert,
                path=str(resolved_path),
                metadata=metadata,
                upsert=upsert,
            )
        except Exception:
            logger.exception("Failed to index knowledge file", base_id=self.base_id, path=str(resolved_path))
            return False

        self._indexed_files.add(relative_path)
        logger.info("Indexed knowledge file", base_id=self.base_id, path=relative_path)
        return True

    async def reindex_all(self) -> int:
        """Clear and rebuild the knowledge index from disk."""
        files = self.list_files()

        async with self._lock:
            await asyncio.to_thread(self._reset_collection)
            self._indexed_files.clear()
            for file_path in files:
                await self._index_file_locked(file_path, upsert=True)
            return len(self._indexed_files)

    async def index_file(self, file_path: Path | str, *, upsert: bool = True) -> bool:
        """Index or reindex a single file."""
        resolved_path = self.resolve_file_path(file_path)
        if not resolved_path.exists() or not resolved_path.is_file():
            return False

        async with self._lock:
            return await self._index_file_locked(resolved_path, upsert=upsert)

    async def remove_file(self, file_path: Path | str) -> bool:
        """Remove a file from the vector database index."""
        resolved_path = self.resolve_file_path(file_path)
        relative_path = self._relative_path(resolved_path)

        async with self._lock:
            removed = await asyncio.to_thread(
                self._knowledge.remove_vectors_by_metadata,
                {"source_path": relative_path},
            )
            self._indexed_files.discard(relative_path)

        logger.info("Removed knowledge file from index", base_id=self.base_id, path=relative_path, removed=removed)
        return removed

    def get_status(self) -> dict[str, Any]:
        """Get current knowledge indexing status."""
        files = self.list_files()
        return {
            "base_id": self.base_id,
            "folder_path": str(self.knowledge_path),
            "file_count": len(files),
            "indexed_count": len(self._indexed_files),
        }

    async def _watch_loop(self) -> None:
        """Watch the knowledge folder for file changes."""
        async for changes in awatch(self.knowledge_path, stop_event=self._watch_stop_event):
            if not changes:
                continue

            for change, changed_path in changes:
                await self._handle_file_change(change, Path(changed_path))

    async def _handle_file_change(self, change: Change, file_path: Path) -> None:
        """Handle one filesystem change event."""
        try:
            resolved_path = self.resolve_file_path(file_path)
        except ValueError:
            return

        if change in {Change.added, Change.modified}:
            if resolved_path.exists() and resolved_path.is_file():
                await self.index_file(resolved_path, upsert=True)
        elif change == Change.deleted:
            await self.remove_file(resolved_path)


_knowledge_managers: dict[str, KnowledgeManager] = {}


async def initialize_knowledge_managers(
    config: Config,
    storage_path: Path,
    *,
    start_watchers: bool = False,
    reindex_on_create: bool = True,
) -> dict[str, KnowledgeManager]:
    """Initialize process-wide knowledge managers for all configured knowledge bases."""
    configured_base_ids = set(config.knowledge_bases)

    for base_id in sorted(set(_knowledge_managers) - configured_base_ids):
        await _knowledge_managers[base_id].stop_watcher()
        del _knowledge_managers[base_id]

    for base_id in sorted(configured_base_ids):
        existing = _knowledge_managers.get(base_id)

        if existing is not None and existing.matches(config, storage_path):
            existing.config = config
            if start_watchers:
                await existing.start_watcher()
            continue

        if existing is not None:
            await existing.stop_watcher()

        manager = KnowledgeManager(base_id=base_id, config=config, storage_path=storage_path)
        if reindex_on_create:
            await manager.initialize()
        else:
            indexed_count = await manager.load_indexed_files()
            logger.info(
                "Knowledge manager initialized without full reindex",
                base_id=base_id,
                path=str(manager.knowledge_path),
                indexed_count=indexed_count,
            )

        if start_watchers:
            await manager.start_watcher()

        _knowledge_managers[base_id] = manager

    return dict(_knowledge_managers)


def get_knowledge_manager(base_id: str) -> KnowledgeManager | None:
    """Get one process-wide knowledge manager by base ID."""
    return _knowledge_managers.get(base_id)


async def shutdown_knowledge_managers() -> None:
    """Shutdown and clear all process-wide knowledge managers."""
    for manager in list(_knowledge_managers.values()):
        await manager.stop_watcher()

    _knowledge_managers.clear()
