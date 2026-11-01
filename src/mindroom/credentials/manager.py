"""Unified credentials management for MindRoom.

This module provides centralized credential storage and retrieval for all integrations,
used by both agents and the dashboard interface.
"""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mindroom import runtime_env_policy as _runtime_env_policy
from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.credentials.legacy_env_credentials import remove_environment_copies
from mindroom.credentials.policy import CredentialPlacement, CredentialServicePolicy, credential_service_policy
from mindroom.durable_write import create_directory_durable
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root, open_regular_file_within_root
from mindroom.tool_system.worker_routing import (
    WORKER_CREDENTIALS_DIRNAME,
    WORKER_SHARED_CREDENTIALS_DIRNAME,
    worker_log_id,
    worker_root_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.environment import DeclaredCredential
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

# The characters a credential service name may contain. A config name that becomes part of one, such as a model
# name in its dashboard key ``model:<name>``, follows the same rule.
SERVICE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9:_-]+$")
SERVICE_NAME_CHARACTERS = "letters, numbers, colon, underscore, and hyphen"
_CREDENTIALS_FILE_SUFFIX = "_credentials.json"
# Worker roots are mounted read-write into worker containers under the primary's own
# uid, so each operation pins its credential directory with a no-follow descriptor and
# opens payloads relative to it. Payloads are opened without blocking so a FIFO planted
# by worker code cannot stall the primary; anything but a regular file is then rejected.
_CREDENTIALS_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY
_MAX_CREDENTIALS_PAYLOAD_BYTES = 1024 * 1024
# A missing path, a link, and a non-directory parent all mean the same thing to the
# store: no credential payload it owns is stored there.
_ABSENT_CREDENTIALS_ERRNOS = frozenset({errno.ELOOP, errno.ENOENT, errno.ENOTDIR})
# A saved document that raises one of these on read or decode, such as one encrypted under another key, is unreadable.
_UNREADABLE_CREDENTIALS_ERRORS = (OSError, TypeError, ValueError, InvalidTag)
# A directory whose name is now held by something else is never hardened through, and a
# layer mounted read-only into a worker is hardened by the primary that owns it.
_UNHARDENABLE_DIRECTORY_ERRNOS = _ABSENT_CREDENTIALS_ERRNOS | {errno.EEXIST, errno.EROFS}
_PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME = "private_oauth"
# Sanitized scope directory parts never start with "_", so this literal cannot
# collide with a requester directory inside the primary-runtime scoped store.
_PRIMARY_RUNTIME_AGENT_SCOPED_DIRNAME = "_agents"
# A grant passes on documents saved in the dashboard or declared by the environment, never OAuth connections.
_WORKER_GRANTABLE_SHARED_CREDENTIAL_SOURCES = frozenset({"ui", None})
# Securing one dormant worker store takes several filesystem round trips, which on network
# storage with thousands of workers made the primary's first credential access take seconds.
_WORKER_STORE_HARDENING_CONCURRENCY = 16
_ENCRYPTED_CREDENTIALS_MAGIC = b"MINDROOM-CREDENTIALS-V1\n"
_AES_GCM_NONCE_SIZE = 12
logger = get_logger(__name__)

__all__ = [
    "SERVICE_NAME_CHARACTERS",
    "SERVICE_NAME_PATTERN",
    "CredentialLayers",
    "CredentialsManager",
    "StoredCredentialsUpdate",
    "WorkerCredentialPathError",
    "credential_layers",
    "delete_scoped_credentials",
    "get_runtime_credentials_manager",
    "get_runtime_shared_credentials_manager",
    "load_scoped_credentials",
    "remove_worker_service_credentials",
    "runtime_credentials_manager_key",
    "save_scoped_credentials",
    "scoped_credentials_path",
    "sync_shared_credentials_to_worker",
    "update_stored_service_credentials",
    "validate_service_name",
]


@dataclass(frozen=True, slots=True)
class _CredentialsManagerKey:
    base_path: Path
    shared_base_path: Path
    current_worker_key: str | None
    current_worker_root: Path | None
    encryption_key: str | None
    # The primary's environment inputs, whose declared credentials its store reads under saved ones; None elsewhere.
    declaring_env_digest: str | None


_credentials_managers: dict[_CredentialsManagerKey, CredentialsManager] = {}
_credentials_manager_lock = threading.Lock()


class WorkerCredentialPathError(RuntimeError):
    """Raised when a worker credential directory is not a real directory."""


def validate_service_name(service: str) -> str:
    """Validate and normalize credential service names."""
    normalized = service.strip()
    if not normalized:
        msg = "Service name is required"
        raise ValueError(msg)
    if not SERVICE_NAME_PATTERN.fullmatch(normalized):
        msg = f"Service name can only include {SERVICE_NAME_CHARACTERS}"
        raise ValueError(msg)
    return normalized


def _scoped_credentials_dir_part(value: str) -> str:
    safe_prefix = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._-")[:80]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{safe_prefix or 'scope'}-{digest}"


def _decode_credentials_encryption_key(value: str) -> bytes:
    normalized = value.strip()
    padding = "=" * (-len(normalized) % 4)
    try:
        key = base64.b64decode(f"{normalized}{padding}".encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        msg = f"{_runtime_env_policy.CREDENTIALS_ENCRYPTION_KEY_ENV} must be a base64-encoded 32-byte key"
        raise ValueError(msg) from exc
    if len(key) != 32:
        msg = f"{_runtime_env_policy.CREDENTIALS_ENCRYPTION_KEY_ENV} must decode to exactly 32 bytes"
        raise ValueError(msg)
    return key


def _credentials_aad(service: str) -> bytes:
    return service.encode("utf-8")


def _encrypted_credentials_payload(
    credentials: dict[str, Any],
    *,
    service: str,
    key: bytes,
) -> bytes:
    plaintext = json.dumps(credentials, separators=(",", ":"), sort_keys=True).encode("utf-8")
    nonce = secrets.token_bytes(_AES_GCM_NONCE_SIZE)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _credentials_aad(service))
    return _ENCRYPTED_CREDENTIALS_MAGIC + base64.urlsafe_b64encode(nonce + ciphertext)


