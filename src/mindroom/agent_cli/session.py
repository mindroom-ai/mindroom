"""Trusted response-turn identity and ephemeral CLI bearer grants."""

from __future__ import annotations

__all__ = [
    "MAX_CLI_GRANT_LIFETIME_NS",
    "CliAuthenticationError",
    "CliBashWindowRequiredError",
    "CliCallConflictError",
    "CliGrant",
    "CliOperationError",
    "CliOperationOwner",
    "CliTurnOwner",
    "TurnToolBridge",
    "TurnToolRegistry",
    "cli_turn_owner",
]

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_execution_identity_from_runtime_context,
    execution_identity_matches_tool_runtime_context,
)

if TYPE_CHECKING:
    from mindroom.agent_cli.protocol import AgentCliOperation
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

MAX_CLI_GRANT_LIFETIME_NS = 24 * 60 * 60 * 1_000_000_000
_DUMMY_DIGEST = hashlib.sha256(b"mindroom-agent-cli-invalid-token").digest()


class CliCallConflictError(ValueError):
    """A live call ID was reused with a different qualified key or arguments."""


class CliBashWindowRequiredError(ValueError):
    """A tool operation arrived while no Bash call of its turn was executing."""


class CliOperationError(ValueError):
    """A caller-safe rejection whose message is relayed to the CLI."""


@dataclass(frozen=True, slots=True)
class CliTurnOwner:
    """Server-derived identity for one response generation and shell worker."""

    execution_identity: ToolExecutionIdentity
    turn_id: str
    generation: str
    worker_id: str


def cli_turn_owner(
    runtime_context: ToolRuntimeContext,
    turn_context: ResponseTurnContext,
    *,
    worker_id: str,
) -> CliTurnOwner:
    """Build one owner only from matching trusted runtime objects."""
    if runtime_context.agent_name not in runtime_context.config.agents:
        msg = "CLI turn owner requires an agent dispatch"
        raise ValueError(msg)
    if runtime_context.agent_name != turn_context.entity_label:
        msg = "CLI turn owner agent identity does not match"
        raise ValueError(msg)
    expected = (
        (runtime_context.requester_id, turn_context.requester_id, "requester"),
        (runtime_context.room_id, turn_context.room_id, "room"),
        (runtime_context.session_id, turn_context.session_id, "session"),
        (runtime_context.resolved_thread_id, turn_context.thread_id, "thread"),
        (runtime_context.reply_to_event_id, turn_context.reply_to_event_id, "reply target"),
    )
    for runtime_value, turn_value, label in expected:
        if runtime_value != turn_value:
            msg = f"CLI turn owner {label} identity does not match"
            raise ValueError(msg)
    if runtime_context.correlation_id is not None and runtime_context.correlation_id != turn_context.correlation_id:
        msg = "CLI turn owner correlation identity does not match"
        raise ValueError(msg)
    required = (
        (runtime_context.membership_turn_id, "turn"),
        (turn_context.run_id, "generation"),
        (runtime_context.requester_id, "requester"),
        (runtime_context.room_id, "room"),
        (runtime_context.session_id, "session"),
        (worker_id, "worker"),
    )
    for value, label in required:
        if not value:
            msg = f"CLI turn owner requires a non-empty {label} identity"
            raise ValueError(msg)
    identity = build_execution_identity_from_runtime_context(runtime_context)
    if not execution_identity_matches_tool_runtime_context(identity, runtime_context):
        msg = "CLI turn owner execution identity does not match its runtime"
        raise ValueError(msg)
    return CliTurnOwner(
        execution_identity=identity,
        turn_id=cast("str", runtime_context.membership_turn_id),
        generation=cast("str", turn_context.run_id),
        worker_id=worker_id,
    )


class CliAuthenticationError(ValueError):
    """Raised when a response's bearer grant is absent, expired, or revoked."""


@dataclass(frozen=True, slots=True)
class CliGrant:
    """One raw bearer returned to trusted worker setup exactly once."""

    raw_token: str = field(repr=False)
    expires_at_ns: int


