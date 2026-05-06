"""Unified credentials management for MindRoom.

This module provides centralized credential storage and retrieval for all integrations,
used by both agents and the dashboard interface.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mindroom.constants import CREDENTIALS_ENCRYPTION_KEY_ENV
from mindroom.credential_policy import credential_service_policy
from mindroom.logging_config import get_logger
from mindroom.tool_system.worker_routing import worker_root_path

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_SERVICE_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9:_-]+$")
_WORKER_SHARED_CREDENTIALS_DIRNAME = ".shared_credentials"
_PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME = "private_oauth"
SHARED_CREDENTIALS_PATH_ENV = "MINDROOM_SHARED_CREDENTIALS_PATH"
_DEDICATED_WORKER_KEY_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY"
_DEDICATED_WORKER_ROOT_ENV = "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT"
_WORKER_GRANTABLE_SHARED_CREDENTIAL_SOURCES = frozenset({"env", "ui", None})
_ENCRYPTED_CREDENTIALS_MAGIC = b"MINDROOM-CREDENTIALS-V1\n"
_AES_GCM_NONCE_SIZE = 12
logger = get_logger(__name__)


def validate_service_name(service: str) -> str:
    """Validate and normalize credential service names."""
    normalized = service.strip()
    if not normalized:
        msg = "Service name is required"
        raise ValueError(msg)
    if not _SERVICE_NAME_PATTERN.fullmatch(normalized):
        msg = "Service name can only include letters, numbers, colon, underscore, and hyphen"
        raise ValueError(msg)
    return normalized


def _scoped_credentials_dir_part(value: str) -> str:
    safe_prefix = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("._-")[:80]
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"{safe_prefix or 'scope'}-{digest}"


def _configured_credentials_encryption_key() -> str | None:
    value = os.environ.get(CREDENTIALS_ENCRYPTION_KEY_ENV, "").strip()
    return value or None


def _decode_credentials_encryption_key(value: str) -> bytes:
    normalized = value.strip()
    padding = "=" * (-len(normalized) % 4)
    try:
        key = base64.b64decode(f"{normalized}{padding}".encode("ascii"), altchars=b"-_", validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        msg = f"{CREDENTIALS_ENCRYPTION_KEY_ENV} must be a base64-encoded 32-byte key"
        raise ValueError(msg) from exc
    if len(key) != 32:
        msg = f"{CREDENTIALS_ENCRYPTION_KEY_ENV} must decode to exactly 32 bytes"
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


def _ensure_private_directory(path: Path) -> None:
    missing_paths: list[Path] = []
    current_path = path
    while not current_path.exists():
        missing_paths.append(current_path)
        current_path = current_path.parent

    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    directories_to_chmod = reversed(missing_paths) if missing_paths else [path]
    for directory_path in directories_to_chmod:
        directory_path.chmod(0o700)


def _atomic_write_private_file(path: Path, payload: bytes) -> None:
    _ensure_private_directory(path.parent)
    tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        tmp_path.replace(path)
        path.chmod(0o600)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


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
    ) -> None:
        """Initialize the credentials manager.

        Args:
            base_path: Base directory for storing credentials.
            shared_base_path: Optional shared credential layer used for inherited or mirrored
                credentials within the current execution context.
            current_worker_key: Optional worker key for the current runtime context.
            current_worker_root: Optional worker root for the current runtime context.
            encryption_key: Optional base64-encoded 32-byte credential encryption key.

        """
        self.base_path = Path(base_path)
        if shared_base_path is None:
            self.shared_base_path = _default_shared_credentials_base_path(self.base_path)
        else:
            self.shared_base_path = Path(shared_base_path)
        self.current_worker_key = current_worker_key
        self.current_worker_root = (
            Path(current_worker_root).expanduser().resolve() if current_worker_root is not None else None
        )
        self._encryption_key_config = (
            encryption_key.strip() if encryption_key is not None else _configured_credentials_encryption_key()
        )
        self._encryption_key = (
            _decode_credentials_encryption_key(self._encryption_key_config)
            if self._encryption_key_config is not None
            else None
        )

        # Ensure the directory exists
        if self.encrypted_storage_enabled:
            _ensure_private_directory(self.base_path)
        else:
            self.base_path.mkdir(parents=True, exist_ok=True)
        if self.shared_base_path != self.base_path:
            if self.encrypted_storage_enabled:
                _ensure_private_directory(self.shared_base_path)
            else:
                self.shared_base_path.mkdir(parents=True, exist_ok=True)

    @property
    def encrypted_storage_enabled(self) -> bool:
        """Return whether credential files are encrypted at rest."""
        return self._encryption_key is not None

    @property
    def storage_root(self) -> Path:
        """Return the storage root that owns this credentials directory."""
        return self.base_path.parent

    def for_worker(self, worker_key: str) -> CredentialsManager:
        """Return a credentials manager rooted in one worker's persistent state."""
        worker_root = worker_root_path(self.storage_root, worker_key)
        worker_credentials_path = worker_root / "credentials"
        worker_shared_credentials_path = worker_root / _WORKER_SHARED_CREDENTIALS_DIRNAME
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
        agent_dir = _scoped_credentials_dir_part(agent_name or "_shared")
        scoped_path = self.storage_root / _PRIMARY_RUNTIME_SCOPED_CREDENTIALS_DIRNAME / requester_dir / agent_dir
        return CredentialsManager(
            base_path=scoped_path,
            shared_base_path=scoped_path,
            encryption_key=self._encryption_key_config,
        )

    def shared_manager(self) -> CredentialsManager:
        """Return a manager rooted in the shared credential layer for this execution context."""
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
        return self.base_path / f"{normalized_service}_credentials.json"

    def load_credentials(self, service: str) -> dict[str, Any] | None:
        """Load credentials for a service.

        Args:
            service: Name of the service

        Returns:
            Credentials dictionary or None if not found

        """
        normalized_service = validate_service_name(service)
        credentials_path = self.get_credentials_path(service)
        if credentials_path.exists():
            try:
                if self._encryption_key is not None:
                    return _decrypt_credentials_payload(
                        credentials_path.read_bytes(),
                        service=normalized_service,
                        key=self._encryption_key,
                    )
                with credentials_path.open(encoding="utf-8") as f:
                    data: dict[str, Any] = json.load(f)
                    return data
            except (OSError, TypeError, ValueError, json.JSONDecodeError, InvalidTag, binascii.Error):
                logger.exception(
                    "Failed to load credentials",
                    service=service,
                    path=str(credentials_path),
                )
                return None
        return None

    def save_credentials(self, service: str, credentials: dict[str, Any]) -> None:
        """Save credentials for a service.

        Args:
            service: Name of the service
            credentials: Credentials dictionary to save

        """
        normalized_service = validate_service_name(service)
        credentials_path = self.get_credentials_path(service)
        if self._encryption_key is not None:
            payload = _encrypted_credentials_payload(
                credentials,
                service=normalized_service,
                key=self._encryption_key,
            )
            _atomic_write_private_file(credentials_path, payload)
            return
        with credentials_path.open("w", encoding="utf-8") as f:
            json.dump(credentials, f, indent=2)

    def delete_credentials(self, service: str) -> None:
        """Delete credentials for a service.

        Args:
            service: Name of the service

        """
        credentials_path = self.get_credentials_path(service)
        if credentials_path.exists():
            credentials_path.unlink()

    def list_services(self) -> list[str]:
        """List all services with stored credentials.

        Returns:
            List of service names

        """
        services = []
        if self.base_path.exists():
            for path in self.base_path.glob("*_credentials.json"):
                service = path.stem.replace("_credentials", "")
                if _SERVICE_NAME_PATTERN.fullmatch(service):
                    services.append(service)
        return sorted(services)

    def get_api_key(self, service: str, key_name: str = "api_key") -> str | None:
        """Get an API key for a service.

        Args:
            service: Name of the service (e.g., 'openai', 'anthropic')
            key_name: Name of the key field (default: 'api_key')

        Returns:
            API key string or None if not found

        """
        credentials = self.load_credentials(service)
        if credentials:
            return credentials.get(key_name)
        return None

    def set_api_key(self, service: str, api_key: str, key_name: str = "api_key") -> None:
        """Set an API key for a service.

        Args:
            service: Name of the service
            api_key: The API key to store
            key_name: Name of the key field (default: 'api_key')

        """
        credentials = self.load_credentials(service) or {}
        credentials[key_name] = api_key
        self.save_credentials(service, credentials)


