"""Knowledge base management for file-backed RAG."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import IO, TYPE_CHECKING, Any, NoReturn, TypeVar, cast

from agno.knowledge.document.base import Document
from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.json_reader import JSONReader
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader
from agno.vectordb.chroma import ChromaDb

from mindroom.chunking import SafeFixedSizeChunking
from mindroom.constants import (
    DEFAULT_MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES,
    KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV,
    MAX_ALLOWED_CONCURRENT_KNOWLEDGE_FILE_INDEXES,
    RuntimePaths,
    resolve_config_relative_path,
)
from mindroom.embedding_errors import (
    classified_embedder_error,
    embedder_failure_is_transient,
    is_embedder_auth_failure_detail,
)
from mindroom.embedding_factory import create_configured_embedder
from mindroom.knowledge.candidate_checkpoint import (
    CandidateCheckpoint,
    CandidateFailure,
    FileSignature,
    append_candidate_journal,
    delete_candidate_checkpoint,
    file_signature_from_fields,
    load_candidate_checkpoint,
    save_candidate_checkpoint,
)
from mindroom.knowledge.collections import (
    SOURCE_DIGEST_KEY,
    SOURCE_MTIME_NS_KEY,
    SOURCE_PATH_KEY,
    SOURCE_SIZE_KEY,
    VECTOR_VERIFY_BATCH,
    CollectionSpace,
    build_knowledge,
    build_vector_db,
    candidate_collection_name,
    cleanup_superseded_collections,
    collection_has_source_path,
    delete_collection,
    delete_source_path_vectors,
    paths_with_vectors,
    require_chroma_vector_db,
    reset_vector_db,
)
from mindroom.knowledge.embedding_batch import (
    DEFAULT_MAX_EMBEDDING_BATCH_ITEMS,
    DEFAULT_MAX_EMBEDDING_BATCH_PAYLOAD_BYTES,
    BatchPrefetchEmbedder,
    plan_embedding_batches,
)
from mindroom.knowledge.file_listing import (
    git_tracked_relative_paths_from_checkout,
    knowledge_files_from_relative_paths,
    list_knowledge_files,
)
from mindroom.knowledge.git_source import GitKnowledgeSource
from mindroom.knowledge.index_metadata import (
    PublishedIndexState,
    load_published_index_state,
    save_published_index_state,
    state_for_publication,
)
from mindroom.knowledge.index_retry import EmbeddingRetryPolicy, run_with_embedding_retry
from mindroom.knowledge.indexing_config import (
    IndexingSettings,
    chroma_collection_exists,
    indexing_settings_key,
    storage_key_for_base,
)
from mindroom.knowledge.redaction import redact_credentials_in_text
from mindroom.knowledge.refresh_outcome import RefreshOutcome
from mindroom.logging_config import get_logger
from mindroom.strict_knowledge import StrictInsertKnowledge as Knowledge

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from agno.knowledge.embedder.base import Embedder
    from agno.knowledge.reader.base import Reader
    from chromadb.api.types import Embeddings, Metadata

    from mindroom.config.main import Config

logger = get_logger(__name__)


class _MalformedJSONSourceError(Exception):
    """A JSON parser failure carrying the already-read source text."""

    def __init__(self, source_text: str, *, line: int, column: int) -> None:
        super().__init__("Malformed JSON knowledge source")
        self.source_text = source_text
        self.line = line
        self.column = column


class _FallbackAwareJSONReader(JSONReader):
    """Tag only JSON decoding failures raised inside the source reader."""

    def read(self, path: Path | IO[Any], name: str | None = None) -> list[Document]:
        try:
            return super().read(path, name=name)
        except json.JSONDecodeError as error:
            raise _MalformedJSONSourceError(error.doc, line=error.lineno, column=error.colno) from error


class _InMemoryTextReader(TextReader):
    """Read the malformed JSON text already retained by its parse error.

    Only ``read`` is overridden, because indexing goes through the synchronous
    ``Knowledge.insert`` path. ``TextReader.async_read`` does not delegate to
    ``read``, so anything switching this to ``Knowledge.ainsert`` must override
    it too or it will re-read the source instead of serving the retained text.
    """

    def __init__(self, source_text: str) -> None:
        super().__init__()
        self._source_text = source_text

    def read(self, file: Path | IO[Any], name: str | None = None) -> list[Document]:
        document = Document(
            name=name or str(file),
            id=str(uuid.uuid4()),
            content=self._source_text,
        )
        if not self.chunk:
            return [document]
        return self.chunk_document(document)


_POST_INDEX_VECTOR_VISIBILITY_RETRY_DELAYS_SECONDS = (0.0, 0.01, 0.05)
#: Files pulled into one prepare/embed/write batch. This bounds live asyncio
#: tasks and peak memory independently of corpus size; the provider request
#: bounds are applied separately when the batch's chunks are planned.
_INDEX_FILES_PER_BATCH = 64
#: Chunk text held in memory for one prefetch pass. File count alone does not
#: bound memory: 64 large files can materialize far more text (and far more
#: cached vectors) than 64 small ones.
_MAX_PREFETCH_TEXT_BYTES = 8_000_000
#: Source files whose signatures are computed per thread hop, so a huge corpus
#: still yields to the event loop and to cancellation while it is scanned.
_SIGNATURE_SCAN_CHUNK = 512
#: Published chunk rows read in one copy query. Chroma binds one SQL variable
#: per *returned* row and SQLite caps a statement at 32,766, so nothing about
#: the file or path count bounds a copy query -- only the rows it may return.
#: The page also bounds how much vector data the copy holds in memory at once.
_PUBLISHED_VECTOR_COPY_PAGE_ROWS = 500
#: Reconciliation passes before a refresh gives up for now. A source that keeps
#: changing keeps its candidate and converges over successive refreshes instead
#: of thrashing inside one.
_MAX_CANDIDATE_RECONCILE_ROUNDS = 4
#: Journal appends tolerated before the candidate snapshot is recompacted.
_CANDIDATE_JOURNAL_COMPACT_ENTRIES = 5_000
_PROGRESS_LOG_INTERVAL_FILES = 500
_PROGRESS_LOG_INTERVAL_SECONDS = 30.0
#: Consecutive classified embedder rejections, with no success in between,
#: taken as proof the fault is global rather than specific to a few files.
_GLOBAL_EMBEDDER_FAILURE_STREAK = 20
_EMBEDDING_RETRY_POLICY = EmbeddingRetryPolicy()
_ShieldedResult = TypeVar("_ShieldedResult")
#: Indirection point so fault-injection tests can drive backoff without waiting.
_EMBEDDING_RETRY_SLEEP: Callable[[float], Awaitable[None]] = asyncio.sleep


def _max_concurrent_knowledge_file_indexes() -> int:
    """Return bounded file-level indexing concurrency."""
    raw_value = os.getenv(KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV)
    if raw_value is None:
        return DEFAULT_MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES
    try:
        value = int(raw_value)
    except ValueError as exc:
        msg = f"{KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV} must be an integer, got {raw_value!r}"
        raise ValueError(msg) from exc
    if not 1 <= value <= MAX_ALLOWED_CONCURRENT_KNOWLEDGE_FILE_INDEXES:
        msg = (
            f"{KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV} must be between 1 and "
            f"{MAX_ALLOWED_CONCURRENT_KNOWLEDGE_FILE_INDEXES}, got {value}"
        )
        raise ValueError(msg)
    return value


@dataclass
class _CandidatePublishState:
    index_published: bool = False


@dataclass
class _CandidateRun:
    """One refresh's live view of the durable candidate it is advancing."""

    checkpoint: CandidateCheckpoint
    knowledge: Knowledge
    vector_db: ChromaDb
    embedder: BatchPrefetchEmbedder | None
    completed: dict[str, FileSignature] = field(default_factory=dict)
    failed: dict[str, CandidateFailure] = field(default_factory=dict)
    vanished: set[str] = field(default_factory=set)
    #: Completed entries whose vectors this process has already confirmed, so
    #: repeated reconciliation rounds do not re-query Chroma for every file.
    verified: set[str] = field(default_factory=set)
    #: Size of the corpus this candidate is currently targeting, refreshed by
    #: each reconciliation so progress reporting shows real pending work.
    total_files: int = 0
    #: Journal appends since the last compaction, tracked in memory so deciding
    #: when to compact never re-reads and re-parses the whole journal.
    journal_appends: int = 0
    resumed: bool = False
    published: bool = False


@dataclass(frozen=True)
class _OpenedCandidate:
    """One candidate collection made ready for a refresh to advance."""

    checkpoint: CandidateCheckpoint
    knowledge: Knowledge
    vector_db: ChromaDb
    reused: dict[str, FileSignature] = field(default_factory=dict)
    resumed: bool = False


