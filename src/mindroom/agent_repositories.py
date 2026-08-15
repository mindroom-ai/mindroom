"""Trusted policy, broker contracts, and durable state for agent repositories."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast
from urllib.parse import urlsplit

import httpx

from mindroom.durable_write import write_json_file_durable
from mindroom.file_locks import advisory_file_lock
from mindroom.runtime_env_policy import (
    AGENT_REPOSITORY_ENV_BY_KEY,
    KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY,
)
from mindroom.tool_system.worker_routing import descriptive_worker_id_for_key

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_BINDING_VERSION = 1
_BINDING_DIRECTORY = "repository_bindings"
_GITHUB_REPOSITORY_NAME_MAX_LENGTH = 100
_LONG_NAME_DIGEST_LENGTH = 12
_SAFE_SLUG_PATTERN = re.compile(r"[^a-z0-9]+")
_GITHUB_REPOSITORY_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9_.-]+")
_EXPECTED_BINDING_FIELDS = frozenset(
    {
        "version",
        "worker_key",
        "repository_id",
        "organization",
        "repository_name",
        "clone_url",
    },
)
_EXPECTED_LEASE_FIELDS = frozenset(
    {
        "repository_id",
        "organization",
        "repository_name",
        "clone_url",
    },
)
_AGENT_VAULT_ENSURE_PATH = "/v1/internal/mindroom/repositories/ensure"
_DEFAULT_AGENT_VAULT_NAME_PREFIX = "agent-vault"
_AGENT_VAULT_PREFLIGHT_TIMEOUT_SECONDS = 30
_AGENT_VAULT_ENSURE_TIMEOUT_SECONDS = 5 * 60
_BROKER_HTTP_TIMEOUT_SECONDS = _AGENT_VAULT_PREFLIGHT_TIMEOUT_SECONDS + _AGENT_VAULT_ENSURE_TIMEOUT_SECONDS + 15.0
_MAX_BROKER_RESPONSE_BYTES = 64 * 1024
_MAX_GITHUB_REPOSITORY_ID_DIGITS = 20
_MAX_GIT_CONFIG_BYTES = 1024 * 1024
_GIT_CONFIG_PARSE_TIMEOUT_SECONDS = 5.0


class RepositoryBindingError(RuntimeError):
    """Raised when repository ownership or durable state cannot be trusted."""


class RepositoryBrokerError(RuntimeError):
    """Raised when the trusted repository broker cannot satisfy a request."""


class RepositoryOriginConflictError(RepositoryBindingError):
    """Raised when a workspace origin targets a different repository."""


@dataclass(frozen=True)
class RepositoryEnsureRequest:
    """Trusted request sent from MindRoom to its repository broker."""

    worker_key: str
    organization: str
    repository_name: str


@dataclass(frozen=True)
class RepositoryLease:
    """Credential-free repository identity returned by the broker."""

    repository_id: str
    organization: str
    repository_name: str
    clone_url: str


class RepositoryBroker(Protocol):
    """Minimal broker seam implemented by Agent Vault and test fakes."""

    async def ensure_repository(self, request: RepositoryEnsureRequest) -> RepositoryLease:
        """Ensure the request's one bound private repository."""
        ...


