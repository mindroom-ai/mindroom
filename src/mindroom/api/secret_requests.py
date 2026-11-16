"""Chat clients' answer to an agent's secret request: the requester's key or temporary secret, or a decline.

A saved key becomes the agent's Agent Keys key for the request's service in the scope of the tool call that asked,
and the broker adds it only to requests to that service's hosts. Only the person the agent asked may answer, once,
while the request is pending, and only while the config the API serves now still lets them set this agent's keys,
still keeps them in that scope, and still gives the service the hosts the card showed.
A temporary secret goes to the conversation that asked, kept as the requester chooses: in memory, or also on disk.
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
from mindroom.egress_broker.secrets import egress_key_path, save_secret, validate_secret
from mindroom.egress_broker.user_services import consistent_read
from mindroom.logging_config import get_logger
from mindroom.plugin_http import ClientUser, require_room_member
from mindroom.secret_requests import (
    KeyTarget,
    SecretKeep,
    secret_request_hosts,
    secret_request_log_fields,
    secret_request_registry,
    temporary_secrets,
)

if TYPE_CHECKING:
    from mindroom.api.egress_status import AgentKeysScope
    from mindroom.secret_requests import PendingSecretRequest, SecretRequestOutcome, TemporaryTarget

logger = get_logger(__name__)
_NOT_ACTIVE = "This request is no longer active."

router = APIRouter(prefix="/api/rooms/{room_id}", tags=["secret-requests"])

_Member = Annotated[ClientUser, Depends(require_room_member)]


class SecretRequestAnswer(BaseModel):
    """The requester's answer: exactly one of the secret or ``decline: true``.

    ``keep`` says where MindRoom keeps a temporary secret, and so for how long; without it, in memory.
    """

    model_config = ConfigDict(extra="forbid")
    secret: SecretStr | None = None
    decline: bool = False
    keep: SecretKeep | None = None

    @model_validator(mode="after")
    def _secret_or_decline(self) -> Self:
        if (self.secret is not None) == self.decline:
            msg = "Send either a secret or decline: true."
            raise ValueError(msg)
        if self.keep is not None and self.secret is None:
            msg = "Only a secret can be kept."
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
    """Take the requester's answer and tell the waiting tool.

    A key is stored in the agent's scope, a temporary secret is given to the conversation that asked, and a decline
    stores nothing.
    404 when the request is unknown, answered, expired, or asked in another room, and also when the config served now
    keeps the agent's keys in another scope or changed the service; that ends the request as expired, so the agent
    can ask again with a new card. 403 when the caller is not the requester or may no longer set the agent's keys; 422
    with the reason when the value is refused, or when a key's answer names a keep. A 403 or 422 leaves the request
    pending.
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
    target = pending.target
    if isinstance(target, KeyTarget) and answer.keep is not None:
        raise HTTPException(422, "A key is always stored as the agent's key, so it has no keep to choose.")
    if answer.secret is None:
        return "declined"
    if not isinstance(target, KeyTarget):
        return await _give_to_conversation(request, pending, target, user, answer.secret, answer.keep or "memory")
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
            else await asyncio.to_thread(_save_where_asked, scope, target, answer.secret.get_secret_value())
        )
    except ValueError as error:
        # The key store's reasons never repeat the value.
        _log_answer(pending, user, "invalid")
        raise HTTPException(422, str(error)) from error
    if outcome == "forbidden":
        _log_answer(pending, user, "forbidden")
        raise HTTPException(403, "You can no longer set keys for this agent.")
    return outcome


async def _give_to_conversation(
    request: Request,
    pending: PendingSecretRequest,
    target: TemporaryTarget,
    user: ClientUser,
    secret: SecretStr,
    keep: SecretKeep,
) -> SecretRequestOutcome:
    """Give the conversation that asked the requester's temporary secret, kept as they chose.

    Nothing is stored for the agent, so no config is checked: the value reaches only the shell commands of the
    conversation the card was posted in. The value must be one a key could be. The write of a kept file runs in a
    thread.
    """
    value = secret.get_secret_value()
    try:
        validate_secret(value)
    except ValueError as error:
        # The reasons never repeat the value.
        _log_answer(pending, user, "invalid")
        raise HTTPException(422, str(error)) from error
    await asyncio.to_thread(
        temporary_secrets().give,
        target.holder,
        target.name,
        value,
        keep=keep,
        now=time.time(),
        storage_root=config_lifecycle.bind_current_request_snapshot(request).runtime_paths.storage_root,
    )
    pending.kept = keep
    return "saved"


def _save_where_asked(
    scope: AgentKeysScope,
    target: KeyTarget,
    secret: str,
) -> Literal["saved", "expired", "forbidden"]:
    """Store the key in the card's scope for the card's service, while the config served now still agrees with both.

    `expired` when the agent's keys for the requester now live in another store, which never gets the key, or when
    the service is gone, no longer takes the requester's key (`key_save_refusal`), or has other hosts than the card
    showed. `forbidden` when the requester may no longer manage keys in the card's store. User-service edits wait from
    the service check to the write, so the key is saved for the service as checked. Runs in a thread: it reads the
    credential store.
    """
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
        status=status,
        user_id=user.user_id,
        **secret_request_log_fields(pending.target),
    )
