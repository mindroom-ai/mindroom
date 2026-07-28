"""Knowledge base management for file-backed RAG."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import time
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol, cast, runtime_checkable
from urllib.parse import quote, urlparse, urlunparse

from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader
from agno.vectordb.chroma import ChromaDb
from chromadb.errors import InternalError, NotFoundError

from mindroom.chunking import SafeFixedSizeChunking
from mindroom.constants import (
    DEFAULT_MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES,
    KNOWLEDGE_FILE_INDEX_CONCURRENCY_ENV,
    MAX_ALLOWED_CONCURRENT_KNOWLEDGE_FILE_INDEXES,
    RuntimePaths,
    resolve_config_relative_path,
)
from mindroom.credentials import get_runtime_shared_credentials_manager
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
    load_candidate_checkpoint,
    save_candidate_checkpoint,
)
from mindroom.knowledge.embedding_batch import (
    DEFAULT_MAX_EMBEDDING_BATCH_ITEMS,
    DEFAULT_MAX_EMBEDDING_BATCH_PAYLOAD_BYTES,
    BatchPrefetchEmbedder,
    plan_embedding_batches,
)
from mindroom.knowledge.file_listing import (
    git_checkout_present,
    git_tracked_relative_paths_from_checkout,
    include_knowledge_relative_path,
    knowledge_files_from_relative_paths,
    list_knowledge_files,
)
from mindroom.knowledge.index_metadata import (
    load_index_metadata_payload,
    parse_index_metadata_fields,
    write_index_metadata_payload,
)
from mindroom.knowledge.index_retry import EmbeddingRetryPolicy, run_with_embedding_retry
from mindroom.knowledge.indexing_config import (
    IndexingSettings,
    chroma_collection_exists,
    indexing_settings_key,
    storage_key_for_base,
)
from mindroom.knowledge.redaction import (
    credential_free_repo_url,
    embedded_http_userinfo,
    redact_credentials_in_text,
    redact_url_credentials,
)
from mindroom.logging_config import get_logger
from mindroom.strict_knowledge import StrictInsertKnowledge as Knowledge

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping, Sequence
    from pathlib import Path

    from agno.knowledge.document.base import Document
    from agno.knowledge.embedder.base import Embedder
    from agno.knowledge.reader.base import Reader
    from chromadb.api.models.Collection import Collection

    from mindroom.config.knowledge import KnowledgeGitConfig
    from mindroom.config.main import Config

logger = get_logger(__name__)

_COLLECTION_PREFIX = "mindroom_knowledge"
_SOURCE_PATH_KEY = "source_path"
_SOURCE_MTIME_NS_KEY = "source_mtime_ns"
_SOURCE_SIZE_KEY = "source_size"
_SOURCE_DIGEST_KEY = "source_digest"
_POST_INDEX_VECTOR_VISIBILITY_RETRY_DELAYS_SECONDS = (0.0, 0.01, 0.05)
_INDEXING_STATUS_RESETTING = "resetting"
_INDEXING_STATUS_INDEXING = "indexing"
_INDEXING_STATUS_COMPLETE = "complete"
_INDEXING_STATUSES = {
    _INDEXING_STATUS_RESETTING,
    _INDEXING_STATUS_INDEXING,
    _INDEXING_STATUS_COMPLETE,
}
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
#: Completed candidate entries whose vectors are confirmed in one Chroma query.
#: Only a starting point: the query splits itself when the store refuses it,
#: because the real limit is matched rows, which this cannot know up front.
_VECTOR_VERIFY_BATCH = 128
#: Source paths whose vectors are dropped in one Chroma delete. Independent of
#: the verify batch: a delete binds no variable per matched row, so this bounds
#: only how much work one call does.
_VECTOR_DELETE_BATCH = 128
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


@runtime_checkable
class _CollectionListingClient(Protocol):
    """Vector client surface needed for best-effort collection cleanup."""

    def list_collections(self) -> list[object]:
        """Return collection names or collection objects."""
        ...


@runtime_checkable
class _NamedCollection(Protocol):
    """Collection object shape returned by Chroma clients."""

    name: str


@dataclass(frozen=True)
class _PersistedIndexState:
    settings: IndexingSettings
    status: Literal["resetting", "indexing", "complete"]
    collection: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    indexed_count: int | None = None
    source_signature: str | None = None


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

    def log_summary(self, *, published: bool, error: str | None) -> None:
        """Emit the single terminal summary for this refresh."""
        logger.info(
            "knowledge_candidate_finished",
            published=published,
            error=error,
            **self._fields(),
        )


class _PermanentEmbeddingError(Exception):
    """Internal signal that no further file in this refresh can be embedded.

    Raised instead of grinding one doomed provider request per remaining file
    when the embedder rejects work for a reason retrying cannot fix.
    """


def _raise_cancelled() -> NoReturn:
    raise asyncio.CancelledError


def _iter_file_batches(files: Sequence[Path], batch_size: int) -> Iterator[list[Path]]:
    """Yield bounded slices so a huge corpus never becomes one huge fan-out."""
    size = max(batch_size, 1)
    for start in range(0, len(files), size):
        yield list(files[start : start + size])


def _collection_has_source_path(collection: Collection, relative_path: str) -> bool:
    """Return whether one source path has any vector, at one row of cost.

    Chroma binds one SQL variable per *returned* row, so an unbounded probe on
    a heavily chunked file exceeds SQLite's ceiling and fails outright. One row
    is all existence needs, and ``limit=1`` is what keeps this answerable at
    any file size. Do not widen it.
    """
    result = collection.get(where={_SOURCE_PATH_KEY: relative_path}, limit=1, include=[])
    return bool(result.get("ids"))


def _paths_with_vectors(collection: Collection, relative_paths: Sequence[str]) -> set[str]:
    """Return which of `relative_paths` have at least one vector in the collection.

    Chroma binds one SQL variable per *matched row*, not one per queried path,
    so a fixed batch of paths does not bound the query at all: whether it fits
    under SQLite's ceiling depends on how many chunks those particular files
    produced. That makes any batch size chosen up front a gamble. Small files
    leave a batch of 128 far below the ceiling, a handful of large ones puts
    the same batch over it, and once over, every verification query for that
    base fails identically and the candidate is stranded for good.

    So the batch is not guessed, it is *adapted*: ask for the whole thing, and
    on refusal halve it and ask again. Each split halves the matched rows too,
    so it converges, and a store that can answer the batch pays nothing.

    A single path is the floor, where splitting can no longer help, so it is
    asked for a single row instead, which stays under any ceiling however many
    chunks the file has. That floor is what makes the recursion total, and it
    is also where a failure that was never about query size finally surfaces.
    """
    if len(relative_paths) == 1:
        relative_path = relative_paths[0]
        return {relative_path} if _collection_has_source_path(collection, relative_path) else set()

    try:
        result = collection.get(where={_SOURCE_PATH_KEY: {"$in": list(relative_paths)}}, include=["metadatas"])
    except NotFoundError:
        # Splitting answers "the store refused this query for its size". A
        # collection that is gone is not that, and stays gone however small the
        # query gets, so descending would only cost log2(batch) + 1 doomed
        # queries before raising exactly the same error from the first leaf.
        raise
    except InternalError as error:
        if "too many SQL variables" not in str(error):
            raise
        # Splitting stays correct but multiplies the queries, and how far it
        # degrades depends on chunk counts nothing here can see. Without a
        # trace, a base whose files outgrew the ceiling just gets quietly
        # slower -- the same invisibility that let the unsplit query strand
        # candidates undiagnosed.
        logger.debug("Split a refused knowledge vector verification query", paths=len(relative_paths))
        midpoint = len(relative_paths) // 2
        return _paths_with_vectors(collection, relative_paths[:midpoint]) | _paths_with_vectors(
            collection,
            relative_paths[midpoint:],
        )

    found: set[str] = set()
    for metadata in result.get("metadatas") or []:
        source_path = metadata.get(_SOURCE_PATH_KEY)
        if isinstance(source_path, str):
            found.add(source_path)
    return found


def _require_chroma_vector_db(knowledge: Knowledge) -> ChromaDb:
    vector_db = knowledge.vector_db
    if not isinstance(vector_db, ChromaDb):
        msg = "Knowledge reindex candidate collection requires a ChromaDb vector database"
        raise TypeError(msg)
    return vector_db


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


def _collection_name(base_id: str, knowledge_path: Path) -> str:
    return f"{_COLLECTION_PREFIX}_{storage_key_for_base(base_id, knowledge_path)}"


def _semantic_indexing_enabled(config: Config, base_id: str) -> bool:
    return config.get_knowledge_base_config(base_id).mode == "semantic"


def _authenticated_repo_url(
    repo_url: str,
    credentials_service: str | None,
    runtime_paths: RuntimePaths,
) -> str:
    """Inject HTTPS credentials from CredentialsManager into a repository URL."""
    if not credentials_service:
        return repo_url

    credentials = get_runtime_shared_credentials_manager(runtime_paths).load_credentials(credentials_service) or {}
    username = credentials.get("username")
    token = credentials.get("token") or credentials.get("api_key")
    password = credentials.get("password")

    if not isinstance(username, str) and token and not password:
        username = "x-access-token"

    if not isinstance(username, str) or not username:
        return repo_url

    secret: str | None
    if isinstance(password, str) and password:
        secret = password
    elif isinstance(token, str) and token:
        secret = token
    else:
        secret = None

    if secret is None:
        return repo_url

    parsed = urlparse(repo_url)
    if parsed.scheme not in {"http", "https"}:
        return repo_url

    hostname = parsed.netloc.split("@")[-1]
    auth_netloc = f"{quote(username, safe='')}:{quote(secret, safe='')}@{hostname}"
    return urlunparse(parsed._replace(netloc=auth_netloc))


def _credentials_service_http_userinfo(
    credentials_service: str | None,
    runtime_paths: RuntimePaths,
) -> tuple[str, str] | None:
    if not credentials_service:
        return None

    credentials = get_runtime_shared_credentials_manager(runtime_paths).load_credentials(credentials_service) or {}
    username = credentials.get("username")
    token = credentials.get("token") or credentials.get("api_key")
    password = credentials.get("password")

    if not isinstance(username, str) and token and not password:
        username = "x-access-token"

    if not isinstance(username, str) or not username:
        return None

    if isinstance(password, str) and password:
        return username, password
    if isinstance(token, str) and token:
        return username, token
    return None


def _git_http_basic_auth_env(clean_url: str, username: str, secret: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{secret}".encode()).decode("ascii")
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{clean_url}.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
    }


def _git_auth_env(
    repo_url: str,
    credentials_service: str | None,
    runtime_paths: RuntimePaths,
) -> dict[str, str] | None:
    """Return process-local Git config that injects credentials without persisting them."""
    clean_url = credential_free_repo_url(repo_url)
    parsed_clean_url = urlparse(clean_url)

    embedded_userinfo = embedded_http_userinfo(repo_url)
    if embedded_userinfo is not None:
        return _git_http_basic_auth_env(clean_url, *embedded_userinfo)

    credentials_userinfo = (
        _credentials_service_http_userinfo(credentials_service, runtime_paths)
        if parsed_clean_url.scheme in {"http", "https"}
        else None
    )
    if credentials_userinfo is not None:
        return _git_http_basic_auth_env(clean_url, *credentials_userinfo)

    authenticated_url = (
        repo_url if clean_url != repo_url else _authenticated_repo_url(clean_url, credentials_service, runtime_paths)
    )
    if authenticated_url == clean_url:
        return None
    parsed_authenticated_url = urlparse(authenticated_url)
    if parsed_authenticated_url.netloc and "@" in parsed_authenticated_url.netloc:
        return None
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"url.{authenticated_url}.insteadOf",
        "GIT_CONFIG_VALUE_0": clean_url,
    }


def _merge_git_env(*envs: dict[str, str] | None) -> dict[str, str] | None:
    merged: dict[str, str] = {}
    for env in envs:
        if env:
            merged.update(env)
    return merged or None


def _file_content_digest(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def knowledge_source_signature(
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
    _indexing_settings: IndexingSettings = field(init=False)
    _base_storage_path: Path = field(init=False)
    _indexing_settings_path: Path = field(init=False)
    _git_lfs_hydrated_head_path: Path = field(init=False)
    _knowledge: Knowledge = field(init=False)
    _indexed_files: set[str] = field(default_factory=set, init=False)
    _indexed_signatures: dict[str, FileSignature] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _git_sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _git_last_successful_commit: str | None = field(default=None, init=False)
    _last_refresh_error: str | None = field(default=None, init=False)
    _last_file_index_error: str | None = field(default=None, init=False)
    _git_lfs_checked: bool = field(default=False, init=False)
    _git_lfs_repository_ready: bool = field(default=False, init=False)
    _git_tracked_relative_paths: set[str] | None = field(default=None, init=False, repr=False)
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
        self._git_lfs_hydrated_head_path = self._base_storage_path / "git_lfs_hydrated_head.txt"
        persisted_state = self._load_persisted_index_state()
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
            else self._default_collection_name()
        )
        self._knowledge = self._build_knowledge(collection_name)

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

    def _persisted_collection_missing(self, persisted_state: _PersistedIndexState | None) -> bool:
        if persisted_state is None or persisted_state.status != _INDEXING_STATUS_COMPLETE:
            return False
        collection_name = persisted_state.collection or self._default_collection_name()
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

    def _load_persisted_index_state(self) -> _PersistedIndexState | None:
        payload = load_index_metadata_payload(self._indexing_settings_path)
        if payload is None:
            return None
        fields = parse_index_metadata_fields(
            payload,
            allowed_statuses=_INDEXING_STATUSES,
            require_complete_fields_for_all_statuses=True,
        )
        if fields is None:
            return None
        (
            settings,
            status,
            collection,
            last_published_at,
            published_revision,
            indexed_count,
            source_signature,
        ) = fields
        indexing_settings = IndexingSettings.from_metadata(settings)
        if indexing_settings is None:
            return None
        return _PersistedIndexState(
            indexing_settings,
            cast('Literal["resetting", "indexing", "complete"]', status),
            collection=collection,
            last_published_at=last_published_at,
            published_revision=published_revision,
            indexed_count=indexed_count,
            source_signature=source_signature,
        )

    def _save_persisted_index_state(
        self,
        status: Literal["resetting", "indexing", "complete"],
        *,
        settings: IndexingSettings | None = None,
        collection: str | None = None,
        last_published_at: str | None = None,
        published_revision: str | None = None,
        indexed_count: int | None = None,
        source_signature: str | None = None,
    ) -> None:
        write_index_metadata_payload(
            self._indexing_settings_path,
            settings=(settings or self._indexing_settings).to_metadata(),
            status=status,
            collection=collection,
            last_published_at=last_published_at,
            published_revision=published_revision,
            indexed_count=indexed_count,
            source_signature=source_signature,
        )

    def _load_git_lfs_hydrated_head(self) -> str | None:
        try:
            hydrated_head = self._git_lfs_hydrated_head_path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return hydrated_head or None

    def _save_git_lfs_hydrated_head(self, head: str) -> None:
        self._git_lfs_hydrated_head_path.write_text(head, encoding="utf-8")

    def _clear_git_lfs_hydrated_head(self) -> None:
        self._git_lfs_hydrated_head_path.unlink(missing_ok=True)

    def _has_existing_index(self) -> bool:
        vector_db = self._knowledge.vector_db
        return isinstance(vector_db, ChromaDb) and vector_db.exists()

    def _needs_full_reindex_on_create(self) -> bool:
        if self._persisted_collection_missing_on_init:
            return True
        persisted_state = self._load_persisted_index_state()
        if persisted_state is None:
            return self._indexing_settings_path.exists() and self._has_existing_index()
        return (
            persisted_state.settings != self._indexing_settings or persisted_state.status == _INDEXING_STATUS_RESETTING
        )

    def _git_config(self) -> KnowledgeGitConfig | None:
        return self.config.get_knowledge_base_config(self.base_id).git

    def _git_uses_lfs(self) -> bool:
        git_config = self._git_config()
        return bool(git_config and git_config.lfs)

    def _git_sync_timeout_seconds(self) -> float | None:
        git_config = self._git_config()
        if git_config is None:
            return None
        return float(git_config.sync_timeout_seconds)

    async def _git_checkout_present(self) -> bool:
        return await asyncio.to_thread(
            git_checkout_present,
            self._knowledge_source_path(),
            timeout_seconds=self._git_sync_timeout_seconds(),
        )

    def _include_active_relative_path(self, relative_path: str) -> bool:
        return include_knowledge_relative_path(self.config, self.base_id, relative_path)

    async def _run_git(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        repo_root = cwd or self._knowledge_source_path()
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(repo_root),
            env=None if env is None else {**os.environ, **env},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            timeout_seconds = self._git_sync_timeout_seconds()
            if timeout_seconds is None:
                stdout, stderr = await process.communicate()
            else:
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
            raise
        except TimeoutError as exc:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
            command = " ".join(["git", *(redact_url_credentials(arg) for arg in args)])
            msg = f"Git command timed out after {timeout_seconds:.0f}s: {command}"
            raise RuntimeError(msg) from exc

        if process.returncode == 0:
            return stdout.decode("utf-8", errors="replace")

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        details = redact_credentials_in_text(stderr_text or stdout_text)
        command = " ".join(["git", *(redact_url_credentials(arg) for arg in args)])
        msg = f"Git command failed with exit code {process.returncode}: {command}"
        if details:
            msg = f"{msg}\n{details}"
        raise RuntimeError(msg)

    async def _ensure_git_lfs_available(self, *, cwd: Path) -> None:
        if not self._git_uses_lfs() or self._git_lfs_checked:
            return
        try:
            await self._run_git(["lfs", "version"], cwd=cwd)
        except RuntimeError as exc:
            msg = "Git LFS is required for this knowledge base but is not available in the runtime image"
            raise RuntimeError(msg) from exc
        self._git_lfs_checked = True

    async def _ensure_git_lfs_repository_ready(self, repo_root: Path) -> None:
        if not self._git_uses_lfs() or self._git_lfs_repository_ready:
            return
        await self._ensure_git_lfs_available(cwd=repo_root)
        await self._run_git(["lfs", "install", "--local"], cwd=repo_root)
        self._git_lfs_repository_ready = True

    def _git_lfs_skip_smudge_env(self, git_config: KnowledgeGitConfig) -> dict[str, str] | None:
        if not git_config.lfs:
            return None
        return {"GIT_LFS_SKIP_SMUDGE": "1"}

    def _git_lfs_pull_args(self, git_config: KnowledgeGitConfig) -> list[str]:
        return ["lfs", "pull", "origin", git_config.branch]

    async def _hydrate_git_lfs_worktree(
        self,
        git_config: KnowledgeGitConfig,
        *,
        repo_root: Path | None = None,
        current_head: str | None = None,
    ) -> None:
        if not git_config.lfs:
            return
        resolved_head = current_head or await self._git_rev_parse("HEAD")
        if resolved_head is not None:
            hydrated_head = await asyncio.to_thread(self._load_git_lfs_hydrated_head)
            if hydrated_head == resolved_head:
                return
        await self._run_git(
            self._git_lfs_pull_args(git_config),
            cwd=repo_root or self._knowledge_source_path(),
            env=_git_auth_env(git_config.repo_url, git_config.credentials_service, self.runtime_paths),
        )
        if resolved_head is None:
            resolved_head = await self._git_rev_parse("HEAD")
        if resolved_head is not None:
            await asyncio.to_thread(self._save_git_lfs_hydrated_head, resolved_head)

    async def _git_rev_parse(self, ref: str) -> str | None:
        try:
            output = await self._run_git(["rev-parse", ref])
        except RuntimeError:
            return None
        return output.strip() or None

    async def _git_list_tracked_files(self) -> set[str]:
        output = await self._run_git(["ls-files", "-z"])
        raw_paths = [entry for entry in output.split("\x00") if entry]
        tracked_files = {path for path in raw_paths if self._include_active_relative_path(path)}
        self._git_tracked_relative_paths = set(tracked_files)
        return tracked_files

    async def _ensure_git_repository(self, git_config: KnowledgeGitConfig) -> bool:
        runtime_paths = self.runtime_paths
        knowledge_root = self._knowledge_source_path()
        if await self._git_checkout_present():
            await self._ensure_git_lfs_repository_ready(knowledge_root)
            current_remote = (await self._run_git(["remote", "get-url", "origin"])).strip()
            expected_remote = credential_free_repo_url(git_config.repo_url)
            if current_remote != expected_remote:
                await self._run_git(["remote", "set-url", "origin", expected_remote])
            return False

        if knowledge_root.exists() and any(knowledge_root.iterdir()):
            msg = (
                f"Cannot clone knowledge git repository into non-empty path {knowledge_root}. "
                "Clear the folder or use a dedicated path."
            )
            raise RuntimeError(msg)

        knowledge_root.parent.mkdir(parents=True, exist_ok=True)
        if git_config.lfs:
            await self._ensure_git_lfs_available(cwd=knowledge_root.parent)
        clone_url = credential_free_repo_url(git_config.repo_url)
        await self._run_git(
            [
                "clone",
                "--single-branch",
                "--branch",
                git_config.branch,
                clone_url,
                str(knowledge_root),
            ],
            cwd=knowledge_root.parent,
            env=_merge_git_env(
                _git_auth_env(git_config.repo_url, git_config.credentials_service, runtime_paths),
                self._git_lfs_skip_smudge_env(git_config),
            ),
        )
        await self._run_git(["remote", "set-url", "origin", clone_url], cwd=knowledge_root)
        await asyncio.to_thread(self._clear_git_lfs_hydrated_head)
        await self._ensure_git_lfs_repository_ready(knowledge_root)
        await self._hydrate_git_lfs_worktree(git_config, repo_root=knowledge_root)
        return True

    async def _sync_git_source_once(self, git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        cloned = await self._ensure_git_repository(git_config)
        if cloned:
            return await self._git_list_tracked_files(), set(), True

        before_head = await self._git_rev_parse("HEAD")

        remote_ref = f"origin/{git_config.branch}"
        await self._run_git(
            ["fetch", "origin", f"+refs/heads/{git_config.branch}:refs/remotes/{remote_ref}"],
            env=_git_auth_env(git_config.repo_url, git_config.credentials_service, self.runtime_paths),
        )
        remote_head = await self._git_rev_parse(remote_ref)
        if remote_head is None:
            msg = f"Could not resolve remote ref '{remote_ref}' for knowledge base '{self.base_id}'"
            raise RuntimeError(msg)

        if before_head == remote_head:
            await self._hydrate_git_lfs_worktree(git_config, current_head=remote_head)
            return set(), set(), False

        before_files = await self._git_list_tracked_files()

        await self._run_git(
            ["checkout", "--force", "-B", git_config.branch, remote_ref],
            env=self._git_lfs_skip_smudge_env(git_config),
        )
        # Reviewed with Bas (2026-04-17): program-owned checkout, hard reset is the
        # intentional way to realign it with the configured remote state.
        await self._run_git(["reset", "--hard", remote_ref], env=self._git_lfs_skip_smudge_env(git_config))
        await self._hydrate_git_lfs_worktree(git_config, current_head=remote_head)

        after_files = await self._git_list_tracked_files()
        if before_head is None:
            changed_paths = after_files
        else:
            diff_output = await self._run_git(["diff", "--name-only", "--no-renames", f"{before_head}..HEAD"])
            changed_paths = {path for path in diff_output.splitlines() if self._include_active_relative_path(path)}

        removed_files = before_files - after_files
        changed_files = {path for path in changed_paths if path in after_files} | (after_files - before_files)
        return changed_files, removed_files, True

    def list_files(self) -> list[Path]:
        """List all files currently present in the knowledge folder."""
        knowledge_root = self._knowledge_source_path()
        if self._git_config() is not None:
            if self._git_tracked_relative_paths is None:
                if not git_checkout_present(knowledge_root, timeout_seconds=self._git_sync_timeout_seconds()):
                    return []
                self._git_tracked_relative_paths = git_tracked_relative_paths_from_checkout(
                    self.config,
                    self.base_id,
                    knowledge_root,
                )
            return knowledge_files_from_relative_paths(
                self.config,
                self.base_id,
                knowledge_root,
                self._git_tracked_relative_paths,
            )
        return list_knowledge_files(self.config, self.base_id, knowledge_root)

    def _relative_path(self, file_path: Path) -> str:
        return file_path.relative_to(self._knowledge_source_path()).as_posix()

    def _file_signature(self, file_path: Path) -> FileSignature:
        stat = file_path.stat()
        return stat.st_mtime_ns, stat.st_size, _file_content_digest(file_path)

    def _has_vectors_for_source_path(
        self,
        relative_path: str,
        *,
        knowledge: Knowledge | None = None,
    ) -> bool:
        target_knowledge = knowledge or self._knowledge
        vector_db = target_knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return True
        if not vector_db.exists():
            return False

        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        return _collection_has_source_path(collection, relative_path)

    async def _wait_for_source_vectors(
        self,
        relative_path: str,
        *,
        knowledge: Knowledge | None = None,
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

    def _build_reader(self, file_path: Path) -> Reader:
        """Build a per-file reader with conservative chunking for text-like content."""
        reader = ReaderFactory.get_reader_for_extension(file_path.suffix.lower())

        # Large markdown/plain-text files are the common source of oversized embed requests.
        if not isinstance(reader, (TextReader, MarkdownReader)):
            return reader

        chunking_strategy = self._chunking_strategy()
        configured_reader = deepcopy(reader)
        configured_reader.chunk = True
        configured_reader.chunk_size = chunking_strategy.chunk_size
        configured_reader.chunking_strategy = chunking_strategy
        return configured_reader

    def _default_collection_name(self) -> str:
        return _collection_name(self.base_id, self._knowledge_source_path())

    def _candidate_collection_name(self) -> str:
        return f"{self._default_collection_name()}_candidate_{uuid.uuid4().hex[:16]}"

    def _build_vector_db(self, collection_name: str, *, embedder: Embedder | None = None) -> ChromaDb:
        return ChromaDb(
            collection=collection_name,
            path=str(self._base_storage_path),
            persistent_client=True,
            embedder=embedder if embedder is not None else create_configured_embedder(self.config, self.runtime_paths),
        )

    def _build_knowledge(self, collection_name: str, *, embedder: Embedder | None = None) -> Knowledge:
        return Knowledge(vector_db=self._build_vector_db(collection_name, embedder=embedder))

    def _cleanup_superseded_collections(
        self,
        *,
        preserved: frozenset[str],
        candidates_only: bool = False,
    ) -> None:
        """Delete this base's superseded collections, preserving proven-live ones.

        Ownership is proven by name: both the default collection and the
        candidate prefix embed this base's identity and resolved source path,
        and both live in this base's own private storage directory. Anything
        else in that directory is left alone and reported rather than deleted,
        because nothing here can prove who owns it.
        """
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return
        client = vector_db.client
        if client is None or not isinstance(client, _CollectionListingClient):
            return

        default_collection = self._default_collection_name()
        candidate_prefix = f"{default_collection}_candidate_"

        try:
            collection_names = self._listed_collection_names(client)
        except Exception:
            logger.warning(
                "Failed to list superseded knowledge collections for cleanup",
                base_id=self.base_id,
                exc_info=True,
            )
            return

        unowned: list[str] = []
        for collection_name in collection_names:
            if collection_name in preserved:
                continue
            is_candidate = collection_name.startswith(candidate_prefix)
            if not is_candidate and (candidates_only or collection_name != default_collection):
                # Reclaiming abandoned candidates must never race a legacy
                # published collection whose metadata predates this layout.
                if collection_name != default_collection:
                    unowned.append(collection_name)
                continue
            try:
                self._build_vector_db(collection_name).delete()
            except Exception:
                logger.warning(
                    "Failed to clean superseded knowledge collection",
                    base_id=self.base_id,
                    collection=collection_name,
                    exc_info=True,
                )
        if unowned:
            logger.info(
                "Preserved knowledge collections with unprovable ownership",
                base_id=self.base_id,
                collections=sorted(unowned),
            )

    def _listed_collection_names(self, client: _CollectionListingClient) -> tuple[str, ...]:
        names: list[str] = []
        for collection in client.list_collections():
            if isinstance(collection, str):
                names.append(collection)
            elif isinstance(collection, _NamedCollection):
                names.append(collection.name)
        return tuple(dict.fromkeys(names))

    def _reset_vector_db(self, vector_db: ChromaDb) -> None:
        vector_db.delete()
        vector_db.create()

    async def _save_candidate_publish_metadata(
        self,
        *,
        candidate_vector_db: ChromaDb,
        indexed_count: int,
        source_signature: str,
    ) -> bool:
        save_task = asyncio.create_task(
            asyncio.to_thread(
                self._save_persisted_index_state,
                _INDEXING_STATUS_COMPLETE,
                collection=candidate_vector_db.collection_name,
                last_published_at=datetime.now(tz=UTC).isoformat(),
                published_revision=self._git_last_successful_commit,
                indexed_count=indexed_count,
                source_signature=source_signature,
            ),
        )
        try:
            await asyncio.shield(save_task)
        except asyncio.CancelledError:
            await save_task
            return True
        return False

    async def _adopt_candidate_vector_db(
        self,
        *,
        candidate_vector_db: ChromaDb,
        indexed_files: set[str],
        indexed_signatures: dict[str, FileSignature],
    ) -> None:
        self._knowledge.vector_db = candidate_vector_db
        async with self._state_lock:
            self._indexed_files = indexed_files
            self._indexed_signatures = indexed_signatures

    async def _publish_candidate_after_metadata_save(
        self,
        *,
        candidate_vector_db: ChromaDb,
        indexed_files: set[str],
        indexed_signatures: dict[str, FileSignature],
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
        await self._adopt_candidate_vector_db(
            candidate_vector_db=candidate_vector_db,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )
        if publish_cancelled:
            _raise_cancelled()

    async def sync_git_source(self) -> dict[str, Any]:
        """Fetch and force-align one configured Git repository checkout."""
        git_config = self._git_config()
        if git_config is None:
            return {"updated": False, "changed_count": 0, "removed_count": 0}

        async with self._git_sync_lock:
            changed_files, removed_files, updated = await self._sync_git_source_once(git_config)
            current_head = await self._git_rev_parse("HEAD")
            self._git_last_successful_commit = current_head

        if updated:
            logger.info(
                "Knowledge Git repository synchronized",
                base_id=self.base_id,
                repo_url=redact_url_credentials(git_config.repo_url),
                branch=git_config.branch,
                changed_count=len(changed_files),
                removed_count=len(removed_files),
                commit=current_head,
            )
        return {
            "updated": updated,
            "changed_count": len(changed_files),
            "removed_count": len(removed_files),
        }

    async def _index_file_locked(
        self,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: Knowledge | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, FileSignature] | None = None,
    ) -> bool:
        """Index one file while the caller owns the operation lock."""
        relative_path = self._relative_path(resolved_path)
        source_mtime_ns, source_size, source_digest = await asyncio.to_thread(self._file_signature, resolved_path)
        metadata = {
            _SOURCE_PATH_KEY: relative_path,
            _SOURCE_MTIME_NS_KEY: source_mtime_ns,
            _SOURCE_SIZE_KEY: source_size,
            _SOURCE_DIGEST_KEY: source_digest,
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
        target_knowledge = knowledge or self._knowledge

        async def _insert_once() -> None:
            if upsert:
                # Agno/Chroma upsert keys by content hash, so stale chunks from an older
                # version of the same file can remain unless we clear by source metadata first.
                await asyncio.to_thread(target_knowledge.remove_vectors_by_metadata, {_SOURCE_PATH_KEY: relative_path})
            # Knowledge.ainsert is async by name only: it eventually calls into the
            # vector database's synchronous batch upsert (e.g. ChromaDB's Rust
            # _upsert) on the running event loop, blocking every other coroutine
            # for as long as the embed+upsert batch takes. Use the sync insert API
            # via asyncio.to_thread so embedding + vector database work runs on a
            # worker thread and the loop stays responsive to Matrix sync, tool
            # calls, and cache writes.
            await asyncio.to_thread(
                target_knowledge.insert,
                path=str(resolved_path),
                metadata=metadata,
                upsert=upsert,
                reader=reader,
            )

        try:
            # Remove-then-insert is idempotent, so a transient embedding fault
            # costs one retry of this file instead of the whole refresh.
            await run_with_embedding_retry(
                _insert_once,
                policy=_EMBEDDING_RETRY_POLICY,
                sleep=_EMBEDDING_RETRY_SLEEP,
                on_retry=self._record_embedding_retry,
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
            knowledge=target_knowledge,
        )
        if not has_vectors:
            return await self._handle_vectorless_file(
                relative_path,
                (source_mtime_ns, source_size, source_digest),
                indexed_files=indexed_files,
                indexed_signatures=indexed_signatures,
            )

        if indexed_signatures is not None:
            if indexed_files is not None:
                indexed_files.add(relative_path)
            indexed_signatures[relative_path] = (source_mtime_ns, source_size, source_digest)
        else:
            async with self._state_lock:
                self._indexed_files.add(relative_path)
                self._indexed_signatures[relative_path] = (source_mtime_ns, source_size, source_digest)
        self._file_index_errors.pop(relative_path, None)
        self._note_embedder_success()
        # DEBUG, not INFO: a large corpus is 10^5 of these lines per refresh.
        # Operators get periodic aggregate progress instead.
        logger.debug("Indexed knowledge file", base_id=self.base_id, path=relative_path)
        return True

    async def _handle_vectorless_file(
        self,
        relative_path: str,
        signature: FileSignature,
        *,
        indexed_files: set[str] | None,
        indexed_signatures: dict[str, FileSignature] | None,
    ) -> bool:
        """Record one insert that produced no vectors; success only for empty sources."""
        source_size = signature[1]
        if source_size == 0:
            if indexed_signatures is not None:
                if indexed_files is not None:
                    indexed_files.add(relative_path)
                indexed_signatures[relative_path] = signature
            else:
                async with self._state_lock:
                    self._indexed_files.add(relative_path)
                    self._indexed_signatures[relative_path] = signature
            logger.debug("Scanned empty knowledge file with no vectors", base_id=self.base_id, path=relative_path)
            return True

        logger.warning("Indexing produced no vectors for file", base_id=self.base_id, path=relative_path)
        if indexed_signatures is not None:
            if indexed_files is not None:
                indexed_files.discard(relative_path)
            indexed_signatures.pop(relative_path, None)
        else:
            async with self._state_lock:
                self._indexed_files.discard(relative_path)
                self._indexed_signatures.pop(relative_path, None)
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
        knowledge: Knowledge | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, FileSignature] | None = None,
        vanished_files: set[str] | None = None,
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
                indexed_files=indexed_files,
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
        knowledge: Knowledge | None,
        indexed_files: set[str] | None,
        indexed_signatures: dict[str, FileSignature] | None,
        vanished_files: set[str] | None,
    ) -> bool:
        try:
            return await self._index_file_locked(
                file_path,
                upsert=True,
                knowledge=knowledge,
                indexed_files=indexed_files,
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
            if vanished_files is not None:
                vanished_files.add(relative_path)
            return False

    async def _index_file_batch(
        self,
        batch: Sequence[Path],
        *,
        knowledge: Knowledge | None,
        indexed_files: set[str] | None,
        indexed_signatures: dict[str, FileSignature] | None,
        vanished_files: set[str] | None,
        on_file_result: Callable[[Path], Awaitable[None]] | None = None,
    ) -> int:
        """Index one bounded batch, capping live tasks at the concurrency limit."""

        async def _index_one(file_path: Path) -> bool:
            if self._global_embedder_failure is not None:
                return False
            indexed = await self._index_file_or_skip_vanished(
                file_path,
                knowledge=knowledge,
                indexed_files=indexed_files,
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

    def _candidate_paths_with_vectors(
        self,
        vector_db: ChromaDb,
        relative_paths: Sequence[str],
    ) -> set[str]:
        """Return which of the given source paths actually have candidate vectors."""
        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        return _paths_with_vectors(collection, relative_paths)

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
        for start in range(0, len(verifiable), _VECTOR_VERIFY_BATCH):
            batch = verifiable[start : start + _VECTOR_VERIFY_BATCH]
            found = await asyncio.to_thread(self._candidate_paths_with_vectors, run.vector_db, batch)
            missing.update(set(batch) - found)
            run.verified.update(found)
        return missing

    async def _open_candidate_run(self) -> _CandidateRun:
        """Resolve the durable candidate to continue, or start one clean candidate."""
        checkpoint = await asyncio.to_thread(load_candidate_checkpoint, self._base_storage_path)
        persisted_state = await asyncio.to_thread(self._load_persisted_index_state)
        published_collection = (
            persisted_state.collection
            if persisted_state is not None and persisted_state.status == _INDEXING_STATUS_COMPLETE
            else None
        )
        live_collection, cleanup_is_safe = await asyncio.to_thread(self._published_collection_for_cleanup)
        # Both names matter: the strict parser drops the collection when any
        # required field is missing, while the raw payload still records it.
        # Trusting only the strict one would let a surviving checkpoint reopen
        # the published collection, or delete it as an incompatible candidate.
        published_collections = {name for name in (published_collection, live_collection) if name is not None}

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
        if checkpoint is not None and checkpoint.collection in published_collections:
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
            await self._delete_candidate_collection(checkpoint.collection)
            await asyncio.to_thread(delete_candidate_checkpoint, self._base_storage_path)
            checkpoint = None

        embedder = BatchPrefetchEmbedder(inner=create_configured_embedder(self.config, self.runtime_paths))
        resumed = False
        if checkpoint is None:
            checkpoint = CandidateCheckpoint(
                collection=self._candidate_collection_name(),
                settings=self._indexing_settings,
            )
            # Persist the candidate's identity before its collection exists, so
            # a crash can never strand a collection nothing references.
            checkpoint = await asyncio.to_thread(save_candidate_checkpoint, self._base_storage_path, checkpoint)
            knowledge = self._build_knowledge(checkpoint.collection, embedder=embedder)
            vector_db = _require_chroma_vector_db(knowledge)
            await asyncio.to_thread(self._reset_vector_db, vector_db)
        else:
            knowledge = self._build_knowledge(checkpoint.collection, embedder=embedder)
            vector_db = _require_chroma_vector_db(knowledge)
            if await asyncio.to_thread(vector_db.exists):
                resumed = True
            else:
                logger.warning(
                    "Knowledge candidate collection is missing; rebuilding it from scratch",
                    base_id=self.base_id,
                    collection=checkpoint.collection,
                )
                checkpoint = replace(checkpoint, completed={}, failed={})
                checkpoint = await asyncio.to_thread(save_candidate_checkpoint, self._base_storage_path, checkpoint)
                await asyncio.to_thread(self._reset_vector_db, vector_db)

        run = _CandidateRun(
            checkpoint=checkpoint,
            knowledge=knowledge,
            vector_db=vector_db,
            embedder=embedder,
            completed=dict(checkpoint.completed),
            failed=dict(checkpoint.failed),
            journal_appends=checkpoint.replayed_journal_entries,
            resumed=resumed,
        )
        # Reconcile candidates abandoned by earlier crashed refreshes now, so
        # storage stays bounded even when a build never reaches publication.
        if cleanup_is_safe:
            preserved = {checkpoint.collection, *published_collections}
            await asyncio.to_thread(
                self._cleanup_superseded_collections,
                preserved=frozenset(preserved),
                candidates_only=True,
            )
        else:
            logger.warning(
                "Skipping knowledge candidate cleanup because published metadata is unreadable",
                base_id=self.base_id,
            )
        return run

    def _published_collection_for_cleanup(self) -> tuple[str | None, bool]:
        """Return the live collection to protect, and whether cleanup may run at all.

        A published collection is itself candidate-named, so the only proof of
        which candidate-prefixed collections are superseded is the published
        metadata. The strict state parser rejects metadata that is merely
        incomplete, which would silently drop that proof, so the collection
        name is read straight from the payload. If the file exists but yields
        no payload at all, nothing can be proven and cleanup is skipped rather
        than risking the last good index.
        """
        payload = load_index_metadata_payload(self._indexing_settings_path)
        if payload is None:
            return None, not self._indexing_settings_path.exists()
        collection = payload.get("collection")
        return (collection if isinstance(collection, str) and collection else None), True

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
        if checkpoint.collection != published_collection and not await self._delete_candidate_collection(
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

    async def _delete_candidate_collection(self, collection_name: str) -> bool:
        """Delete one candidate collection, reporting whether it is really gone.

        Agno's ``ChromaDb.delete`` swallows the provider error and returns
        ``False`` rather than raising, so catching exceptions alone would
        report every real failure as a success. A ``False`` result is also
        returned when the collection simply was not there, which is the
        outcome we want, so the two are told apart by probing existence.
        """
        try:
            deleted = await asyncio.to_thread(self._delete_candidate_collection_sync, collection_name)
        except Exception:
            logger.warning(
                "Failed to delete knowledge candidate collection",
                base_id=self.base_id,
                collection=collection_name,
                exc_info=True,
            )
            return False
        if deleted:
            return True
        logger.warning(
            "Knowledge candidate collection still exists after deletion failed",
            base_id=self.base_id,
            collection=collection_name,
        )
        return False

    def _delete_candidate_collection_sync(self, collection_name: str) -> bool:
        """Delete one candidate, treating an already-absent collection as success."""
        vector_db = self._build_vector_db(collection_name)
        if vector_db.delete():
            return True
        try:
            vector_db.client.get_collection(name=vector_db.collection_name)
        except NotFoundError:
            return True
        return False

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

    def _delete_candidate_vectors(self, vector_db: ChromaDb, relative_paths: Sequence[str]) -> None:
        """Delete vectors for many source paths in one vector-store round trip.

        Agno's ``delete_by_metadata`` wraps values in ``$eq`` and so can only
        take one path per call, which turns a large source update into one
        thread hop and one get+delete per file. The collection accepts ``$in``
        directly.

        Unlike the ``$in`` the verification query issues, this one needs no
        ceiling protection: a delete does not bind one SQL variable per matched
        row, so the batch size alone bounds it. Measured on ChromaDB 1.5.8,
        deleting 51,200 rows across 128 paths in one call succeeds, where the
        equivalent read fails with ``too many SQL variables``.
        """
        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        for start in range(0, len(relative_paths), _VECTOR_DELETE_BATCH):
            batch = list(relative_paths[start : start + _VECTOR_DELETE_BATCH])
            collection.delete(where={_SOURCE_PATH_KEY: {"$in": batch}})

    async def _drop_candidate_paths(self, run: _CandidateRun, relative_paths: Sequence[str]) -> None:
        """Remove candidate vectors and checkpoint entries for gone or stale paths."""
        if not relative_paths:
            return
        await asyncio.to_thread(self._delete_candidate_vectors, run.vector_db, relative_paths)
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
                target_revision=self._git_last_successful_commit,
                # The corpus this candidate targets, not a high-water mark of
                # completed files: status subtracts completed from this to
                # report how much work is still outstanding.
                total_files=run.total_files,
            ),
        )
        run.journal_appends = 0

    async def reindex_all(self) -> int:
        """Advance the durable candidate index and publish it when it matches the source."""
        if not _semantic_indexing_enabled(self.config, self.base_id):
            self._last_refresh_error = None
            return 0

        async with self._lock:
            self._last_refresh_error = None
            self._last_file_index_error = None
            self._embedding_retry_count = 0
            self._file_index_errors.clear()
            self._embedder_failure_streak = 0
            self._global_embedder_failure = None
            run = await self._open_candidate_run()
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
                    self._last_refresh_error = redact_credentials_in_text(str(exc))
                raise
            finally:
                progress.retrying = self._embedding_retry_count
                progress.log_summary(published=run.published, error=self._last_refresh_error)
                await self._finalize_candidate_checkpoint(run)
            return progress.indexed_this_run

    async def _finalize_candidate_checkpoint(self, run: _CandidateRun) -> None:
        """Compact the candidate snapshot even when the refresh is being cancelled.

        Per-batch journal appends already made progress durable, so this is a
        compaction, not the write that protects the work.
        """
        compact_task = asyncio.create_task(self._compact_candidate_checkpoint(run, force=True))
        try:
            await asyncio.shield(compact_task)
        except asyncio.CancelledError:
            with suppress(Exception):
                await compact_task
            raise
        except Exception:
            logger.warning(
                "Failed to compact knowledge candidate checkpoint",
                base_id=self.base_id,
                collection=run.checkpoint.collection,
                exc_info=True,
            )

    async def _source_revision(self) -> str | None:
        """Return the current Git revision, or None when the source is not Git-backed."""
        if self._git_config() is None:
            return None
        return await self._git_rev_parse("HEAD")

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
            live_source_signature = await asyncio.to_thread(
                knowledge_source_signature,
                self.config,
                self.base_id,
                self._knowledge_source_path(),
                tracked_relative_paths=self._git_tracked_relative_paths,
            )
            return live_source_signature == candidate_source_signature

        if await self._git_rev_parse("HEAD") != round_revision:
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
                    indexed_files=None,
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
                self._last_refresh_error = summary
                return

            candidate_signatures = {
                relative_path: signature
                for relative_path, signature in run.completed.items()
                if relative_path in expected_paths
            }
            if set(candidate_signatures) != expected_paths:
                self._last_refresh_error = (
                    f"Indexed signatures covered {len(candidate_signatures)} of {len(expected_paths)} managed files"
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

        self._last_refresh_error = (
            "Knowledge source kept changing during refresh; candidate progress was kept for the next refresh"
        )

    async def _publish_candidate(self, run: _CandidateRun, source_signature: str) -> None:
        """Publish the verified candidate and retire the state it supersedes."""
        if run.embedder is not None:
            run.embedder.clear_cache()
        publish_state = _CandidatePublishState()
        try:
            await self._publish_candidate_after_metadata_save(
                candidate_vector_db=run.vector_db,
                indexed_files=set(run.completed),
                indexed_signatures=dict(run.completed),
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
            self._cleanup_superseded_collections,
            preserved=frozenset({run.vector_db.collection_name}),
        )