class AgentVaultRepositoryBroker:
    """Strict HTTP adapter for Agent Vault's MindRoom repository endpoint."""

    def __init__(
        self,
        *,
        broker_url: str,
        broker_token_file: Path,
        vault_name_prefix: str,
    ) -> None:
        try:
            parsed_url = urlsplit(broker_url)
            port = parsed_url.port
        except ValueError as exc:
            msg = "Agent repository broker URL must be an uncredentialed origin-only HTTP(S) URL"
            raise RepositoryBrokerError(msg) from exc
        if (
            broker_url != broker_url.strip()
            or parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
            or "?" in broker_url
            or "#" in broker_url
            or port == 0
        ):
            msg = "Agent repository broker URL must be an uncredentialed origin-only HTTP(S) URL"
            raise RepositoryBrokerError(msg)
        if not broker_token_file.is_absolute():
            msg = "Agent repository broker token file must use an absolute path"
            raise RepositoryBrokerError(msg)
        if not vault_name_prefix.strip():
            msg = "Agent repository worker-vault prefix must not be empty"
            raise RepositoryBrokerError(msg)
        self._broker_url = broker_url.rstrip("/")
        self._broker_token_file = broker_token_file
        self._vault_name_prefix = vault_name_prefix.strip()

    @classmethod
    def from_runtime(cls, runtime_paths: RuntimePaths) -> AgentVaultRepositoryBroker:
        """Build the adapter from primary-runtime-only environment values."""
        broker_url = (runtime_paths.env_value(AGENT_REPOSITORY_ENV_BY_KEY["broker_url"]) or "").strip()
        token_file_value = (runtime_paths.env_value(AGENT_REPOSITORY_ENV_BY_KEY["broker_token_file"]) or "").strip()
        missing = []
        if not broker_url:
            missing.append(AGENT_REPOSITORY_ENV_BY_KEY["broker_url"])
        if not token_file_value:
            missing.append(AGENT_REPOSITORY_ENV_BY_KEY["broker_token_file"])
        if missing:
            msg = f"Agent repository broker requires these environment values: {', '.join(missing)}"
            raise RepositoryBrokerError(msg)
        token_file = Path(token_file_value).expanduser()
        vault_prefix_env = KUBERNETES_WORKER_BACKEND_CONFIG_ENV_BY_KEY["agent_vault_vault_name_prefix"]
        vault_name_prefix = (
            runtime_paths.env_value(vault_prefix_env, default=_DEFAULT_AGENT_VAULT_NAME_PREFIX)
            or _DEFAULT_AGENT_VAULT_NAME_PREFIX
        )
        return cls(
            broker_url=broker_url,
            broker_token_file=token_file,
            vault_name_prefix=vault_name_prefix,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_BROKER_HTTP_TIMEOUT_SECONDS, trust_env=False)

    def _token(self) -> str:
        try:
            token = self._broker_token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            msg = "Could not read the Agent repository broker token file"
            raise RepositoryBrokerError(msg) from exc
        if not token:
            msg = "Agent repository broker token file is empty"
            raise RepositoryBrokerError(msg)
        return token

    async def ensure_repository(self, request: RepositoryEnsureRequest) -> RepositoryLease:
        """Call the one constrained Agent Vault ensure endpoint."""
        token = self._token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Vault": descriptive_worker_id_for_key(
                request.worker_key,
                prefix=self._vault_name_prefix,
            ),
        }
        payload = {
            "worker_key": request.worker_key,
            "organization": request.organization,
            "repository_name": request.repository_name,
        }
        try:
            async with asyncio.timeout(_BROKER_HTTP_TIMEOUT_SECONDS):
                async with self._client() as client:
                    async with client.stream(
                        "POST",
                        f"{self._broker_url}{_AGENT_VAULT_ENSURE_PATH}",
                        headers=headers,
                        json=payload,
                    ) as response:
                        if response.status_code not in {200, 201}:
                            msg = f"Agent repository broker returned HTTP {response.status_code}"
                            raise RepositoryBrokerError(msg)
                        response_chunks: list[bytes] = []
                        response_size = 0
                        async for chunk in response.aiter_bytes():
                            response_size += len(chunk)
                            if response_size > _MAX_BROKER_RESPONSE_BYTES:
                                msg = "Agent repository broker response is too large"
                                raise RepositoryBrokerError(msg)
                            response_chunks.append(chunk)
        except (TimeoutError, httpx.HTTPError) as exc:
            msg = "Agent repository broker request failed"
            raise RepositoryBrokerError(msg) from exc
        try:
            response_payload = json.loads(b"".join(response_chunks))
        except (json.JSONDecodeError, UnicodeError) as exc:
            msg = "Agent repository broker returned invalid JSON"
            raise RepositoryBrokerError(msg) from exc
        if not isinstance(response_payload, dict) or set(response_payload) != _EXPECTED_LEASE_FIELDS:
            msg = "Agent repository broker response has an invalid schema"
            raise RepositoryBrokerError(msg)
        data = cast("dict[str, object]", response_payload)
        repository_id = data["repository_id"]
        organization = data["organization"]
        repository_name = data["repository_name"]
        clone_url = data["clone_url"]
        if not _is_canonical_repository_id(repository_id) or not all(
            isinstance(value, str) for value in (organization, repository_name, clone_url)
        ):
            msg = "Agent repository broker response has invalid field types"
            raise RepositoryBrokerError(msg)
        lease = RepositoryLease(
            repository_id=cast("str", repository_id),
            organization=cast("str", organization),
            repository_name=cast("str", repository_name),
            clone_url=cast("str", clone_url),
        )
        _validate_lease(request, lease)
        return lease


@dataclass(frozen=True)
class RepositoryBinding:
    """Immutable local binding from one worker identity to one GitHub repository ID."""

    version: int
    worker_key: str
    repository_id: str
    organization: str
    repository_name: str
    clone_url: str


def _repository_slug(value: str, *, label: str) -> str:
    slug = _SAFE_SLUG_PATTERN.sub("-", value.casefold()).strip("-")
    if not slug:
        msg = f"Cannot derive a repository {label} slug from {value!r}"
        raise RepositoryBindingError(msg)
    return slug