@dataclass(frozen=True)
class _CandidateReconciliation:
    """Work the candidate still owes the current source listing."""

    expected: frozenset[str]
    pending: tuple[Path, ...]


@dataclass
class _CandidateProgress:
    """Throttled progress accounting for one candidate build."""

    base_id: str
    resumed: bool = False
    target_revision: str | None = None
    collection: str = ""
    total: int = 0
    completed: int = 0
    failed: int = 0
    retrying: int = 0
    #: Files this pass actually embedded, as opposed to reused from the candidate.
    indexed_this_run: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _last_logged_at: float = field(default_factory=time.monotonic, repr=False)
    _last_logged_completed: int = field(default=0, repr=False)

    @property
    def pending(self) -> int:
        """Return files still owed by the candidate."""
        return max(self.total - self.completed, 0)

    def elapsed_seconds(self) -> float:
        """Return wall-clock seconds since this refresh started working."""
        return max(time.monotonic() - self.started_at, 0.0)

    def _fields(self) -> dict[str, object]:
        return {
            "base_id": self.base_id,
            "collection": self.collection,
            "resumed": self.resumed,
            "target_revision": self.target_revision,
            "total": self.total,
            "completed": self.completed,
            "indexed_this_run": self.indexed_this_run,
            "pending": self.pending,
            "failed": self.failed,
            "retrying": self.retrying,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
        }

    def maybe_log(self) -> None:
        """Emit one periodic INFO summary instead of one line per file."""
        now = time.monotonic()
        due = (self.completed - self._last_logged_completed) >= _PROGRESS_LOG_INTERVAL_FILES or (
            now - self._last_logged_at
        ) >= _PROGRESS_LOG_INTERVAL_SECONDS
        if not due:
            return
        self._last_logged_at = now
        self._last_logged_completed = self.completed
        logger.info("knowledge_candidate_progress", **self._fields())

    def log_summary(self, outcome: RefreshOutcome) -> None:
        """Emit the single terminal summary for this refresh."""
        logger.info(
            "knowledge_candidate_finished",
            published=outcome.published,
            error=outcome.error,
            **self._fields(),
        )


class _PermanentEmbeddingError(Exception):
    """Internal signal that no further file in this refresh can be embedded.

    Raised instead of grinding one doomed provider request per remaining file
    when the embedder rejects work for a reason retrying cannot fix.
    """


def _raise_cancelled() -> NoReturn:
    raise asyncio.CancelledError


