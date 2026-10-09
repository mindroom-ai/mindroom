"""Signed worker proxy tokens for egress broker authentication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 - Required at runtime for load_or_create parameter
from typing import Literal

from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, ToolExecutionIdentity

_TOKEN_PREFIX = "mrb1"  # noqa: S105
_TOKEN_VERSION_TAG = b"mindroom-egress-broker-token-v1"
_MAX_TOKEN_LENGTH = 4096
_KEY_SIZE = 32


@dataclass(frozen=True)
class WorkerClaims:
    """Worker claims embedded in proxy tokens."""

    worker_key: str
    worker_scope: Literal["shared", "user", "user_agent"] | None
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


class TokenSigner:
    """Signs and verifies worker proxy tokens."""

    def __init__(self, key: bytes, *, ttl_seconds: int = 604800) -> None:
        """Initialize token signer with signing key and TTL."""
        self._key = key
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def load_or_create(path: Path, *, ttl_seconds: int = 604800) -> TokenSigner:
        """Load existing key from path or create new 32-byte key with mode 0600.

        Parent directory is created with mode 0700 if it does not exist.
        """
        # Ensure parent directory exists with mode 0700
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

        if path.exists():
            key = path.read_bytes()
        else:
            key = secrets.token_bytes(_KEY_SIZE)
            # Write with restricted permissions
            path.touch(mode=0o600)
            path.write_bytes(key)
            # Ensure mode is set correctly (touch may be affected by umask)
            path.chmod(0o600)

        return TokenSigner(key, ttl_seconds=ttl_seconds)

    def mint(self, claims: WorkerClaims, *, now: float | None = None) -> str:
        """Mint a signed token for the given claims."""
        if now is None:
            now = time.time()

        exp = int(now + self._ttl_seconds)

        # Build payload with sorted keys and compact separators
        payload_dict = {
            "claims": {
                "worker_key": claims.worker_key,
                "worker_scope": claims.worker_scope,
                "routing_agent_name": claims.routing_agent_name,
                "tenant_id": claims.tenant_id,
                "account_id": claims.account_id,
                "channel": claims.channel,
                "agent_name": claims.agent_name,
                "requester_id": claims.requester_id,
            },
            "exp": exp,
        }
        payload_bytes = json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")

        # Compute MAC over version tag + payload
        mac_input = _TOKEN_VERSION_TAG + payload_bytes
        mac = hmac.new(self._key, mac_input, hashlib.sha256).digest()

        # Encode without padding
        payload_b64 = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        mac_b64 = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")

        return f"{_TOKEN_PREFIX}.{payload_b64}.{mac_b64}"

    def verify(self, token: str, *, now: float | None = None) -> WorkerClaims | None:  # noqa: C901, PLR0911, PLR0912
        """Verify a token and return claims, or None if invalid/expired."""
        if now is None:
            now = time.time()

        # Reject oversized tokens immediately
        if len(token) > _MAX_TOKEN_LENGTH:
            return None

        # Split and validate structure
        parts = token.split(".")
        if len(parts) != 3:
            return None

        prefix, payload_b64, mac_b64 = parts
        if prefix != _TOKEN_PREFIX:
            return None

        # Decode payload
        try:
            # Add padding if needed
            payload_b64_padded = payload_b64 + "=" * (-len(payload_b64) % 4)
            payload_bytes = base64.urlsafe_b64decode(payload_b64_padded)
        except Exception:
            return None

        # Recompute MAC and compare
        try:
            mac_b64_padded = mac_b64 + "=" * (-len(mac_b64) % 4)
            received_mac = base64.urlsafe_b64decode(mac_b64_padded)
        except Exception:
            return None

        mac_input = _TOKEN_VERSION_TAG + payload_bytes
        expected_mac = hmac.new(self._key, mac_input, hashlib.sha256).digest()

        if not hmac.compare_digest(received_mac, expected_mac):
            return None

        # Parse payload JSON
        try:
            payload_dict = json.loads(payload_bytes)
        except Exception:
            return None

        if not isinstance(payload_dict, dict):
            return None

        # Validate and extract exp
        exp = payload_dict.get("exp")
        if not isinstance(exp, int):
            return None

        # Check expiration
        if now >= exp:
            return None

        # Validate and extract claims
        claims_dict = payload_dict.get("claims")
        if not isinstance(claims_dict, dict):
            return None

        # Parse claims with strict type checking
        try:
            worker_key = claims_dict.get("worker_key")
            if not isinstance(worker_key, str):
                return None

            worker_scope = claims_dict.get("worker_scope")
            if worker_scope is not None and worker_scope not in ("shared", "user", "user_agent"):
                return None

            routing_agent_name = claims_dict.get("routing_agent_name")
            if routing_agent_name is not None and not isinstance(routing_agent_name, str):
                return None

            tenant_id = claims_dict.get("tenant_id")
            if tenant_id is not None and not isinstance(tenant_id, str):
                return None

            account_id = claims_dict.get("account_id")
            if account_id is not None and not isinstance(account_id, str):
                return None

            channel = claims_dict.get("channel")
            if channel not in ("matrix", "openai_compat", "mcp"):
                return None

            agent_name = claims_dict.get("agent_name")
            if not isinstance(agent_name, str):
                return None

            requester_id = claims_dict.get("requester_id")
            if requester_id is not None and not isinstance(requester_id, str):
                return None

            # Reject unknown keys
            expected_keys = {
                "worker_key",
                "worker_scope",
                "routing_agent_name",
                "tenant_id",
                "account_id",
                "channel",
                "agent_name",
                "requester_id",
            }
            if set(claims_dict.keys()) != expected_keys:
                return None

            return WorkerClaims(
                worker_key=worker_key,
                worker_scope=worker_scope,  # type: ignore[arg-type]
                routing_agent_name=routing_agent_name,
                tenant_id=tenant_id,
                account_id=account_id,
                channel=channel,  # type: ignore[arg-type]
                agent_name=agent_name,
                requester_id=requester_id,
            )
        except Exception:
            return None
