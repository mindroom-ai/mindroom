"""In-memory registry of pending secret requests and the card content they post.

An agent asks for a key with a card in the thread; the requester answers through
the backend route. The registry holds only metadata and a future for the outcome,
never the secret value.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

SECRET_REQUEST_CONTENT_KEY = "io.mindroom.secret_request"  # noqa: S105 - Matrix content key, not a secret
SECRET_REQUEST_TTL_SECONDS = 15 * 60

type SecretRequestOutcome = Literal["saved", "declined", "expired"]
type SecretRequestStatus = Literal["pending", "saved", "declined", "expired"]


@dataclass
class PendingSecretRequest:
    """One open request for a secret, awaiting the requester's answer."""

    request_id: str
    room_id: str
    requester_id: str
    agent_name: str
    service: str
    worker_target: ResolvedWorkerTarget
    expires_at: float
    outcome: asyncio.Future[SecretRequestOutcome]
    claimed: bool = False


class SecretRequestRegistry:
    """Pending requests by ID; every method is synchronous on the event loop."""

    def __init__(self) -> None:
        self._requests: dict[str, PendingSecretRequest] = {}

    def register(
        self,
        *,
        room_id: str,
        requester_id: str,
        agent_name: str,
        service: str,
        worker_target: ResolvedWorkerTarget,
        now: float,
    ) -> PendingSecretRequest:
        """Create and store a pending request that expires after the TTL."""
        request = PendingSecretRequest(
            request_id=secrets.token_urlsafe(24),
            room_id=room_id,
            requester_id=requester_id,
            agent_name=agent_name,
            service=service,
            worker_target=worker_target,
            expires_at=now + SECRET_REQUEST_TTL_SECONDS,
            outcome=asyncio.get_running_loop().create_future(),
        )
        self._requests[request.request_id] = request
        return request

    def claim(self, request_id: str, *, room_id: str, now: float) -> PendingSecretRequest | None:
        """Take the request for answering, or None when it cannot be answered now."""
        request = self._requests.get(request_id)
        if (
            request is None
            or request.room_id != room_id
            or request.claimed
            or request.outcome.done()
            or now >= request.expires_at
        ):
            return None
        request.claimed = True
        return request

    def release(self, request_id: str) -> None:
        """Give a claim back so a failed save can be retried."""
        request = self._requests.get(request_id)
        if request is not None:
            request.claimed = False

    def resolve(self, request_id: str, outcome: SecretRequestOutcome) -> None:
        """Set the outcome unless the request is unknown or already done."""
        request = self._requests.get(request_id)
        if request is not None and not request.outcome.done():
            request.outcome.set_result(outcome)

    def discard(self, request_id: str) -> None:
        """Forget the request."""
        self._requests.pop(request_id, None)


_REGISTRY = SecretRequestRegistry()


def secret_request_registry() -> SecretRequestRegistry:
    """Return the process-wide registry."""
    return _REGISTRY


def secret_request_body(
    status: SecretRequestStatus,
    *,
    agent_display_name: str,
    display_name: str,
) -> str:
    """Return the plain-text fallback body for a card in `status`."""
    if status == "pending":
        return f"🔑 {agent_display_name} asks for your {display_name} key. Open MindRoom Chat to enter it."
    if status == "saved":
        return f"🔑 {display_name} key saved."
    if status == "declined":
        return f"🔑 {display_name} key request declined."
    return f"🔑 {display_name} key request expired."


def secret_request_content(
    request: PendingSecretRequest,
    *,
    agent_display_name: str,  # noqa: ARG001 - the body carries the agent name, not this object
    display_name: str,
    hosts: list[str],
    reason: str,
    status: SecretRequestStatus,
) -> dict[str, object]:
    """Return the `io.mindroom.secret_request` object: metadata only, never a value."""
    return {
        "request_id": request.request_id,
        "requester_id": request.requester_id,
        "service": request.service,
        "display_name": display_name,
        "hosts": list(hosts),
        "reason": reason,
        "status": status,
        "expires_at": int(request.expires_at * 1000),
    }
