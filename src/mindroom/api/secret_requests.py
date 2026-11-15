"""Chat clients' answer to an agent's secret request: the requester's key, or a decline.

A saved key becomes the agent's Agent Keys key for the request's service in the scope of the tool call that asked,
and the broker adds it only to requests to that service's hosts. Only the person the agent asked may answer, once,
while the request is pending, and only while the config the API serves now still lets them set this agent's keys.
The value never appears in a response body or a log line.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated, Literal, Self

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, SecretStr, model_validator

from mindroom.api import config_lifecycle
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.egress_broker.key_access import egress_key_access
from mindroom.egress_broker.secrets import save_secret
from mindroom.logging_config import get_logger
from mindroom.plugin_http import ClientUser, require_room_member
from mindroom.secret_requests import secret_request_registry

if TYPE_CHECKING:
    from mindroom.secret_requests import PendingSecretRequest

logger = get_logger(__name__)

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

    404 when the request is unknown, answered, expired, or asked in another room; 403 when the caller is not the
    requester or may no longer set the agent's keys; 422 with the key store's reason when it refuses the value. A
    refused answer leaves the request pending.
    """
    registry = secret_request_registry()
    pending = registry.claim(request_id, room_id=room_id, now=time.time())
    if pending is None:
        raise HTTPException(404, "This request is no longer active.")
    try:
        outcome = await _apply_answer(request, pending, user, answer)
    except BaseException:
        registry.release(request_id)
        raise
    registry.resolve(request_id, outcome)
    _log_answer(pending, user, outcome)


async def _apply_answer(
    request: Request,
    pending: PendingSecretRequest,
    user: ClientUser,
    answer: SecretRequestAnswer,
) -> Literal["saved", "declined"]:
    """Check the caller and store the key, or raise the refusal; a decline needs only the requester."""
    if user.user_id != pending.requester_id:
        _log_answer(pending, user, "forbidden")
        raise HTTPException(403, "Only the person the agent asked can answer.")
    if answer.secret is None:
        return "declined"
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    config = snapshot.runtime_config
    access = (
        None
        if config is None
        else egress_key_access(
            user.user_id,
            pending.agent_name,
            config,
            snapshot.runtime_paths,
            config_lifecycle.app_state(request.app).runtime.agent_reply_memberships,
        )
    )
    if access is None or not access[1]:
        _log_answer(pending, user, "forbidden")
        raise HTTPException(403, "You can no longer set keys for this agent.")
    manager = get_runtime_credentials_manager(snapshot.runtime_paths)
    try:
        await asyncio.to_thread(
            save_secret,
            manager,
            pending.worker_target,
            pending.service,
            answer.secret.get_secret_value(),
        )
    except ValueError as error:
        # The key store's reasons never repeat the value.
        _log_answer(pending, user, "invalid")
        raise HTTPException(422, str(error)) from error
    return "saved"


def _log_answer(pending: PendingSecretRequest, user: ClientUser, status: str) -> None:
    logger.info(
        "secret_request_answered",
        request_id=pending.request_id,
        agent=pending.agent_name,
        service=pending.service,
        status=status,
        user_id=user.user_id,
    )
