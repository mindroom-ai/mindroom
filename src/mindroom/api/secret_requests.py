"""Chat clients' answer to an agent's secret request: the requester's key, or a decline.

A saved key becomes the agent's Agent Keys key for the request's service in the scope of the tool call that asked,
and the broker adds it only to requests to that service's hosts. Only the person the agent asked may answer, once,
while the request is pending, and only while the config the API serves now still lets them set this agent's keys,
still keeps them in that scope, and still gives the service the hosts the card showed.
The value never appears in a response body or a log line.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from mindroom.api import config_lifecycle
from mindroom.api.egress_status import agent_services, key_save_refusal, resolve_agent_keys_scope
from mindroom.egress_broker.secrets import egress_key_path, save_secret
from mindroom.egress_broker.user_services import consistent_read
from mindroom.logging_config import get_logger
from mindroom.plugin_http import ClientUser, require_room_member
from mindroom.secret_requests import secret_request_hosts, secret_request_registry

if TYPE_CHECKING:
    from mindroom.api.egress_status import AgentKeysScope
    from mindroom.secret_requests import PendingSecretRequest, SecretRequestOutcome

logger = get_logger(__name__)
_NOT_ACTIVE = "This request is no longer active."

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["secret-requests"])

_Member = Annotated[ClientUser, Depends(require_room_member)]


class SecretRequestAnswer(BaseModel):
    """The requester's answer: exactly one of the secret or ``decline: true``."""

    model_config = ConfigDict(extra="forbid")
    secret: SecretStr | None = None
    decline: bool = False

    @model_validator(mode="after")
    def _secret_or_decline(self) -> Self:
        if (self.secret is not None) == self.decline:
            msg = "Send either a secret or decline: true."
            raise ValueError(msg)
        return self


@router.post("/secret-requests/{request_id}", status_code=204)
async def answer_secret_request(
    request: Request,
    room_id: str,
    request_id: str,
    answer: SecretRequestAnswer,
    user: _Member,
) -> None:
    """Store the requester's key in the agent's scope or record their decline, and tell the waiting tool.

    404 when the request is unknown, answered, expired, or asked in another room, and also when the config served now
    keeps the agent's keys in another scope or changed the service; that ends the request as expired, so the agent
    can ask again with a new card. 403 when the caller is not the requester or may no longer set the agent's keys; 422
    with the key store's reason when it refuses the value. A 403 or 422 leaves the request pending.
    """
    registry = secret_request_registry()
    pending = registry.claim(request_id, room_id=room_id, now=time.time())
    if pending is None:
        raise HTTPException(404, _NOT_ACTIVE)
    try:
        outcome = await _apply_answer(request, pending, user, answer)
    except BaseException:
        registry.release(request_id)
        raise
    registry.resolve(request_id, outcome)
    _log_answer(pending, user, "stale" if outcome == "expired" else outcome)
    if outcome == "expired":
        raise HTTPException(404, _NOT_ACTIVE)


async def _apply_answer(
    request: Request,
    pending: PendingSecretRequest,
    user: ClientUser,
    answer: SecretRequestAnswer,
) -> SecretRequestOutcome:
    """Check the caller and store the key, or raise the refusal; a decline needs only the requester.

    Returns `expired` when the request no longer matches the config served now (see `_save_where_asked`).
    """
    if user.user_id != pending.requester_id:
        _log_answer(pending, user, "forbidden")
        raise HTTPException(403, "Only the person the agent asked can answer.")
    if answer.secret is None:
        return "declined"
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    memberships = config_lifecycle.app_state(request.app).runtime.agent_reply_memberships
    scope = (
        None
        if config is None
        else resolve_agent_keys_scope(config, snapshot.runtime_paths, memberships, user.user_id, pending.agent_name)
    )
    try:
        outcome = (
            "forbidden"
            if scope is None
            else await asyncio.to_thread(_save_where_asked, scope, pending, answer.secret.get_secret_value())
        )
    except ValueError as error:
        # The key store's reasons never repeat the value.
        _log_answer(pending, user, "invalid")
        raise HTTPException(422, str(error)) from error
    if outcome == "forbidden":
        _log_answer(pending, user, "forbidden")
        raise HTTPException(403, "You can no longer set keys for this agent.")
    return outcome


def _save_where_asked(
    scope: AgentKeysScope,
    pending: PendingSecretRequest,
    secret: str,
) -> Literal["saved", "expired", "forbidden"]:
    """Store the key in the card's scope for the card's service, while the config served now still agrees with both.

    `expired` when the agent's keys for the requester now live in another store, which never gets the key, or when
    the service is gone, no longer takes the requester's key (`key_save_refusal`), or has other hosts than the card
    showed. `forbidden` when the requester may no longer manage keys in the card's store. User-service edits wait from
    the service check to the write, so the key is saved for the service as checked. Runs in a thread: it reads the
    credential store.
    """
    target = pending.target
    destination = egress_key_path(scope.manager, scope.target, target.service)
    if destination != egress_key_path(scope.manager, target.worker_target, target.service):
        return "expired"
    if not scope.can_manage:
        return "forbidden"
    with consistent_read():
        service = agent_services(scope.config, scope.manager, scope.target).get(target.service)
        if service is None or key_save_refusal(service) is not None or secret_request_hosts(service) != target.hosts:
            return "expired"
        save_secret(scope.manager, target.worker_target, target.service, secret)
    return "saved"


def _log_answer(pending: PendingSecretRequest, user: ClientUser, status: str) -> None:
    logger.info(
        "secret_request_answered",
        request_id=pending.request_id,
        agent=pending.agent_name,
        service=pending.target.service,
        status=status,
        user_id=user.user_id,
    )