async def _drain_owned_task_after_cancellation(
    task: asyncio.Task[_ShieldedResult],
    *,
    suppress_errors: bool,
) -> None:
    """Drain one owned task despite repeated cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if suppress_errors:
        with suppress(Exception):
            task.result()
        return
    task.result()


async def _shielded_write(write: Coroutine[Any, Any, _ShieldedResult]) -> _ShieldedResult:
    """Run one durable write to completion even if the caller is cancelled.

    ``asyncio.shield`` only detaches the wait: the shielded task keeps running
    either way, so a cancelled caller that does not drain it leaves the write
    racing process exit. Draining swallows the write's own failure so that
    cancellation, not the failure, is what the caller sees -- every user of
    this treats a lost write as recoverable and cancellation as authoritative.
    The shared drain also defers repeated cancellation until the write finishes.
    """
    task = asyncio.create_task(write)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _drain_owned_task_after_cancellation(task, suppress_errors=True)
        raise


def _iter_file_batches(files: Sequence[Path], batch_size: int) -> Iterator[list[Path]]:
    """Yield bounded slices so a huge corpus never becomes one huge fan-out."""
    size = max(batch_size, 1)
    for start in range(0, len(files), size):
        yield list(files[start : start + size])


def _resolve_knowledge_path(
    path: str,
    runtime_paths: RuntimePaths,
) -> Path:
    return resolve_config_relative_path(path, runtime_paths=runtime_paths)


def _ensure_knowledge_directory_ready(knowledge_path: Path) -> None:
    if knowledge_path.exists() and not knowledge_path.is_dir():
        msg = f"Knowledge path {knowledge_path} must be a directory"
        raise ValueError(msg)
    knowledge_path.mkdir(parents=True, exist_ok=True)


def _semantic_indexing_enabled(config: Config, base_id: str) -> bool:
    return config.get_knowledge_base_config(base_id).mode == "semantic"


def _file_content_digest(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _knowledge_source_signature(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    *,
    tracked_relative_paths: Iterable[str] | None = None,
) -> str:
    """Return a robust signature for the currently managed local file corpus."""
    root = knowledge_root.resolve()
    digest = hashlib.sha256()
    base_config = config.get_knowledge_base_config(base_id)
    if base_config.git is None:
        files = list_knowledge_files(config, base_id, root)
    else:
        tracked_paths = (
            set(tracked_relative_paths)
            if tracked_relative_paths is not None
            else git_tracked_relative_paths_from_checkout(config, base_id, root)
        )
        files = knowledge_files_from_relative_paths(config, base_id, root, tracked_paths)
    for path in files:
        try:
            stat = path.stat()
            relative_path = path.relative_to(root).as_posix()
            source_digest = _file_content_digest(path)
        except OSError:
            continue
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(source_digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _reusable_row_signature(
    metadata: Metadata,
    managed_paths: frozenset[str],
) -> tuple[str, FileSignature] | None:
    """Return the managed source path and signature one published chunk carries."""
    source_path = metadata.get(SOURCE_PATH_KEY)
    if not isinstance(source_path, str) or source_path not in managed_paths:
        return None
    signature = file_signature_from_fields(
        metadata.get(SOURCE_MTIME_NS_KEY),
        metadata.get(SOURCE_SIZE_KEY),
        metadata.get(SOURCE_DIGEST_KEY),
    )
    if signature is None:
        return None
    return source_path, signature


def _source_signature_from_file_signatures(file_signatures: Mapping[str, FileSignature]) -> str:
    """Return the same corpus signature from already-indexed relative path signatures."""
    digest = hashlib.sha256()
    for relative_path, (source_mtime_ns, source_size, source_digest) in sorted(file_signatures.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(source_mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(source_size).encode("ascii"))
        digest.update(b"\0")
        digest.update(source_digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass
class KnowledgeManager:
    """Manage indexing for one knowledge base folder."""

    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    storage_path: Path | None = None
    knowledge_path: Path | None = None
    #: Collaborator that owns the Git checkout this base indexes, when one is
    #: configured. Always present: it answers ``is_configured()`` for itself.
    git_source: GitKnowledgeSource = field(init=False)
    _indexing_settings: IndexingSettings = field(init=False)
    _base_storage_path: Path = field(init=False)
    _indexing_settings_path: Path = field(init=False)
    _collections: CollectionSpace = field(init=False, repr=False)
    _knowledge: Knowledge = field(init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _last_refresh_error: str | None = field(default=None, init=False)
    _last_file_index_error: str | None = field(default=None, init=False)
    _persisted_collection_missing_on_init: bool = field(default=False, init=False, repr=False)
    _max_concurrent_file_indexes: int = field(init=False, repr=False)
    _embedding_retry_count: int = field(default=0, init=False, repr=False)
    _file_index_errors: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _embedder_failure_streak: int = field(default=0, init=False, repr=False)
    _global_embedder_failure: str | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize filesystem paths and the underlying vector database."""
        self._max_concurrent_file_indexes = _max_concurrent_knowledge_file_indexes()
        base_config = self.config.get_knowledge_base_config(self.base_id)
        if self.storage_path is None:
            self.storage_path = self.runtime_paths.storage_root
        if self.knowledge_path is None:
            self.knowledge_path = _resolve_knowledge_path(base_config.path, self.runtime_paths)
        if self.storage_path is None or self.knowledge_path is None:
            msg = f"Knowledge manager '{self.base_id}' requires storage_path and knowledge_path"
            raise ValueError(msg)
        self.storage_path = self.storage_path.resolve()
        self.knowledge_path = self.knowledge_path.resolve()
        _ensure_knowledge_directory_ready(self.knowledge_path)
        self._set_settings(self.config, self.runtime_paths, self.storage_path, self.knowledge_path)
        self._base_storage_path = (
            self.storage_path / "knowledge_db" / storage_key_for_base(self.base_id, self.knowledge_path)
        ).resolve()
        self._base_storage_path.mkdir(parents=True, exist_ok=True)
        self._indexing_settings_path = self._base_storage_path / "indexing_settings.json"
        self.git_source = GitKnowledgeSource(
            base_id=self.base_id,
            config=self.config,
            runtime_paths=self.runtime_paths,
            source_path=self.knowledge_path,
            lfs_hydrated_head_path=self._base_storage_path / "git_lfs_hydrated_head.txt",
        )
        self._collections = CollectionSpace(
            base_id=self.base_id,
            knowledge_path=self.knowledge_path,
            storage_path=self._base_storage_path,
            # A factory, not an embedder: this runs for every base, including
            # non-semantic ones that never open a collection, and a status read
            # must construct no embedder at all. Deferring puts the cost only
            # where a handle is really built -- for a cleanup sweep, once per
            # collection it actually deletes, and nothing when it deletes none.
            embedder_factory=lambda: create_configured_embedder(self.config, self.runtime_paths),
        )
        persisted_state = load_published_index_state(self._indexing_settings_path)
        if not _semantic_indexing_enabled(self.config, self.base_id):
            self._persisted_collection_missing_on_init = False
            self._knowledge = Knowledge()
            return
        self._persisted_collection_missing_on_init = self._persisted_collection_missing(persisted_state)
        collection_name = (
            persisted_state.collection
            if (
                persisted_state is not None
                and persisted_state.collection is not None
                and not self._persisted_collection_missing_on_init
            )
            else self._collections.default_collection
        )
        self._knowledge = build_knowledge(self._collections, collection_name)

    def _set_settings(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        storage_path: Path,
        knowledge_path: Path,
    ) -> None:
        self.config = config
        self.runtime_paths = runtime_paths
        self.storage_path = storage_path
        self.knowledge_path = knowledge_path.resolve()
        self._indexing_settings = indexing_settings_key(
            config,
            storage_path,
            self.base_id,
            self.knowledge_path,
        )

    def _knowledge_source_path(self) -> Path:
        knowledge_path = self.knowledge_path
        if knowledge_path is None:
            msg = f"Knowledge path for base '{self.base_id}' is not initialized"
            raise RuntimeError(msg)
        return knowledge_path

    def _persisted_collection_missing(self, persisted_state: PublishedIndexState | None) -> bool:
        if persisted_state is None or persisted_state.status != "complete":
            return False
        collection_name = persisted_state.collection or self._collections.default_collection
        try:
            return not chroma_collection_exists(self._base_storage_path, collection_name)
        except Exception:
            logger.warning(
                "Knowledge collection existence check failed during manager initialization",
                base_id=self.base_id,
                collection=collection_name,
                exc_info=True,
            )
            return True

    def _has_existing_index(self) -> bool:
        vector_db = self._knowledge.vector_db
        return isinstance(vector_db, ChromaDb) and vector_db.exists()

    def needs_full_reindex_on_create(self) -> bool:
        """Return whether persisted metadata forces a full rebuild for this base."""
        if self._persisted_collection_missing_on_init:
            return True
        persisted_state = load_published_index_state(self._indexing_settings_path)
        if persisted_state is None:
            return self._indexing_settings_path.exists() and self._has_existing_index()
        return persisted_state.settings != self._indexing_settings or persisted_state.status == "resetting"

    def list_files(self) -> list[Path]:
        """List all files currently present in the knowledge folder."""
        knowledge_root = self._knowledge_source_path()
        if self.git_source.is_configured():
            tracked_relative_paths = self.git_source.tracked_relative_paths()
            if tracked_relative_paths is None:
                return []
            return knowledge_files_from_relative_paths(
                self.config,
                self.base_id,
                knowledge_root,
                tracked_relative_paths,
            )
        return list_knowledge_files(self.config, self.base_id, knowledge_root)

    async def source_signature(self) -> str:
        """Return the signature of the corpus this base currently manages.

        Git-backed bases hand over the tracked paths this process already
        listed, so an unchanged checkout is not re-listed just to hash it.
        """
        return await asyncio.to_thread(
            _knowledge_source_signature,
            self.config,
            self.base_id,
            self._knowledge_source_path(),
            tracked_relative_paths=self.git_source.cached_tracked_relative_paths(),
        )

    def _relative_path(self, file_path: Path) -> str:
        return file_path.relative_to(self._knowledge_source_path()).as_posix()

    def _file_signature(self, file_path: Path) -> FileSignature:
        stat = file_path.stat()
        return stat.st_mtime_ns, stat.st_size, _file_content_digest(file_path)

    def _has_vectors_for_source_path(
        self,
        relative_path: str,
        *,
        knowledge: Knowledge,
    ) -> bool:
        vector_db = knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return True
        if not vector_db.exists():
            return False

        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        return collection_has_source_path(collection, relative_path)

    async def _wait_for_source_vectors(
        self,
        relative_path: str,
        *,
        knowledge: Knowledge,
    ) -> bool:
        """Retry post-insert visibility checks to tolerate brief vector-store lag."""
        for attempt, delay_seconds in enumerate(_POST_INDEX_VECTOR_VISIBILITY_RETRY_DELAYS_SECONDS):
            if attempt > 0:
                await asyncio.sleep(delay_seconds)
            has_vectors = await asyncio.to_thread(
                self._has_vectors_for_source_path,
                relative_path,
                knowledge=knowledge,
            )
            if has_vectors:
                return True
        return False

    def _chunking_strategy(self) -> SafeFixedSizeChunking:
        """Build the chunking strategy every text-like read of this base uses."""
        base_config = self.config.get_knowledge_base_config(self.base_id)
        return SafeFixedSizeChunking(
            chunk_size=base_config.chunk_size,
            overlap=base_config.chunk_overlap,
        )

    def _configure_text_reader(self, reader: TextReader | MarkdownReader) -> TextReader | MarkdownReader:
        """Apply this base's text chunking policy to ``reader`` in place."""
        chunking_strategy = self._chunking_strategy()
        reader.chunk = True
        reader.chunk_size = chunking_strategy.chunk_size
        reader.chunking_strategy = chunking_strategy
        return reader

    def _build_reader(self, file_path: Path) -> Reader:
        """Build a per-file reader with conservative chunking for text-like content."""
        reader = ReaderFactory.get_reader_for_extension(file_path.suffix.lower())

        # ReaderFactory hands out cached shared instances, so any branch that
        # configures a reader copies it first instead of mutating the cache.
        if isinstance(reader, JSONReader):
            # Carry the factory reader's configuration (encoding, chunking) onto
            # the subclass that tags its own decode failures for the text fallback.
            return _FallbackAwareJSONReader(**deepcopy(vars(reader)))

        # Large markdown/plain-text files are the common source of oversized embed requests.
        if not isinstance(reader, (TextReader, MarkdownReader)):
            return reader

        return self._configure_text_reader(deepcopy(reader))

    async def _insert_with_malformed_json_fallback(
        self,
        insert: Callable[[Reader], Awaitable[None]],
        *,
        relative_path: str,
        reader: Reader,
    ) -> None:
        """Insert through the selected reader, falling back only for malformed source JSON."""

        async def _insert_with_retry(selected_reader: Reader) -> None:
            await run_with_embedding_retry(
                partial(insert, selected_reader),
                policy=_EMBEDDING_RETRY_POLICY,
                sleep=_EMBEDDING_RETRY_SLEEP,
                on_retry=self._record_embedding_retry,
            )

        try:
            await _insert_with_retry(reader)
        except _MalformedJSONSourceError as error:
            logger.warning(
                "Malformed JSON knowledge file; indexing as text",
                base_id=self.base_id,
                path=relative_path,
                line=error.line,
                column=error.column,
            )
            fallback_reader = self._configure_text_reader(_InMemoryTextReader(error.source_text))
            await _insert_with_retry(fallback_reader)

    async def _save_candidate_publish_metadata(
        self,
        *,
        candidate_vector_db: ChromaDb,
        indexed_count: int,
        source_signature: str,
    ) -> bool:
        state = state_for_publication(
            settings=self._indexing_settings,
            collection=candidate_vector_db.collection_name,
            indexed_count=indexed_count,
            source_signature=source_signature,
            published_revision=self.git_source.last_synced_head,
        )
        save_task = asyncio.create_task(
            asyncio.to_thread(save_published_index_state, self._indexing_settings_path, state),
        )
        try:
            await asyncio.shield(save_task)
        except asyncio.CancelledError:
            # Use the shared repeated-cancellation drain without suppressing
            # task errors: the caller marks the index published on a
            # cancelled-but-saved outcome, so a failed write must surface.
            await _drain_owned_task_after_cancellation(save_task, suppress_errors=False)
            return True
        return False

    async def _publish_candidate_after_metadata_save(
        self,
        *,
        candidate_vector_db: ChromaDb,
        indexed_count: int,
        source_signature: str,
        publish_state: _CandidatePublishState,
    ) -> None:
        publish_cancelled = await self._save_candidate_publish_metadata(
            candidate_vector_db=candidate_vector_db,
            indexed_count=indexed_count,
            source_signature=source_signature,
        )
        publish_state.index_published = True
        # Adopt the candidate as this manager's live vector database:
        # `cleanup_superseded_collections` runs right after publish and is handed
        # `self._knowledge.vector_db` as the Chroma client it lists with, so the
        # adoption has to land before that call reads the attribute.
        self._knowledge.vector_db = candidate_vector_db
        if publish_cancelled:
            _raise_cancelled()

    async def _index_file_locked(
        self,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: Knowledge,
        indexed_signatures: dict[str, FileSignature],
    ) -> bool:
        """Index one file while the caller owns the operation lock."""
        relative_path = self._relative_path(resolved_path)
        source_mtime_ns, source_size, source_digest = await asyncio.to_thread(self._file_signature, resolved_path)
        metadata = {
            SOURCE_PATH_KEY: relative_path,
            SOURCE_MTIME_NS_KEY: source_mtime_ns,
            SOURCE_SIZE_KEY: source_size,
            SOURCE_DIGEST_KEY: source_digest,
        }
        try:
            reader = self._build_reader(resolved_path)
        except ImportError as exc:
            logger.warning(
                "Skipping knowledge file because its reader dependency is not installed",
                base_id=self.base_id,
                path=relative_path,
                extension=resolved_path.suffix.lower(),
                error=str(exc),
            )
            return False

        async def _insert_once(selected_reader: Reader) -> None:
            if upsert:
                # Agno/Chroma upsert keys by content hash, so stale chunks from an older
                # version of the same file can remain unless we clear by source metadata first.
                await asyncio.to_thread(knowledge.remove_vectors_by_metadata, {SOURCE_PATH_KEY: relative_path})
            # Knowledge.ainsert is async by name only: it eventually calls into the
            # vector database's synchronous batch upsert (e.g. ChromaDB's Rust
            # _upsert) on the running event loop, blocking every other coroutine
            # for as long as the embed+upsert batch takes. Use the sync insert API
            # via asyncio.to_thread so embedding + vector database work runs on a
            # worker thread and the loop stays responsive to Matrix sync, tool
            # calls, and cache writes.
            await asyncio.to_thread(
                knowledge.insert,
                path=str(resolved_path),
                metadata=metadata,
                upsert=upsert,
                reader=selected_reader,
            )

        try:
            # Remove-then-insert is idempotent, so a transient embedding fault
            # costs one retry of this file instead of the whole refresh.
            await self._insert_with_malformed_json_fallback(
                _insert_once,
                relative_path=relative_path,
                reader=reader,
            )
        except Exception as exc:
            classified = classified_embedder_error(exc)
            error = classified or f"knowledge indexing failed ({type(exc).__name__})"
            if self._last_file_index_error is None:
                self._last_file_index_error = error
            self._file_index_errors[relative_path] = error
            self._record_embedder_rejection(classified)
            logger.exception("Failed to index knowledge file", base_id=self.base_id, path=str(resolved_path))
            return False

        has_vectors = await self._wait_for_source_vectors(
            relative_path,
            knowledge=knowledge,
        )
        if not has_vectors:
            return self._handle_vectorless_file(
                relative_path,
                (source_mtime_ns, source_size, source_digest),
                indexed_signatures=indexed_signatures,
            )

        indexed_signatures[relative_path] = (source_mtime_ns, source_size, source_digest)
        self._file_index_errors.pop(relative_path, None)
        self._note_embedder_success()
        # DEBUG, not INFO: a large corpus is 10^5 of these lines per refresh.
        # Operators get periodic aggregate progress instead.
        logger.debug("Indexed knowledge file", base_id=self.base_id, path=relative_path)
        return True

    def _handle_vectorless_file(
        self,
        relative_path: str,
        signature: FileSignature,
        *,
        indexed_signatures: dict[str, FileSignature],
    ) -> bool:
        """Record one insert that produced no vectors; success only for empty sources."""
        source_size = signature[1]
        if source_size == 0:
            indexed_signatures[relative_path] = signature
            logger.debug("Scanned empty knowledge file with no vectors", base_id=self.base_id, path=relative_path)
            return True

        logger.warning("Indexing produced no vectors for file", base_id=self.base_id, path=relative_path)
        indexed_signatures.pop(relative_path, None)
        return False

    def _record_embedding_retry(self) -> None:
        self._embedding_retry_count += 1

    def _record_embedder_rejection(self, classified: str | None) -> None:
        """Track evidence that the embedder is rejecting everything, not one file.

        Providers without a batch surface, and files read by a non-text reader,
        never reach the batch-prefetch stop, so without this the same doomed
        request is issued once per remaining file.
        """
        if classified is None:
            return
        self._embedder_failure_streak += 1
        if is_embedder_auth_failure_detail(classified):
            # A rejected credential is global by construction; one file is proof enough.
            self._global_embedder_failure = classified
        elif self._embedder_failure_streak >= _GLOBAL_EMBEDDER_FAILURE_STREAK:
            self._global_embedder_failure = classified

    def _note_embedder_success(self) -> None:
        self._embedder_failure_streak = 0

    def _chunk_texts_for_prefetch(self, resolved_path: Path) -> tuple[str, ...]:
        """Return the chunk texts Agno will embed for one file, or ``()``.

        Only the text-like readers MindRoom configures chunking for are
        pre-read: for those, reading twice is negligible next to one embedding
        round trip per chunk. Any reader failure here is swallowed on purpose
        because prefetching is an optimization; the real insert path below owns
        error reporting for this file.
        """
        try:
            reader = self._build_reader(resolved_path)
        except Exception:
            return ()
        if not isinstance(reader, (TextReader, MarkdownReader)):
            return ()
        try:
            documents: Sequence[Document] = reader.read(resolved_path, name=resolved_path.name)
        except Exception:
            logger.debug(
                "Skipping embedding prefetch for knowledge file",
                base_id=self.base_id,
                path=str(resolved_path),
                exc_info=True,
            )
            return ()
        return tuple(document.content for document in documents if document.content)

    def _chunk_texts_for_batch(self, files: Sequence[Path]) -> list[str]:
        """Return chunk texts to prefetch, stopping at the memory budget.

        The size check has to precede the read: chunking materializes a file's
        entire content, so a budget consulted afterwards cannot stop a single
        oversized file from blowing the bound. A file that cannot fit the
        remaining budget is skipped rather than ending the pass, so smaller
        files behind it still benefit.

        Overlapping chunks re-emit the same characters many times over, so a
        file's size on disk stops bounding the text its chunks occupy: 4 KB at
        chunk_size=128/overlap=127 materializes ~484 KB. The admission test is
        therefore the chunker's own worst-case expansion of that size, never
        the size itself.

        Skipped files are simply not prefetched; their chunks are embedded by
        the normal per-file path, so the only cost of the bound is speed,
        never correctness.
        """
        chunking_strategy = self._chunking_strategy()
        chunk_texts: list[str] = []
        remaining = _MAX_PREFETCH_TEXT_BYTES
        skipped = 0
        for resolved_path in files:
            if remaining <= 0:
                break
            try:
                source_size = resolved_path.stat().st_size
            except OSError:
                continue
            if chunking_strategy.max_chunk_text_bytes(source_size) > remaining:
                skipped += 1
                continue
            for text in self._chunk_texts_for_prefetch(resolved_path):
                chunk_texts.append(text)
                remaining -= len(text.encode("utf-8"))
                if remaining <= 0:
                    break
        if skipped or remaining <= 0:
            logger.debug(
                "Bounded embedding prefetch at the memory budget",
                base_id=self.base_id,
                chunks=len(chunk_texts),
                skipped_files=skipped,
            )
        return chunk_texts

    async def _prefetch_batch_embeddings(
        self,
        embedder: BatchPrefetchEmbedder,
        files: Sequence[Path],
    ) -> None:
        """Embed one batch's chunks in as few provider requests as limits allow."""
        if not embedder.supports_batching():
            return
        # One thread hop for the whole batch: a hop per file would serialize
        # reads that cost far less than the round trip scheduling them.
        chunk_texts = await asyncio.to_thread(self._chunk_texts_for_batch, list(files))
        if not chunk_texts:
            return

        for planned_batch in plan_embedding_batches(
            embedder.uncached(chunk_texts),
            max_items=DEFAULT_MAX_EMBEDDING_BATCH_ITEMS,
            max_payload_bytes=DEFAULT_MAX_EMBEDDING_BATCH_PAYLOAD_BYTES,
        ):

            async def _embed(batch: list[str] = planned_batch) -> int:
                return await asyncio.to_thread(embedder.embed_batch_into_cache, batch)

            try:
                await run_with_embedding_retry(
                    _embed,
                    policy=_EMBEDDING_RETRY_POLICY,
                    sleep=_EMBEDDING_RETRY_SLEEP,
                    on_retry=self._record_embedding_retry,
                )
            except Exception as exc:
                if not embedder_failure_is_transient(exc):
                    # Bad credentials or a wrong model will reject every
                    # request; stop now instead of grinding out one doomed
                    # request per remaining chunk, and report the failure the
                    # same way a per-file rejection would.
                    if self._last_file_index_error is None:
                        self._last_file_index_error = classified_embedder_error(exc) or (
                            f"knowledge indexing failed ({type(exc).__name__})"
                        )
                    raise _PermanentEmbeddingError from exc
                # Exhausted transient retries: stop batching for this batch and
                # let the per-file insert path retry, so the failure is
                # attributed to specific files and nothing already cached is
                # embedded again.
                logger.warning(
                    "Falling back to per-file embedding after batch retries were exhausted",
                    base_id=self.base_id,
                    batch_items=len(planned_batch),
                    exc_info=True,
                )
                break

    async def _reindex_files_locked(
        self,
        files: list[Path],
        *,
        knowledge: Knowledge,
        indexed_signatures: dict[str, FileSignature],
        vanished_files: set[str],
        embedder: BatchPrefetchEmbedder | None = None,
        on_file_result: Callable[[Path], Awaitable[None]] | None = None,
        on_batch_complete: Callable[[Sequence[Path]], Awaitable[None]] | None = None,
    ) -> int:
        """Reindex resolved files in bounded batches while holding the operation lock.

        Work is pulled batch by batch rather than fanned out over the whole
        list: live asyncio tasks stay bounded by the per-file concurrency limit
        regardless of corpus size, and each batch's chunks are embedded
        together before the batch is written.
        """
        if not files:
            return 0

        indexed_count = 0
        for batch in _iter_file_batches(files, _INDEX_FILES_PER_BATCH):
            if embedder is not None:
                try:
                    await self._prefetch_batch_embeddings(embedder, batch)
                except _PermanentEmbeddingError:
                    return indexed_count
            indexed_count += await self._index_file_batch(
                batch,
                knowledge=knowledge,
                indexed_signatures=indexed_signatures,
                vanished_files=vanished_files,
                on_file_result=on_file_result,
            )
            if self._global_embedder_failure is not None:
                logger.warning(
                    "Stopping knowledge refresh: the embedder is rejecting every request",
                    base_id=self.base_id,
                    detail=self._global_embedder_failure,
                )
                return indexed_count
            if embedder is not None:
                # Prefetched vectors are only useful for the batch that planned
                # them; dropping them keeps peak memory independent of corpus size.
                embedder.clear_cache()
            if on_batch_complete is not None:
                await on_batch_complete(batch)
        return indexed_count

    async def _index_file_or_skip_vanished(
        self,
        file_path: Path,
        *,
        knowledge: Knowledge,
        indexed_signatures: dict[str, FileSignature],
        vanished_files: set[str],
    ) -> bool:
        try:
            return await self._index_file_locked(
                file_path,
                upsert=True,
                knowledge=knowledge,
                indexed_signatures=indexed_signatures,
            )
        except FileNotFoundError:
            # Live source folders (e.g. thread exports) delete files while
            # a refresh runs; a file vanishing between listing and indexing
            # is not an indexing failure. Record it so the caller can drop
            # it from its completeness accounting: the trailing
            # source-signature comparison then decides whether the
            # surviving corpus is publishable or another refresh is needed.
            relative_path = self._relative_path(file_path)
            logger.warning(
                "Knowledge file vanished during refresh; skipping",
                base_id=self.base_id,
                path=relative_path,
            )
            vanished_files.add(relative_path)
            return False

    async def _index_file_batch(
        self,
        batch: Sequence[Path],
        *,
        knowledge: Knowledge,
        indexed_signatures: dict[str, FileSignature],
        vanished_files: set[str],
        on_file_result: Callable[[Path], Awaitable[None]] | None = None,
    ) -> int:
        """Index one bounded batch, capping live tasks at the concurrency limit."""

        async def _index_one(file_path: Path) -> bool:
            if self._global_embedder_failure is not None:
                return False
            indexed = await self._index_file_or_skip_vanished(
                file_path,
                knowledge=knowledge,
                indexed_signatures=indexed_signatures,
                vanished_files=vanished_files,
            )
            if on_file_result is not None:
                # Recorded per file, not per batch: an interruption partway
                # through a batch must still keep every file it finished.
                await on_file_result(file_path)
            return indexed

        concurrency = min(self._max_concurrent_file_indexes, len(batch))
        if concurrency <= 1:
            batch_indexed = 0
            for file_path in batch:
                batch_indexed += int(await _index_one(file_path))
            return batch_indexed

        semaphore = asyncio.Semaphore(concurrency)

        async def _index_one_bounded(file_path: Path) -> bool:
            async with semaphore:
                return await _index_one(file_path)

        # return_exceptions=True so a failing or cancelled child cannot leave its
        # siblings running: they would keep appending journal entries and mutating
        # candidate bookkeeping while the caller's `finally` compacts the
        # checkpoint, silently dropping the work those files had finished.
        results = await asyncio.gather(
            *(_index_one_bounded(file_path) for file_path in batch),
            return_exceptions=True,
        )
        first_error = next((result for result in results if isinstance(result, BaseException)), None)
        if first_error is not None:
            raise first_error
        return sum(1 for result in results if result is True)

    async def _candidate_paths_missing_vectors(self, run: _CandidateRun, relative_paths: Sequence[str]) -> set[str]:
        """Return completed entries the candidate cannot actually serve.

        A checkpoint entry is a claim, not proof: the process may have died
        between the vector write and the journal append, or the collection may
        have been truncated. Verification is batched so proving 10^5 entries
        costs a bounded number of vector-store queries.
        """
        # Empty sources legitimately produce no vectors, so a vector probe can
        # never confirm them; their signature already encodes the empty content.
        verifiable = [
            relative_path for relative_path in relative_paths if (run.completed.get(relative_path) or (0, 0, ""))[1] > 0
        ]
        run.verified.update(set(relative_paths) - set(verifiable))
        missing: set[str] = set()
        for start in range(0, len(verifiable), VECTOR_VERIFY_BATCH):
            batch = verifiable[start : start + VECTOR_VERIFY_BATCH]
            found = await asyncio.to_thread(paths_with_vectors, run.vector_db, batch)
            missing.update(set(batch) - found)
            run.verified.update(found)
        return missing

    def _copy_published_vectors(
        self,
        *,
        published_collection: str,
        candidate_vector_db: ChromaDb,
        managed_paths: frozenset[str],
    ) -> dict[str, FileSignature]:
        """Copy stored chunks for currently-managed paths into the candidate.

        Every published chunk carries the signature of the file it came from,
        so the published collection is already an index from source path to
        vectors. Chunks are copied verbatim -- ids, embeddings, documents and
        metadata -- because ids key content in the vector store and
        regenerating them would corrupt the candidate.

        Reads are paged because Chroma binds one SQL variable per *returned*
        row and SQLite caps a statement at 32,766 of them. Neither the file
        count nor the path count bounds that, so a single large file can refuse
        an unpaged read on its own; only bounding the returned rows escapes.

        Returns the signature published for each path that actually received
        rows, so a path the published index cannot serve is never claimed.
        """
        client = candidate_vector_db.client
        published = client.get_collection(name=published_collection)
        candidate = client.get_collection(name=candidate_vector_db.collection_name)
        reused: dict[str, FileSignature] = {}
        offset = 0
        while True:
            page = published.get(
                limit=_PUBLISHED_VECTOR_COPY_PAGE_ROWS,
                offset=offset,
                include=["embeddings", "documents", "metadatas"],
            )
            ids = page["ids"]
            if not ids:
                return reused
            offset += len(ids)
            # Chroma types every one of these optional because the caller may
            # not have asked for it; this one did, in the ``include`` above.
            # The annotation keeps the TYPE_CHECKING-only Embeddings import
            # visible to vulture; the string cast avoids a runtime import.
            embeddings: Embeddings = cast("Embeddings", page["embeddings"])
            documents = cast("list[str]", page["documents"])
            metadatas = cast("list[Metadata]", page["metadatas"])
            kept: list[int] = []
            for index, metadata in enumerate(metadatas):
                entry = _reusable_row_signature(metadata, managed_paths)
                if entry is None:
                    continue
                relative_path, signature = entry
                reused[relative_path] = signature
                kept.append(index)
            if kept:
                candidate.add(
                    ids=[ids[index] for index in kept],
                    embeddings=[embeddings[index] for index in kept],
                    documents=[documents[index] for index in kept],
                    metadatas=[dict(metadatas[index]) for index in kept],
                )

    def _reusable_published_collection(self, persisted_state: PublishedIndexState | None) -> str | None:
        """Return the published collection a fresh candidate may copy, if any.

        Matching ``IndexingSettings`` is what makes a copy sound. They pin the
        chunker, the embedder identity and every corpus filter, so identical
        bytes under identical settings produce identical chunks and identical
        vectors. Whether the bytes are still identical is not assumed here:
        each copied path is recorded with the signature the published index
        stored for it, and reconciliation compares that against the file on
        disk, dropping and re-indexing whatever moved. Reuse is therefore an
        optimization on top of the existing correctness check, not a new one.

        Only a collection the metadata calls published is provably finished:
        the rest of the candidate lifecycle treats any other collection as an
        abandoned candidate it may reclaim.
        """
        if (
            persisted_state is None
            or persisted_state.status != "complete"
            or persisted_state.collection is None
            or persisted_state.settings != self._indexing_settings
        ):
            return None
        return persisted_state.collection

    async def _seed_candidate_from_published(
        self,
        *,
        candidate_vector_db: ChromaDb,
        published_collection: str,
    ) -> dict[str, FileSignature]:
        """Start a fresh candidate from the published index instead of from zero.

        Publication retires the candidate checkpoint, so without this the next
        refresh mints an empty candidate and re-embeds the whole corpus to
        reproduce chunks byte-identical to ones already stored: every refresh
        costs O(corpus) provider requests where the change was one file.

        The published collection is only ever read. Publication stays an atomic
        swap into a separate collection.
        """
        managed_paths = frozenset(
            self._relative_path(file_path) for file_path in await asyncio.to_thread(self.list_files)
        )
        try:
            reused = await _shielded_write(
                asyncio.to_thread(
                    self._copy_published_vectors,
                    published_collection=published_collection,
                    candidate_vector_db=candidate_vector_db,
                    managed_paths=managed_paths,
                ),
            )
        except Exception:
            # Reuse is an optimization, so a vector store that cannot serve the
            # copy must cost a full rebuild rather than a failed refresh. The
            # partially filled candidate is reset so the rebuild starts clean.
            logger.warning(
                "Falling back to a full knowledge rebuild after the published index could not be copied",
                base_id=self.base_id,
                collection=published_collection,
                exc_info=True,
            )
            await asyncio.to_thread(reset_vector_db, candidate_vector_db)
            return {}
        logger.info(
            "Reused published knowledge vectors in a new candidate",
            base_id=self.base_id,
            collection=candidate_vector_db.collection_name,
            reused_files=len(reused),
            managed_files=len(managed_paths),
        )
        return reused

    def _candidate_holds_unclaimed_rows(
        self,
        checkpoint: CandidateCheckpoint,
        *,
        embedder: Embedder,
    ) -> bool:
        """Return whether an interrupted bulk copy is visible from candidate shape.

        The published-vector copy writes many paths before recording any claim,
        then records every copied path in one write after the last row lands.
        Its interrupted shape is therefore no claims and a non-empty collection,
        which one bounded query answers. Ordinary indexing records each file
        immediately after its rows land, so its pre-existing unclaimed window is
        bounded by the in-flight file rather than the whole copied corpus.
        """
        if checkpoint.completed:
            return False
        vector_db = build_vector_db(self._collections, checkpoint.collection, embedder=embedder)
        if not vector_db.exists():
            return False
        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        return bool(collection.get(limit=1, include=[])["ids"])

    async def _rebuild_candidate_collection(
        self,
        checkpoint: CandidateCheckpoint,
        *,
        embedder: BatchPrefetchEmbedder,
        published_collection: str | None,
    ) -> _OpenedCandidate:
        """Empty one candidate collection, seeding it when a copy source is given.

        Two orderings are load-bearing. The checkpoint names the collection
        before ``Knowledge`` is built, because Agno creates a missing collection
        on construction and a crash must never strand a collection nothing
        references. And the copy's claims are recorded in one write after the
        last row lands, never per page, because a file's chunks can span pages
        and a claim covering only some of them would publish a silently
        truncated file.

        An interrupted published copy can leave rows the checkpoint does not
        account for. That copy-specific state is recoverable rather than
        prevented: it leaves ``completed`` empty with a non-empty collection,
        which ``_candidate_holds_unclaimed_rows`` detects on the next open.
        """
        checkpoint = await asyncio.to_thread(
            save_candidate_checkpoint,
            self._base_storage_path,
            replace(checkpoint, completed={}, failed={}),
        )
        knowledge = build_knowledge(self._collections, checkpoint.collection, embedder=embedder)
        vector_db = require_chroma_vector_db(knowledge)
        await asyncio.to_thread(reset_vector_db, vector_db)
        if published_collection is None:
            return _OpenedCandidate(checkpoint=checkpoint, knowledge=knowledge, vector_db=vector_db)
        reused = await self._seed_candidate_from_published(
            candidate_vector_db=vector_db,
            published_collection=published_collection,
        )
        if reused:
            checkpoint = await _shielded_write(
                asyncio.to_thread(
                    save_candidate_checkpoint,
                    self._base_storage_path,
                    replace(checkpoint, completed=reused),
                ),
            )
        return _OpenedCandidate(checkpoint=checkpoint, knowledge=knowledge, vector_db=vector_db, reused=reused)

    async def _resume_candidate_collection(
        self,
        checkpoint: CandidateCheckpoint,
        *,
        embedder: BatchPrefetchEmbedder,
    ) -> _OpenedCandidate:
        """Continue a candidate whose recorded work is still backed by its collection.

        Probe before building ``Knowledge`` because Agno creates a missing
        collection on construction, after which an existence check always
        answers yes.
        """
        if not await asyncio.to_thread(
            chroma_collection_exists,
            self._base_storage_path,
            checkpoint.collection,
        ):
            logger.warning(
                "Knowledge candidate collection is missing; rebuilding it from scratch",
                base_id=self.base_id,
                collection=checkpoint.collection,
            )
            # Rebuilt without a copy: whatever lost this collection may equally
            # have damaged the published one, and a full rebuild is the safe repair.
            return await self._rebuild_candidate_collection(checkpoint, embedder=embedder, published_collection=None)
        knowledge = build_knowledge(self._collections, checkpoint.collection, embedder=embedder)
        return _OpenedCandidate(
            checkpoint=checkpoint,
            knowledge=knowledge,
            vector_db=require_chroma_vector_db(knowledge),
            resumed=True,
        )

    async def _open_candidate_run(self, *, force_reindex: bool = False) -> _CandidateRun:
        """Resolve the durable candidate to continue, or start one clean candidate."""
        checkpoint = await asyncio.to_thread(load_candidate_checkpoint, self._base_storage_path)
        persisted_state, cleanup_is_safe = await asyncio.to_thread(self._published_state_and_cleanup_safety)
        # A collection name is only ever recorded by a publication, so whatever
        # the state names is the live index whatever its current status says.
        # Trusting a narrower reading would let a surviving checkpoint reopen
        # the published collection, or delete it as an incompatible candidate.
        published_collection = None if persisted_state is None else persisted_state.collection

        if checkpoint is not None and not cleanup_is_safe:
            # The checkpoint may name the live collection whose identity was
            # lost with the unreadable metadata. Never resume or delete it:
            # start a fresh candidate and leave every unknown collection alone.
            logger.warning(
                "Ignoring knowledge candidate checkpoint because published metadata is unreadable",
                base_id=self.base_id,
                collection=checkpoint.collection,
            )
            checkpoint = None
        if checkpoint is not None and checkpoint.collection == published_collection:
            # The candidate already became the published index and the process
            # died before its checkpoint was cleaned up. Writing into it again
            # would mutate a live queryable index.
            await asyncio.to_thread(delete_candidate_checkpoint, self._base_storage_path)
            checkpoint = None
        if checkpoint is not None and checkpoint.settings != self._indexing_settings:
            logger.info(
                "Discarding knowledge candidate built under incompatible settings",
                base_id=self.base_id,
                collection=checkpoint.collection,
            )
            # A failed delete must not block indexing: an incompatible candidate
            # is never published or resumed, and the superseded-collection sweep
            # below reclaims it on this same run, or on a later one.
            await delete_collection(self._collections, checkpoint.collection)
            await asyncio.to_thread(delete_candidate_checkpoint, self._base_storage_path)
            checkpoint = None
        if checkpoint is not None and force_reindex:
            # A refresh that never published leaves its candidate behind, and
            # that candidate's claims are exactly the vectors a forced rebuild
            # was asked to stop trusting. Suppressing the copy alone would keep
            # every file the interrupted build had already embedded.
            logger.info(
                "Discarding knowledge candidate because a rebuild was forced",
                base_id=self.base_id,
                collection=checkpoint.collection,
                completed=len(checkpoint.completed),
            )
            await delete_collection(self._collections, checkpoint.collection)
            await asyncio.to_thread(delete_candidate_checkpoint, self._base_storage_path)
            checkpoint = None

        embedder = BatchPrefetchEmbedder(inner=create_configured_embedder(self.config, self.runtime_paths))
        rebuild = checkpoint is None
        if checkpoint is None:
            checkpoint = CandidateCheckpoint(
                collection=candidate_collection_name(self._collections),
                settings=self._indexing_settings,
            )
        elif await asyncio.to_thread(self._candidate_holds_unclaimed_rows, checkpoint, embedder=embedder):
            logger.warning(
                "Rebuilding a knowledge candidate holding vectors it never claimed",
                base_id=self.base_id,
                collection=checkpoint.collection,
            )
            rebuild = True

        if rebuild:
            opened = await self._rebuild_candidate_collection(
                checkpoint,
                embedder=embedder,
                published_collection=None if force_reindex else self._reusable_published_collection(persisted_state),
            )
        else:
            opened = await self._resume_candidate_collection(checkpoint, embedder=embedder)
        checkpoint = opened.checkpoint

        run = _CandidateRun(
            checkpoint=checkpoint,
            knowledge=opened.knowledge,
            vector_db=opened.vector_db,
            embedder=embedder,
            completed=dict(checkpoint.completed),
            failed=dict(checkpoint.failed),
            # Rows this process just copied need no vector-existence probe: the
            # copy already reported which paths actually received them.
            verified=set(opened.reused),
            journal_appends=checkpoint.replayed_journal_entries,
            resumed=opened.resumed,
        )
        # Reconcile candidates abandoned by earlier crashed refreshes now, so
        # storage stays bounded even when a build never reaches publication.
        if cleanup_is_safe:
            preserved = {name for name in (checkpoint.collection, published_collection) if name is not None}
            await asyncio.to_thread(
                cleanup_superseded_collections,
                self._collections,
                vector_db=self._knowledge.vector_db,
                preserved=frozenset(preserved),
                candidates_only=True,
            )
        else:
            logger.warning(
                "Skipping knowledge candidate cleanup because published metadata is unreadable",
                base_id=self.base_id,
            )
        return run

    def _published_state_and_cleanup_safety(self) -> tuple[PublishedIndexState | None, bool]:
        """Return the persisted state, and whether candidate cleanup may run at all.

        A published collection is itself candidate-named, so the only proof of
        which candidate-prefixed collections are superseded is this state file.
        If it exists but yields no state, nothing about the live index can be
        proven, and cleanup is skipped rather than guessing from a rawer read
        of the same bytes and risking the last good index.

        One read answers both questions: an in-progress or failed record still
        parses and still names whatever collection the last publication left
        live, so protecting it never needs a second, looser look.
        """
        state = load_published_index_state(self._indexing_settings_path)
        return state, state is not None or not self._indexing_settings_path.exists()

    async def discard_superseded_candidate(self, *, published_collection: str | None) -> None:
        """Drop candidate state that publishing an unchanged index made obsolete.

        A forced rebuild interrupted part-way leaves a candidate behind. If the
        next refresh finds the source unchanged it republishes the existing
        index and returns before the candidate is ever opened, so nothing else
        can reach that state: the checkpoint and its collection would otherwise
        sit on disk indefinitely.
        Retiring it discards partial forced-rebuild progress, so a later forced
        rebuild starts from zero.
        """
        checkpoint = await asyncio.to_thread(load_candidate_checkpoint, self._base_storage_path)
        if checkpoint is None:
            return
        if checkpoint.collection != published_collection and not await delete_collection(
            self._collections,
            checkpoint.collection,
        ):
            return
        await asyncio.to_thread(delete_candidate_checkpoint, self._base_storage_path)
        logger.info(
            "Discarded knowledge candidate superseded by an unchanged published index",
            base_id=self.base_id,
            collection=checkpoint.collection,
            completed=len(checkpoint.completed),
        )

    async def _file_signatures_for(self, files: Sequence[Path]) -> dict[str, tuple[FileSignature, Path]]:
        """Return current signatures for the listed files, skipping vanished ones."""

        def _scan(batch: Sequence[Path]) -> list[tuple[str, FileSignature, Path]]:
            scanned: list[tuple[str, FileSignature, Path]] = []
            for file_path in batch:
                relative_path = self._relative_path(file_path)
                try:
                    signature = self._file_signature(file_path)
                except OSError:
                    continue
                scanned.append((relative_path, signature, file_path))
            return scanned

        signatures: dict[str, tuple[FileSignature, Path]] = {}
        for start in range(0, len(files), _SIGNATURE_SCAN_CHUNK):
            for relative_path, signature, file_path in await asyncio.to_thread(
                _scan,
                files[start : start + _SIGNATURE_SCAN_CHUNK],
            ):
                signatures[relative_path] = (signature, file_path)
        return signatures

    async def _drop_candidate_paths(self, run: _CandidateRun, relative_paths: Sequence[str]) -> None:
        """Remove candidate vectors and checkpoint entries for gone or stale paths."""
        if not relative_paths:
            return
        await asyncio.to_thread(delete_source_path_vectors, run.vector_db, relative_paths)
        for relative_path in relative_paths:
            run.completed.pop(relative_path, None)
            run.failed.pop(relative_path, None)
            run.verified.discard(relative_path)
        await asyncio.to_thread(
            append_candidate_journal,
            self._base_storage_path,
            removed=tuple(relative_paths),
        )
        run.journal_appends += len(relative_paths)

    async def _restamp_candidate_paths(
        self,
        run: _CandidateRun,
        restamped: Sequence[tuple[str, FileSignature]],
    ) -> None:
        """Adopt new mtimes for files whose content is unchanged."""
        for relative_path, signature in restamped:
            run.completed[relative_path] = signature
        await asyncio.to_thread(
            append_candidate_journal,
            self._base_storage_path,
            completed=tuple(restamped),
        )
        run.journal_appends += len(restamped)
        logger.info(
            "Kept knowledge candidate vectors whose content is unchanged",
            base_id=self.base_id,
            count=len(restamped),
        )

    async def _reconcile_candidate(
        self,
        run: _CandidateRun,
        files: Sequence[Path],
    ) -> _CandidateReconciliation:
        """Align the durable candidate with the current source listing."""
        # ``vanished`` describes files lost during one indexing pass, so it must
        # not outlive the pass and permanently exclude a path that came back.
        run.vanished.clear()
        signatures = await self._file_signatures_for(files)
        present = set(signatures)

        # Vectors are dropped for paths that left the corpus and for paths whose
        # content changed: a changed file whose re-index later fails must not
        # leave either a stale checkpoint claim or stale vectors behind.
        gone = (set(run.completed) | set(run.failed)) - present
        changed: set[str] = set()
        restamped: list[tuple[str, FileSignature]] = []
        for relative_path in set(run.completed) & present:
            recorded = run.completed[relative_path]
            current = signatures[relative_path][0]
            # Git checkouts and archive restores may change only mtime. Size and
            # digest are the content identity that decides whether vectors survive.
            if recorded[1:] != current[1:]:
                changed.add(relative_path)
            elif recorded != current:
                # Same bytes, new mtime: keep the vectors and adopt the new
                # stamp so the candidate signature can still match the source.
                restamped.append((relative_path, current))
        removed = tuple(sorted(gone | changed))
        if removed:
            await self._drop_candidate_paths(run, removed)
        if restamped:
            await self._restamp_candidate_paths(run, restamped)

        unverified = sorted((set(run.completed) & present) - run.verified)
        missing_vectors = await self._candidate_paths_missing_vectors(run, unverified)
        if missing_vectors:
            logger.warning(
                "Knowledge candidate entries lost their vectors; requeueing them",
                base_id=self.base_id,
                collection=run.checkpoint.collection,
                count=len(missing_vectors),
            )
            await self._drop_candidate_paths(run, sorted(missing_vectors))

        pending = tuple(
            file_path
            for relative_path, (_signature, file_path) in sorted(signatures.items())
            if relative_path not in run.completed or relative_path in run.failed
        )
        run.total_files = len(present)
        return _CandidateReconciliation(expected=frozenset(present), pending=pending)

    async def _persist_candidate_batch(self, run: _CandidateRun, batch: Sequence[Path]) -> None:
        """Durably record finished files' outcomes on the candidate."""
        completed: list[tuple[str, FileSignature]] = []
        failed: list[tuple[str, CandidateFailure]] = []
        for file_path in batch:
            relative_path = self._relative_path(file_path)
            signature = run.completed.get(relative_path)
            if signature is not None:
                run.failed.pop(relative_path, None)
                completed.append((relative_path, signature))
            elif relative_path not in run.vanished:
                previous = run.failed.get(relative_path)
                failure = CandidateFailure(
                    attempts=(previous.attempts if previous is not None else 0) + 1,
                    last_error=self._file_index_errors.get(relative_path),
                    last_attempt_at=datetime.now(tz=UTC).isoformat(),
                )
                run.failed[relative_path] = failure
                failed.append((relative_path, failure))
        if not completed and not failed:
            return
        await asyncio.to_thread(
            append_candidate_journal,
            self._base_storage_path,
            completed=tuple(completed),
            failed=tuple(failed),
        )
        run.journal_appends += len(completed) + len(failed)

    async def _compact_candidate_checkpoint(self, run: _CandidateRun, *, force: bool = False) -> None:
        """Fold journal appends back into the candidate snapshot."""
        if run.published:
            return
        if not force and run.journal_appends < _CANDIDATE_JOURNAL_COMPACT_ENTRIES:
            return
        run.checkpoint = await asyncio.to_thread(
            save_candidate_checkpoint,
            self._base_storage_path,
            replace(
                run.checkpoint,
                status="failed" if run.failed else "building",
                completed=dict(run.completed),
                failed=dict(run.failed),
                # The target revision advances only once the reconciled state
                # it describes is about to be durable.
                target_revision=self.git_source.last_synced_head,
                # The corpus this candidate targets, not a high-water mark of
                # completed files: status subtracts completed from this to
                # report how much work is still outstanding.
                total_files=run.total_files,
            ),
        )
        run.journal_appends = 0

    def _record_refresh_error(self, detail: str) -> None:
        """Store why this refresh failed, redacted once at the point of record.

        Redacting here rather than when the outcome is built keeps the stored
        string and the reported one identical, so an operator reading the
        attribute in a debugger sees exactly what was logged and persisted. It
        also keeps redaction out of the ``finally`` that owns checkpoint
        compaction, where a raise would both mask the real failure and skip
        durability work. ``redact_credentials_in_text`` is total, so that second
        reason is defence in depth rather than load-bearing -- but only for as
        long as it stays total, which is why that property has its own tests.
        """
        self._last_refresh_error = redact_credentials_in_text(detail)

    async def reindex_all(self, *, force_reindex: bool = False) -> RefreshOutcome:
        """Advance the durable candidate index and publish it when it matches the source.

        ``force_reindex`` is the operator asking for vectors to be rebuilt
        rather than trusted, so it suppresses copying them from the published
        index. Every other reason a rebuild is forced -- changed settings, a
        missing collection -- the reuse gates already reject on their own.
        """
        if not _semantic_indexing_enabled(self.config, self.base_id):
            return RefreshOutcome(indexed_count=0, published=False, error=None)

        async with self._lock:
            self._last_refresh_error = None
            self._last_file_index_error = None
            self._embedding_retry_count = 0
            self._file_index_errors.clear()
            self._embedder_failure_streak = 0
            self._global_embedder_failure = None
            run = await self._open_candidate_run(force_reindex=force_reindex)
            progress = _CandidateProgress(
                base_id=self.base_id,
                resumed=run.resumed,
                target_revision=run.checkpoint.target_revision,
                collection=run.checkpoint.collection,
                completed=len(run.completed),
            )
            try:
                await self._advance_candidate(run, progress)
            except Exception as exc:
                if self._last_refresh_error is None:
                    self._record_refresh_error(str(exc))
                raise
            finally:
                progress.retrying = self._embedding_retry_count
                outcome = RefreshOutcome(
                    indexed_count=progress.indexed_this_run,
                    published=run.published,
                    error=self._last_refresh_error,
                )
                progress.log_summary(outcome)
                await self._finalize_candidate_checkpoint(run)
            return outcome

    async def _finalize_candidate_checkpoint(self, run: _CandidateRun) -> None:
        """Compact the candidate snapshot even when the refresh is being cancelled.

        Per-batch journal appends already made progress durable, so this is a
        compaction, not the write that protects the work.
        """
        try:
            await _shielded_write(self._compact_candidate_checkpoint(run, force=True))
        except Exception:
            logger.warning(
                "Failed to compact knowledge candidate checkpoint",
                base_id=self.base_id,
                collection=run.checkpoint.collection,
                exc_info=True,
            )

    async def _source_revision(self) -> str | None:
        """Return the current Git revision, or None when the source is not Git-backed."""
        if not self.git_source.is_configured():
            return None
        return await self.git_source.head()

    async def _candidate_matches_source(
        self,
        round_revision: str | None,
        candidate_signatures: Mapping[str, FileSignature],
        candidate_source_signature: str,
    ) -> bool:
        """Return whether the candidate still matches the source after one pass.

        Two independent things must hold. The source must not have moved while the
        pass ran, and the candidate must cover every managed file: a file whose
        signature scan or read failed is dropped from the pass's own completeness
        accounting (``_file_signatures_for``, ``run.vanished``), so without a
        coverage check a transient I/O error would publish a silently truncated
        index -- and the unchanged fast path would then republish it at the same
        revision forever.

        Hashing the corpus proves both at once, but reads every byte. For a Git
        checkout the revision proves content, because the checkout is
        program-owned and realigned with a hard reset, and re-listing proves
        coverage. Neither reads file contents.
        """
        if round_revision is None:
            return await self.source_signature() == candidate_source_signature

        if await self.git_source.head() != round_revision:
            return False
        current_files = await asyncio.to_thread(self.list_files)
        return {self._relative_path(path) for path in current_files} == set(candidate_signatures)

    async def _advance_candidate(self, run: _CandidateRun, progress: _CandidateProgress) -> None:
        """Reconcile, index and publish until the candidate matches the live source."""
        for _round in range(_MAX_CANDIDATE_RECONCILE_ROUNDS):
            round_revision = await self._source_revision()
            files = await asyncio.to_thread(self.list_files)
            plan = await self._reconcile_candidate(run, files)
            progress.total = len(plan.expected)
            progress.completed = len(run.completed)
            if run.checkpoint.total_files != run.total_files:
                # Publish the corpus size as soon as it is known, so a reader
                # watching a long build sees real outstanding work instead of
                # waiting for the next journal compaction.
                await self._compact_candidate_checkpoint(run, force=True)

            if plan.pending:

                async def _record_file(file_path: Path, active_run: _CandidateRun = run) -> None:
                    await self._persist_candidate_batch(active_run, (file_path,))
                    progress.completed = len(active_run.completed)
                    progress.failed = len(active_run.failed)
                    progress.retrying = self._embedding_retry_count
                    progress.maybe_log()

                async def _record_batch(batch: Sequence[Path], active_run: _CandidateRun = run) -> None:
                    _ = batch
                    await self._compact_candidate_checkpoint(active_run)

                progress.indexed_this_run += await self._reindex_files_locked(
                    list(plan.pending),
                    knowledge=run.knowledge,
                    indexed_signatures=run.completed,
                    vanished_files=run.vanished,
                    embedder=run.embedder,
                    on_file_result=_record_file,
                    on_batch_complete=_record_batch,
                )
                progress.completed = len(run.completed)
                progress.failed = len(run.failed)

            expected_paths = set(plan.expected) - run.vanished
            unresolved = expected_paths - set(run.completed)
            if unresolved:
                summary = f"Indexed {len(run.completed)} of {len(plan.expected)} managed knowledge files"
                if self._last_file_index_error is not None:
                    summary = f"{summary} (first error: {self._last_file_index_error})"
                self._record_refresh_error(summary)
                return

            candidate_signatures = {
                relative_path: signature
                for relative_path, signature in run.completed.items()
                if relative_path in expected_paths
            }
            if set(candidate_signatures) != expected_paths:
                self._record_refresh_error(
                    f"Indexed signatures covered {len(candidate_signatures)} of {len(expected_paths)} managed files",
                )
                return

            candidate_source_signature = _source_signature_from_file_signatures(candidate_signatures)
            if not await self._candidate_matches_source(
                round_revision,
                candidate_signatures,
                candidate_source_signature,
            ):
                # The source moved while this pass ran. Keep every unchanged
                # vector and reconcile the delta instead of discarding the
                # candidate; only the changed files are re-embedded.
                logger.info(
                    "Knowledge source changed during refresh; reconciling candidate",
                    base_id=self.base_id,
                    collection=run.checkpoint.collection,
                )
                continue

            await self._publish_candidate(run, candidate_source_signature)
            return

        self._record_refresh_error(
            "Knowledge source kept changing during refresh; candidate progress was kept for the next refresh",
        )

    async def _publish_candidate(self, run: _CandidateRun, source_signature: str) -> None:
        """Publish the verified candidate and retire the state it supersedes."""
        if run.embedder is not None:
            run.embedder.clear_cache()
        publish_state = _CandidatePublishState()
        try:
            await self._publish_candidate_after_metadata_save(
                candidate_vector_db=run.vector_db,
                indexed_count=len(run.completed),
                source_signature=source_signature,
                publish_state=publish_state,
            )
        finally:
            # Publication can be cancelled after the metadata write lands. The
            # candidate is then the published index, so the checkpoint must
            # never be rewritten as if the build were still in progress.
            run.published = publish_state.index_published
        await asyncio.to_thread(delete_candidate_checkpoint, self._base_storage_path)
        await asyncio.to_thread(
            cleanup_superseded_collections,
            self._collections,
            vector_db=self._knowledge.vector_db,
            preserved=frozenset({run.vector_db.collection_name}),
        )