def _decrypt_credentials_payload(payload: bytes, *, service: str, key: bytes) -> dict[str, Any]:
    if not payload.startswith(_ENCRYPTED_CREDENTIALS_MAGIC):
        msg = "Plaintext credential JSON is not accepted when credential encryption is enabled"
        raise ValueError(msg)
    encoded_payload = payload[len(_ENCRYPTED_CREDENTIALS_MAGIC) :].strip()
    encrypted_payload = base64.b64decode(encoded_payload, altchars=b"-_", validate=True)
    nonce = encrypted_payload[:_AES_GCM_NONCE_SIZE]
    ciphertext = encrypted_payload[_AES_GCM_NONCE_SIZE:]
    plaintext = AESGCM(key).decrypt(nonce, ciphertext, _credentials_aad(service))
    data = json.loads(plaintext.decode("utf-8"))
    if not isinstance(data, dict):
        msg = "Encrypted credential payload must contain a JSON object"
        raise TypeError(msg)
    return data


def _ensure_private_directory(path: Path, *, harden_existing: bool = False) -> None:
    missing_paths: list[Path] = []
    current_path = path
    while not current_path.exists():
        missing_paths.append(current_path)
        current_path = current_path.parent

    directories_to_chmod = list(reversed(missing_paths))
    if harden_existing:
        for directory_path in _credential_owned_directory_chain(path):
            if directory_path not in directories_to_chmod:
                directories_to_chmod.append(directory_path)
    for directory_path in directories_to_chmod:
        try:
            create_directory_durable(directory_path, mode=0o700)
        except PermissionError as exc:
            raise _private_permissions_error(directory_path, 0o700) from exc
        except OSError as exc:
            if exc.errno not in _UNHARDENABLE_DIRECTORY_ERRNOS:
                raise


def _set_private_file_mode(fd: int, path: Path) -> None:
    """Make one opened payload owner-only, leaving a read-only layer to the primary that owns it."""
    if stat.S_IMODE(os.fstat(fd).st_mode) == 0o600:
        return
    try:
        os.fchmod(fd, 0o600)
    except PermissionError as exc:
        raise _private_permissions_error(path, 0o600) from exc
    except OSError as exc:
        if exc.errno != errno.EROFS:
            raise


def _private_permissions_error(path: Path, mode: int) -> PermissionError:
    """Build actionable ownership guidance for one credential permission failure."""
    return PermissionError(
        f"Cannot secure credential path '{path}' with mode {mode:#05o}. "
        "Ensure the MindRoom OS user owns this path and can change its permissions.",
    )


def _credential_owned_directory_chain(path: Path) -> list[Path]:
    """Return credential-owned directories that should be private."""
    chain = [path, *path.parents]
    for index, directory_path in enumerate(chain):
        if directory_path.name == _PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME:
            return list(reversed(chain[: index + 1]))
    return [path]


def _credentials_file_entries(directory_fd: int) -> list[tuple[str, os.stat_result]]:
    """List the owned regular credential payloads inside one opened credential directory."""
    entries = []
    for entry in os.scandir(directory_fd):
        if not entry.name.endswith(_CREDENTIALS_FILE_SUFFIX):
            continue
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError:
            # A concurrent save replaces entries by rename, so a vanished one is not ours.
            continue
        if stat.S_ISREG(entry_stat.st_mode):
            entries.append((entry.name, entry_stat))
    return entries