@dataclass(frozen=True, slots=True)
class _GrantRecord:
    digest: bytes
    expires_at_ns: int


class TurnToolBridge:
    """Ephemeral bearer authority owned by one verified response turn."""

    def __init__(self, owner: CliTurnOwner) -> None:
        self.owner = owner
        self._grant: _GrantRecord | None = None
        self._revoked = False

    def issue(self, *, now_ns: int, expires_at_ns: int) -> CliGrant:
        """Mint a non-extendable grant whose raw token is never retained."""
        if now_ns < 0 or expires_at_ns <= now_ns:
            msg = "CLI grant expiry must be later than issuance"
            raise ValueError(msg)
        if self._grant is not None or self._revoked:
            msg = "A CLI turn can issue only one grant"
            raise RuntimeError(msg)
        expiry = min(expires_at_ns, now_ns + MAX_CLI_GRANT_LIFETIME_NS)
        raw_token = secrets.token_urlsafe(32)
        self._grant = _GrantRecord(hashlib.sha256(raw_token.encode()).digest(), expiry)
        return CliGrant(raw_token=raw_token, expires_at_ns=expiry)

    def authenticate(self, raw_token: str, *, now_ns: int) -> CliTurnOwner:
        """Resolve bearer authority; possession does not authenticate the caller's host."""
        digest = hashlib.sha256(raw_token.encode()).digest()
        record = self._grant
        candidate = record.digest if record is not None else _DUMMY_DIGEST
        digest_matches = hmac.compare_digest(digest, candidate)
        valid = record is not None and digest_matches and not self._revoked and now_ns < record.expires_at_ns
        if not valid:
            msg = "Agent CLI authority is unavailable"
            raise CliAuthenticationError(msg)
        return self.owner

    def revoke(self) -> None:
        """Revoke this turn's bearer before worker retirement."""
        self._revoked = True


class CliOperationOwner(Protocol):
    """Registered response owner; transport never executes prepared tools."""

    owner: CliTurnOwner

    def authenticate(self, raw_token: str, *, now_ns: int) -> CliTurnOwner:
        """Validate the response's ephemeral bearer authority."""
        ...

    def revoke(self) -> None:
        """Fence authority before response-owned teardown."""
        ...

    async def operation(self, operation: AgentCliOperation) -> dict[str, object]:
        """Read metadata or enqueue one owned operation."""
        ...

    async def get_call(self, call_id: str) -> dict[str, object]:
        """Read one exact-owner receipt."""
        ...


class TurnToolRegistry:
    """One orchestrator's live turn registrations, with no global execution lock."""

    def __init__(self) -> None:
        self._owners: list[CliOperationOwner] = []
        self._closed = False

    def register(self, owner: CliOperationOwner) -> None:
        """Register only a response-created owner while transport is available."""
        if self._closed:
            msg = "Agent CLI registry is closed"
            raise CliAuthenticationError(msg)
        self._owners.append(owner)

    def unregister(self, owner: CliOperationOwner) -> None:
        """Remove an owner after revocation and response cleanup."""
        if owner in self._owners:
            self._owners.remove(owner)

    def resolve(self, authorization: str | None, *, now_ns: int) -> CliOperationOwner:
        """Resolve bearer only; caller-supplied worker or owner fields are ignored."""
        if (
            authorization is not None
            and authorization.startswith("Bearer ")
            and len(authorization) <= 4096
            and not self._closed
        ):
            token = authorization.removeprefix("Bearer ")
            for owner in self._owners:
                try:
                    owner.authenticate(token, now_ns=now_ns)
                except CliAuthenticationError:
                    continue
                return owner
        msg = "Agent CLI authority is unavailable"
        raise CliAuthenticationError(msg)

    def close(self) -> None:
        """Fence new registrations and revoke grants; response owners drain work."""
        self._closed = True
        for owner in self._owners:
            owner.revoke()