def _matrix_requester_localpart(requester_id: str | None) -> str:
    if not requester_id or not requester_id.startswith("@") or ":" not in requester_id:
        msg = "Private agent repository naming requires an authoritative Matrix requester ID"
        raise RepositoryBindingError(msg)
    localpart, server_name = requester_id[1:].split(":", 1)
    if not localpart or not server_name:
        msg = "Private agent repository naming requires a non-empty Matrix requester localpart and server"
        raise RepositoryBindingError(msg)
    return localpart


def _bounded_repository_name(name: str) -> str:
    if len(name) <= _GITHUB_REPOSITORY_NAME_MAX_LENGTH:
        return name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:_LONG_NAME_DIGEST_LENGTH]
    prefix_length = _GITHUB_REPOSITORY_NAME_MAX_LENGTH - len(digest) - 1
    return f"{name[:prefix_length].rstrip('-')}-{digest}"


def derive_repository_name(*, prefix: str, worker_target: ResolvedWorkerTarget) -> str:
    """Derive the only repository name one trusted worker target may own."""
    if prefix != "MindRoom":
        msg = "Agent repository prefix must be exactly 'MindRoom'"
        raise RepositoryBindingError(msg)

    agent_name = worker_target.routing_agent_name
    if not agent_name:
        msg = "Agent repository naming requires a routing agent name"
        raise RepositoryBindingError(msg)
    agent_slug = _repository_slug(agent_name, label="agent")

    if worker_target.worker_scope == "shared":
        if not worker_target.worker_key:
            msg = "Shared agent repository naming requires a worker identity"
            raise RepositoryBindingError(msg)
        return _bounded_repository_name(f"{prefix}-{agent_slug}")

    private_names = worker_target.private_agent_names or frozenset()
    if worker_target.worker_scope != "user_agent" or agent_name not in private_names:
        msg = "Agent repositories require a shared worker or private.per=user_agent identity"
        raise RepositoryBindingError(msg)
    identity = worker_target.execution_identity
    requester_id = identity.requester_id if identity is not None else None
    username_slug = _repository_slug(
        _matrix_requester_localpart(requester_id),
        label="requester",
    )
    if not worker_target.worker_key:
        msg = "Private agent repository naming requires a worker identity"
        raise RepositoryBindingError(msg)
    return _bounded_repository_name(f"{prefix}-{agent_slug}-{username_slug}")


def _is_canonical_repository_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= _MAX_GITHUB_REPOSITORY_ID_DIGITS
        and value.isascii()
        and value.isdecimal()
        and not value.startswith("0")
    )


def _validate_lease(request: RepositoryEnsureRequest, lease: RepositoryLease) -> None:
    if not request.worker_key.strip():
        msg = "Repository binding requires a non-empty worker key"
        raise RepositoryBindingError(msg)
    if not _is_canonical_repository_id(lease.repository_id):
        msg = "Repository broker returned an invalid repository ID"
        raise RepositoryBindingError(msg)
    if lease.organization != request.organization:
        msg = "Repository broker returned an unexpected organization"
        raise RepositoryBindingError(msg)
    if lease.repository_name != request.repository_name:
        msg = "Repository broker returned an unexpected repository name"
        raise RepositoryBindingError(msg)

    expected_clone_url = f"https://github.com/{request.organization}/{request.repository_name}.git"
    if lease.clone_url != expected_clone_url:
        msg = "Repository broker returned a non-canonical or credentialed clone URL"
        raise RepositoryBindingError(msg)


def _binding_from_payload(payload: object) -> RepositoryBinding:
    if not isinstance(payload, dict) or set(payload) != _EXPECTED_BINDING_FIELDS:
        msg = "Repository binding file has an invalid schema"
        raise RepositoryBindingError(msg)
    data = cast("dict[str, object]", payload)
    if type(data["version"]) is not int or data["version"] != _BINDING_VERSION:
        msg = "Repository binding file has an unsupported version"
        raise RepositoryBindingError(msg)
    repository_id = data["repository_id"]
    if not _is_canonical_repository_id(repository_id):
        msg = "Repository binding file has an invalid repository ID"
        raise RepositoryBindingError(msg)
    string_fields = ("worker_key", "organization", "repository_name", "clone_url")
    if any(not isinstance(data[field], str) or not cast("str", data[field]) for field in string_fields):
        msg = "Repository binding file has an invalid string field"
        raise RepositoryBindingError(msg)
    return RepositoryBinding(
        version=_BINDING_VERSION,
        worker_key=cast("str", data["worker_key"]),
        repository_id=cast("str", repository_id),
        organization=cast("str", data["organization"]),
        repository_name=cast("str", data["repository_name"]),
        clone_url=cast("str", data["clone_url"]),
    )


