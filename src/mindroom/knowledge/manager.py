"""Knowledge base management for file-backed RAG."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import time
import uuid
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NoReturn, Protocol, cast, runtime_checkable
from urllib.parse import quote, urlparse, urlunparse

from agno.knowledge.embedder.base import Embedder
from agno.knowledge.embedder.ollama import OllamaEmbedder
from agno.knowledge.knowledge import Knowledge
from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader
from agno.vectordb.chroma import ChromaDb

from mindroom.constants import RuntimePaths, resolve_config_relative_path
from mindroom.credentials import get_runtime_shared_credentials_manager
from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.embeddings import (
    MindRoomOpenAIEmbedder,
    create_sentence_transformers_embedder,
    effective_knowledge_embedder_signature,
)
from mindroom.knowledge.chunking import SafeFixedSizeChunking
from mindroom.knowledge.redaction import (
    credential_free_url_identity,
    embedded_http_userinfo,
    redact_credentials_in_text,
    redact_url_credentials,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from agno.knowledge.reader.base import Reader

    from mindroom.config.knowledge import KnowledgeGitConfig
    from mindroom.config.main import Config

logger = get_logger(__name__)

_COLLECTION_PREFIX = "mindroom_knowledge"
_SOURCE_PATH_KEY = "source_path"
_SOURCE_MTIME_NS_KEY = "source_mtime_ns"
_SOURCE_SIZE_KEY = "source_size"
_SOURCE_DIGEST_KEY = "source_digest"
_FAILED_SIGNATURE_RETRY_SECONDS = 300
_FAILED_SIGNATURE_RETRY_NS = _FAILED_SIGNATURE_RETRY_SECONDS * 1_000_000_000
_MAX_CONCURRENT_KNOWLEDGE_FILE_INDEXES = 32
_POST_INDEX_VECTOR_VISIBILITY_RETRY_DELAYS_SECONDS = (0.0, 0.01, 0.05)
_GIT_CHECKOUT_DETECTION_TIMEOUT_SECONDS = 5.0
_INDEXING_STATUS_RESETTING = "resetting"
_INDEXING_STATUS_INDEXING = "indexing"
_INDEXING_STATUS_COMPLETE = "complete"
_INDEXING_STATUSES = {
    _INDEXING_STATUS_RESETTING,
    _INDEXING_STATUS_INDEXING,
    _INDEXING_STATUS_COMPLETE,
}
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
_FileSignature = tuple[int, int, str]
_FailedFileSignature = tuple[int, int, str, int]
INDEXING_SETTINGS_BASE_ID_INDEX = 0
INDEXING_SETTINGS_STORAGE_ROOT_INDEX = 1
INDEXING_SETTINGS_KNOWLEDGE_PATH_INDEX = 2
INDEXING_SETTINGS_QUERY_COMPATIBLE_PREFIX_LENGTH = 7
INDEXING_SETTINGS_CHUNK_SIZE_INDEX = 7
INDEXING_SETTINGS_CHUNK_OVERLAP_INDEX = 8
INDEXING_SETTINGS_REPO_IDENTITY_INDEX = 9
INDEXING_SETTINGS_CORPUS_COMPATIBLE_INDEXES = (
    INDEXING_SETTINGS_BASE_ID_INDEX,
    INDEXING_SETTINGS_STORAGE_ROOT_INDEX,
    INDEXING_SETTINGS_KNOWLEDGE_PATH_INDEX,
    INDEXING_SETTINGS_REPO_IDENTITY_INDEX,
    10,
    11,
    12,
    13,
    14,
    15,
    16,
)
INDEXING_SETTINGS_LAYOUT_LENGTH = 17


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


class _CollectionExistenceEmbedder(Embedder):
    """Minimal embedder for collection probes that must never embed content."""

    def get_embedding(self, text: str) -> list[float]:
        _ = text
        msg = "Knowledge collection existence checks must not embed content"
        raise NotImplementedError(msg)

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, object] | None]:
        _ = text
        msg = "Knowledge collection existence checks must not embed content"
        raise NotImplementedError(msg)

    async def async_get_embedding(self, text: str) -> list[float]:
        _ = text
        msg = "Knowledge collection existence checks must not embed content"
        raise NotImplementedError(msg)

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, object] | None]:
        _ = text
        msg = "Knowledge collection existence checks must not embed content"
        raise NotImplementedError(msg)


@dataclass(frozen=True)
class _PersistedIndexingState:
    settings: tuple[str, ...]
    status: Literal["resetting", "indexing", "complete"]
    collection: str | None = None
    last_published_at: str | None = None
    published_revision: str | None = None
    indexed_count: int | None = None
    source_signature: str | None = None


@dataclass
class _CandidatePublishState:
    published: bool = False


def _raise_cancelled() -> NoReturn:
    raise asyncio.CancelledError


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


def git_checkout_present(root: Path, *, timeout_seconds: float | None = None) -> bool:
    """Return whether root itself is a Git worktree checkout."""
    if not root.is_dir():
        return False
    effective_timeout_seconds = _GIT_CHECKOUT_DETECTION_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
    if effective_timeout_seconds <= 0:
        effective_timeout: float | None = None
    else:
        effective_timeout = effective_timeout_seconds
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2 or lines[0] != "true":
        return False
    try:
        return Path(lines[1]).resolve() == root.resolve()
    except OSError:
        return False


def _git_metadata_present(root: Path) -> bool:
    """Return whether root has cheap local Git metadata."""
    return root.is_dir() and (root / ".git").exists()


def chroma_collection_exists(storage_path: Path, collection_name: str) -> bool:
    """Check collection existence without constructing Agno Knowledge."""
    vector_db = ChromaDb(
        collection=collection_name,
        path=str(storage_path),
        persistent_client=True,
        embedder=_CollectionExistenceEmbedder(),
    )
    return vector_db.exists()


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
    settings = (
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
        credential_free_url_identity(git_config.repo_url) if git_config is not None else "",
        git_config.branch if git_config is not None else "",
        str(git_config.lfs) if git_config is not None else "",
        str(git_config.skip_hidden) if git_config is not None else "",
        str(tuple(git_config.include_patterns)) if git_config is not None else "",
        str(tuple(git_config.exclude_patterns)) if git_config is not None else "",
        str(tuple(base_config.include_extensions)) if base_config.include_extensions is not None else "",
        str(tuple(base_config.exclude_extensions)),
    )
    if len(settings) != INDEXING_SETTINGS_LAYOUT_LENGTH:
        msg = "Knowledge indexing settings layout constants must match _indexing_settings_key"
        raise AssertionError(msg)
    return settings


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


def _credential_free_repo_url(repo_url: str) -> str:
    """Return a repository URL suitable for persistent Git config."""
    parsed = urlparse(repo_url)
    if not parsed.scheme or not parsed.netloc:
        return repo_url
    path = parsed.path.split(";", 1)[0]
    if parsed.scheme == "ssh" and "@" in parsed.netloc and parsed.password is None:
        userinfo, host = parsed.netloc.rsplit("@", 1)
        if userinfo and ":" not in userinfo:
            return urlunparse(
                parsed._replace(
                    netloc=f"{userinfo}@{host}",
                    path=path,
                    params="",
                    query="",
                    fragment="",
                ),
            )
    hostname = parsed.netloc.rsplit("@", 1)[-1]
    return urlunparse(
        parsed._replace(
            netloc=hostname,
            path=path,
            params="",
            query="",
            fragment="",
        ),
    )


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
    clean_url = _credential_free_repo_url(repo_url)
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


def _is_hidden_relative_path(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)


def include_knowledge_relative_path(config: Config, base_id: str, relative_path: str) -> bool:
    """Return whether a relative path is managed by the base path filters."""
    path_obj = Path(relative_path)
    if path_obj.is_absolute() or ".." in path_obj.parts:
        return False

    base_config = config.get_knowledge_base_config(base_id)
    git_config = base_config.git
    if git_config is not None and git_config.skip_hidden and _is_hidden_relative_path(path_obj):
        return False

    if git_config is None:
        return True

    if git_config.include_patterns and not any(
        _matches_root_glob(relative_path, pattern) for pattern in git_config.include_patterns
    ):
        return False

    return not any(_matches_root_glob(relative_path, pattern) for pattern in git_config.exclude_patterns)


def include_semantic_knowledge_relative_path(config: Config, base_id: str, relative_path: str) -> bool:
    """Return whether a relative path is semantically indexable for one base."""
    if not include_knowledge_relative_path(config, base_id, relative_path):
        return False

    base_config = config.get_knowledge_base_config(base_id)
    include_extensions = set(base_config.include_extensions) if base_config.include_extensions is not None else None
    exclude_extensions = set(base_config.exclude_extensions)
    allowed_extensions = include_extensions if include_extensions is not None else _TEXT_LIKE_EXTENSIONS

    suffix = Path(relative_path).suffix.lower()
    if suffix not in allowed_extensions:
        return False
    return suffix not in exclude_extensions


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


def include_knowledge_file(config: Config, base_id: str, knowledge_root: Path, file_path: Path) -> bool:
    """Return whether a file belongs to the managed semantic file set."""
    root = knowledge_root.resolve()
    candidate = file_path if file_path.is_absolute() else root / file_path
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    if _path_is_symlink_or_under_symlink(root, candidate):
        return False
    try:
        resolved_candidate = candidate.resolve(strict=True)
        resolved_candidate.relative_to(root)
    except (OSError, ValueError):
        return False
    if not candidate.is_file():
        return False
    relative_path = candidate.relative_to(root)
    return include_semantic_knowledge_relative_path(config, base_id, relative_path.as_posix())


def list_knowledge_files(config: Config, base_id: str, knowledge_root: Path) -> list[Path]:
    """List managed semantic files without constructing a knowledge manager."""
    root = knowledge_root.resolve()
    if not root.is_dir():
        return []

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        current_dir = Path(dirpath)
        dirnames[:] = [dirname for dirname in dirnames if not (current_dir / dirname).is_symlink()]
        for filename in filenames:
            path = current_dir / filename
            if include_knowledge_file(config, base_id, root, path):
                files.append(path)
    return sorted(files)


def _semantic_file_paths_from_relative_paths(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    relative_paths: Iterable[str],
) -> list[Path]:
    root = knowledge_root.resolve()
    files: list[Path] = []
    for relative_path in sorted(set(relative_paths)):
        path = root / relative_path
        if include_knowledge_file(config, base_id, root, path):
            files.append(path)
    return files


def _git_tracked_relative_paths_from_checkout(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    *,
    timeout_seconds: float | None = None,
) -> set[str]:
    git_config = config.get_knowledge_base_config(base_id).git
    if git_config is None:
        return set()
    effective_timeout_seconds = float(
        git_config.sync_timeout_seconds if timeout_seconds is None else timeout_seconds,
    )
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(knowledge_root),
            check=False,
            capture_output=True,
            text=True,
            timeout=effective_timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        msg = f"Git command timed out after {effective_timeout_seconds:g}s: git ls-files -z"
        raise RuntimeError(msg) from exc
    except OSError as exc:
        msg = f"Git command failed: git ls-files -z\n{exc}"
        raise RuntimeError(msg) from exc

    if result.returncode != 0:
        details = redact_credentials_in_text((result.stderr or result.stdout).strip())
        msg = f"Git command failed with exit code {result.returncode}: git ls-files -z"
        if details:
            msg = f"{msg}\n{details}"
        raise RuntimeError(msg)

    return {
        path
        for path in result.stdout.split("\x00")
        if path and include_semantic_knowledge_relative_path(config, base_id, path)
    }


def list_git_tracked_knowledge_files(
    config: Config,
    base_id: str,
    knowledge_root: Path,
    *,
    timeout_seconds: float | None = None,
) -> list[Path]:
    """List Git-tracked semantic files using the same source set as indexing."""
    root = knowledge_root.resolve()
    if not git_checkout_present(root, timeout_seconds=timeout_seconds):
        return []
    return _semantic_file_paths_from_relative_paths(
        config,
        base_id,
        root,
        _git_tracked_relative_paths_from_checkout(config, base_id, root, timeout_seconds=timeout_seconds),
    )


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
            else _git_tracked_relative_paths_from_checkout(config, base_id, root)
        )
        files = _semantic_file_paths_from_relative_paths(config, base_id, root, tracked_paths)
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


def _source_signature_from_file_signatures(file_signatures: Mapping[str, _FileSignature]) -> str:
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
    _settings: tuple[str, ...] = field(init=False)
    _indexing_settings: tuple[str, ...] = field(init=False)
    _base_storage_path: Path = field(init=False)
    _index_failures_path: Path = field(init=False)
    _indexing_settings_path: Path = field(init=False)
    _git_lfs_hydrated_head_path: Path = field(init=False)
    _knowledge: Knowledge = field(init=False)
    _indexed_files: set[str] = field(default_factory=set, init=False)
    _indexed_signatures: dict[str, _FileSignature | None] = field(default_factory=dict, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _state_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _git_sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _git_syncing: bool = field(default=False, init=False)
    _git_repo_present: bool = field(default=False, init=False)
    _git_initial_sync_complete: bool = field(default=False, init=False)
    _git_last_successful_sync_at: datetime | None = field(default=None, init=False)
    _git_last_successful_commit: str | None = field(default=None, init=False)
    _git_last_error: str | None = field(default=None, init=False)
    _last_refresh_error: str | None = field(default=None, init=False)
    _git_lfs_checked: bool = field(default=False, init=False)
    _git_lfs_repository_ready: bool = field(default=False, init=False)
    _git_tracked_relative_paths: set[str] | None = field(default=None, init=False, repr=False)
    _cached_persisted_indexing_state: _PersistedIndexingState | None = field(default=None, init=False, repr=False)
    _persisted_collection_missing_on_init: bool = field(default=False, init=False, repr=False)

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
        self._git_repo_present = base_config.git is not None and _git_metadata_present(self.knowledge_path)
        persisted_state = self._load_persisted_indexing_state()
        self._cached_persisted_indexing_state = persisted_state
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

    def _persisted_collection_missing(self, persisted_state: _PersistedIndexingState | None) -> bool:
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

    def _load_persisted_indexing_state(self) -> _PersistedIndexingState | None:
        if not self._indexing_settings_path.exists():
            return None
        try:
            payload = json.loads(self._indexing_settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

        if not isinstance(payload, dict):
            return None
        raw_settings = payload.get("settings")
        raw_status = payload.get("status")
        raw_collection = payload.get("collection")
        raw_indexed_count = payload.get("indexed_count")
        indexed_count = _coerce_int(raw_indexed_count)
        raw_source_signature = payload.get("source_signature")
        if (
            not isinstance(raw_settings, list)
            or not all(isinstance(item, str) for item in raw_settings)
            or raw_status not in _INDEXING_STATUSES
            or not isinstance(raw_collection, str)
            or not raw_collection
            or indexed_count is None
            or indexed_count < 0
            or not isinstance(raw_source_signature, str)
            or not raw_source_signature
        ):
            return None
        raw_last_published_at = payload.get("last_published_at")
        last_published_at = (
            raw_last_published_at if isinstance(raw_last_published_at, str) and raw_last_published_at else None
        )
        raw_published_revision = payload.get("published_revision")
        published_revision = (
            raw_published_revision if isinstance(raw_published_revision, str) and raw_published_revision else None
        )
        return _PersistedIndexingState(
            tuple(raw_settings),
            cast('Literal["resetting", "indexing", "complete"]', raw_status),
            collection=raw_collection,
            last_published_at=last_published_at,
            published_revision=published_revision,
            indexed_count=indexed_count,
            source_signature=raw_source_signature,
        )

    def _load_persisted_indexing_settings(self) -> tuple[str, ...] | None:
        persisted_state = self._load_persisted_indexing_state()
        return persisted_state.settings if persisted_state is not None else None

    def _save_persisted_indexing_state(
        self,
        status: Literal["resetting", "indexing", "complete"],
        *,
        settings: tuple[str, ...] | None = None,
        collection: str | None = None,
        last_published_at: str | None = None,
        published_revision: str | None = None,
        indexed_count: int | None = None,
        source_signature: str | None = None,
    ) -> None:
        persisted_settings = settings or self._indexing_settings
        payload: dict[str, object] = {
            "settings": list(persisted_settings),
            "status": status,
        }
        if collection is not None:
            payload["collection"] = collection
        if last_published_at is not None:
            payload["last_published_at"] = last_published_at
        if published_revision is not None:
            payload["published_revision"] = published_revision
        if indexed_count is not None:
            payload["indexed_count"] = indexed_count
        if source_signature is not None:
            payload["source_signature"] = source_signature
        tmp_path = self._indexing_settings_path.with_name(
            f".{self._indexing_settings_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp",
        )
        try:
            tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            tmp_path.replace(self._indexing_settings_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        self._cached_persisted_indexing_state = _PersistedIndexingState(
            settings=persisted_settings,
            status=status,
            collection=collection,
            last_published_at=last_published_at,
            published_revision=published_revision,
            indexed_count=indexed_count,
            source_signature=source_signature,
        )

    def _save_persisted_indexing_settings(
        self,
        *,
        indexed_count: int,
        source_signature: str,
    ) -> None:
        self._save_persisted_indexing_state(
            _INDEXING_STATUS_COMPLETE,
            collection=self._current_collection_name(),
            last_published_at=datetime.now(tz=UTC).isoformat(),
            published_revision=self._git_last_successful_commit,
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

    def _startup_index_mode(self) -> Literal["full_reindex", "resume", "incremental"]:
        if self._persisted_collection_missing_on_init:
            return "full_reindex"
        persisted_state = self._load_persisted_indexing_state()
        if persisted_state is None:
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

    def _skip_hidden_paths(self) -> bool:
        git_config = self._git_config()
        return bool(git_config and git_config.skip_hidden)

    def _is_hidden_relative_path(self, relative_path: Path) -> bool:
        return _is_hidden_relative_path(relative_path)

    def _include_file(self, file_path: Path) -> bool:
        return include_knowledge_file(self.config, self.base_id, self._knowledge_source_path(), file_path)

    def _include_semantic_relative_path(self, relative_path: str) -> bool:
        if not self._include_relative_path(relative_path):
            return False

        return include_semantic_knowledge_relative_path(self.config, self.base_id, relative_path)

    def _include_relative_path(self, relative_path: str) -> bool:
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
        tracked_files = {path for path in raw_paths if self._include_semantic_relative_path(path)}
        self._git_tracked_relative_paths = set(tracked_files)
        return tracked_files

    async def _git_dirty_tracked_files(self) -> set[str]:
        output = await self._run_git(["diff", "--name-only", "--no-renames", "HEAD"])
        return {path for path in output.splitlines() if self._include_semantic_relative_path(path)}

    async def _ensure_git_repository(self, git_config: KnowledgeGitConfig) -> bool:
        runtime_paths = self.runtime_paths
        knowledge_root = self._knowledge_source_path()
        if await self._git_checkout_present():
            self._git_repo_present = True
            await self._ensure_git_lfs_repository_ready(knowledge_root)
            current_remote = (await self._run_git(["remote", "get-url", "origin"])).strip()
            expected_remote = _credential_free_repo_url(git_config.repo_url)
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
        clone_url = _credential_free_repo_url(git_config.repo_url)
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
                {"GIT_LFS_SKIP_SMUDGE": "1"} if git_config.lfs else None,
            ),
        )
        self._git_repo_present = True
        await self._run_git(["remote", "set-url", "origin", clone_url], cwd=knowledge_root)
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
            if not dirty_tracked_files:
                await self._hydrate_git_lfs_worktree(git_config, current_head=remote_head)
                return set(), set(), False

            await self._run_git(["checkout", "--force", "-B", git_config.branch, remote_ref])
            # Reviewed with Bas (2026-04-17): program-owned checkout, hard reset is the
            # intentional way to realign it with the configured remote state.
            await self._run_git(["reset", "--hard", remote_ref])
            await self._hydrate_git_lfs_worktree(git_config, current_head=remote_head)
            after_files = await self._git_list_tracked_files()
            changed_files = {path for path in dirty_tracked_files if path in after_files}
            return changed_files, set(), True

        await self._run_git(["checkout", "--force", "-B", git_config.branch, remote_ref])
        # Reviewed with Bas (2026-04-17): program-owned checkout, hard reset is the
        # intentional way to realign it with the configured remote state.
        await self._run_git(["reset", "--hard", remote_ref])
        await self._hydrate_git_lfs_worktree(git_config, current_head=remote_head)

        after_files = await self._git_list_tracked_files()
        if before_head is None:
            changed_paths = after_files
        else:
            diff_output = await self._run_git(["diff", "--name-only", "--no-renames", f"{before_head}..HEAD"])
            changed_paths = {path for path in diff_output.splitlines() if self._include_semantic_relative_path(path)}

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
        if self._git_config() is not None:
            if self._git_tracked_relative_paths is None:
                if not git_checkout_present(knowledge_root, timeout_seconds=self._git_sync_timeout_seconds()):
                    self._git_repo_present = False
                    return []
                self._git_repo_present = True
                self._git_tracked_relative_paths = _git_tracked_relative_paths_from_checkout(
                    self.config,
                    self.base_id,
                    knowledge_root,
                )
            return _semantic_file_paths_from_relative_paths(
                self.config,
                self.base_id,
                knowledge_root,
                self._git_tracked_relative_paths,
            )
        return list_knowledge_files(self.config, self.base_id, knowledge_root)

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

    def _file_signature(self, file_path: Path) -> _FileSignature:
        stat = file_path.stat()
        return stat.st_mtime_ns, stat.st_size, _file_content_digest(file_path)

    def _load_failed_signatures(self) -> dict[str, _FailedFileSignature]:
        if not self._index_failures_path.exists():
            return {}

        try:
            payload = json.loads(self._index_failures_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        if not isinstance(payload, dict):
            return {}

        failed_signatures: dict[str, _FailedFileSignature] = {}
        for path, value in payload.items():
            if not isinstance(path, str):
                continue
            if not isinstance(value, list | tuple) or len(value) != 4:
                continue
            mtime_ns = _coerce_int(value[0])
            size = _coerce_int(value[1])
            if mtime_ns is None or size is None:
                continue
            raw_digest = value[2]
            if not isinstance(raw_digest, str) or not raw_digest:
                continue
            source_digest = raw_digest
            failed_at_ns = _coerce_int(value[3])
            failed_signatures[path] = (mtime_ns, size, source_digest, max(failed_at_ns or 0, 0))
        return failed_signatures

    def _save_failed_signatures(self, failed_signatures: dict[str, _FailedFileSignature]) -> None:
        payload = {
            path: [signature[0], signature[1], signature[2], signature[3]]
            for path, signature in failed_signatures.items()
        }
        self._index_failures_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    def _should_skip_failed_signature(
        self,
        *,
        failed_signature: _FailedFileSignature,
        current_signature: _FileSignature,
    ) -> bool:
        failed_mtime_ns, failed_size, failed_digest, failed_at_ns = failed_signature
        if (failed_mtime_ns, failed_size, failed_digest) != current_signature:
            return False
        if failed_at_ns <= 0:
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

    def _candidate_collection_name(self) -> str:
        return f"{self._default_collection_name()}_candidate_{time.time_ns()}_{uuid.uuid4().hex[:8]}"

    def _build_vector_db(self, collection_name: str) -> ChromaDb:
        return ChromaDb(
            collection=collection_name,
            path=str(self._base_storage_path),
            persistent_client=True,
            embedder=_create_embedder(self.config, self.runtime_paths),
        )

    def _build_knowledge(self, collection_name: str) -> Knowledge:
        return Knowledge(vector_db=self._build_vector_db(collection_name))

    def _cleanup_superseded_collections(
        self,
        *,
        active_collection: str,
    ) -> None:
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return
        client = vector_db.client
        if client is None or not isinstance(client, _CollectionListingClient):
            return

        default_collection = self._default_collection_name()
        candidate_prefix = f"{self._default_collection_name()}_candidate_"

        try:
            collection_names = self._listed_collection_names(client)
        except Exception:
            logger.warning(
                "Failed to list superseded knowledge collections for cleanup",
                base_id=self.base_id,
                exc_info=True,
            )
            return

        for collection_name in collection_names:
            same_base_collection = collection_name == default_collection or collection_name.startswith(candidate_prefix)
            if collection_name == active_collection or not same_base_collection:
                continue
            try:
                self._delete_vector_db(self._build_vector_db(collection_name))
            except Exception:
                logger.warning(
                    "Failed to clean superseded knowledge collection",
                    base_id=self.base_id,
                    collection=collection_name,
                    exc_info=True,
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

    def _delete_vector_db(self, vector_db: ChromaDb) -> None:
        vector_db.delete()

    async def _delete_unpublished_candidate_vector_db(self, vector_db: ChromaDb) -> None:
        cleanup_task = asyncio.create_task(asyncio.to_thread(self._delete_vector_db, vector_db))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            try:
                await cleanup_task
            except Exception:
                logger.warning(
                    "Failed to clean unpublished knowledge candidate collection",
                    base_id=self.base_id,
                    collection=vector_db.collection_name,
                    exc_info=True,
                )
            raise
        except Exception:
            logger.warning(
                "Failed to clean unpublished knowledge candidate collection",
                base_id=self.base_id,
                collection=vector_db.collection_name,
                exc_info=True,
            )

    async def _save_candidate_publish_metadata(
        self,
        *,
        candidate_vector_db: ChromaDb,
        indexed_count: int,
        source_signature: str,
    ) -> bool:
        save_task = asyncio.create_task(
            asyncio.to_thread(
                self._save_persisted_indexing_state,
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
        indexed_signatures: dict[str, _FileSignature | None],
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
        indexed_signatures: dict[str, _FileSignature | None],
        indexed_count: int,
        source_signature: str,
        publish_state: _CandidatePublishState,
    ) -> None:
        publish_cancelled = await self._save_candidate_publish_metadata(
            candidate_vector_db=candidate_vector_db,
            indexed_count=indexed_count,
            source_signature=source_signature,
        )
        publish_state.published = True
        await self._adopt_candidate_vector_db(
            candidate_vector_db=candidate_vector_db,
            indexed_files=indexed_files,
            indexed_signatures=indexed_signatures,
        )
        if publish_cancelled:
            _raise_cancelled()

    def _reset_collection(self) -> None:
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return
        self._reset_vector_db(vector_db)

    def _load_indexed_files_from_vector_db(self) -> dict[str, _FileSignature | None]:
        """Load indexed source paths and optional file signatures from the vector collection."""
        vector_db = self._knowledge.vector_db
        if not isinstance(vector_db, ChromaDb):
            return {}
        if not vector_db.exists():
            return {}

        collection = vector_db.client.get_collection(name=vector_db.collection_name)
        indexed_files: dict[str, _FileSignature | None] = {}
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
                source_digest = metadata.get(_SOURCE_DIGEST_KEY)
                signature = (
                    (source_mtime_ns, source_size, source_digest)
                    if (
                        source_mtime_ns is not None
                        and source_size is not None
                        and isinstance(source_digest, str)
                        and source_digest
                    )
                    else None
                )
                if source_path not in indexed_files or (indexed_files[source_path] is None and signature is not None):
                    indexed_files[source_path] = signature

            ids = result.get("ids", []) or []
            fetched_count = len(ids)
            if fetched_count == 0:
                break
            offset += fetched_count

        return indexed_files

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
        files = await asyncio.to_thread(self.list_files)
        current_signatures = await asyncio.to_thread(
            lambda: {self._relative_path(path): self._file_signature(path) for path in files},
        )
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
            if indexed_signature is None or indexed_signature != signature:
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
                source_mtime_ns, source_size, source_digest = current_signatures[relative_path]
                failed_signatures[relative_path] = (source_mtime_ns, source_size, source_digest, time.time_ns())

        await asyncio.to_thread(self._save_failed_signatures, failed_signatures)

        return {
            "loaded_count": len(indexed_files),
            "indexed_count": indexed_count,
            "removed_count": removed_count,
        }

    async def ensure_git_checkout_ready(self) -> None:
        """Ensure the Git checkout exists before direct file writes land in the knowledge folder."""
        if self._git_config() is None:
            return
        if await self._git_checkout_present():
            self._git_repo_present = True
            return
        await self.sync_git_repository(index_changes=False)

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
            self._git_repo_present = await self._git_checkout_present()
            self._git_last_error = redact_credentials_in_text(str(exc))
            raise
        finally:
            self._git_syncing = False

        self._git_repo_present = await self._git_checkout_present()
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
        indexed_signatures: dict[str, _FileSignature | None] | None = None,
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

        has_vectors = await self._wait_for_source_vectors(
            relative_path,
            knowledge=target_knowledge,
        )
        if not has_vectors:
            if source_size == 0:
                if indexed_files is not None and indexed_signatures is not None:
                    indexed_files.add(relative_path)
                    indexed_signatures[relative_path] = (source_mtime_ns, source_size, source_digest)
                else:
                    async with self._state_lock:
                        self._indexed_files.add(relative_path)
                        self._indexed_signatures[relative_path] = (source_mtime_ns, source_size, source_digest)
                logger.info("Scanned empty knowledge file with no vectors", base_id=self.base_id, path=relative_path)
                return True

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
            indexed_signatures[relative_path] = (source_mtime_ns, source_size, source_digest)
        else:
            async with self._state_lock:
                self._indexed_files.add(relative_path)
                self._indexed_signatures[relative_path] = (source_mtime_ns, source_size, source_digest)
        logger.info("Indexed knowledge file", base_id=self.base_id, path=relative_path)
        return True

    async def _reindex_files_locked(
        self,
        files: list[Path],
        *,
        knowledge: Knowledge | None = None,
        indexed_files: set[str] | None = None,
        indexed_signatures: dict[str, _FileSignature | None] | None = None,
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

    async def reindex_all(self, *, protected_collections: tuple[str, ...] = ()) -> int:
        """Clear and rebuild the knowledge index from disk."""
        _ = protected_collections
        async with self._lock:
            self._last_refresh_error = None
            files = await asyncio.to_thread(self.list_files)
            candidate_knowledge = self._build_knowledge(self._candidate_collection_name())
            candidate_vector_db = candidate_knowledge.vector_db
            if not isinstance(candidate_vector_db, ChromaDb):
                msg = "Knowledge reindex candidate collection requires a ChromaDb vector database"
                raise TypeError(msg)

            await asyncio.to_thread(self._reset_vector_db, candidate_vector_db)
            candidate_publish_state = _CandidatePublishState()
            candidate_indexed_files: set[str] = set()
            candidate_indexed_signatures: dict[str, _FileSignature | None] = {}

            try:
                indexed_count = await self._reindex_files_locked(
                    files,
                    knowledge=candidate_knowledge,
                    indexed_files=candidate_indexed_files,
                    indexed_signatures=candidate_indexed_signatures,
                )
                if indexed_count != len(files):
                    self._last_refresh_error = f"Indexed {indexed_count} of {len(files)} managed knowledge files"
                    return indexed_count

                expected_paths = {self._relative_path(file_path) for file_path in files}
                candidate_signatures = {
                    relative_path: signature
                    for relative_path, signature in candidate_indexed_signatures.items()
                    if signature is not None
                }
                if set(candidate_signatures) != expected_paths:
                    self._last_refresh_error = (
                        f"Indexed signatures covered {len(candidate_signatures)} of {len(expected_paths)} managed files"
                    )
                    return indexed_count

                candidate_source_signature = _source_signature_from_file_signatures(candidate_signatures)
                live_source_signature = await asyncio.to_thread(
                    knowledge_source_signature,
                    self.config,
                    self.base_id,
                    self._knowledge_source_path(),
                    tracked_relative_paths=self._git_tracked_relative_paths,
                )
                if live_source_signature != candidate_source_signature:
                    self._last_refresh_error = "Knowledge source changed during refresh; refresh skipped"
                    return indexed_count

                await self._publish_candidate_after_metadata_save(
                    candidate_vector_db=candidate_vector_db,
                    indexed_files=candidate_indexed_files,
                    indexed_signatures=candidate_indexed_signatures,
                    indexed_count=len(candidate_indexed_files),
                    source_signature=candidate_source_signature,
                    publish_state=candidate_publish_state,
                )
                await asyncio.to_thread(
                    self._cleanup_superseded_collections,
                    active_collection=candidate_vector_db.collection_name,
                )
            except Exception as exc:
                self._last_refresh_error = redact_credentials_in_text(str(exc))
                raise
            else:
                return indexed_count
            finally:
                if not candidate_publish_state.published:
                    await self._delete_unpublished_candidate_vector_db(candidate_vector_db)

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
                "repo_url": redact_url_credentials(git_config.repo_url),
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
            }
        return status
