"""Knowledge base management for file-backed RAG."""

from __future__ import annotations

import asyncio
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

    from .config import Config

logger = get_logger(__name__)

_COLLECTION_NAME = "mindroom_knowledge"


def _resolve_knowledge_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _settings_key(config: Config, storage_path: Path) -> tuple[str, ...]:
    embedder_config = config.memory.embedder.config
    knowledge_path = _resolve_knowledge_path(config.knowledge.path)
    return (
        str(storage_path.resolve()),
        str(knowledge_path),
        config.memory.embedder.provider,
        embedder_config.model,
        embedder_config.host or "",
        str(config.knowledge.watch),
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
    """Manage indexing and watching for the knowledge folder."""

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
        self.knowledge_path = _resolve_knowledge_path(self.config.knowledge.path)
        self.knowledge_path.mkdir(parents=True, exist_ok=True)
        self._settings = _settings_key(self.config, self.storage_path)

        vector_db = ChromaDb(
            collection=_COLLECTION_NAME,
            path=str((self.storage_path / "knowledge_db").resolve()),
            persistent_client=True,
            embedder=_create_embedder(self.config),
        )
        self._knowledge = Knowledge(vector_db=vector_db)

    def matches(self, config: Config, storage_path: Path) -> bool:
        """Return True when manager settings match the provided config."""
        return self._settings == _settings_key(config, storage_path)

    def get_knowledge(self) -> Knowledge:
        """Return the agno Knowledge instance."""
        return self._knowledge

    def list_files(self) -> list[Path]:
        """List all files currently present in the knowledge folder."""
        if not self.knowledge_path.exists():
            return []
        return sorted([path for path in self.knowledge_path.rglob("*") if path.is_file()])

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

    async def initialize(self) -> None:
        """Initialize and index all existing knowledge files."""
        indexed_count = await self.reindex_all()
        logger.info("Knowledge base initialized", indexed_count=indexed_count, path=str(self.knowledge_path))

    async def start_watcher(self) -> None:
        """Start background file watching if enabled."""
        if not self.config.knowledge.watch:
            return
        if self._watch_task is not None and not self._watch_task.done():
            return

        self._watch_stop_event = asyncio.Event()
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info("Knowledge folder watcher started", path=str(self.knowledge_path))

    async def stop_watcher(self) -> None:
        """Stop the background file watcher."""
        if self._watch_task is None:
            return

        self._watch_stop_event.set()
        self._watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._watch_task

        self._watch_task = None
        logger.info("Knowledge folder watcher stopped")

    async def shutdown(self) -> None:
        """Shutdown all background work for this manager."""
        await self.stop_watcher()

    async def reindex_all(self) -> int:
        """Clear and rebuild the knowledge index from disk."""
        files = self.list_files()

        async with self._lock:
            await asyncio.to_thread(self._reset_collection)
            self._indexed_files.clear()

        for file_path in files:
            await self.index_file(file_path, upsert=True)

        return len(self._indexed_files)

    async def index_file(self, file_path: Path | str, *, upsert: bool = True) -> bool:
        """Index or reindex a single file."""
        resolved_path = self.resolve_file_path(file_path)
        if not resolved_path.exists() or not resolved_path.is_file():
            return False

        relative_path = self._relative_path(resolved_path)
        metadata = {"source_path": relative_path}

        async with self._lock:
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
                logger.exception("Failed to index knowledge file", path=str(resolved_path))
                return False
            self._indexed_files.add(relative_path)

        logger.info("Indexed knowledge file", path=relative_path)
        return True

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

        logger.info("Removed knowledge file from index", path=relative_path, removed=removed)
        return removed

    def get_status(self) -> dict[str, Any]:
        """Get current knowledge indexing status."""
        files = self.list_files()
        return {
            "enabled": self.config.knowledge.enabled,
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


_knowledge_manager: KnowledgeManager | None = None


async def initialize_knowledge_manager(
    config: Config,
    storage_path: Path,
    *,
    start_watcher: bool = False,
    reindex_on_create: bool = True,
) -> KnowledgeManager | None:
    """Initialize the process-wide knowledge manager for the given config."""
    global _knowledge_manager

    if not config.knowledge.enabled:
        await shutdown_knowledge_manager()
        return None

    if _knowledge_manager is not None and _knowledge_manager.matches(config, storage_path):
        if start_watcher:
            await _knowledge_manager.start_watcher()
        return _knowledge_manager

    if _knowledge_manager is not None:
        await _knowledge_manager.shutdown()

    _knowledge_manager = KnowledgeManager(config=config, storage_path=storage_path)
    if reindex_on_create:
        await _knowledge_manager.initialize()
    else:
        logger.info("Knowledge manager initialized without full reindex", path=str(_knowledge_manager.knowledge_path))

    if start_watcher:
        await _knowledge_manager.start_watcher()

    return _knowledge_manager


def get_knowledge_manager() -> KnowledgeManager | None:
    """Get the process-wide knowledge manager if initialized."""
    return _knowledge_manager


def get_knowledge() -> Knowledge | None:
    """Get the process-wide agno Knowledge instance if available."""
    if _knowledge_manager is None:
        return None
    return _knowledge_manager.get_knowledge()


async def shutdown_knowledge_manager() -> None:
    """Shutdown and clear the process-wide knowledge manager."""
    global _knowledge_manager

    if _knowledge_manager is not None:
        await _knowledge_manager.shutdown()
    _knowledge_manager = None