def _credentials_base_path(storage_root: Path) -> Path:
    """Return the credentials directory under one explicit storage root."""
    return Path(storage_root).expanduser().resolve() / "credentials"


def _default_shared_credentials_base_path(base_path: Path) -> Path:
    return base_path


def _runtime_shared_credentials_base_path(runtime_paths: RuntimePaths, base_path: Path) -> Path:
    shared_storage_path = runtime_paths.env_value(SHARED_CREDENTIALS_PATH_ENV, default="") or ""
    if shared_storage_path.strip():
        return Path(shared_storage_path).expanduser().resolve()
    return base_path


def _runtime_dedicated_worker_key(runtime_paths: RuntimePaths) -> str | None:
    raw_worker_key = runtime_paths.env_value(_DEDICATED_WORKER_KEY_ENV, default="") or ""
    normalized = raw_worker_key.strip()
    return normalized or None


def _runtime_dedicated_worker_root(runtime_paths: RuntimePaths) -> Path | None:
    raw_worker_root = runtime_paths.env_value(_DEDICATED_WORKER_ROOT_ENV, default="") or ""
    if not raw_worker_root.strip():
        return None
    return Path(raw_worker_root).expanduser().resolve()


# Global instance for convenience (lazy initialization)
_credentials_manager: CredentialsManager | None = None
_credentials_manager_signature: tuple[Path, Path, str | None, Path | None, str | None] | None = None


