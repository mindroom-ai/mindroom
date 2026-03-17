"""Knowledge base management for file-backed RAG."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import weakref
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse, urlunparse

from agno.knowledge.chunking.fixed import FixedSizeChunking
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
from mindroom.logging_config import get_logger
from mindroom.runtime_resolution import ResolvedKnowledgeBinding, resolve_knowledge_binding

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder
    from agno.knowledge.reader.base import Reader

    from mindroom.config.knowledge import KnowledgeGitConfig
    from mindroom.config.main import Config
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

logger = get_logger(__name__)

_COLLECTION_PREFIX = "mindroom_knowledge"
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")
_SOURCE_PATH_KEY = "source_path"
_SOURCE_MTIME_NS_KEY = "source_mtime_ns"
_SOURCE_SIZE_KEY = "source_size"
_FAILED_SIGNATURE_RETRY_SECONDS = 300
_FAILED_SIGNATURE_RETRY_NS = _FAILED_SIGNATURE_RETRY_SECONDS * 1_000_000_000


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
        str(git_config.skip_hidden) if git_config is not None else "",
        str(tuple(git_config.include_patterns)) if git_config is not None else "",
        str(tuple(git_config.exclude_patterns)) if git_config is not None else "",
    )


def _settings_key(config: Config, storage_path: Path, base_id: str, knowledge_path: Path) -> tuple[str, ...]:
    base_config = config.get_knowledge_base_config(base_id)
    git_config = base_config.git
    return (
        *_indexing_settings_key(config, storage_path, base_id, knowledge_path),
        str(base_config.watch),
        str(git_config.poll_interval_seconds) if git_config is not None else "",
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


@dataclass(frozen=True)
class _KnowledgeManagerKey:
    """Stable cache key for one effective knowledge manager instance."""

    base_id: str
    storage_path: str
    knowledge_path: str


@dataclass(frozen=True)
class _ResolvedKnowledgeManagerTarget:
    """Resolved binding plus stable manager key for one effective knowledge manager."""

    key: _KnowledgeManagerKey
    binding: ResolvedKnowledgeBinding


def _knowledge_manager_key_for_binding(
    base_id: str,
    binding: ResolvedKnowledgeBinding,
) -> _KnowledgeManagerKey:
    return _KnowledgeManagerKey(
        base_id=base_id,
        storage_path=str(binding.storage_root.resolve()),
        knowledge_path=str(binding.knowledge_path.resolve()),
    )


def _current_knowledge_manager_key(manager: KnowledgeManager) -> _KnowledgeManagerKey:
    storage_path = manager.storage_path
    knowledge_path = manager.knowledge_path
    if storage_path is None or knowledge_path is None:
        msg = f"Knowledge manager '{manager.base_id}' requires resolved storage_path and knowledge_path"
        raise ValueError(msg)
    return _KnowledgeManagerKey(
        base_id=manager.base_id,
        storage_path=str(storage_path.resolve()),
        knowledge_path=str(knowledge_path.resolve()),
    )


def _resolve_knowledge_manager_target(
    config: Config,
    runtime_paths: RuntimePaths,
    base_id: str,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
    start_watchers: bool,
    create: bool = False,
) -> _ResolvedKnowledgeManagerTarget:
    binding = resolve_knowledge_binding(
        base_id,
        config,
        runtime_paths,
        execution_identity=execution_identity,
        start_watchers=start_watchers,
        create=create,
    )
    if create:
        _ensure_knowledge_directory_ready(binding.knowledge_path)
    return _ResolvedKnowledgeManagerTarget(
        key=_knowledge_manager_key_for_binding(base_id, binding),
        binding=binding,
    )


@dataclass
class KnowledgeManager:
    """Manage indexing and watching for one knowledge base folder."""

    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    storage_path: Path | None = None
    knowledge_path: Path | None = None
    _settings: tuple[str, ...] = field(init=False)
    _indexing_settings: tuple[str, ...] = field(init=False)
    _base_storage_path: Path = field(init=False)
    _index_failures_path: Path = field(init=False)
    _indexing_settings_path: Path = field(init=False)
    _knowledge: Knowledge = field(init=False)
    _indexed_files: set[str] = field(default_factory=set, init=False)
    _indexed_signatures: dict[str, tuple[int, int] | None] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _watch_task: asyncio.Task[None] | None = field(default=None, init=False)
    _watch_stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _git_sync_task: asyncio.Task[None] | None = field(default=None, init=False)
    _git_sync_stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _git_sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

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

        vector_db = ChromaDb(
            collection=_collection_name(self.base_id, self.knowledge_path),
            path=str(self._base_storage_path),
            persistent_client=True,
            embedder=_create_embedder(self.config, self.runtime_paths),
        )
        self._knowledge = Knowledge(vector_db=vector_db)

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

    def _load_persisted_indexing_settings(self) -> tuple[str, ...] | None:
        if not self._indexing_settings_path.exists():
            return None
        try:
            payload = json.loads(self._indexing_settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, list) or not all(isinstance(item, str) for item in payload):
            return None
        return tuple(payload)

    def _save_persisted_indexing_settings(self) -> None:
        self._indexing_settings_path.write_text(
            json.dumps(list(self._indexing_settings)),
            encoding="utf-8",
        )

    def _has_existing_index(self) -> bool:
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb) or not vector_db.exists():
            return False
        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        return collection.count() > 0

    def _needs_full_reindex_on_create(self) -> bool:
        persisted_settings = self._load_persisted_indexing_settings()
        if persisted_settings is None:
            return self._has_existing_index()
        return persisted_settings != self._indexing_settings

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

    def _skip_hidden_paths(self) -> bool:
        git_config = self._git_config()
        return bool(git_config and git_config.skip_hidden)

    def _is_hidden_relative_path(self, relative_path: Path) -> bool:
        return any(part.startswith(".") for part in relative_path.parts)

    def _include_file(self, file_path: Path) -> bool:
        try:
            relative_path = file_path.relative_to(self._knowledge_source_path())
        except ValueError:
            return False
        return file_path.is_file() and self._include_relative_path(relative_path.as_posix())

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

    async def _run_git(self, args: list[str], *, cwd: Path | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd or self._knowledge_source_path()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                process.kill()
            with suppress(ProcessLookupError):
                await process.wait()
            raise

        if process.returncode != 0:
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            details = _redact_credentials_in_text(stderr_text or stdout_text)
            command = " ".join(["git", *(_redact_url_credentials(arg) for arg in args)])
            msg = f"Git command failed with exit code {process.returncode}: {command}"
            if details:
                msg = f"{msg}\n{details}"
            raise RuntimeError(msg)

        return stdout.decode("utf-8", errors="replace")

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

    async def _ensure_git_repository(self, git_config: KnowledgeGitConfig) -> bool:
        runtime_paths = self.runtime_paths
        knowledge_root = self._knowledge_source_path()
        git_dir = knowledge_root / ".git"
        if git_dir.is_dir():
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
        )
        return True

    async def _sync_git_repository_once(self, git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        cloned = await self._ensure_git_repository(git_config)
        if cloned:
            return await self._git_list_tracked_files(), set(), True

        before_head = await self._git_rev_parse("HEAD")
        before_files = await self._git_list_tracked_files()

        await self._run_git(["fetch", "origin", git_config.branch])
        remote_ref = f"origin/{git_config.branch}"
        remote_head = await self._git_rev_parse(remote_ref)
        if remote_head is None:
            msg = f"Could not resolve remote ref '{remote_ref}' for knowledge base '{self.base_id}'"
            raise RuntimeError(msg)

        if before_head == remote_head:
            return set(), set(), False

        await self._run_git(["checkout", git_config.branch])
        # Force-align the local checkout with remote to tolerate local dirty state.
        await self._run_git(["reset", "--hard", remote_ref])

        after_files = await self._git_list_tracked_files()
        if before_head is None:
            changed_paths = after_files
        else:
            diff_output = await self._run_git(["diff", "--name-only", "--no-renames", f"{before_head}..HEAD"])
            changed_paths = {path for path in diff_output.splitlines() if self._include_relative_path(path)}

        removed_files = before_files - after_files
        changed_files = {path for path in changed_paths if path in after_files} | (after_files - before_files)
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

    def _has_vectors_for_source_path(self, relative_path: str) -> bool:
        vector_db = self._knowledge.vector_db
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
        configured_reader.chunking_strategy = FixedSizeChunking(
            chunk_size=base_config.chunk_size,
            overlap=base_config.chunk_overlap,
        )
        return configured_reader

    def _reset_collection(self) -> None:
        if self._knowledge.vector_db is None:
            return
        self._knowledge.vector_db.delete()
        self._knowledge.vector_db.create()

    def _load_indexed_files_from_vector_db(self) -> dict[str, tuple[int, int] | None]:
        """Load indexed source paths and optional file signatures from the vector collection."""
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return {}
        if not vector_db.exists():
            return {}

        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        total_count = collection.count()
        if total_count == 0:
            return {}

        indexed_files: dict[str, tuple[int, int] | None] = {}
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
        if self._git_config() is not None:
            await self.sync_git_repository()

        indexed_count = await self.reindex_all()
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

    async def sync_git_repository(self) -> dict[str, Any]:
        """Fetch and force-align one configured Git repository, then update the index."""
        git_config = self._git_config()
        if git_config is None:
            return {"updated": False, "changed_count": 0, "removed_count": 0}

        async with self._git_sync_lock:
            changed_files, removed_files, updated = await self._sync_git_repository_once(git_config)

        for relative_path in sorted(removed_files):
            await self.remove_file(relative_path)

        for relative_path in sorted(changed_files):
            await self.index_file(relative_path, upsert=True)

        if updated:
            logger.info(
                "Knowledge Git repository synchronized",
                base_id=self.base_id,
                repo_url=_redact_url_credentials(git_config.repo_url),
                branch=git_config.branch,
                changed_count=len(changed_files),
                removed_count=len(removed_files),
            )
        return {
            "updated": updated,
            "changed_count": len(changed_files),
            "removed_count": len(removed_files),
        }

    async def _git_sync_loop(self) -> None:
        """Poll the configured Git repository and keep the knowledge folder up to date."""
        git_config = self._git_config()
        if git_config is None:
            return

        while not self._git_sync_stop_event.is_set():
            try:
                await self.sync_git_repository()
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

    async def _index_file_locked(self, resolved_path: Path, *, upsert: bool) -> bool:
        """Index one file while holding the manager lock."""
        relative_path = self._relative_path(resolved_path)
        source_mtime_ns, source_size = self._file_signature(resolved_path)
        metadata = {
            _SOURCE_PATH_KEY: relative_path,
            _SOURCE_MTIME_NS_KEY: source_mtime_ns,
            _SOURCE_SIZE_KEY: source_size,
        }
        reader = self._build_reader(resolved_path)

        try:
            if upsert:
                # Agno/Chroma upsert keys by content hash, so stale chunks from an older
                # version of the same file can remain unless we clear by source metadata first.
                await asyncio.to_thread(self._knowledge.remove_vectors_by_metadata, {_SOURCE_PATH_KEY: relative_path})
            await self._knowledge.ainsert(
                path=str(resolved_path),
                metadata=metadata,
                upsert=upsert,
                reader=reader,
            )
        except Exception:
            logger.exception("Failed to index knowledge file", base_id=self.base_id, path=str(resolved_path))
            return False

        has_vectors = await asyncio.to_thread(self._has_vectors_for_source_path, relative_path)
        if not has_vectors:
            logger.warning("Indexing produced no vectors for file", base_id=self.base_id, path=relative_path)
            self._indexed_files.discard(relative_path)
            self._indexed_signatures.pop(relative_path, None)
            return False

        self._indexed_files.add(relative_path)
        self._indexed_signatures[relative_path] = (source_mtime_ns, source_size)
        logger.info("Indexed knowledge file", base_id=self.base_id, path=relative_path)
        return True

    async def reindex_all(self) -> int:
        """Clear and rebuild the knowledge index from disk."""
        files = self.list_files()

        async with self._lock:
            await asyncio.to_thread(self._reset_collection)
            self._indexed_files.clear()
            self._indexed_signatures.clear()
            for file_path in files:
                await self._index_file_locked(file_path, upsert=True)
            await asyncio.to_thread(self._save_persisted_indexing_settings)
            return len(self._indexed_files)

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
            self._indexed_files.discard(relative_path)
            self._indexed_signatures.pop(relative_path, None)

        logger.info("Removed knowledge file from index", base_id=self.base_id, path=relative_path, removed=removed)
        return removed

    def get_status(self) -> dict[str, Any]:
        """Get current knowledge indexing status."""
        files = self.list_files()
        return {
            "base_id": self.base_id,
            "folder_path": str(self._knowledge_source_path()),
            "file_count": len(files),
            "indexed_count": len(self._indexed_files),
        }

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


_shared_knowledge_managers: dict[str, KnowledgeManager] = {}
_shared_knowledge_manager_init_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
_request_knowledge_manager_init_locks: weakref.WeakValueDictionary[_KnowledgeManagerKey, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


async def _stop_and_remove_shared_manager(base_id: str) -> None:
    manager = _shared_knowledge_managers.pop(base_id, None)
    if manager is None:
        return
    await manager.stop_watcher()


async def _sync_manager_without_full_reindex(manager: KnowledgeManager) -> dict[str, Any]:
    if manager._git_config() is not None:
        return await manager.sync_git_repository()
    return await manager.sync_indexed_files()


def _shared_knowledge_manager_init_lock(base_id: str) -> asyncio.Lock:
    lock = _shared_knowledge_manager_init_locks.get(base_id)
    if lock is None:
        lock = asyncio.Lock()
        _shared_knowledge_manager_init_locks[base_id] = lock
    return lock


def _request_knowledge_manager_init_lock(key: _KnowledgeManagerKey) -> asyncio.Lock:
    lock = _request_knowledge_manager_init_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _request_knowledge_manager_init_locks[key] = lock
    return lock


def _shared_manager_matches_target(
    manager: KnowledgeManager,
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
) -> bool:
    binding = target.binding
    if binding.request_scoped:
        return False
    if _current_knowledge_manager_key(manager) != target.key:
        return False
    return manager.matches(config, binding.storage_root, binding.knowledge_path)


def _lookup_shared_manager_for_target(
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
) -> KnowledgeManager | None:
    manager = _shared_knowledge_managers.get(target.key.base_id)
    if manager is None:
        return None
    if not _shared_manager_matches_target(manager, target=target, config=config):
        return None
    return manager


async def _create_knowledge_manager_for_target(
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    reindex_on_create: bool,
) -> KnowledgeManager:
    binding = target.binding
    manager = KnowledgeManager(
        base_id=target.key.base_id,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=binding.storage_root,
        knowledge_path=binding.knowledge_path,
    )
    if reindex_on_create or manager._needs_full_reindex_on_create():
        await manager.initialize()
    else:
        sync_result = await _sync_manager_without_full_reindex(manager)
        await asyncio.to_thread(manager._save_persisted_indexing_settings)
        sync_log_context: dict[str, object] = {
            "base_id": target.key.base_id,
            "path": str(manager.knowledge_path),
        }
        if manager._git_config() is not None:
            sync_log_context.update(
                {
                    "updated": sync_result["updated"],
                    "changed_count": sync_result["changed_count"],
                    "removed_count": sync_result["removed_count"],
                },
            )
        else:
            sync_log_context.update(
                {
                    "loaded_count": sync_result["loaded_count"],
                    "indexed_count": sync_result["indexed_count"],
                    "removed_count": sync_result["removed_count"],
                },
            )
        logger.info("Knowledge manager initialized without full reindex", **sync_log_context)

    if binding.start_background_watchers:
        await manager.start_watcher()
    return manager


async def _ensure_shared_knowledge_manager_for_target(
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    reindex_on_create: bool,
) -> KnowledgeManager:
    if target.binding.request_scoped:
        msg = f"Shared knowledge manager target '{target.key.base_id}' must not be request-scoped"
        raise ValueError(msg)

    async with _shared_knowledge_manager_init_lock(target.key.base_id):
        existing = _shared_knowledge_managers.get(target.key.base_id)
        if existing is not None:
            if existing.needs_full_reindex(
                config,
                target.binding.storage_root,
                target.binding.knowledge_path,
            ):
                await existing.stop_watcher()
                manager = await _create_knowledge_manager_for_target(
                    target=target,
                    config=config,
                    runtime_paths=runtime_paths,
                    reindex_on_create=True,
                )
                _shared_knowledge_managers[target.key.base_id] = manager
                return manager

            existing._refresh_settings(
                config,
                runtime_paths,
                target.binding.storage_root,
                target.binding.knowledge_path,
            )
            if target.binding.incremental_sync_on_access:
                await _sync_manager_without_full_reindex(existing)
            if target.binding.start_background_watchers:
                await existing.start_watcher()
            return existing

        manager = await _create_knowledge_manager_for_target(
            target=target,
            config=config,
            runtime_paths=runtime_paths,
            reindex_on_create=reindex_on_create,
        )
        _shared_knowledge_managers[target.key.base_id] = manager
        return manager


async def _create_request_knowledge_manager_for_target(
    *,
    target: _ResolvedKnowledgeManagerTarget,
    config: Config,
    runtime_paths: RuntimePaths,
    reindex_on_create: bool,
) -> KnowledgeManager:
    """Create one request-owned knowledge manager without registering it globally."""
    if not target.binding.request_scoped:
        msg = f"Request knowledge manager target '{target.key.base_id}' must be request-scoped"
        raise ValueError(msg)
    async with _request_knowledge_manager_init_lock(target.key):
        return await _create_knowledge_manager_for_target(
            target=target,
            config=config,
            runtime_paths=runtime_paths,
            reindex_on_create=reindex_on_create,
        )


async def ensure_agent_knowledge_managers(
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    start_watchers: bool = True,
    reindex_on_create: bool = False,
) -> dict[str, KnowledgeManager]:
    """Ensure knowledge managers exist for one agent in one execution scope."""
    agent_config = config.agents.get(agent_name)
    if agent_config is None:
        return {}
    base_ids = config.get_agent_knowledge_base_ids(agent_name)
    if not base_ids:
        return {}

    managers: dict[str, KnowledgeManager] = {}
    for base_id in base_ids:
        target = _resolve_knowledge_manager_target(
            config,
            runtime_paths,
            base_id,
            execution_identity=execution_identity,
            start_watchers=start_watchers,
            create=True,
        )
        if target.binding.request_scoped:
            managers[base_id] = await _create_request_knowledge_manager_for_target(
                target=target,
                config=config,
                runtime_paths=runtime_paths,
                reindex_on_create=reindex_on_create,
            )
            continue

        managers[base_id] = await _ensure_shared_knowledge_manager_for_target(
            target=target,
            config=config,
            runtime_paths=runtime_paths,
            reindex_on_create=reindex_on_create,
        )
    return managers


async def initialize_shared_knowledge_managers(
    config: Config,
    runtime_paths: RuntimePaths,
    start_watchers: bool = False,
    reindex_on_create: bool = True,
) -> dict[str, KnowledgeManager]:
    """Initialize process-global shared knowledge managers for configured shared bases only."""
    configured_base_ids = set(config.knowledge_bases)
    managers: dict[str, KnowledgeManager] = {}

    for base_id in sorted(configured_base_ids):
        target = _resolve_knowledge_manager_target(
            config,
            runtime_paths,
            base_id,
            start_watchers=start_watchers,
            create=True,
        )
        if target.binding.request_scoped:
            continue
        managers[base_id] = await _ensure_shared_knowledge_manager_for_target(
            target=target,
            config=config,
            runtime_paths=runtime_paths,
            reindex_on_create=reindex_on_create,
        )

    for base_id in [candidate for candidate in list(_shared_knowledge_managers) if candidate not in managers]:
        await _stop_and_remove_shared_manager(base_id)

    return managers


def _get_shared_knowledge_manager(base_id: str) -> KnowledgeManager | None:
    """Return the current shared knowledge manager for a base ID, if one exists."""
    return _shared_knowledge_managers.get(base_id)


def get_shared_knowledge_manager_for_config(
    base_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    candidate_manager: KnowledgeManager | None = None,
) -> KnowledgeManager | None:
    """Return the current shared manager for one config, treating stale candidates as cache misses."""
    try:
        target = _resolve_knowledge_manager_target(
            config,
            runtime_paths,
            base_id,
            start_watchers=False,
        )
    except ValueError:
        return None
    manager = candidate_manager
    if manager is not None and not _shared_manager_matches_target(manager, target=target, config=config):
        manager = None
    if manager is None:
        manager = _lookup_shared_manager_for_target(target=target, config=config)
    if manager is None:
        return None
    return manager


async def shutdown_shared_knowledge_managers() -> None:
    """Shutdown and clear all process-global shared knowledge managers."""
    for base_id in list(_shared_knowledge_managers):
        await _stop_and_remove_shared_manager(base_id)

    _shared_knowledge_manager_init_locks.clear()
    _request_knowledge_manager_init_locks.clear()
    _shared_knowledge_managers.clear()
