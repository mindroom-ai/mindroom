"""Knowledge base management for file-backed RAG."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, overload
from urllib.parse import quote, urlparse, urlunparse

from agno.knowledge.embedder.ollama import OllamaEmbedder
from agno.knowledge.knowledge import Knowledge
from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader
from agno.vectordb.chroma import ChromaDb
from watchfiles import Change, awatch

from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.embeddings import (
    MindRoomOpenAIEmbedder,
    create_sentence_transformers_embedder,
    effective_knowledge_embedder_signature,
)
from mindroom.knowledge.chunking import SafeFixedSizeChunking
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder
    from agno.knowledge.reader.base import Reader

    from mindroom.config.knowledge import KnowledgeGitConfig
    from mindroom.config.main import Config

logger = get_logger(__name__)

_COLLECTION_PREFIX = "mindroom_knowledge"
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")
_SOURCE_PATH_KEY = "source_path"
_SOURCE_MTIME_NS_KEY = "source_mtime_ns"
_SOURCE_SIZE_KEY = "source_size"
_FAILED_SIGNATURE_RETRY_SECONDS = 300
_FAILED_SIGNATURE_RETRY_NS = _FAILED_SIGNATURE_RETRY_SECONDS * 1_000_000_000
_MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES = 32
_INDEXING_STATUS_RESETTING = "resetting"
_INDEXING_STATUS_INDEXING = "indexing"
_INDEXING_STATUS_COMPLETE = "complete"
_INDEXING_STATUSES = {
    _INDEXING_STATUS_RESETTING,
    _INDEXING_STATUS_INDEXING,
    _INDEXING_STATUS_COMPLETE,
}
_INDEXING_AVAILABILITY_INITIALIZING = "initializing"
_INDEXING_AVAILABILITY_READY = "ready"
_INDEXING_AVAILABILITY_REFRESH_FAILED = "refresh_failed"
_TEXT_LIKE_EXTENSIONS = {
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".csv",
    ".tsv",
    ".html",
    ".xml",
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".scala",
    ".sc",
    ".sh",
    ".bash",
    ".zsh",
    ".fish",
    ".ps1",
    ".sql",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".vue",
    ".svelte",
    ".proto",
}


@dataclass(frozen=True)
class _PersistedIndexingState:
    settings: tuple[str, ...]
    status: Literal["resetting", "indexing", "complete"]
    collection: str | None = None
    availability: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None


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


def _safe_identifier(value: str) -> str:
    sanitized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return sanitized or "default"


def _base_storage_key(base_id: str, knowledge_path: Path) -> str:
    digest_source = f"{base_id}:{knowledge_path.resolve()}"
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()[:8]
    return f"{_safe_identifier(base_id)}_{digest}"


def _collection_name(base_id: str, knowledge_path: Path) -> str:
    return f"{_COLLECTION_PREFIX}_{_base_storage_key(base_id, knowledge_path)}"


def _indexing_settings_key(config: Config, storage_path: Path, base_id: str, knowledge_path: Path) -> tuple[str, ...]:
    embedder_config = config.memory.embedder.config
    base_config = config.get_knowledge_base_config(base_id)
    git_config = base_config.git
    return (
        base_id,
        str(storage_path.resolve()),
        str(knowledge_path.resolve()),
        *effective_knowledge_embedder_signature(
            config.memory.embedder.provider,
            embedder_config.model,
            host=embedder_config.host,
            dimensions=embedder_config.dimensions,
        ),
        str(base_config.chunk_size),
        str(base_config.chunk_overlap),
        git_config.repo_url if git_config is not None else "",
        git_config.branch if git_config is not None else "",
        str(git_config.lfs) if git_config is not None else "",
        str(git_config.skip_hidden) if git_config is not None else "",
        str(tuple(git_config.include_patterns)) if git_config is not None else "",
        str(tuple(git_config.exclude_patterns)) if git_config is not None else "",
        str(tuple(base_config.include_extensions)) if base_config.include_extensions is not None else "",
        str(tuple(base_config.exclude_extensions)),
    )


def _settings_key(config: Config, storage_path: Path, base_id: str, knowledge_path: Path) -> tuple[str, ...]:
    base_config = config.get_knowledge_base_config(base_id)
    git_config = base_config.git
    return (
        *_indexing_settings_key(config, storage_path, base_id, knowledge_path),
        str(base_config.watch),
        str(git_config.poll_interval_seconds) if git_config is not None else "",
        git_config.startup_behavior if git_config is not None else "",
        str(git_config.sync_timeout_seconds) if git_config is not None else "",
        git_config.credentials_service or "" if git_config is not None else "",
    )


def _create_embedder(config: Config, runtime_paths: RuntimePaths) -> Embedder:
    provider = config.memory.embedder.provider
    embedder_config = config.memory.embedder.config

    if provider == "openai":
        return MindRoomOpenAIEmbedder(
            id=embedder_config.model,
            api_key=get_api_key_for_provider("openai", runtime_paths=runtime_paths),
            base_url=embedder_config.host,
            dimensions=embedder_config.dimensions,
        )

    if provider == "ollama":
        host = get_ollama_host(runtime_paths=runtime_paths) or embedder_config.host or "http://localhost:11434"
        return OllamaEmbedder(id=embedder_config.model, host=host)

    if provider == "sentence_transformers":
        return create_sentence_transformers_embedder(
            runtime_paths,
            embedder_config.model,
            dimensions=embedder_config.dimensions,
        )

    msg = (
        f"Unsupported knowledge embedder provider: {provider}. "
        "Supported providers: openai, ollama, sentence_transformers"
    )
    raise ValueError(msg)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped and stripped.lstrip("-").isdigit():
            return int(stripped)
    return None


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


def _redact_url_credentials(value: str) -> str:
    """Redact password/token information from an HTTP(S) URL."""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or "@" not in parsed.netloc:
        return value

    userinfo, host = parsed.netloc.rsplit("@", 1)
    if ":" in userinfo:
        username = userinfo.split(":", 1)[0]
        redacted_userinfo = f"{username}:***"
    else:
        redacted_userinfo = "***"
    return urlunparse(parsed._replace(netloc=f"{redacted_userinfo}@{host}"))


def _redact_credentials_in_text(value: str) -> str:
    """Redact credential-bearing URLs embedded inside free-form text."""
    return _URL_PATTERN.sub(lambda match: _redact_url_credentials(match.group(0)), value)


def _split_posix_parts(value: str) -> tuple[str, ...]:
    normalized = value.replace("\\", "/").strip()
    normalized = normalized.removeprefix("./")
    normalized = normalized.strip("/")
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part and part != ".")


def _matches_root_glob(relative_path: str, pattern: str) -> bool:
    """Return True when relative path matches the root-anchored glob pattern."""
    path_parts = _split_posix_parts(relative_path)
    pattern_parts = _split_posix_parts(pattern)
    if not pattern_parts:
        return False

    cache: dict[tuple[int, int], bool] = {}

    def _match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in cache:
            return cache[key]

        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        else:
            pattern_part = pattern_parts[pattern_index]
            if pattern_part == "**":
                next_index = pattern_index
                while next_index < len(pattern_parts) and pattern_parts[next_index] == "**":
                    next_index += 1
                if next_index == len(pattern_parts):
                    result = True
                else:
                    result = any(_match(next_path, next_index) for next_path in range(path_index, len(path_parts) + 1))
            elif path_index < len(path_parts) and fnmatchcase(path_parts[path_index], pattern_part):
                result = _match(path_index + 1, pattern_index + 1)
            else:
                result = False

        cache[key] = result
        return result

    return _match(0, 0)


@dataclass
class KnowledgeManager:
    """Manage indexing and watching for one knowledge base folder."""

    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    storage_path: Path | None = None
    knowledge_path: Path | None = None
    git_background_startup_allowed: bool = field(default=True, repr=False)
    _settings: tuple[str, ...] = field(init=False)
    _indexing_settings: tuple[str, ...] = field(init=False)
    _base_storage_path: Path = field(init=False)
    _index_failures_path: Path = field(init=False)
    _indexing_settings_path: Path = field(init=False)
    _git_lfs_hydrated_head_path: Path = field(init=False)
    _knowledge: Knowledge = field(init=False)
    _indexed_files: set[str] = field(default_factory=set, init=False)
    _indexed_signatures: dict[str, tuple[int, int] | None] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _watch_task: asyncio.Task[None] | None = field(default=None, init=False)
    _watch_stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _git_sync_task: asyncio.Task[None] | None = field(default=None, init=False)
    _git_sync_stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _git_sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _git_startup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _git_syncing: bool = field(default=False, init=False)
    _git_repo_present: bool = field(default=False, init=False)
    _git_initial_sync_complete: bool = field(default=False, init=False)
    _git_last_successful_sync_at: datetime | None = field(default=None, init=False)
    _git_last_successful_commit: str | None = field(default=None, init=False)
    _git_last_error: str | None = field(default=None, init=False)
    _git_background_startup_mode: Literal["resume", "incremental"] | None = field(
        default=None,
        init=False,
    )
    _deferred_shared_runtime_mode: Literal["watch", "git_sync"] | None = field(
        default=None,
        init=False,
    )
    _git_lfs_checked: bool = field(default=False, init=False)
    _git_lfs_repository_ready: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        """Initialize filesystem paths and the underlying vector database."""
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
            self.storage_path / "knowledge_db" / _base_storage_key(self.base_id, self.knowledge_path)
        ).resolve()
        self._base_storage_path.mkdir(parents=True, exist_ok=True)
        self._index_failures_path = self._base_storage_path / "index_failures.json"
        self._indexing_settings_path = self._base_storage_path / "indexing_settings.json"
        self._git_lfs_hydrated_head_path = self._base_storage_path / "git_lfs_hydrated_head.txt"
        self._git_repo_present = (self.knowledge_path / ".git").is_dir()
        persisted_state = self._load_persisted_indexing_state()
        collection_name = (
            persisted_state.collection
            if persisted_state is not None and persisted_state.collection is not None
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
        self._settings = _settings_key(config, storage_path, self.base_id, self.knowledge_path)
        self._indexing_settings = _indexing_settings_key(
            config,
            storage_path,
            self.base_id,
            self.knowledge_path,
        )

    def _refresh_settings(
        self,
        config: Config,
        runtime_paths: RuntimePaths,
        storage_path: Path,
        knowledge_path: Path,
    ) -> None:
        self._set_settings(config, runtime_paths, storage_path, knowledge_path)
        if isinstance(self._knowledge.vector_db, ChromaDb):
            self._knowledge.vector_db.embedder = _create_embedder(config, runtime_paths)

    def _knowledge_source_path(self) -> Path:
        knowledge_path = self.knowledge_path
        if knowledge_path is None:
            msg = f"Knowledge path for base '{self.base_id}' is not initialized"
            raise RuntimeError(msg)
        return knowledge_path

    def _load_persisted_indexing_state(self) -> _PersistedIndexingState | None:
        if not self._indexing_settings_path.exists():
            return None
        try:
            payload = json.loads(self._indexing_settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        settings: tuple[str, ...] | None = None
        status: Literal["resetting", "indexing", "complete"] | None = None
        collection: str | None = None
        availability: str | None = None
        last_published_at: str | None = None
        published_revision: str | None = None
        if isinstance(payload, list):
            if all(isinstance(item, str) for item in payload):
                settings = tuple(payload)
                status = _INDEXING_STATUS_COMPLETE
        elif isinstance(payload, dict):
            raw_settings = payload.get("settings")
            raw_status = payload.get("status")
            if (
                isinstance(raw_settings, list)
                and all(isinstance(item, str) for item in raw_settings)
                and raw_status in _INDEXING_STATUSES
            ):
                settings = tuple(raw_settings)
                status = raw_status
                raw_collection = payload.get("collection")
                collection = raw_collection if isinstance(raw_collection, str) and raw_collection else None
                raw_availability = payload.get("availability")
                availability = raw_availability if isinstance(raw_availability, str) and raw_availability else None
                raw_last_published_at = payload.get("last_published_at")
                last_published_at = (
                    raw_last_published_at if isinstance(raw_last_published_at, str) and raw_last_published_at else None
                )
                raw_published_revision = payload.get("published_revision")
                published_revision = (
                    raw_published_revision
                    if isinstance(raw_published_revision, str) and raw_published_revision
                    else None
                )

        if settings is None or status is None:
            return None
        return _PersistedIndexingState(
            settings,
            status,
            collection=collection,
            availability=availability,
            last_published_at=last_published_at,
            published_revision=published_revision,
        )

    def _load_persisted_indexing_settings(self) -> tuple[str, ...] | None:
        persisted_state = self._load_persisted_indexing_state()
        return persisted_state.settings if persisted_state is not None else None

    def _save_persisted_indexing_state(
        self,
        status: Literal["resetting", "indexing", "complete"],
        *,
        collection: str | None = None,
        availability: str | None = None,
        last_published_at: str | None = None,
        published_revision: str | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "settings": list(self._indexing_settings),
            "status": status,
        }
        if collection is not None:
            payload["collection"] = collection
        if availability is not None:
            payload["availability"] = availability
        if last_published_at is not None:
            payload["last_published_at"] = last_published_at
        if published_revision is not None:
            payload["published_revision"] = published_revision
        self._indexing_settings_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def _save_persisted_indexing_settings(self) -> None:
        self._save_persisted_indexing_state(
            _INDEXING_STATUS_COMPLETE,
            collection=self._current_collection_name(),
            availability=_INDEXING_AVAILABILITY_READY,
            last_published_at=datetime.now(tz=UTC).isoformat(),
            published_revision=self._git_last_successful_commit,
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
        if not isinstance(vector_db, ChromaDb) or not vector_db.exists():
            return False
        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        result = collection.get(limit=1, include=[])
        ids = result.get("ids", []) or []
        return bool(ids)

    def _startup_index_mode(self) -> Literal["full_reindex", "resume", "incremental"]:
        persisted_state = self._load_persisted_indexing_state()
        if persisted_state is None:
            # A missing checkpoint can be legacy state worth resuming, but a
            # present unreadable checkpoint is unsafe once vectors already exist.
            return "full_reindex" if self._indexing_settings_path.exists() and self._has_existing_index() else "resume"
        if persisted_state.settings != self._indexing_settings or persisted_state.status == _INDEXING_STATUS_RESETTING:
            return "full_reindex"
        if persisted_state.status == _INDEXING_STATUS_INDEXING or not self._has_existing_index():
            return "resume"
        return "incremental"

    def _needs_full_reindex_on_create(self) -> bool:
        return self._startup_index_mode() == "full_reindex"

    def matches(
        self,
        config: Config,
        storage_path: Path,
        knowledge_path: Path,
    ) -> bool:
        """Return True when manager settings match the provided config."""
        return self._settings == _settings_key(config, storage_path, self.base_id, knowledge_path)

    def needs_full_reindex(
        self,
        config: Config,
        storage_path: Path,
        knowledge_path: Path,
    ) -> bool:
        """Return True when index-affecting settings changed."""
        return self._indexing_settings != _indexing_settings_key(
            config,
            storage_path,
            self.base_id,
            knowledge_path,
        )

    def get_knowledge(self) -> Knowledge:
        """Return the agno Knowledge instance."""
        return self._knowledge

    def _git_config(self) -> KnowledgeGitConfig | None:
        return self.config.get_knowledge_base_config(self.base_id).git

    def _git_uses_lfs(self) -> bool:
        git_config = self._git_config()
        return bool(git_config and git_config.lfs)

    def _git_startup_behavior(self) -> Literal["blocking", "background"]:
        git_config = self._git_config()
        return git_config.startup_behavior if git_config is not None else "blocking"

    def _clear_git_initial_sync_complete(self) -> None:
        self._git_initial_sync_complete = False

    def _mark_git_initial_sync_complete(self) -> None:
        self._git_initial_sync_complete = True

    def defer_shared_runtime_restore(
        self,
        runtime_mode: Literal["watch", "git_sync"] | None,
    ) -> None:
        """Remember a shared runtime mode to restart after an explicit reindex succeeds."""
        self._deferred_shared_runtime_mode = runtime_mode

    async def restore_deferred_shared_runtime(self) -> None:
        """Restart any shared watcher or Git sync that was deferred during explicit reindex bootstrap."""
        runtime_mode = self._deferred_shared_runtime_mode
        if runtime_mode is None:
            return
        if runtime_mode == "watch":
            await self.start_watcher()
        elif runtime_mode == "git_sync":
            await self._start_git_sync()
        self._deferred_shared_runtime_mode = None

    async def reindex_explicitly(self) -> int:
        """Run a manual full reindex and restore any deferred shared runtime afterward."""
        try:
            if self._git_config() is not None:
                result = await self.finish_pending_background_git_startup(force_full_reindex=True)
                return int(result["indexed_count"])
            return await self.reindex_all()
        finally:
            await self.restore_deferred_shared_runtime()

    def _git_sync_timeout_seconds(self) -> float | None:
        git_config = self._git_config()
        if git_config is None:
            return None
        return float(git_config.sync_timeout_seconds)

    def _git_background_startup_enabled(self) -> bool:
        return (
            self._git_config() is not None
            and self._git_startup_behavior() == "background"
            and self.git_background_startup_allowed
        )

    def _skip_hidden_paths(self) -> bool:
        git_config = self._git_config()
        return bool(git_config and git_config.skip_hidden)

    def _is_hidden_relative_path(self, relative_path: Path) -> bool:
        return any(part.startswith(".") for part in relative_path.parts)

    def _include_file(self, file_path: Path) -> bool:
        if not file_path.is_file():
            return False
        try:
            relative_path = file_path.relative_to(self._knowledge_source_path())
        except ValueError:
            return False

        base_config = self.config.get_knowledge_base_config(self.base_id)
        include_extensions = set(base_config.include_extensions) if base_config.include_extensions is not None else None
        exclude_extensions = set(base_config.exclude_extensions)
        allowed_extensions = include_extensions
        if allowed_extensions is None and base_config.git is None:
            allowed_extensions = _TEXT_LIKE_EXTENSIONS

        suffix = file_path.suffix.lower()
        if allowed_extensions is not None and suffix not in allowed_extensions:
            return False
        if suffix in exclude_extensions:
            return False

        return self._include_relative_path(relative_path.as_posix())

    def _include_relative_path(self, relative_path: str) -> bool:
        path_obj = Path(relative_path)
        if path_obj.is_absolute() or ".." in path_obj.parts:
            return False
        if self._skip_hidden_paths() and self._is_hidden_relative_path(path_obj):
            return False

        git_config = self._git_config()
        if git_config is None:
            return True

        if git_config.include_patterns and not any(
            _matches_root_glob(relative_path, pattern) for pattern in git_config.include_patterns
        ):
            return False

        return not any(_matches_root_glob(relative_path, pattern) for pattern in git_config.exclude_patterns)

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
            command = " ".join(["git", *(_redact_url_credentials(arg) for arg in args)])
            msg = f"Git command timed out after {timeout_seconds:.0f}s: {command}"
            raise RuntimeError(msg) from exc

        if process.returncode == 0:
            return stdout.decode("utf-8", errors="replace")

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        details = _redact_credentials_in_text(stderr_text or stdout_text)
        command = " ".join(["git", *(_redact_url_credentials(arg) for arg in args)])
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
            ["lfs", "pull", "origin", git_config.branch],
            cwd=repo_root or self._knowledge_source_path(),
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
        return {path for path in raw_paths if self._include_relative_path(path)}

    async def _git_dirty_tracked_files(self) -> set[str]:
        output = await self._run_git(["diff", "--name-only", "--no-renames", "HEAD"])
        return {path for path in output.splitlines() if self._include_relative_path(path)}

    async def _ensure_git_repository(self, git_config: KnowledgeGitConfig) -> bool:
        runtime_paths = self.runtime_paths
        knowledge_root = self._knowledge_source_path()
        git_dir = knowledge_root / ".git"
        if git_dir.is_dir():
            self._git_repo_present = True
            await self._ensure_git_lfs_repository_ready(knowledge_root)
            current_remote = (await self._run_git(["remote", "get-url", "origin"])).strip()
            expected_remote = _authenticated_repo_url(
                git_config.repo_url,
                git_config.credentials_service,
                runtime_paths,
            )
            if current_remote != expected_remote:
                await self._run_git(["remote", "set-url", "origin", expected_remote])
            await self._run_git(["checkout", git_config.branch])
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
        clone_url = _authenticated_repo_url(
            git_config.repo_url,
            git_config.credentials_service,
            runtime_paths,
        )
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
            env={"GIT_LFS_SKIP_SMUDGE": "1"} if git_config.lfs else None,
        )
        self._git_repo_present = True
        await asyncio.to_thread(self._clear_git_lfs_hydrated_head)
        await self._ensure_git_lfs_repository_ready(knowledge_root)
        await self._hydrate_git_lfs_worktree(git_config, repo_root=knowledge_root)
        return True

    async def _sync_git_repository_once(self, git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        cloned = await self._ensure_git_repository(git_config)
        if cloned:
            return await self._git_list_tracked_files(), set(), True

        before_head = await self._git_rev_parse("HEAD")
        before_files = await self._git_list_tracked_files()
        dirty_tracked_files = set() if before_head is None else await self._git_dirty_tracked_files()

        await self._run_git(["fetch", "origin", git_config.branch])
        remote_ref = f"origin/{git_config.branch}"
        remote_head = await self._git_rev_parse(remote_ref)
        if remote_head is None:
            msg = f"Could not resolve remote ref '{remote_ref}' for knowledge base '{self.base_id}'"
            raise RuntimeError(msg)

        if before_head == remote_head:
            if not dirty_tracked_files:
                await self._hydrate_git_lfs_worktree(git_config, current_head=remote_head)
                return set(), set(), False

            await self._run_git(["checkout", git_config.branch])
            # Reviewed with Bas (2026-04-17): program-owned checkout, hard reset is the
            # intentional way to realign it with the configured remote state.
            await self._run_git(["reset", "--hard", remote_ref])
            await self._hydrate_git_lfs_worktree(git_config, current_head=remote_head)
            after_files = await self._git_list_tracked_files()
            changed_files = {path for path in dirty_tracked_files if path in after_files}
            return changed_files, set(), True

        await self._run_git(["checkout", git_config.branch])
        # Reviewed with Bas (2026-04-17): program-owned checkout, hard reset is the
        # intentional way to realign it with the configured remote state.
        await self._run_git(["reset", "--hard", remote_ref])
        await self._hydrate_git_lfs_worktree(git_config, current_head=remote_head)

        after_files = await self._git_list_tracked_files()
        if before_head is None:
            changed_paths = after_files
        else:
            diff_output = await self._run_git(["diff", "--name-only", "--no-renames", f"{before_head}..HEAD"])
            changed_paths = {path for path in diff_output.splitlines() if self._include_relative_path(path)}

        removed_files = before_files - after_files
        changed_files = (
            {path for path in changed_paths if path in after_files}
            | (after_files - before_files)
            | {path for path in dirty_tracked_files if path in after_files}
        )
        return changed_files, removed_files, True

    def list_files(self) -> list[Path]:
        """List all files currently present in the knowledge folder."""
        knowledge_root = self._knowledge_source_path()
        if not knowledge_root.exists():
            return []
        return sorted(path for path in knowledge_root.rglob("*") if self._include_file(path))

    def resolve_file_path(self, file_path: Path | str) -> Path:
        """Resolve a path and ensure it stays inside the knowledge folder."""
        knowledge_root = self._knowledge_source_path()
        candidate = Path(file_path)
        resolved = (
            candidate.expanduser().resolve() if candidate.is_absolute() else (knowledge_root / candidate).resolve()
        )

        try:
            resolved.relative_to(knowledge_root)
        except ValueError as exc:
            msg = f"Path {resolved} is outside knowledge folder {knowledge_root}"
            raise ValueError(msg) from exc

        return resolved

    def _relative_path(self, file_path: Path) -> str:
        return file_path.relative_to(self._knowledge_source_path()).as_posix()

    def _file_signature(self, file_path: Path) -> tuple[int, int]:
        stat = file_path.stat()
        return stat.st_mtime_ns, stat.st_size

    def _load_failed_signatures(self) -> dict[str, tuple[int, int, int]]:
        if not self._index_failures_path.exists():
            return {}

        try:
            payload = json.loads(self._index_failures_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        if not isinstance(payload, dict):
            return {}

        failed_signatures: dict[str, tuple[int, int, int]] = {}
        for path, value in payload.items():
            if not isinstance(path, str):
                continue
            if not isinstance(value, list | tuple) or len(value) not in {2, 3}:
                continue
            mtime_ns = _coerce_int(value[0])
            size = _coerce_int(value[1])
            if mtime_ns is None or size is None:
                continue
            failed_at_ns = _coerce_int(value[2]) if len(value) == 3 else 0
            failed_signatures[path] = (mtime_ns, size, max(failed_at_ns or 0, 0))
        return failed_signatures

    def _save_failed_signatures(self, failed_signatures: dict[str, tuple[int, int, int]]) -> None:
        payload = {path: [signature[0], signature[1], signature[2]] for path, signature in failed_signatures.items()}
        self._index_failures_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def _should_skip_failed_signature(
        self,
        *,
        failed_signature: tuple[int, int, int],
        current_signature: tuple[int, int],
    ) -> bool:
        failed_mtime_ns, failed_size, failed_at_ns = failed_signature
        if (failed_mtime_ns, failed_size) != current_signature:
            return False
        if failed_at_ns <= 0:
            # Legacy entries without timestamps should be retried.
            return False
        elapsed_ns = time.time_ns() - failed_at_ns
        return elapsed_ns < _FAILED_SIGNATURE_RETRY_NS

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
        result = collection.get(
            where={_SOURCE_PATH_KEY: relative_path},
            limit=1,
            include=[],
        )
        ids = result.get("ids", []) or []
        return bool(ids)

    def _build_reader(self, file_path: Path) -> Reader:
        """Build a per-file reader with conservative chunking for text-like content."""
        base_config = self.config.get_knowledge_base_config(self.base_id)
        reader = ReaderFactory.get_reader_for_extension(file_path.suffix.lower())

        # Large markdown/plain-text files are the common source of oversized embed requests.
        if not isinstance(reader, (TextReader, MarkdownReader)):
            return reader

        configured_reader = deepcopy(reader)
        configured_reader.chunk = True
        configured_reader.chunk_size = base_config.chunk_size
        configured_reader.chunking_strategy = SafeFixedSizeChunking(
            chunk_size=base_config.chunk_size,
            overlap=base_config.chunk_overlap,
        )
        return configured_reader

    def _default_collection_name(self) -> str:
        return _collection_name(self.base_id, self._knowledge_source_path())

    def _current_collection_name(self) -> str:
        vector_db = self._knowledge.vector_db
        if isinstance(vector_db, ChromaDb):
            return vector_db.collection_name
        return self._default_collection_name()

    def _shadow_collection_name(self, live_collection_name: str) -> str:
        default_collection = self._default_collection_name()
        pending_collection = f"{default_collection}_pending"
        return pending_collection if live_collection_name == default_collection else default_collection

    def _build_vector_db(self, collection_name: str) -> ChromaDb:
        return ChromaDb(
            collection=collection_name,
            path=str(self._base_storage_path),
            persistent_client=True,
            embedder=_create_embedder(self.config, self.runtime_paths),
        )

    def _build_knowledge(self, collection_name: str) -> Knowledge:
        return Knowledge(vector_db=self._build_vector_db(collection_name))

    def _reset_vector_db(self, vector_db: ChromaDb) -> None:
        vector_db.delete()
        vector_db.create()

    def _delete_vector_db(self, vector_db: ChromaDb) -> None:
        vector_db.delete()

    def _reset_collection(self) -> None:
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return
        self._reset_vector_db(vector_db)

    def _load_indexed_files_from_vector_db(self) -> dict[str, tuple[int, int] | None]:
        """Load indexed source paths and optional file signatures from the vector collection."""
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return {}
        if not vector_db.exists():
            return {}

        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        indexed_files: dict[str, tuple[int, int] | None] = {}
        offset = 0
        batch_size = 1_000

        while True:
            result = collection.get(
                limit=batch_size,
                offset=offset,
                include=["metadatas"],
            )

            metadatas = result.get("metadatas", []) or []
            for metadata in metadatas:
                if not isinstance(metadata, dict):
                    continue
                source_path = metadata.get(_SOURCE_PATH_KEY)
                if not isinstance(source_path, str) or not source_path:
                    continue

                source_mtime_ns = _coerce_int(metadata.get(_SOURCE_MTIME_NS_KEY))
                source_size = _coerce_int(metadata.get(_SOURCE_SIZE_KEY))
                signature = (
                    (source_mtime_ns, source_size) if source_mtime_ns is not None and source_size is not None else None
                )
                if source_path not in indexed_files or (indexed_files[source_path] is None and signature is not None):
                    indexed_files[source_path] = signature

            ids = result.get("ids", []) or []
            fetched_count = len(ids)
            if fetched_count == 0:
                break
            offset += fetched_count

        return indexed_files

    async def initialize(self) -> None:
        """Initialize and index all existing knowledge files."""
        git_config = self._git_config()
        if git_config is not None:
            await self.sync_git_repository(index_changes=False)

        indexed_count = await self.reindex_all()
        if git_config is not None:
            self._mark_git_initial_sync_complete()
        logger.info(
            "Knowledge base initialized",
            base_id=self.base_id,
            indexed_count=indexed_count,
            path=str(self._knowledge_source_path()),
        )

    async def load_indexed_files(self) -> int:
        """Load in-memory indexed file state from the existing vector DB collection."""
        indexed_files = await asyncio.to_thread(self._load_indexed_files_from_vector_db)
        async with self._lock:
            self._indexed_signatures = indexed_files
            self._indexed_files = set(indexed_files)
        return len(indexed_files)

    async def sync_indexed_files(self) -> dict[str, int]:
        """Incrementally align index with files on disk."""
        await self.load_indexed_files()
        files = self.list_files()
        current_signatures = {self._relative_path(path): self._file_signature(path) for path in files}
        failed_signatures = await asyncio.to_thread(self._load_failed_signatures)

        async with self._lock:
            indexed_files = set(self._indexed_files)
            indexed_signatures = dict(self._indexed_signatures)

        removed_paths = sorted(indexed_files - set(current_signatures))
        changed_or_missing_paths: list[str] = []
        for path, signature in current_signatures.items():
            if path not in indexed_files:
                failed_signature = failed_signatures.get(path)
                if failed_signature and self._should_skip_failed_signature(
                    failed_signature=failed_signature,
                    current_signature=signature,
                ):
                    continue
                changed_or_missing_paths.append(path)
                continue

            indexed_signature = indexed_signatures.get(path)
            if indexed_signature is not None and indexed_signature != signature:
                changed_or_missing_paths.append(path)
        changed_or_missing_paths.sort()

        removed_count = 0
        for relative_path in removed_paths:
            removed = await self.remove_file(relative_path)
            removed_count += int(removed)
            failed_signatures.pop(relative_path, None)

        indexed_count = 0
        for relative_path in changed_or_missing_paths:
            indexed = await self.index_file(relative_path, upsert=True)
            indexed_count += int(indexed)
            if indexed:
                failed_signatures.pop(relative_path, None)
            elif relative_path not in indexed_files:
                # Only suppress retries for genuinely new files.  Previously-
                # indexed files lost their vectors during the upsert attempt
                # and must be retried on the next startup.
                source_mtime_ns, source_size = current_signatures[relative_path]
                failed_signatures[relative_path] = (source_mtime_ns, source_size, time.time_ns())

        await asyncio.to_thread(self._save_failed_signatures, failed_signatures)

        return {
            "loaded_count": len(indexed_files),
            "indexed_count": indexed_count,
            "removed_count": removed_count,
        }

    async def prepare_background_git_startup(
        self,
        startup_mode: Literal["resume", "incremental"],
    ) -> dict[str, Any]:
        """Record startup state and defer Git sync/index refresh to the background loop."""
        await self.load_indexed_files()
        self._git_background_startup_mode = startup_mode
        self._git_repo_present = (self._knowledge_source_path() / ".git").is_dir()
        self._clear_git_initial_sync_complete()
        return {
            "startup_mode": startup_mode,
            "loaded_count": len(self._indexed_files),
            "indexed_count": 0,
            "removed_count": 0,
            "git_deferred": True,
        }

    @overload
    async def finish_pending_background_git_startup(
        self,
        *,
        force_full_reindex: Literal[True],
    ) -> dict[str, Any]: ...

    @overload
    async def finish_pending_background_git_startup(
        self,
        *,
        force_full_reindex: Literal[False] = False,
    ) -> dict[str, Any] | None: ...

    async def finish_pending_background_git_startup(
        self,
        *,
        force_full_reindex: bool = False,
    ) -> dict[str, Any] | None:
        """Finish deferred Git startup work immediately when a caller cannot wait for the poll loop."""
        git_config = self._git_config()
        if git_config is None:
            if force_full_reindex:
                msg = f"Knowledge base '{self.base_id}' is not Git-backed"
                raise RuntimeError(msg)
            return None

        async with self._git_startup_lock:
            startup_mode = self._git_background_startup_mode
            effective_mode = "full_reindex" if force_full_reindex else startup_mode
            if effective_mode is None:
                return None

            git_result = await self.sync_git_repository(index_changes=False)
            if effective_mode == "full_reindex":
                indexed_count = await self.reindex_all()
                result = {
                    **git_result,
                    "startup_mode": effective_mode,
                    "indexed_count": indexed_count,
                }
            else:
                sync_result = await self.sync_indexed_files()
                await asyncio.to_thread(self._save_persisted_indexing_settings)
                result = {
                    **git_result,
                    "startup_mode": effective_mode,
                    **sync_result,
                }
            self._git_background_startup_mode = None
            self._mark_git_initial_sync_complete()
            return result

    async def ensure_git_checkout_ready(self) -> None:
        """Ensure the Git checkout exists before direct file writes land in the knowledge folder."""
        if self._git_config() is None:
            return
        if await self.finish_pending_background_git_startup() is not None:
            return
        if (self._knowledge_source_path() / ".git").is_dir():
            self._git_repo_present = True
            return
        await self.sync_git_repository(index_changes=False)

    async def _run_pending_background_git_startup(self) -> dict[str, Any]:
        result = await self.finish_pending_background_git_startup()
        if result is not None:
            return result
        return await self.sync_git_repository()

    async def sync_git_repository(self, *, index_changes: bool = True) -> dict[str, Any]:
        """Fetch and force-align one configured Git repository, then update the index."""
        git_config = self._git_config()
        if git_config is None:
            return {"updated": False, "changed_count": 0, "removed_count": 0}

        self._git_syncing = True
        try:
            async with self._git_sync_lock:
                changed_files, removed_files, updated = await self._sync_git_repository_once(git_config)
            current_head = await self._git_rev_parse("HEAD")
        except Exception as exc:
            self._git_repo_present = (self._knowledge_source_path() / ".git").is_dir()
            self._git_last_error = _redact_credentials_in_text(str(exc))
            raise
        finally:
            self._git_syncing = False

        self._git_repo_present = (self._knowledge_source_path() / ".git").is_dir()
        self._git_last_successful_sync_at = datetime.now(tz=UTC)
        self._git_last_successful_commit = current_head
        self._git_last_error = None

        if index_changes:
            for relative_path in sorted(removed_files):
                await self.remove_file(relative_path)

            for relative_path in sorted(changed_files):
                await self.index_file(relative_path, upsert=True)
            self._mark_git_initial_sync_complete()

        if updated:
            logger.info(
                "Knowledge Git repository synchronized",
                base_id=self.base_id,
                repo_url=_redact_url_credentials(git_config.repo_url),
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

    async def _git_sync_loop(self) -> None:
        """Poll the configured Git repository and keep the knowledge folder up to date."""
        while not self._git_sync_stop_event.is_set():
            git_config = self._git_config()
            if git_config is None:
                return
            try:
                await self._run_pending_background_git_startup()
            except Exception:
                logger.exception(
                    "Knowledge Git sync failed",
                    base_id=self.base_id,
                    repo_url=_redact_url_credentials(git_config.repo_url),
                    branch=git_config.branch,
                )

            try:
                await asyncio.wait_for(
                    self._git_sync_stop_event.wait(),
                    timeout=float(git_config.poll_interval_seconds),
                )
            except TimeoutError:
                continue

    async def _start_git_sync(self) -> None:
        git_config = self._git_config()
        if git_config is None:
            return
        if self._git_sync_task is not None and not self._git_sync_task.done():
            return

        self._git_sync_stop_event = asyncio.Event()
        self._git_sync_task = asyncio.create_task(self._git_sync_loop())
        logger.info(
            "Knowledge Git sync started",
            base_id=self.base_id,
            repo_url=_redact_url_credentials(git_config.repo_url),
            branch=git_config.branch,
            poll_interval_seconds=git_config.poll_interval_seconds,
        )

    async def _stop_git_sync(self) -> None:
        if self._git_sync_task is None:
            return

        self._git_sync_stop_event.set()
        self._git_sync_task.cancel()
        with suppress(asyncio.CancelledError):
            await self._git_sync_task

        self._git_sync_task = None
        logger.info("Knowledge Git sync stopped", base_id=self.base_id)

    async def start_watcher(self) -> None:
        """Start background file watching if enabled."""
        await self._start_git_sync()

        base_config = self.config.get_knowledge_base_config(self.base_id)
        if not base_config.watch:
            return
        if self._watch_task is not None and not self._watch_task.done():
            return

        self._watch_stop_event = asyncio.Event()
        self._watch_task = asyncio.create_task(self._watch_loop())
        logger.info("Knowledge folder watcher started", base_id=self.base_id, path=str(self._knowledge_source_path()))

    async def stop_watcher(self) -> None:
        """Stop the background file watcher."""
        await self._stop_git_sync()

        if self._watch_task is not None:
            self._watch_stop_event.set()
            self._watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watch_task

            self._watch_task = None
            logger.info("Knowledge folder watcher stopped", base_id=self.base_id)

    async def _index_file_locked(
        self,
        resolved_path: Path,
        *,
        upsert: bool,
        knowledge: Knowledge | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> bool:
        """Index one file while the caller owns the operation lock."""
        relative_path = self._relative_path(resolved_path)
        source_mtime_ns, source_size = self._file_signature(resolved_path)
        metadata = {
            _SOURCE_PATH_KEY: relative_path,
            _SOURCE_MTIME_NS_KEY: source_mtime_ns,
            _SOURCE_SIZE_KEY: source_size,
        }
        reader = self._build_reader(resolved_path)
        target_knowledge = knowledge or self._knowledge

        try:
            if upsert:
                # Agno/Chroma upsert keys by content hash, so stale chunks from an older
                # version of the same file can remain unless we clear by source metadata first.
                await asyncio.to_thread(target_knowledge.remove_vectors_by_metadata, {_SOURCE_PATH_KEY: relative_path})
            await target_knowledge.ainsert(
                path=str(resolved_path),
                metadata=metadata,
                upsert=upsert,
                reader=reader,
            )
        except Exception:
            logger.exception("Failed to index knowledge file", base_id=self.base_id, path=str(resolved_path))
            return False

        has_vectors = await asyncio.to_thread(
            self._has_vectors_for_source_path,
            relative_path,
            knowledge=target_knowledge,
        )
        if not has_vectors:
            logger.warning("Indexing produced no vectors for file", base_id=self.base_id, path=relative_path)
            if indexed_files is not None and indexed_signatures is not None:
                indexed_files.discard(relative_path)
                indexed_signatures.pop(relative_path, None)
            else:
                async with self._state_lock:
                    self._indexed_files.discard(relative_path)
                    self._indexed_signatures.pop(relative_path, None)
            return False

        if indexed_files is not None and indexed_signatures is not None:
            indexed_files.add(relative_path)
            indexed_signatures[relative_path] = (source_mtime_ns, source_size)
        else:
            async with self._state_lock:
                self._indexed_files.add(relative_path)
                self._indexed_signatures[relative_path] = (source_mtime_ns, source_size)
        logger.info("Indexed knowledge file", base_id=self.base_id, path=relative_path)
        return True

    async def _reindex_files_locked(
        self,
        files: list[Path],
        *,
        knowledge: Knowledge | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, tuple[int, int] | None] | None = None,
    ) -> int:
        """Reindex resolved files with bounded concurrency while holding the operation lock."""
        if not files:
            return 0

        concurrency = min(_MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES, len(files))
        if concurrency <= 1:
            indexed_count = 0
            for file_path in files:
                indexed_count += int(
                    await self._index_file_locked(
                        file_path,
                        upsert=True,
                        knowledge=knowledge,
                        indexed_files=indexed_files,
                        indexed_signatures=indexed_signatures,
                    ),
                )
            return indexed_count

        semaphore = asyncio.Semaphore(concurrency)

        async def _index_one(file_path: Path) -> bool:
            async with semaphore:
                return await self._index_file_locked(
                    file_path,
                    upsert=True,
                    knowledge=knowledge,
                    indexed_files=indexed_files,
                    indexed_signatures=indexed_signatures,
                )

        results = await asyncio.gather(*(_index_one(file_path) for file_path in files))
        return sum(int(indexed) for indexed in results)

    async def reindex_all(self) -> int:
        """Clear and rebuild the knowledge index from disk."""
        files = self.list_files()

        async with self._lock:
            persisted_state = await asyncio.to_thread(self._load_persisted_indexing_state)
            live_collection_name = self._current_collection_name()
            has_published_snapshot = (
                persisted_state is not None
                and persisted_state.status == _INDEXING_STATUS_COMPLETE
                and await asyncio.to_thread(self._has_existing_index)
            )
            if not has_published_snapshot:
                await asyncio.to_thread(
                    self._save_persisted_indexing_state,
                    _INDEXING_STATUS_RESETTING,
                    collection=live_collection_name,
                    availability=_INDEXING_AVAILABILITY_INITIALIZING,
                )
                await asyncio.to_thread(self._reset_collection)
                async with self._state_lock:
                    self._indexed_files.clear()
                    self._indexed_signatures.clear()
                await asyncio.to_thread(
                    self._save_persisted_indexing_state,
                    _INDEXING_STATUS_INDEXING,
                    collection=live_collection_name,
                    availability=_INDEXING_AVAILABILITY_INITIALIZING,
                )
                indexed_count = await self._reindex_files_locked(files)
                await asyncio.to_thread(self._save_persisted_indexing_settings)
                return indexed_count

            shadow_knowledge = self._build_knowledge(self._shadow_collection_name(live_collection_name))
            shadow_vector_db = shadow_knowledge.vector_db
            if not isinstance(shadow_vector_db, ChromaDb):
                msg = "Knowledge reindex shadow collection requires a ChromaDb vector database"
                raise TypeError(msg)
            assert persisted_state is not None
            await asyncio.to_thread(self._reset_vector_db, shadow_vector_db)
            shadow_indexed_files: set[str] = set()
            shadow_indexed_signatures: dict[str, tuple[int, int] | None] = {}
            last_published_at = persisted_state.last_published_at
            published_revision = persisted_state.published_revision
            try:
                indexed_count = await self._reindex_files_locked(
                    files,
                    knowledge=shadow_knowledge,
                    indexed_files=shadow_indexed_files,
                    indexed_signatures=shadow_indexed_signatures,
                )
            except Exception:
                await asyncio.to_thread(self._delete_vector_db, shadow_vector_db)
                await asyncio.to_thread(
                    self._save_persisted_indexing_state,
                    _INDEXING_STATUS_COMPLETE,
                    collection=live_collection_name,
                    availability=_INDEXING_AVAILABILITY_REFRESH_FAILED,
                    last_published_at=last_published_at,
                    published_revision=published_revision,
                )
                raise

            previous_vector_db = self._knowledge.vector_db
            # Keep the Knowledge object stable so any already-resolved handles see the swap atomically.
            self._knowledge.vector_db = shadow_vector_db
            async with self._state_lock:
                self._indexed_files = shadow_indexed_files
                self._indexed_signatures = shadow_indexed_signatures
            await asyncio.to_thread(self._save_persisted_indexing_settings)
            if isinstance(previous_vector_db, ChromaDb):
                await asyncio.to_thread(self._delete_vector_db, previous_vector_db)
            return indexed_count

    async def index_file(self, file_path: Path | str, *, upsert: bool = True) -> bool:
        """Index or reindex a single file."""
        resolved_path = self.resolve_file_path(file_path)
        if not self._include_file(resolved_path):
            return False
        if not resolved_path.exists() or not resolved_path.is_file():
            return False

        async with self._lock:
            return await self._index_file_locked(resolved_path, upsert=upsert)

    async def remove_file(self, file_path: Path | str) -> bool:
        """Remove a file from the vector database index."""
        resolved_path = self.resolve_file_path(file_path)
        relative_path = self._relative_path(resolved_path)
        if not self._include_relative_path(relative_path):
            return False

        async with self._lock:
            removed = await asyncio.to_thread(
                self._knowledge.remove_vectors_by_metadata,
                {_SOURCE_PATH_KEY: relative_path},
            )
            async with self._state_lock:
                self._indexed_files.discard(relative_path)
                self._indexed_signatures.pop(relative_path, None)

        logger.info("Removed knowledge file from index", base_id=self.base_id, path=relative_path, removed=removed)
        return removed

    def get_status(self) -> dict[str, Any]:
        """Get current knowledge indexing status."""
        files = self.list_files()
        status = {
            "base_id": self.base_id,
            "folder_path": str(self._knowledge_source_path()),
            "file_count": len(files),
            "indexed_count": len(self._indexed_files),
        }
        git_config = self._git_config()
        if git_config is not None:
            status["git"] = {
                "repo_url": _redact_url_credentials(git_config.repo_url),
                "branch": git_config.branch,
                "lfs": git_config.lfs,
                "startup_behavior": git_config.startup_behavior,
                "syncing": self._git_syncing,
                "repo_present": self._git_repo_present,
                "initial_sync_complete": self._git_initial_sync_complete,
                "last_successful_sync_at": (
                    self._git_last_successful_sync_at.isoformat() if self._git_last_successful_sync_at else None
                ),
                "last_successful_commit": self._git_last_successful_commit,
                "last_error": self._git_last_error,
                "pending_startup_mode": self._git_background_startup_mode,
            }
        return status

    async def _watch_loop(self) -> None:
        """Watch the knowledge folder for file changes."""
        async for changes in awatch(self._knowledge_source_path(), stop_event=self._watch_stop_event):
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
        if not self._include_relative_path(self._relative_path(resolved_path)):
            return

        if change in {Change.added, Change.modified}:
            if resolved_path.exists() and resolved_path.is_file():
                await self.index_file(resolved_path, upsert=True)
        elif change == Change.deleted:
            await self.remove_file(resolved_path)
