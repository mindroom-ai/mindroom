"""Knowledge base management for file-backed RAG."""

from __future__ import annotations

import asyncio
import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse, urlunparse

from agno.knowledge.embedder.ollama import OllamaEmbedder
from agno.knowledge.embedder.openai import OpenAIEmbedder
from agno.knowledge.knowledge import Knowledge
from agno.vectordb.chroma import ChromaDb
from watchfiles import Change, awatch

from .credentials import get_credentials_manager
from .credentials_sync import get_api_key_for_provider, get_ollama_host
from .logging_config import get_logger

if TYPE_CHECKING:
    from agno.knowledge.embedder.base import Embedder

    from .config import Config, KnowledgeBaseConfig, KnowledgeGitConfig

logger = get_logger(__name__)

_COLLECTION_PREFIX = "mindroom_knowledge"
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+")


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
    git_config = base_config.git
    return (
        base_id,
        str(storage_path.resolve()),
        str(knowledge_path),
        config.memory.embedder.provider,
        embedder_config.model,
        embedder_config.host or "",
        str(base_config.watch),
        git_config.repo_url if git_config is not None else "",
        git_config.branch if git_config is not None else "",
        str(git_config.poll_interval_seconds) if git_config is not None else "",
        git_config.credentials_service or "" if git_config is not None else "",
        str(git_config.skip_hidden) if git_config is not None else "",
        str(tuple(git_config.include_patterns)) if git_config is not None else "",
        str(tuple(git_config.exclude_patterns)) if git_config is not None else "",
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


def _authenticated_repo_url(repo_url: str, credentials_service: str | None) -> str:
    """Inject HTTPS credentials from CredentialsManager into a repository URL."""
    if not credentials_service:
        return repo_url

    credentials = get_credentials_manager().load_credentials(credentials_service) or {}
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
    storage_path: Path

    knowledge_path: Path = field(init=False)
    _settings: tuple[str, ...] = field(init=False)
    _knowledge: Knowledge = field(init=False)
    _indexed_files: set[str] = field(default_factory=set, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _watch_task: asyncio.Task[None] | None = field(default=None, init=False)
    _watch_stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _git_sync_task: asyncio.Task[None] | None = field(default=None, init=False)
    _git_sync_stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    _git_sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

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

    def _git_config(self) -> KnowledgeGitConfig | None:
        return _knowledge_base_config(self.config, self.base_id).git

    def _skip_hidden_paths(self) -> bool:
        git_config = self._git_config()
        return bool(git_config and git_config.skip_hidden)

    def _is_hidden_relative_path(self, relative_path: Path) -> bool:
        return any(part.startswith(".") for part in relative_path.parts)

    def _include_file(self, file_path: Path) -> bool:
        try:
            relative_path = file_path.relative_to(self.knowledge_path)
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
            cwd=str(cwd or self.knowledge_path),
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

    async def _ensure_git_repository(self, git_config: KnowledgeGitConfig) -> None:
        git_dir = self.knowledge_path / ".git"
        if git_dir.is_dir():
            current_remote = (await self._run_git(["remote", "get-url", "origin"])).strip()
            expected_remote = _authenticated_repo_url(git_config.repo_url, git_config.credentials_service)
            if current_remote != expected_remote:
                await self._run_git(["remote", "set-url", "origin", expected_remote])
            await self._run_git(["checkout", git_config.branch])
            return

        if self.knowledge_path.exists() and any(self.knowledge_path.iterdir()):
            msg = (
                f"Cannot clone knowledge git repository into non-empty path {self.knowledge_path}. "
                "Clear the folder or use a dedicated path."
            )
            raise RuntimeError(msg)

        self.knowledge_path.parent.mkdir(parents=True, exist_ok=True)
        clone_url = _authenticated_repo_url(git_config.repo_url, git_config.credentials_service)
        await self._run_git(
            [
                "clone",
                "--single-branch",
                "--branch",
                git_config.branch,
                clone_url,
                str(self.knowledge_path),
            ],
            cwd=self.knowledge_path.parent,
        )

    async def _sync_git_repository_once(self, git_config: KnowledgeGitConfig) -> tuple[set[str], set[str], bool]:
        await self._ensure_git_repository(git_config)

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
        if not self.knowledge_path.exists():
            return []
        return sorted(path for path in self.knowledge_path.rglob("*") if self._include_file(path))

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
        if self._git_config() is not None:
            await self.sync_git_repository()

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
        if not self._include_relative_path(self._relative_path(resolved_path)):
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