def _open_credentials_file(directory_fd: int, name: str) -> int | None:
    """Open one payload by name relative to its directory, or None when nothing is stored."""
    try:
        return os.open(name, _CREDENTIALS_FILE_FLAGS, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno in _ABSENT_CREDENTIALS_ERRNOS:
            return None
        raise


def _read_credentials_payload(path: Path) -> bytes | None:
    """Return one stored credential payload, or None when no owned regular file is stored."""
    with ExitStack() as stack:
        try:
            file_fd = stack.enter_context(open_regular_file_within_root(path.parent, path.name))
        except ValueError:
            # A FIFO, device, or directory planted under the payload name stores nothing.
            return None
        except OSError as exc:
            if exc.errno in _ABSENT_CREDENTIALS_ERRNOS:
                return None
            raise
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(file_fd, 64 * 1024):
            size += len(chunk)
            if size > _MAX_CREDENTIALS_PAYLOAD_BYTES:
                msg = f"Stored credential payload exceeds {_MAX_CREDENTIALS_PAYLOAD_BYTES} bytes"
                raise ValueError(msg)
            chunks.append(chunk)
        return b"".join(chunks)


def _harden_existing_credential_files(path: Path) -> None:
    """Make existing credential payloads owner-readable only."""
    try:
        with open_directory_within_root(path) as directory_fd:
            for name, entry_stat in _credentials_file_entries(directory_fd):
                if stat.S_IMODE(entry_stat.st_mode) != 0o600:
                    _harden_credentials_file(directory_fd, path / name)
    except OSError as exc:
        if exc.errno not in _ABSENT_CREDENTIALS_ERRNOS:
            raise


def _harden_credentials_file(directory_fd: int, path: Path) -> None:
    """Make one payload owner-only through its descriptor so a swapped-in link is never followed."""
    try:
        file_fd = _open_credentials_file(directory_fd, path.name)
    except PermissionError as exc:
        raise _private_permissions_error(path, 0o600) from exc
    if file_fd is None:
        return
    try:
        if stat.S_ISREG(os.fstat(file_fd).st_mode):
            _set_private_file_mode(file_fd, path)
    finally:
        os.close(file_fd)


def _reject_linked_primary_credential_directories(credential_paths: set[Path]) -> None:
    """Fail loudly when an operator links a primary credential directory.

    Every operation opens credential directories with ``O_NOFOLLOW``, so a linked
    directory would otherwise read as an empty store and hide every saved credential.
    """
    for path in sorted(credential_paths):
        if path.is_symlink():
            msg = (
                f"Credential directory '{path}' is a symbolic link; "
                "point the storage path at the real directory instead"
            )
            raise ValueError(msg)


def _validate_worker_credential_directories(worker_root: Path, credential_paths: tuple[Path, ...]) -> None:
    """Fail closed before the primary touches one worker's credential directories.

    The worker root is mounted read-write into the worker container under the primary's
    own uid, so worker code can swap the credential directories for links into the
    deployment-wide credential store or another requester's scoped store. The root is
    checked first so no link hides behind a parent that was not validated. The two
    credential directories are MindRoom's alone, so anything else planted there is dropped
    and recreated rather than locking every requester out of a shared worker for good.
    A replacement planted after this check is still never followed, because every later
    operation opens these directories with ``O_NOFOLLOW``.
    """
    root_mode = _lstat_mode(worker_root)
    if root_mode is not None and not stat.S_ISDIR(root_mode):
        msg = f"Worker root '{worker_root}' is not a directory"
        raise WorkerCredentialPathError(msg)
    for path in credential_paths:
        _drop_planted_entry(path)


def _lstat_mode(path: Path) -> int | None:
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None
    except OSError as exc:
        msg = f"Cannot inspect worker credential path '{path}'"
        raise WorkerCredentialPathError(msg) from exc


def _drop_planted_entry(path: Path) -> None:
    """Remove a non-directory planted in place of a credential directory MindRoom owns."""
    mode = _lstat_mode(path)
    if mode is None or stat.S_ISDIR(mode):
        return
    logger.warning("Removing entry planted in place of a worker credential directory", path=str(path))
    try:
        # Unlinking never follows a link; a directory that replaced the entry meanwhile is kept.
        path.unlink()
    except (FileNotFoundError, IsADirectoryError):
        return
    except OSError as exc:
        msg = f"Cannot remove the entry at worker credential path '{path}'"
        raise WorkerCredentialPathError(msg) from exc


@dataclass(frozen=True, slots=True)
class _WorkerCredentialPaths:
    """Credential directories of existing workers, and those that could not be inspected."""

    existing: tuple[Path, ...]
    uninspectable: tuple[Path, ...]


def _worker_credential_paths(storage_root: Path) -> _WorkerCredentialPaths:
    """Return real credential directories belonging to existing workers, and those that could not be inspected."""
    workers_root = storage_root / "workers"
    if workers_root.is_symlink() or not workers_root.is_dir():
        return _WorkerCredentialPaths(existing=(), uninspectable=())

    existing: list[Path] = []
    uninspectable: list[Path] = []
    for worker_root in workers_root.iterdir():
        if worker_root.is_symlink() or not worker_root.is_dir():
            continue
        for directory_name in (WORKER_CREDENTIALS_DIRNAME, WORKER_SHARED_CREDENTIALS_DIRNAME):
            credential_path = worker_root / directory_name
            try:
                # Path.is_dir() and is_symlink() hide permission errors on newer Pythons.
                mode = credential_path.lstat().st_mode
            except FileNotFoundError:
                continue
            except OSError:
                # Worker code may make its own root unsearchable, which hides only that worker's store.
                logger.warning("Skipping an uninspectable worker credential path", path=str(credential_path))
                uninspectable.append(credential_path)
                continue
            if stat.S_ISDIR(mode):
                existing.append(credential_path)
    return _WorkerCredentialPaths(existing=tuple(existing), uninspectable=tuple(uninspectable))


def _harden_worker_credential_store(credential_path: Path) -> None:
    """Secure one dormant worker store, warning instead of failing when worker code locked it."""
    try:
        _ensure_private_directory(credential_path, harden_existing=True)
        _harden_existing_credential_files(credential_path)
    except OSError as exc:
        # Worker code owns its store and may change its modes, which must never stop the primary.
        logger.warning("Cannot secure a worker credential store", path=str(credential_path), error=str(exc))


def _harden_worker_credential_stores(credential_paths: tuple[Path, ...]) -> None:
    """Secure every dormant worker store before returning, overlapping their filesystem round trips."""
    if len(credential_paths) < 2:
        for credential_path in credential_paths:
            _harden_worker_credential_store(credential_path)
        return
    with ThreadPoolExecutor(
        max_workers=min(_WORKER_STORE_HARDENING_CONCURRENCY, len(credential_paths)),
        thread_name_prefix="mindroom-credential-hardening",
    ) as executor:
        # Consuming the results re-raises anything but the per-store OSError that is only logged.
        for _ in executor.map(_harden_worker_credential_store, credential_paths):
            pass


def _atomic_write_private_file(path: Path, payload: bytes) -> None:
    if len(payload) > _MAX_CREDENTIALS_PAYLOAD_BYTES:
        # Refuse what the store could never read back, rather than publishing a dead entry.
        msg = f"Credential payload exceeds {_MAX_CREDENTIALS_PAYLOAD_BYTES} bytes"
        raise ValueError(msg)
    _ensure_private_directory(path.parent)
    with open_directory_within_root(path.parent) as directory_fd:
        atomic_write_bytes_at(directory_fd, path.name, payload, file_mode=0o600, temp_prefix=f".{path.name}.")


class CredentialsManager:
    """Centralized credentials storage and retrieval for MindRoom."""

    def __init__(
        self,
        base_path: Path,
        *,
        shared_base_path: Path | None = None,
        current_worker_key: str | None = None,
        current_worker_root: Path | None = None,
        encryption_key: str | None = None,
        declared: Mapping[str, DeclaredCredential] | None = None,
    ) -> None:
        """Initialize the credentials manager.

        Args:
            base_path: Base directory for storing credentials.
            shared_base_path: Optional shared credential layer used for inherited or mirrored
                credentials within the current execution context.
            current_worker_key: Optional worker key for the current runtime context.
            current_worker_root: Optional worker root for the current runtime context.
            encryption_key: Optional base64-encoded 32-byte credential encryption key.
            declared: Credentials the environment declares, read for a service with no saved document.

        """
        self.base_path = Path(base_path)
        self.shared_base_path = self.base_path if shared_base_path is None else Path(shared_base_path)
        self.current_worker_key = current_worker_key
        self.current_worker_root = (
            Path(current_worker_root).expanduser().resolve() if current_worker_root is not None else None
        )
        self._encryption_key_config = _runtime_env_policy.credentials_encryption_key_value(encryption_key)
        self._declared: Mapping[str, DeclaredCredential] = dict(declared or {})
        self._encryption_key = (
            _decode_credentials_encryption_key(self._encryption_key_config)
            if self._encryption_key_config is not None
            else None
        )

        credential_paths = {self.base_path, self.shared_base_path}
        worker_credential_paths: tuple[Path, ...] = ()
        if self.current_worker_key is None and self.base_path.name == "credentials":
            _reject_linked_primary_credential_directories(credential_paths)
            worker_credential_paths = _worker_credential_paths(self.storage_root).existing
        for credential_path in credential_paths:
            _ensure_private_directory(credential_path, harden_existing=True)
            _harden_existing_credential_files(credential_path)
        _harden_worker_credential_stores(worker_credential_paths)

    @property
    def storage_root(self) -> Path:
        """Return the storage root that owns this credentials directory."""
        return self.base_path.parent

    def for_worker(self, worker_key: str) -> CredentialsManager:
        """Return a credentials manager rooted in one worker's persistent state."""
        worker_root = worker_root_path(self.storage_root, worker_key)
        worker_credentials_path = worker_root / WORKER_CREDENTIALS_DIRNAME
        worker_shared_credentials_path = worker_root / WORKER_SHARED_CREDENTIALS_DIRNAME
        _validate_worker_credential_directories(
            worker_root,
            (worker_credentials_path, worker_shared_credentials_path),
        )
        return CredentialsManager(
            base_path=worker_credentials_path,
            shared_base_path=worker_shared_credentials_path,
            current_worker_key=worker_key,
            current_worker_root=worker_root,
            encryption_key=self._encryption_key_config,
        )

    def for_primary_runtime_scope(self, requester_id: str, agent_name: str | None) -> CredentialsManager:
        """Return a primary-runtime-only scoped credentials manager."""
        requester_dir = _scoped_credentials_dir_part(requester_id)
        # Config reserves "_shared" as an entity name, so no agent's store collides with the requester-wide one.
        agent_dir = _scoped_credentials_dir_part(agent_name or "_shared")
        scoped_path = self.storage_root / _PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME / requester_dir / agent_dir
        return CredentialsManager(
            base_path=scoped_path,
            encryption_key=self._encryption_key_config,
        )

    def for_primary_runtime_agent_scope(self, agent_name: str) -> CredentialsManager:
        """Return a primary-runtime-only agent-scoped credentials manager."""
        agent_dir = _scoped_credentials_dir_part(agent_name)
        scoped_path = (
            self.storage_root
            / _PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME
            / _PRIMARY_RUNTIME_AGENT_SCOPED_DIRNAME
            / agent_dir
        )
        return CredentialsManager(
            base_path=scoped_path,
            encryption_key=self._encryption_key_config,
        )

    def shared_manager(self) -> CredentialsManager:
        """Return the shared credential layer for this execution context: this store itself, or a worker's mirror."""
        if self.shared_base_path == self.base_path:
            return self
        return CredentialsManager(
            base_path=self.shared_base_path,
            shared_base_path=self.shared_base_path,
            current_worker_key=self.current_worker_key,
            current_worker_root=self.current_worker_root,
            encryption_key=self._encryption_key_config,
        )

    def get_credentials_path(self, service: str) -> Path:
        """Get the path for a service's credentials file.

        Args:
            service: Name of the service (e.g., 'google', 'homeassistant')

        Returns:
            Path to the credentials file

        """
        normalized_service = validate_service_name(service)
        return self._credentials_file(normalized_service)

    def _credentials_file(self, normalized_service: str) -> Path:
        return self.base_path / f"{normalized_service}_credentials.json"

    def encode_credentials(self, service: str, credentials: dict[str, Any]) -> bytes:
        """Serialize credentials with the same policy used by the file store."""
        normalized_service = validate_service_name(service)
        if self._encryption_key is not None:
            return _encrypted_credentials_payload(
                credentials,
                service=normalized_service,
                key=self._encryption_key,
            )
        return json.dumps(credentials, indent=2).encode("utf-8")

    def decode_credentials(self, service: str, payload: bytes) -> dict[str, Any]:
        """Deserialize credentials with the same policy used by the file store."""
        normalized_service = validate_service_name(service)
        if self._encryption_key is not None:
            return _decrypt_credentials_payload(
                payload,
                service=normalized_service,
                key=self._encryption_key,
            )
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict):
            msg = "Credential payload must contain a JSON object"
            raise TypeError(msg)
        return data

    def load_credentials(self, service: str) -> dict[str, Any] | None:
        """Load credentials for a service.

        Args:
            service: Name of the service

        Returns:
            Credentials dictionary or None if not found

        """
        normalized_service = validate_service_name(service)
        credentials_path = self._credentials_file(normalized_service)
        return self._load_credentials_file(normalized_service, credentials_path)

    def _read_saved(self, normalized_service: str, credentials_path: Path) -> dict[str, Any] | None:
        """Return the document saved at ``credentials_path``, raising ``_UNREADABLE_CREDENTIALS_ERRORS`` if unreadable."""
        payload = _read_credentials_payload(credentials_path)
        return None if payload is None else self.decode_credentials(normalized_service, payload)

    def _load_credentials_file(self, normalized_service: str, credentials_path: Path) -> dict[str, Any] | None:
        try:
            saved = self._read_saved(normalized_service, credentials_path)
        except _UNREADABLE_CREDENTIALS_ERRORS as exc:
            logger.warning(
                "Failed to load encrypted credentials"
                if self._encryption_key is not None
                else "Failed to load credentials",
                service=normalized_service,
                path=str(credentials_path),
                error_type=type(exc).__name__,
            )
            return None
        if saved is not None:
            return saved
        declared = self._declared.get(normalized_service)
        return dict(declared.credentials) if declared is not None else None

    def save_credentials(self, service: str, credentials: dict[str, Any]) -> None:
        """Save credentials for a service.

        Args:
            service: Name of the service
            credentials: Credentials dictionary to save

        """
        normalized_service = validate_service_name(service)
        credentials_path = self._credentials_file(normalized_service)
        self._save_credentials_file(normalized_service, credentials_path, credentials)

    def _save_credentials_file(
        self,
        normalized_service: str,
        credentials_path: Path,
        credentials: dict[str, Any],
    ) -> None:
        stored_payload = _read_credentials_payload(credentials_path)
        if stored_payload is not None:
            if (
                self._encryption_key is not None
                and self._load_credentials_file(normalized_service, credentials_path) is None
            ):
                msg = f"Stored credentials for {normalized_service} could not be loaded; refusing to overwrite"
                raise ValueError(msg)
            if self._encryption_key is None and stored_payload.startswith(_ENCRYPTED_CREDENTIALS_MAGIC):
                msg = f"Stored credentials for {normalized_service} are encrypted; refusing to overwrite without a key"
                raise ValueError(msg)
        _atomic_write_private_file(credentials_path, self.encode_credentials(normalized_service, credentials))

    def delete_credentials(self, service: str) -> None:
        """Delete credentials for a service.

        Args:
            service: Name of the service

        """
        credentials_path = self.get_credentials_path(service)
        try:
            with open_directory_within_root(credentials_path.parent) as directory_fd:
                os.unlink(credentials_path.name, dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno not in _ABSENT_CREDENTIALS_ERRNOS:
                raise

    def list_services(self) -> list[str]:
        """List every service with credentials: saved ones and the ones the environment declares."""
        return sorted({*self._saved_services(), *self._declared})

    def _saved_services(self) -> list[str]:
        try:
            with open_directory_within_root(self.base_path) as directory_fd:
                names = [name for name, _entry_stat in _credentials_file_entries(directory_fd)]
        except OSError as exc:
            if exc.errno not in _ABSENT_CREDENTIALS_ERRNOS:
                raise
            return []
        services = [name.removesuffix(_CREDENTIALS_FILE_SUFFIX) for name in names]
        return [service for service in services if SERVICE_NAME_PATTERN.fullmatch(service)]

    def declared_variable(self, service: str) -> str | None:
        """Return the variable that supplies a service's credentials, or None when a saved document supplies them."""
        normalized_service = validate_service_name(service)
        declared = self._declared.get(normalized_service)
        if declared is None or _read_credentials_payload(self._credentials_file(normalized_service)) is not None:
            return None
        return declared.variable

    def remove_saved_documents(self, matches: Callable[[dict[str, Any]], bool]) -> _SavedDocumentsRemoval:
        """Delete every saved document ``matches`` selects, counting the documents that cannot be read."""
        removed: list[str] = []
        unreadable = 0
        for service in self._saved_services():
            credentials_path = self._credentials_file(service)
            try:
                credentials = self._read_saved(service, credentials_path)
            except _UNREADABLE_CREDENTIALS_ERRORS:
                unreadable += 1
                continue
            if credentials is not None and matches(credentials):
                self.delete_credentials(service)
                removed.append(service)
        return _SavedDocumentsRemoval(removed=tuple(removed), unreadable=unreadable)

    def get_api_key(self, service: str, key_name: str = "api_key") -> str | None:
        """Get an API key for a service.

        Args:
            service: Name of the service (e.g., 'openai', 'anthropic')
            key_name: Name of the key field (default: 'api_key')

        Returns:
            API key string, or None if not found or not stored as a string

        """
        credentials = self.load_credentials(service)
        if not credentials:
            return None
        value = credentials.get(key_name)
        if value is None:
            return None
        if not isinstance(value, str):
            # The generic credentials API accepts arbitrary JSON, so a
            # malformed save must resolve as "no key", not crash resolution.
            logger.warning(
                "Ignoring non-string credential value",
                service=service,
                key_name=key_name,
                value_type=type(value).__name__,
            )
            return None
        return value


def _credentials_base_path(storage_root: Path) -> Path:
    """Return the credentials directory under one explicit storage root."""
    return Path(storage_root).expanduser().resolve() / "credentials"


def _runtime_shared_credentials_base_path(runtime_paths: RuntimePaths, base_path: Path) -> Path:
    shared_storage_path = runtime_paths.env_value(_runtime_env_policy.SHARED_CREDENTIALS_PATH_ENV)
    if shared_storage_path is None:
        return base_path
    return Path(shared_storage_path).expanduser().resolve()


def _runtime_dedicated_worker_key(runtime_paths: RuntimePaths) -> str | None:
    raw_worker_key = runtime_paths.env_value(_runtime_env_policy.SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"])
    return None if raw_worker_key is None else raw_worker_key.strip()


def _runtime_dedicated_worker_root(runtime_paths: RuntimePaths) -> Path | None:
    raw_worker_root = runtime_paths.env_value(_runtime_env_policy.SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"])
    return None if raw_worker_root is None else Path(raw_worker_root).expanduser().resolve()


def runtime_credentials_manager_key(runtime_paths: RuntimePaths) -> _CredentialsManagerKey:
    """Return the cache key for one explicit runtime credential context."""
    base_path = _credentials_base_path(runtime_paths.storage_root)
    # Only the primary reads the credentials its environment declares; every runner establishes runner mode.
    runner = runtime_paths.env_flag(_runtime_env_policy.SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"])
    return _CredentialsManagerKey(
        base_path=base_path,
        shared_base_path=_runtime_shared_credentials_base_path(runtime_paths, base_path),
        current_worker_key=_runtime_dedicated_worker_key(runtime_paths),
        current_worker_root=_runtime_dedicated_worker_root(runtime_paths),
        encryption_key=_runtime_env_policy.credentials_encryption_key_value(
            runtime_paths.env_value(_runtime_env_policy.CREDENTIALS_ENCRYPTION_KEY_ENV),
        ),
        declaring_env_digest=None if runner else runtime_paths.env_digest,
    )


def get_runtime_credentials_manager(runtime_paths: RuntimePaths) -> CredentialsManager:
    """Return the cached credentials manager for one explicit runtime context.

    The primary's manager reads the credentials its environment declares under the saved ones; opening it the first
    time also removes the copies earlier releases imported from the environment.
    """
    key = runtime_credentials_manager_key(runtime_paths)
    with _credentials_manager_lock:
        manager = _credentials_managers.get(key)
        if manager is None:
            declared: Mapping[str, DeclaredCredential] = {}
            if key.declaring_env_digest is not None:
                # The environment module validates service names with this module.
                from mindroom.credentials.environment import declared_credentials  # noqa: PLC0415

                declared = declared_credentials(runtime_paths)
            manager = CredentialsManager(
                base_path=key.base_path,
                shared_base_path=key.shared_base_path,
                current_worker_key=key.current_worker_key,
                current_worker_root=key.current_worker_root,
                encryption_key=key.encryption_key,
                declared=declared,
            )
            if key.declaring_env_digest is not None:
                remove_environment_copies(manager, declared_services=declared)
            _credentials_managers[key] = manager
        return manager


@dataclass(frozen=True, slots=True)
class _SavedDocumentsRemoval:
    """Which saved documents one removal deleted, and how many it could not read."""

    removed: tuple[str, ...]
    unreadable: int


@dataclass(frozen=True, slots=True)
class StoredCredentialsUpdate:
    """How one update across every credential store went."""

    rewritten: int
    unreadable: int


def update_stored_service_credentials(
    runtime_paths: RuntimePaths,
    service: str,
    update: Callable[[dict[str, Any]], dict[str, Any] | None],
) -> StoredCredentialsUpdate:
    """Rewrite one service's document in the primary store and every existing worker store.

    ``update`` returns the replacement document, or None to leave that store unchanged.
    Worker directories are read and written through the same no-follow reads, encryption
    policy, and atomic replacement as every other operation. A document that exists but
    cannot be read, for example under a different encryption key, is counted, not skipped silently.
    """
    manager = get_runtime_credentials_manager(runtime_paths)
    normalized_service = validate_service_name(service)
    rewritten = 0
    unreadable = 0
    for directory in (manager.base_path, *_worker_credential_paths(manager.storage_root).existing):
        credentials_path = directory / f"{normalized_service}{_CREDENTIALS_FILE_SUFFIX}"
        try:
            credentials = manager._read_saved(normalized_service, credentials_path)
        except _UNREADABLE_CREDENTIALS_ERRORS as exc:
            logger.warning(
                "Stored credentials could not be read for an update",
                service=normalized_service,
                path=str(credentials_path),
                error_type=type(exc).__name__,
            )
            unreadable += 1
            continue
        if credentials is None:
            continue
        updated = update(credentials)
        if updated is not None:
            manager._save_credentials_file(normalized_service, credentials_path, updated)
            rewritten += 1
    return StoredCredentialsUpdate(rewritten=rewritten, unreadable=unreadable)


@dataclass(frozen=True, slots=True)
class _WorkerCredentialsRemoval:
    """How removing services from every worker's own credential store went."""

    removed: int
    failed: int


def remove_worker_service_credentials(
    runtime_paths: RuntimePaths,
    services: frozenset[str],
) -> _WorkerCredentialsRemoval:
    """Delete these services' documents from every existing worker's own store without reading them.

    Shared-credential mirrors are left alone, because every worker sync rewrites them from the granted services.
    A store that cannot be inspected, opened, or cleaned, for example one whose worker root or directory worker code
    made unsearchable, is counted, not skipped silently.
    """
    manager = get_runtime_credentials_manager(runtime_paths)
    file_names = {f"{validate_service_name(service)}{_CREDENTIALS_FILE_SUFFIX}" for service in services}
    worker_paths = _worker_credential_paths(manager.storage_root)
    removed = 0
    failed = sum(1 for directory in worker_paths.uninspectable if directory.name == WORKER_CREDENTIALS_DIRNAME)
    for directory in worker_paths.existing:
        if directory.name != WORKER_CREDENTIALS_DIRNAME:
            continue
        try:
            with open_directory_within_root(directory) as directory_fd:
                for name in file_names:
                    try:
                        entry_mode = os.stat(name, dir_fd=directory_fd, follow_symlinks=False).st_mode
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(entry_mode):
                        os.unlink(name, dir_fd=directory_fd)
                        removed += 1
        except OSError as exc:
            logger.warning(
                "A worker credential store could not be cleaned",
                path=str(directory),
                error_type=type(exc).__name__,
            )
            failed += 1
    return _WorkerCredentialsRemoval(removed=removed, failed=failed)


def get_runtime_shared_credentials_manager(runtime_paths: RuntimePaths) -> CredentialsManager:
    """Return the shared credential layer for one explicit runtime context."""
    return get_runtime_credentials_manager(runtime_paths).shared_manager()


def _reset_credentials_manager_cache() -> None:
    """Reset cached credentials managers. Intended for tests."""
    with _credentials_manager_lock:
        _credentials_managers.clear()


def _resolve_worker_credentials_manager(
    credentials_manager: CredentialsManager,
    worker_key: str,
) -> CredentialsManager:
    """Return the store of one worker, reusing the manager when this runtime is that worker."""
    current_storage_root = credentials_manager.storage_root.expanduser().resolve()
    if (
        credentials_manager.current_worker_key == worker_key
        and credentials_manager.current_worker_root is not None
        and current_storage_root == credentials_manager.current_worker_root
    ):
        return credentials_manager
    if current_storage_root == worker_root_path(credentials_manager.storage_root, worker_key):
        return credentials_manager
    return credentials_manager.for_worker(worker_key)


def _merge_credential_layers(
    shared_credentials: Mapping[str, Any] | None,
    worker_credentials: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    merged_credentials: dict[str, Any] = {}
    if shared_credentials is not None:
        merged_credentials.update(shared_credentials)
    if worker_credentials is not None:
        merged_credentials.update(worker_credentials)
    return merged_credentials or None


def _granted(service: str, allowed_services: frozenset[str] | None) -> bool:
    """Return whether the worker grant passes a service's shared document on to workers."""
    return (
        allowed_services is not None
        and service in allowed_services
        and credential_service_policy(service, None).grantable
    )


def _grantable_document(credentials: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return a shared document a grant may pass on: one saved in the dashboard or declared by the environment."""
    if credentials is None or credentials.get("_source") not in _WORKER_GRANTABLE_SHARED_CREDENTIAL_SOURCES:
        return None
    return dict(credentials)


def sync_shared_credentials_to_worker(
    worker_key: str,
    *,
    allowed_services: frozenset[str],
    credentials_manager: CredentialsManager,
) -> None:
    """Sync shared credentials into one worker's dedicated shared-credential mirror.

    The worker's override store remains separate. Only ``allowed_services`` may be
    mirrored into the worker's shared credential layer, regardless of whether the
    shared credential was declared by the environment or saved through the dashboard/API.
    """
    worker_shared_manager = credentials_manager.for_worker(worker_key).shared_manager()
    mirrored_services = set(worker_shared_manager.list_services())
    copied_services: set[str] = set()
    logger.debug(
        "Starting worker shared credential sync",
        worker=worker_log_id(worker_key),
        allowed_services=sorted(allowed_services),
    )

    # An empty allowlist can grant nothing, so the copy loop is a guaranteed no-op that would only
    # re-scan the shared-credential store from disk and log one skip per service. This is the
    # default configuration and it runs on every worker-routed tool call, so short-circuit it and
    # fall straight through to the cleanup pass, which still tears down any stale mirror.
    if allowed_services:
        source_manager = credentials_manager.shared_manager()
        for service in source_manager.list_services():
            shared_credentials = (
                _grantable_document(source_manager.load_credentials(service))
                if _granted(service, allowed_services)
                else None
            )
            if shared_credentials is None:
                if service not in allowed_services:
                    logger.debug(
                        "Skipping non-grantable shared credentials during worker sync",
                        worker=worker_log_id(worker_key),
                        service=service,
                    )
                continue

            copied_services.add(service)
            worker_shared_manager.save_credentials(service, shared_credentials)

    for service in mirrored_services - copied_services:
        worker_shared_manager.delete_credentials(service)


@dataclass(frozen=True, slots=True)
class CredentialLayers:
    """Where one service's credentials live for one target: one store over an optional shared layer."""

    # The store saves and deletes go to; None when the target has no store for the service.
    store: CredentialsManager | None
    # Read under the store, its fields overridden by the store's.
    shared: CredentialsManager | None
    # The shared layer passes on only documents a grant may pass on.
    granted_only: bool
    # Why saves and deletes are refused when there is no store.
    refusal: str

    def load(self, service: str) -> dict[str, Any] | None:
        """Return the service's document: the store's fields over the shared layer's."""
        shared = self.shared.load_credentials(service) if self.shared is not None else None
        if self.granted_only:
            shared = _grantable_document(shared)
        own = self.store.load_credentials(service) if self.store is not None else None
        return _merge_credential_layers(shared, own)

    def writable_store(self) -> CredentialsManager:
        """Return the store saves and deletes go to, refusing a target that has none."""
        if self.store is None:
            raise ValueError(self.refusal)
        return self.store


def credential_layers(
    service: str,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    allowed_shared_services: frozenset[str] | None = None,
    primary_built_tool: bool = False,
) -> CredentialLayers:
    """Return the stores one service's credentials are read from and written to for one target.

    ``worker_target=None`` is a global call. A target may be unscoped yet carry a requester and an agent.
    ``allowed_shared_services`` is the configured worker grant; ``primary_built_tool`` marks the settings of a tool
    the primary builds for this agent, which never come from a store worker code can write.
    """
    worker_scope = worker_target.worker_scope if worker_target is not None else None
    policy = credential_service_policy(service, worker_scope, primary_built_tool=primary_built_tool)
    # A dedicated worker's runtime reads a mirror that holds only its grants, under its own store.
    worker_runtime = credentials_manager.shared_base_path != credentials_manager.base_path
    reads_shared = (policy.reads_shared and _granted(service, allowed_shared_services)) or (
        worker_runtime and (policy.reads_shared or policy.placement is CredentialPlacement.GLOBAL)
    )
    store, refusal = _placement_store(service, policy, credentials_manager, worker_target)
    return CredentialLayers(
        store=store,
        shared=credentials_manager.shared_manager() if reads_shared else None,
        granted_only=not worker_runtime,
        refusal=refusal,
    )


def _placement_store(
    service: str,
    policy: CredentialServicePolicy,
    manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> tuple[CredentialsManager | None, str]:
    """Return the store a placement names for one target, or None and why the target has none."""
    placement = policy.placement
    if placement is CredentialPlacement.GLOBAL:
        return manager, ""
    if placement is CredentialPlacement.SHARED:
        return manager.shared_manager(), ""
    if worker_target is None or placement in {CredentialPlacement.REQUESTER, CredentialPlacement.REQUESTER_AGENT}:
        return _requester_store(service, policy, manager, worker_target)
    if placement is CredentialPlacement.AGENT:
        if not worker_target.routing_agent_name:
            msg = f"Agent-scoped credentials for {service} require an agent name"
            raise ValueError(msg)
        return manager.for_primary_runtime_agent_scope(worker_target.routing_agent_name), ""
    if worker_target.worker_key is None:
        return None, f"Credentials for {service} in a {worker_target.worker_scope} scope require a requester identity"
    return _resolve_worker_credentials_manager(manager, worker_target.worker_key), ""


def _requester_store(
    service: str,
    policy: CredentialServicePolicy,
    manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> tuple[CredentialsManager | None, str]:
    """Return the primary store of the target's requester, alone or with its agent, as the placement names."""
    agent_name = worker_target.routing_agent_name if worker_target is not None else None
    identity = worker_target.execution_identity if worker_target is not None else None
    requester_id = identity.requester_id if identity is not None else None
    if policy.placement is CredentialPlacement.REQUESTER_AGENT:
        if requester_id is None or not agent_name:
            return None, f"Credentials for {service} require a requester and an agent"
        return manager.for_primary_runtime_scope(requester_id, agent_name), ""
    if requester_id is None:
        if policy.reads_shared:
            # Without a requester there is no requester store, so only shared settings apply.
            return None, f"Settings for {service} require a requester identity"
        msg = f"Primary-runtime scoped credentials for {service} require a requester identity"
        raise ValueError(msg)
    assert worker_target is not None
    scoped_agent = agent_name if worker_target.worker_scope == "user_agent" else None
    return manager.for_primary_runtime_scope(requester_id, scoped_agent), ""


def scoped_credentials_path(
    service: str,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> Path:
    """Return the file path that scoped credential writes target for one service."""
    normalized_service = validate_service_name(service)
    layers = credential_layers(normalized_service, credentials_manager=credentials_manager, worker_target=worker_target)
    return layers.writable_store().get_credentials_path(normalized_service)


def load_scoped_credentials(
    service: str,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    allowed_shared_services: frozenset[str] | None = None,
    primary_built_tool: bool = False,
) -> dict[str, Any] | None:
    """Load the service's scoped store over its permitted shared layer."""
    return credential_layers(
        service,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
        allowed_shared_services=allowed_shared_services,
        primary_built_tool=primary_built_tool,
    ).load(service)


def save_scoped_credentials(
    service: str,
    credentials: dict[str, Any],
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    primary_built_tool: bool = False,
) -> None:
    """Save to the store the service's credentials live in for this target."""
    normalized_service = validate_service_name(service)
    credential_layers(
        normalized_service,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
        primary_built_tool=primary_built_tool,
    ).writable_store().save_credentials(normalized_service, credentials)


def delete_scoped_credentials(
    service: str,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    primary_built_tool: bool = False,
) -> None:
    """Delete from the store the service's credentials live in for this target."""
    normalized_service = validate_service_name(service)
    credential_layers(
        normalized_service,
        credentials_manager=credentials_manager,
        worker_target=worker_target,
        primary_built_tool=primary_built_tool,
    ).writable_store().delete_credentials(normalized_service)