def _get_credentials_manager(*, storage_root: Path) -> CredentialsManager:
    """Get the global credentials manager instance.

    Returns:
        The global CredentialsManager instance

    """
    global _credentials_manager, _credentials_manager_signature

    base_path = _credentials_base_path(storage_root)
    shared_base_path = _default_shared_credentials_base_path(base_path)
    encryption_key = _configured_credentials_encryption_key()
    current_signature = (
        base_path,
        shared_base_path,
        None,
        None,
        encryption_key,
    )

    if _credentials_manager is None or _credentials_manager_signature != current_signature:
        _credentials_manager = CredentialsManager(
            base_path=base_path,
            shared_base_path=shared_base_path,
            encryption_key=encryption_key,
        )
        _credentials_manager_signature = current_signature
    return _credentials_manager


def get_runtime_credentials_manager(runtime_paths: RuntimePaths) -> CredentialsManager:
    """Return the global credentials manager for one explicit runtime context."""
    global _credentials_manager, _credentials_manager_signature

    base_path = _credentials_base_path(runtime_paths.storage_root)
    shared_base_path = _runtime_shared_credentials_base_path(runtime_paths, base_path)
    encryption_key = runtime_paths.env_value(CREDENTIALS_ENCRYPTION_KEY_ENV, default="") or ""
    encryption_key = encryption_key.strip() or None
    current_signature = (
        base_path,
        shared_base_path,
        _runtime_dedicated_worker_key(runtime_paths),
        _runtime_dedicated_worker_root(runtime_paths),
        encryption_key,
    )

    if _credentials_manager is None or _credentials_manager_signature != current_signature:
        _credentials_manager = CredentialsManager(
            base_path=base_path,
            shared_base_path=shared_base_path,
            current_worker_key=_runtime_dedicated_worker_key(runtime_paths),
            current_worker_root=_runtime_dedicated_worker_root(runtime_paths),
            encryption_key=encryption_key,
        )
        _credentials_manager_signature = current_signature
    return _credentials_manager


def _shared_credentials_manager(credentials_manager: CredentialsManager) -> CredentialsManager:
    """Return the shared credential layer for one execution context."""
    if credentials_manager.shared_base_path == credentials_manager.base_path:
        return credentials_manager
    return credentials_manager.shared_manager()


def get_runtime_shared_credentials_manager(runtime_paths: RuntimePaths) -> CredentialsManager:
    """Return the shared credential layer for one explicit runtime context."""
    return _shared_credentials_manager(get_runtime_credentials_manager(runtime_paths))


