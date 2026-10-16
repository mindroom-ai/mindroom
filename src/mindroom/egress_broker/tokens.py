"""Signed worker proxy tokens for egress broker authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path  # noqa: TC003 - Required at runtime for load_or_create parameter
from typing import Any, Literal, get_args, get_type_hints

from mindroom.atomic_file import atomic_write_bytes_at
from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity, WorkerScope

_TOKEN_PREFIX = "mrb1"  # noqa: S105
_TOKEN_VERSION_TAG = b"mindroom-egress-broker-token-v1"
_MAX_TOKEN_LENGTH = 4096
_KEY_SIZE = 32


@dataclass(frozen=True)
class WorkerClaims:
    """Worker claims embedded in proxy tokens."""

    worker_key: str
    worker_scope: WorkerScope | None
    routing_agent_name: str | None
    tenant_id: str | None
    account_id: str | None
    channel: Literal["matrix", "openai_compat", "mcp"]
    agent_name: str
    requester_id: str | None

    @staticmethod
    def from_worker_target(target: ResolvedWorkerTarget) -> WorkerClaims | None:
        """Extract worker claims from a resolved worker target.

        Returns None when worker_key or execution_identity is None.
        """
        if target.worker_key is None or target.execution_identity is None:
            return None

        return WorkerClaims(
            worker_key=target.worker_key,
            worker_scope=target.worker_scope,
            routing_agent_name=target.routing_agent_name,
            tenant_id=target.tenant_id,
            account_id=target.account_id,
            channel=target.execution_identity.channel,
            agent_name=target.execution_identity.agent_name,
            requester_id=target.execution_identity.requester_id,
        )

    def to_worker_target(self) -> ResolvedWorkerTarget:
        """Rebuild a ResolvedWorkerTarget from these claims.

        The returned identity has room_id, thread_id, resolved_thread_id,
        and session_id set to None. private_agent_names is also None.
        """
        identity = ToolExecutionIdentity(
            channel=self.channel,
            agent_name=self.agent_name,
            requester_id=self.requester_id,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
            session_id=None,
            tenant_id=self.tenant_id,
            account_id=self.account_id,
        )
        return ResolvedWorkerTarget(
            worker_scope=self.worker_scope,
            routing_agent_name=self.routing_agent_name,
            execution_identity=identity,
            tenant_id=self.tenant_id,
            account_id=self.account_id,
            worker_key=self.worker_key,
            private_agent_names=None,
        )

    @property
    def scope_label(self) -> str:
        """Return the worker scope or 'unscoped'."""
        return self.worker_scope if self.worker_scope is not None else "unscoped"


# What a token's claims must hold, read from `WorkerClaims` so the check follows the dataclass.
_CLAIM_KEYS = frozenset(field.name for field in fields(WorkerClaims))
_CLAIM_TYPES = get_type_hints(WorkerClaims)
_WORKER_SCOPES = (*get_args(WorkerScope), None)
_CHANNELS = get_args(_CLAIM_TYPES["channel"])
_OPTIONAL_STR_CLAIMS = tuple(name for name, hint in _CLAIM_TYPES.items() if hint == str | None)


class TokenSigner:
    """Signs and verifies worker proxy tokens."""

    def __init__(self, key: bytes, *, ttl_seconds: int = 604800) -> None:
        """Initialize token signer with signing key and TTL."""
        # A short key, above all an empty one, would let anyone forge tokens.
        if len(key) != _KEY_SIZE:
            msg = f"Token signing key must be exactly {_KEY_SIZE} bytes"
            raise ValueError(msg)
        self._key = key
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def load_or_create(path: Path, *, ttl_seconds: int = 604800) -> TokenSigner:
        """Load existing key from path or create new 32-byte key with mode 0600.

        Parent directory is created with mode 0700 if it does not exist.
        A new key is published atomically, so a crash never leaves a partial file.
        Raises ValueError, naming the file but never its contents, when the key
        is not exactly 32 bytes.
        """
        # Ensure parent directory exists with mode 0700
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        if not path.exists():
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                atomic_write_bytes_at(
                    directory_fd,
                    path.name,
                    secrets.token_bytes(_KEY_SIZE),
                    file_mode=0o600,
                    temp_prefix=f".{path.name}.",
                )
            finally:
                os.close(directory_fd)

        key = path.read_bytes()
        if len(key) != _KEY_SIZE:
            msg = (
                f"Egress broker token key file {path} holds {len(key)} bytes instead of {_KEY_SIZE}; "
                "delete it so the broker creates a new key"
            )
            raise ValueError(msg)
        return TokenSigner(key, ttl_seconds=ttl_seconds)

    def mint(self, claims: WorkerClaims, *, now: float | None = None) -> str:
        """Mint a signed token for the given claims."""
        if now is None:
            now = time.time()

        exp = int(now + self._ttl_seconds)

        # Build payload with sorted keys and compact separators
        payload_dict = {"claims": asdict(claims), "exp": exp}
        payload_bytes = json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")

        mac = self._mac(payload_bytes)

        # Encode without padding
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        mac_b64 = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")

        return f"{_TOKEN_PREFIX}.{payload_b64}.{mac_b64}"

    def verify(self, token: str, *, now: float | None = None) -> WorkerClaims | None:
        """Verify a token and return claims, or None if invalid/expired.

        Three steps, each of which returns None on failure: parse the token into its payload and MAC, check the MAC
        over the payload, then check the payload's expiry and claims.
        """
        if now is None:
            now = time.time()

        parsed = _parse_token(token)
        if parsed is None:
            return None
        payload_bytes, received_mac = parsed
        if not hmac.compare_digest(received_mac, self._mac(payload_bytes)):
            return None
        claims_dict = _unexpired_claims(payload_bytes, now=now)
        if claims_dict is None:
            return None
        return _claims_from_dict(claims_dict)

    def _mac(self, payload_bytes: bytes) -> bytes:
        """Return the HMAC-SHA256 of the version tag followed by the payload."""
        return hmac.new(self._key, _TOKEN_VERSION_TAG + payload_bytes, hashlib.sha256).digest()


def _b64decode_unpadded(segment: str) -> bytes | None:
    """Decode an unpadded base64url token segment, or return None when it is not base64."""
    try:
        return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
    except ValueError:  # binascii.Error for bad padding, plain ValueError for non-ASCII text
        return None


def _parse_token(token: str) -> tuple[bytes, bytes] | None:
    """Return the decoded payload and MAC of a token with the expected size, prefix, and segments, or None."""
    if len(token) > _MAX_TOKEN_LENGTH:
        return None

    parts = token.split(".")
    if len(parts) != 3:
        return None

    prefix, payload_b64, mac_b64 = parts
    if prefix != _TOKEN_PREFIX:
        return None

    payload_bytes = _b64decode_unpadded(payload_b64)
    if payload_bytes is None:
        return None
    received_mac = _b64decode_unpadded(mac_b64)
    if received_mac is None:
        return None
    return payload_bytes, received_mac


def _unexpired_claims(payload_bytes: bytes, *, now: float) -> dict[str, Any] | None:
    """Return the claims object of a signed payload whose integer `exp` is after `now`, or None."""
    try:
        payload_dict = json.loads(payload_bytes)
    except ValueError:  # JSONDecodeError, or UnicodeDecodeError for bytes that are not UTF-8, -16, or -32
        return None

    if not isinstance(payload_dict, dict):
        return None

    exp = payload_dict.get("exp")
    if not isinstance(exp, int):
        return None
    if now >= exp:
        return None

    claims_dict = payload_dict.get("claims")
    if not isinstance(claims_dict, dict):
        return None
    return claims_dict


def _claims_from_dict(claims_dict: dict[str, Any]) -> WorkerClaims | None:
    """Return the claims when exactly the expected keys are present, each with its expected type, or None."""
    if set(claims_dict) != _CLAIM_KEYS:
        return None
    if not isinstance(claims_dict["worker_key"], str) or not isinstance(claims_dict["agent_name"], str):
        return None
    if claims_dict["worker_scope"] not in _WORKER_SCOPES or claims_dict["channel"] not in _CHANNELS:
        return None
    if any(claims_dict[key] is not None and not isinstance(claims_dict[key], str) for key in _OPTIONAL_STR_CLAIMS):
        return None
    return WorkerClaims(**claims_dict)