class RepositoryBindingStore:
    """Write-once repository bindings rooted in canonical MindRoom storage."""

    def __init__(self, runtime_paths: RuntimePaths) -> None:
        self._storage_root = runtime_paths.storage_root.expanduser().resolve()
        self._root = self._storage_root / _BINDING_DIRECTORY

    def _binding_path(self, worker_key: str) -> Path:
        """Return the non-reversible binding path for one worker key."""
        digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"

    def _lock_path(self, worker_key: str) -> Path:
        digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()
        return self._root / f".{digest}.lock"

    def workspace_lock_path(self, worker_key: str) -> Path:
        """Return the out-of-workspace lock used for local Git configuration."""
        digest = hashlib.sha256(worker_key.encode("utf-8")).hexdigest()
        return self._root / f".{digest}.workspace.lock"

    def _ensure_root(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or self._root.resolve().parent != self._storage_root:
            msg = "Repository binding directory must stay inside configured storage_root"
            raise RepositoryBindingError(msg)

    def _read_unlocked(self, worker_key: str) -> RepositoryBinding | None:
        path = self._binding_path(worker_key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            msg = f"Could not read repository binding: {exc}"
            raise RepositoryBindingError(msg) from exc
        binding = _binding_from_payload(payload)
        if binding.worker_key != worker_key:
            msg = "Repository binding worker identity does not match its storage key"
            raise RepositoryBindingError(msg)
        return binding

    def read(self, worker_key: str) -> RepositoryBinding | None:
        """Read one binding under the same lock used by writers."""
        self._ensure_root()
        with advisory_file_lock(self._lock_path(worker_key)):
            return self._read_unlocked(worker_key)

    def bind(self, request: RepositoryEnsureRequest, lease: RepositoryLease) -> RepositoryBinding:
        """Persist or replay one exact binding; reject every attempted rebind."""
        _validate_lease(request, lease)
        candidate = RepositoryBinding(
            version=_BINDING_VERSION,
            worker_key=request.worker_key,
            repository_id=lease.repository_id,
            organization=lease.organization,
            repository_name=lease.repository_name,
            clone_url=lease.clone_url,
        )
        self._ensure_root()
        with advisory_file_lock(self._lock_path(request.worker_key)):
            existing = self._read_unlocked(request.worker_key)
            if existing is not None:
                if existing != candidate:
                    msg = "Worker identity already has a different immutable repository binding"
                    raise RepositoryBindingError(msg)
                return existing

            path = self._binding_path(request.worker_key)
            write_json_file_durable(
                path,
                asdict(candidate),
                strict_atomic_replace=True,
                indent=2,
                sort_keys=True,
                trailing_newline=True,
            )
            path.chmod(0o600)
            return candidate


def _absolute_workspace_path(workspace: Path) -> Path:
    expanded_workspace = workspace.expanduser()
    absolute_workspace = expanded_workspace if expanded_workspace.is_absolute() else Path.cwd() / expanded_workspace
    if ".." in absolute_workspace.parts:
        msg = "Agent repository workspace path must not contain parent traversal"
        raise RepositoryBindingError(msg)
    return absolute_workspace


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _open_or_create_directory_at(parent_fd: int, name: str, *, error: str) -> int:
    try:
        return os.open(name, _directory_open_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        with suppress(FileExistsError):
            os.mkdir(name, mode=0o755, dir_fd=parent_fd)
        try:
            return os.open(name, _directory_open_flags(), dir_fd=parent_fd)
        except OSError as exc:
            raise RepositoryBindingError(error) from exc
    except OSError as exc:
        raise RepositoryBindingError(error) from exc


def _open_workspace_directory(workspace: Path) -> tuple[Path, int]:
    absolute_workspace = _absolute_workspace_path(workspace)
    current_fd = os.open(absolute_workspace.anchor, _directory_open_flags())
    try:
        for part in absolute_workspace.parts[1:]:
            next_fd = _open_or_create_directory_at(
                current_fd,
                part,
                error="Agent repository workspace path must not contain symlinks",
            )
            os.close(current_fd)
            current_fd = next_fd
    except Exception:
        os.close(current_fd)
        raise
    return absolute_workspace, current_fd


def _open_existing_git_directory(workspace_fd: int) -> int | None:
    try:
        return os.open(".git", _directory_open_flags(), dir_fd=workspace_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        msg = "Agent repository workspace Git metadata must be a local directory"
        raise RepositoryBindingError(msg) from exc


def _ensure_directory_at(parent_fd: int, name: str) -> int:
    return _open_or_create_directory_at(
        parent_fd,
        name,
        error="Agent repository workspace Git metadata must contain only local directories",
    )


def _atomic_write_at(directory_fd: int, name: str, payload: bytes, *, mode: int) -> None:
    temporary_name = f".{name}.mindroom-{secrets.token_hex(8)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        file_fd = os.open(temporary_name, flags, mode, dir_fd=directory_fd)
        try:
            _write_all(file_fd, payload)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        os.replace(temporary_name, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory_fd)
        raise


def _write_all(file_fd: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(file_fd, remaining)
        remaining = remaining[written:]


def _rename_at_no_replace(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
    """Atomically publish one directory entry without replacing an existing path."""
    libc = ctypes.CDLL(None, use_errno=True)
    source_bytes = os.fsencode(source)
    destination_bytes = os.fsencode(destination)
    if sys.platform == "darwin":
        rename = libc.renameatx_np
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_fd, source_bytes, destination_fd, destination_bytes, 0x00000004)
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        result = rename(source_fd, source_bytes, destination_fd, destination_bytes, 1)
    else:
        msg = "Atomic agent repository workspace publication is unsupported on this platform"
        raise RepositoryBindingError(msg)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _remote_origin_config(clone_url: str) -> bytes:
    return (f'[remote "origin"]\n\turl = {clone_url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n').encode()


def _initialize_git_directory(git_fd: int, clone_url: str) -> None:
    objects_fd = _ensure_directory_at(git_fd, "objects")
    try:
        for name in ("info", "pack"):
            child_fd = _ensure_directory_at(objects_fd, name)
            os.close(child_fd)
    finally:
        os.close(objects_fd)
    refs_fd = _ensure_directory_at(git_fd, "refs")
    try:
        for name in ("heads", "tags"):
            child_fd = _ensure_directory_at(refs_fd, name)
            os.close(child_fd)
    finally:
        os.close(refs_fd)

    config = (
        b"[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n\tlogallrefupdates = true\n"
    ) + _remote_origin_config(clone_url)
    _atomic_write_at(git_fd, "HEAD", b"ref: refs/heads/main\n", mode=0o644)
    _atomic_write_at(git_fd, "config", config, mode=0o644)


def _remove_unpublished_git_directory(workspace_fd: int, staging_name: str, git_fd: int) -> None:
    for name in ("HEAD", "config"):
        with suppress(FileNotFoundError):
            os.unlink(name, dir_fd=git_fd)
    for parent_name, child_names in (("objects", ("info", "pack")), ("refs", ("heads", "tags"))):
        try:
            parent_fd = os.open(parent_name, _directory_open_flags(), dir_fd=git_fd)
        except FileNotFoundError:
            continue
        try:
            for child_name in child_names:
                with suppress(FileNotFoundError):
                    os.rmdir(child_name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        with suppress(FileNotFoundError):
            os.rmdir(parent_name, dir_fd=git_fd)
    if _directory_entry_matches(workspace_fd, staging_name, git_fd):
        os.rmdir(staging_name, dir_fd=workspace_fd)


def _initialize_git_directory_atomically(workspace_fd: int, clone_url: str) -> int:
    staging_name = f".git.mindroom-{secrets.token_hex(8)}"
    try:
        os.mkdir(staging_name, mode=0o755, dir_fd=workspace_fd)
        git_fd = os.open(staging_name, _directory_open_flags(), dir_fd=workspace_fd)
    except OSError as exc:
        msg = "Could not stage agent repository workspace Git metadata"
        raise RepositoryBindingError(msg) from exc
    try:
        _initialize_git_directory(git_fd, clone_url)
        os.fsync(git_fd)
        try:
            _rename_at_no_replace(workspace_fd, staging_name, workspace_fd, ".git")
        except OSError as exc:
            msg = "Agent repository workspace Git metadata changed during configuration"
            raise RepositoryBindingError(msg) from exc
    except Exception:
        _remove_unpublished_git_directory(workspace_fd, staging_name, git_fd)
        os.close(git_fd)
        raise
    try:
        os.fsync(workspace_fd)
    except Exception:
        os.close(git_fd)
        raise
    return git_fd


def _open_git_config(git_fd: int) -> int:
    try:
        config_fd = os.open("config", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=git_fd)
    except OSError as exc:
        msg = "Agent repository workspace Git metadata files must be local regular files"
        raise RepositoryBindingError(msg) from exc
    config_stat = os.fstat(config_fd)
    if not stat.S_ISREG(config_stat.st_mode):
        os.close(config_fd)
        msg = "Agent repository workspace Git metadata files must be local regular files"
        raise RepositoryBindingError(msg)
    return config_fd


def _read_git_config_payload(config_fd: int) -> bytes:
    config_stat_before = os.fstat(config_fd)
    if config_stat_before.st_size > _MAX_GIT_CONFIG_BYTES:
        msg = "Agent repository workspace Git config is too large"
        raise RepositoryBindingError(msg)

    chunks: list[bytes] = []
    offset = 0
    while offset <= _MAX_GIT_CONFIG_BYTES:
        chunk = os.pread(
            config_fd,
            min(64 * 1024, _MAX_GIT_CONFIG_BYTES + 1 - offset),
            offset,
        )
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    payload = b"".join(chunks)
    if len(payload) > _MAX_GIT_CONFIG_BYTES:
        msg = "Agent repository workspace Git config is too large"
        raise RepositoryBindingError(msg)

    config_stat_after = os.fstat(config_fd)
    before = (
        config_stat_before.st_dev,
        config_stat_before.st_ino,
        config_stat_before.st_size,
        config_stat_before.st_mtime_ns,
        config_stat_before.st_ctime_ns,
    )
    after = (
        config_stat_after.st_dev,
        config_stat_after.st_ino,
        config_stat_after.st_size,
        config_stat_after.st_mtime_ns,
        config_stat_after.st_ctime_ns,
    )
    if before != after or len(payload) != config_stat_after.st_size:
        msg = "Agent repository workspace Git config changed during inspection"
        raise RepositoryBindingError(msg)
    return payload


def _parse_git_config_entries(payload: bytes) -> tuple[tuple[str, str], ...]:
    try:
        config_text = payload.decode("utf-8")
        result = subprocess.run(
            ["git", "config", "--no-includes", "--file", "-", "--null", "--list"],
            input=config_text,
            check=False,
            capture_output=True,
            text=True,
            timeout=_GIT_CONFIG_PARSE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        msg = "Could not inspect the agent repository workspace Git metadata"
        raise RepositoryBindingError(msg) from exc
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > _MAX_GIT_CONFIG_BYTES:
        msg = "Could not inspect the agent repository workspace Git metadata"
        raise RepositoryBindingError(msg)
    records = result.stdout.removesuffix("\0").split("\0") if result.stdout else []
    return tuple(record.partition("\n")[::2] for record in records)


def _read_git_config_entries(config_fd: int) -> tuple[tuple[str, str], ...]:
    return _parse_git_config_entries(_read_git_config_payload(config_fd))


def _origin_urls_from_config(
    entries: tuple[tuple[str, str], ...],
) -> tuple[list[str], list[str], bool]:
    normalized_entries = tuple((key.casefold(), value) for key, value in entries)
    if any(
        key == "include.path"
        or key.startswith("includeif.")
        or (key.startswith("url.") and key.endswith((".insteadof", ".pushinsteadof")))
        for key, _value in normalized_entries
    ):
        msg = "Agent repository workspace has URL rewriting or included Git configuration"
        raise RepositoryOriginConflictError(msg)

    origin_entries = tuple((key, value) for key, value in entries if key.startswith("remote.origin."))
    fetch_urls = [value for key, value in origin_entries if key == "remote.origin.url"]
    push_urls = [value for key, value in origin_entries if key == "remote.origin.pushurl"]
    return fetch_urls, push_urls, bool(origin_entries)


def _reject_indirect_git_configuration(git_fd: int) -> None:
    for name in ("commondir", "config.worktree"):
        try:
            os.stat(name, dir_fd=git_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        msg = "Agent repository workspace uses unsupported indirect Git configuration"
        raise RepositoryOriginConflictError(msg)


def _require_complete_git_directory(git_fd: int) -> None:
    required_entries = (("HEAD", stat.S_ISREG), ("objects", stat.S_ISDIR), ("refs", stat.S_ISDIR))
    for name, expected_type in required_entries:
        try:
            entry_stat = os.stat(name, dir_fd=git_fd, follow_symlinks=False)
        except OSError as exc:
            msg = "Agent repository workspace has incomplete Git metadata"
            raise RepositoryBindingError(msg) from exc
        if not expected_type(entry_stat.st_mode):
            msg = "Agent repository workspace has incomplete Git metadata"
            raise RepositoryBindingError(msg)


def _directory_entry_matches(parent_fd: int, name: str, directory_fd: int) -> bool:
    try:
        current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    opened_stat = os.fstat(directory_fd)
    return stat.S_ISDIR(current_stat.st_mode) and (current_stat.st_dev, current_stat.st_ino) == (
        opened_stat.st_dev,
        opened_stat.st_ino,
    )


def _git_directory_is_current(workspace_fd: int, git_fd: int) -> bool:
    return _directory_entry_matches(workspace_fd, ".git", git_fd)


def _workspace_directory_is_current(workspace_path: Path, workspace_fd: int) -> bool:
    try:
        current_stat = workspace_path.stat(follow_symlinks=False)
    except OSError:
        return False
    opened_stat = os.fstat(workspace_fd)
    return stat.S_ISDIR(current_stat.st_mode) and (current_stat.st_dev, current_stat.st_ino) == (
        opened_stat.st_dev,
        opened_stat.st_ino,
    )


def _regular_file_descriptor_matches(git_fd: int, name: str, config_fd: int) -> bool:
    try:
        current_stat = os.stat(name, dir_fd=git_fd, follow_symlinks=False)
    except OSError:
        return False
    opened_stat = os.fstat(config_fd)
    return (
        stat.S_ISREG(current_stat.st_mode)
        and opened_stat.st_nlink == 1
        and current_stat.st_nlink == 1
        and (current_stat.st_dev, current_stat.st_ino) == (opened_stat.st_dev, opened_stat.st_ino)
    )


def _git_config_is_current(git_fd: int, config_fd: int) -> bool:
    return _regular_file_descriptor_matches(git_fd, "config", config_fd)


def _stage_git_config(git_fd: int, payload: bytes) -> tuple[str, int]:
    staging_name = f".config.mindroom-{secrets.token_hex(8)}"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        staging_fd = os.open(staging_name, flags, 0o644, dir_fd=git_fd)
        try:
            _write_all(staging_fd, payload)
            os.fsync(staging_fd)
        except Exception:
            os.close(staging_fd)
            raise
    except Exception:
        with suppress(FileNotFoundError):
            os.unlink(staging_name, dir_fd=git_fd)
        raise
    return staging_name, staging_fd


def _restore_git_config_backup(git_fd: int, backup_name: str) -> bool:
    try:
        _rename_at_no_replace(git_fd, backup_name, git_fd, "config")
    except OSError:
        return False
    os.fsync(git_fd)
    return True


def _replace_git_config_if_unchanged(
    git_fd: int,
    config_fd: int,
    original: bytes,
    replacement: bytes,
) -> None:
    staging_name, staging_fd = _stage_git_config(git_fd, replacement)
    backup_name = f".config.mindroom-backup-{secrets.token_hex(8)}"
    backup_exists = False
    published = False
    try:
        os.rename("config", backup_name, src_dir_fd=git_fd, dst_dir_fd=git_fd)
        backup_exists = True
        if (
            not _regular_file_descriptor_matches(git_fd, backup_name, config_fd)
            or _read_git_config_payload(config_fd) != original
        ):
            if _restore_git_config_backup(git_fd, backup_name):
                backup_exists = False
            msg = "Agent repository workspace Git config changed during configuration"
            raise RepositoryBindingError(msg)
        try:
            _rename_at_no_replace(git_fd, staging_name, git_fd, "config")
        except OSError as exc:
            if _restore_git_config_backup(git_fd, backup_name):
                backup_exists = False
            msg = "Agent repository workspace Git config changed during configuration"
            raise RepositoryBindingError(msg) from exc
        published = True
        if not _git_config_is_current(git_fd, staging_fd) or _read_git_config_payload(staging_fd) != replacement:
            msg = "Agent repository workspace Git config changed during configuration"
            raise RepositoryBindingError(msg)
        os.unlink(backup_name, dir_fd=git_fd)
        backup_exists = False
        os.fsync(git_fd)
    finally:
        os.close(staging_fd)
        if not published:
            with suppress(FileNotFoundError):
                os.unlink(staging_name, dir_fd=git_fd)
        if backup_exists:
            os.fsync(git_fd)


def _append_origin_to_git_config(git_fd: int, config_fd: int, clone_url: str) -> None:
    config = _read_git_config_payload(config_fd)
    _fetch_urls, _push_urls, has_origin = _origin_urls_from_config(_parse_git_config_entries(config))
    if has_origin:
        msg = "Agent repository workspace origin changed during configuration"
        raise RepositoryOriginConflictError(msg)
    separator = b"\n" if config and not config.endswith(b"\n") else b""
    expected = config + separator + _remote_origin_config(clone_url)
    if len(expected) > _MAX_GIT_CONFIG_BYTES:
        msg = "Agent repository workspace Git config is too large"
        raise RepositoryBindingError(msg)

    _replace_git_config_if_unchanged(git_fd, config_fd, config, expected)


def _github_transport_and_path(url: str) -> tuple[str, str] | None:
    """Parse supported credential-free GitHub transports into a repository path."""
    if url.startswith("git@github.com:"):
        return "ssh", url.removeprefix("git@github.com:")

    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    valid_https = scheme == "https" and parsed.username is None and parsed.password is None and port in {None, 443}
    valid_ssh = scheme == "ssh" and parsed.username == "git" and parsed.password is None and port in {None, 22}
    if (
        not (valid_https or valid_ssh)
        or parsed.hostname is None
        or parsed.hostname.casefold() != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        return None
    return scheme, parsed.path.removeprefix("/")


def _normalized_github_repository(url: str) -> tuple[str, str, str] | None:
    """Return transport plus credential-free canonical GitHub repository identity."""
    transport_and_path = _github_transport_and_path(url)
    if transport_and_path is None:
        return None
    transport, path = transport_and_path

    path = path.removesuffix("/")
    parts = path.split("/")
    if len(parts) != 2:
        return None
    organization, repository = parts
    if repository.casefold().endswith(".git"):
        repository = repository[:-4]
    if not organization or not repository:
        return None
    if not all(_GITHUB_REPOSITORY_COMPONENT_PATTERN.fullmatch(component) for component in (organization, repository)):
        return None
    return transport, organization.casefold(), repository.casefold()


def _require_expected_origin(
    entries: tuple[tuple[str, str], ...],
    expected: tuple[str, str, str],
) -> bool:
    fetch_urls, push_urls, has_origin = _origin_urls_from_config(entries)
    actual_urls = [*fetch_urls, *(push_urls or fetch_urls)]
    if has_origin and (not fetch_urls or any(_normalized_github_repository(url) != expected for url in actual_urls)):
        msg = "Agent repository workspace has an origin for a different repository"
        raise RepositoryOriginConflictError(msg)
    return has_origin


def _read_current_git_config_entries(git_fd: int, config_fd: int) -> tuple[tuple[str, str], ...]:
    entries = _read_git_config_entries(config_fd)
    if (
        not _git_config_is_current(git_fd, config_fd)
        or _parse_git_config_entries(_read_git_config_payload(config_fd)) != entries
        or not _git_config_is_current(git_fd, config_fd)
    ):
        msg = "Agent repository workspace Git config changed during configuration"
        raise RepositoryBindingError(msg)
    return entries


def _configure_git_origin(
    workspace_fd: int,
    git_fd: int,
    clone_url: str,
    expected: tuple[str, str, str],
) -> None:
    config_fd = _open_git_config(git_fd)
    try:
        has_origin = _require_expected_origin(_read_current_git_config_entries(git_fd, config_fd), expected)
        if not has_origin:
            if not _git_directory_is_current(workspace_fd, git_fd):
                msg = "Agent repository workspace Git metadata changed during configuration"
                raise RepositoryBindingError(msg)
            _append_origin_to_git_config(git_fd, config_fd, clone_url)
    finally:
        os.close(config_fd)


def _verify_configured_git_workspace(
    workspace_path: Path,
    workspace_fd: int,
    git_fd: int,
    expected: tuple[str, str, str],
) -> None:
    _reject_indirect_git_configuration(git_fd)
    _require_complete_git_directory(git_fd)
    if not _git_directory_is_current(workspace_fd, git_fd):
        msg = "Agent repository workspace Git metadata changed during configuration"
        raise RepositoryBindingError(msg)
    config_fd = _open_git_config(git_fd)
    try:
        if not _require_expected_origin(_read_current_git_config_entries(git_fd, config_fd), expected):
            msg = "Agent repository workspace has an origin for a different repository"
            raise RepositoryOriginConflictError(msg)
    finally:
        os.close(config_fd)
    if not _git_directory_is_current(workspace_fd, git_fd):
        msg = "Agent repository workspace Git metadata changed during configuration"
        raise RepositoryBindingError(msg)
    if not _workspace_directory_is_current(workspace_path, workspace_fd):
        msg = "Agent repository workspace path changed during configuration"
        raise RepositoryBindingError(msg)


def configure_repository_workspace(
    *,
    workspace: Path,
    clone_url: str,
    lock_path: Path,
) -> Path:
    """Initialize one workspace and set its immutable credential-free origin."""
    expected = _normalized_github_repository(clone_url)
    if expected is None or expected[0] != "https":
        msg = "Agent repository workspace requires a canonical credential-free HTTPS origin"
        raise RepositoryBindingError(msg)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with advisory_file_lock(lock_path):
        workspace_path, workspace_fd = _open_workspace_directory(workspace)
        try:
            git_fd = _open_existing_git_directory(workspace_fd)
            if git_fd is None:
                git_fd = _initialize_git_directory_atomically(workspace_fd, clone_url)
            try:
                _reject_indirect_git_configuration(git_fd)
                _require_complete_git_directory(git_fd)
                _configure_git_origin(workspace_fd, git_fd, clone_url, expected)
                _verify_configured_git_workspace(workspace_path, workspace_fd, git_fd, expected)
                return workspace_path
            finally:
                os.close(git_fd)
        finally:
            os.close(workspace_fd)