def _resolve_worker_credentials_manager(
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> CredentialsManager | None:
    """Return the worker-scoped credentials manager for the current execution, if any."""
    if worker_target is None or worker_target.worker_scope is None:
        return None

    worker_key = worker_target.worker_key
    if worker_key is None:
        return None

    current_storage_root = credentials_manager.storage_root.expanduser().resolve()
    current_worker_key = credentials_manager.current_worker_key
    current_worker_root = credentials_manager.current_worker_root
    if (
        current_worker_key == worker_key
        and current_worker_root is not None
        and current_storage_root == current_worker_root
    ):
        return credentials_manager

    expected_worker_root = worker_root_path(credentials_manager.storage_root, worker_key)
    if current_storage_root == expected_worker_root:
        return credentials_manager

    return credentials_manager.for_worker(worker_key)


def _merge_unscoped_credentials(
    service: str,
    *,
    shared_manager: CredentialsManager,
    local_manager: CredentialsManager,
) -> dict[str, Any] | None:
    """Merge mirrored shared credentials with local worker overrides for unscoped workers."""
    shared_credentials = shared_manager.load_credentials(service)
    local_credentials = local_manager.load_credentials(service)
    return _merge_credential_layers(shared_credentials, local_credentials)


def _merge_credential_layers(
    shared_credentials: Mapping[str, Any] | None,
    worker_credentials: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    merged_credentials: dict[str, Any] = {}
    if isinstance(shared_credentials, Mapping):
        merged_credentials.update(shared_credentials)
    if isinstance(worker_credentials, Mapping):
        merged_credentials.update(worker_credentials)
    return merged_credentials or None


def load_worker_grantable_shared_credentials(
    service: str,
    *,
    shared_manager: CredentialsManager,
    allowed_services: frozenset[str],
) -> dict[str, Any] | None:
    """Return one shared credential only when the worker allowlist permits mirroring it."""
    if service not in allowed_services:
        return None
    if not credential_service_policy(service, None).worker_grantable_supported:
        return None
    shared_credentials = shared_manager.load_credentials(service)
    if not isinstance(shared_credentials, Mapping):
        return None
    if shared_credentials.get("_source") not in _WORKER_GRANTABLE_SHARED_CREDENTIAL_SOURCES:
        return None
    return dict(shared_credentials)


def list_worker_grantable_shared_services(
    *,
    shared_manager: CredentialsManager,
    allowed_services: frozenset[str],
) -> list[str]:
    """List shared credential services that isolated workers may inherit."""
    return sorted(
        service
        for service in shared_manager.list_services()
        if load_worker_grantable_shared_credentials(
            service,
            shared_manager=shared_manager,
            allowed_services=allowed_services,
        )
        is not None
    )


def _merge_scoped_credentials(
    service: str,
    *,
    base_manager: CredentialsManager,
    worker_manager: CredentialsManager | None,
) -> dict[str, Any] | None:
    """Merge shared credentials with worker-scoped overrides."""
    shared_credentials = base_manager.load_credentials(service)
    worker_credentials = worker_manager.load_credentials(service) if worker_manager is not None else None
    return _merge_credential_layers(shared_credentials, worker_credentials)


def sync_shared_credentials_to_worker(
    worker_key: str,
    *,
    allowed_services: frozenset[str],
    credentials_manager: CredentialsManager,
) -> None:
    """Sync shared credentials into one worker's dedicated shared-credential mirror.

    The worker's override store remains separate. Only ``allowed_services`` may be
    mirrored into the worker's shared credential layer, regardless of whether the
    shared credential originated from env sync or the dashboard/API.
    """
    manager = credentials_manager
    worker_shared_manager = manager.for_worker(worker_key).shared_manager()
    source_manager = _shared_credentials_manager(manager)
    mirrored_services = set(worker_shared_manager.list_services())
    copied_services: set[str] = set()
    logger.debug(
        "Starting worker shared credential sync",
        worker_key=worker_key,
        allowed_services=sorted(allowed_services),
    )

    for service in source_manager.list_services():
        shared_credentials = load_worker_grantable_shared_credentials(
            service,
            shared_manager=source_manager,
            allowed_services=allowed_services,
        )
        if shared_credentials is None:
            if service not in allowed_services:
                logger.info(
                    "Skipping non-grantable shared credentials during worker sync",
                    worker_key=worker_key,
                    service=service,
                )
            continue

        copied_services.add(service)
        worker_shared_manager.save_credentials(service, shared_credentials)

    for service in mirrored_services - copied_services:
        worker_shared_manager.delete_credentials(service)


def _primary_runtime_scoped_credentials_manager(
    service: str,
    *,
    manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget,
) -> CredentialsManager | None:
    policy = credential_service_policy(service, worker_target.worker_scope)
    if not policy.uses_primary_runtime_scoped_credentials:
        return None
    identity = worker_target.execution_identity
    if identity is None or identity.requester_id is None:
        msg = f"Primary-runtime scoped credentials for {service} require a requester identity"
        raise ValueError(msg)
    agent_name = worker_target.routing_agent_name if worker_target.worker_scope == "user_agent" else None
    return manager.for_primary_runtime_scope(identity.requester_id, agent_name)


def load_scoped_credentials(
    service: str,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
    allowed_shared_services: frozenset[str] | None = None,
) -> dict[str, Any] | None:
    """Load credentials for a service, resolving worker-scoped overrides when available."""
    manager = credentials_manager
    shared_manager = _shared_credentials_manager(manager)
    if worker_target is None or worker_target.worker_scope is None:
        if manager.shared_base_path != manager.base_path:
            return _merge_unscoped_credentials(
                service,
                shared_manager=shared_manager,
                local_manager=manager,
            )
        return shared_manager.load_credentials(service)

    primary_runtime_manager = _primary_runtime_scoped_credentials_manager(
        service,
        manager=manager,
        worker_target=worker_target,
    )
    uses_local_shared_credentials = credential_service_policy(
        service,
        worker_target.worker_scope,
    ).uses_local_shared_credentials
    worker_manager = (
        _resolve_worker_credentials_manager(
            credentials_manager=manager,
            worker_target=worker_target,
        )
        if primary_runtime_manager is None and not uses_local_shared_credentials
        else None
    )
    resolved_allowed_shared_services = allowed_shared_services
    if resolved_allowed_shared_services is None and manager.shared_base_path == manager.base_path:
        resolved_allowed_shared_services = frozenset()
    if primary_runtime_manager is not None:
        shared_credentials = None
    elif (
        uses_local_shared_credentials
        or manager.shared_base_path != manager.base_path
        or resolved_allowed_shared_services is None
    ):
        shared_credentials = shared_manager.load_credentials(service)
    else:
        shared_credentials = load_worker_grantable_shared_credentials(
            service,
            shared_manager=shared_manager,
            allowed_services=resolved_allowed_shared_services,
        )
    scoped_manager = primary_runtime_manager or worker_manager
    worker_credentials = scoped_manager.load_credentials(service) if scoped_manager is not None else None
    return _merge_credential_layers(shared_credentials, worker_credentials)


def save_scoped_credentials(
    service: str,
    credentials: dict[str, Any],
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> None:
    """Save credentials for a service to the current worker scope when available."""
    manager = credentials_manager
    if worker_target is None or worker_target.worker_scope is None:
        target_manager = manager if manager.shared_base_path != manager.base_path else manager.shared_manager()
        target_manager.save_credentials(service, credentials)
        return

    if credential_service_policy(service, worker_target.worker_scope).uses_local_shared_credentials:
        manager.shared_manager().save_credentials(service, credentials)
        return

    primary_runtime_manager = _primary_runtime_scoped_credentials_manager(
        service,
        manager=manager,
        worker_target=worker_target,
    )
    if primary_runtime_manager is not None:
        primary_runtime_manager.save_credentials(service, credentials)
        return

    worker_manager = _resolve_worker_credentials_manager(
        credentials_manager=manager,
        worker_target=worker_target,
    )
    target_manager = worker_manager or manager.shared_manager()
    target_manager.save_credentials(service, credentials)


def delete_scoped_credentials(
    service: str,
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> None:
    """Delete credentials for a service from the current worker scope when available."""
    manager = credentials_manager
    if worker_target is None or worker_target.worker_scope is None:
        target_manager = manager if manager.shared_base_path != manager.base_path else manager.shared_manager()
        target_manager.delete_credentials(service)
        return

    if credential_service_policy(service, worker_target.worker_scope).uses_local_shared_credentials:
        manager.shared_manager().delete_credentials(service)
        return

    primary_runtime_manager = _primary_runtime_scoped_credentials_manager(
        service,
        manager=manager,
        worker_target=worker_target,
    )
    if primary_runtime_manager is not None:
        primary_runtime_manager.delete_credentials(service)
        return

    worker_manager = _resolve_worker_credentials_manager(
        credentials_manager=manager,
        worker_target=worker_target,
    )
    target_manager = worker_manager or manager.shared_manager()
    target_manager.delete_credentials(service)
