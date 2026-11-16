"""The card an agent's secret request posts, and the wait for the requester's answer to it.

The tools that ask for a secret share this: they decide who may be asked and what the card says, then post it here
and wait. The answer goes from MindRoom Chat to the backend route and never passes through a tool, Matrix, or the
model.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger
from mindroom.matrix.client_delivery import edit_message_result, send_message_result
from mindroom.matrix.message_builder import build_message_content
from mindroom.matrix_openid import homeserver_openid_binding, openid_audience
from mindroom.origins import allowed_client_origins
from mindroom.runtime_env_policy import COMPUTER_ALLOWED_ORIGINS_ENV
from mindroom.secret_requests import (
    SECRET_REQUEST_CONTENT_KEY,
    TemporaryTarget,
    secret_request_body,
    secret_request_content,
    secret_request_log_fields,
    secret_request_registry,
    temporary_secret_body,
)
from mindroom.tool_jobs.control import current_job_id, job_checkpoint
from mindroom.tool_jobs.runtime import get_background_runtime

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.constants import RuntimePaths
    from mindroom.secret_requests import KeyTarget, PendingSecretRequest, SecretRequestOutcome, SecretRequestStatus
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

_MAX_REASON_LENGTH = 500
# How long a request whose answer the route claimed before the wait ended may still take to save (a local file write).
_SAVE_GRACE_SECONDS = 30


@dataclass(frozen=True)
class SecretRequestCard:
    """What a secret request card shows, who answers it, and what its answer becomes.

    Never a value.
    """

    requester_id: str
    agent_display_name: str
    reason: str
    target: KeyTarget | TemporaryTarget
    # A key card's service as people read its name, and whether the service already has a key the answer replaces.
    display_name: str = ""
    replaces: bool = False

    def message(
        self,
        request: PendingSecretRequest,
        status: SecretRequestStatus,
        *,
        thread_id: str | None = None,
        reply_to_event_id: str | None = None,
        latest_thread_event_id: str | None = None,
    ) -> dict[str, Any]:
        """Return the card's message content in `status`, in the thread when `thread_id` is given."""
        if isinstance(self.target, TemporaryTarget):
            body = temporary_secret_body(status, agent_display_name=self.agent_display_name, name=self.target.name)
        else:
            body = secret_request_body(
                status,
                agent_display_name=self.agent_display_name,
                display_name=self.display_name,
            )
        metadata = secret_request_content(
            request,
            display_name=self.display_name,
            reason=self.reason,
            status=status,
            replaces=self.replaces,
        )
        return build_message_content(
            body,
            thread_event_id=thread_id,
            reply_to_event_id=reply_to_event_id,
            latest_thread_event_id=latest_thread_event_id,
            extra_content={"msgtype": "m.notice", SECRET_REQUEST_CONTENT_KEY: metadata},
        )


def card_reason(reason: str) -> str | None:
    """Return the reason a card shows, cut to its limit, or None when the agent gave none."""
    return reason.strip()[:_MAX_REASON_LENGTH] or None


async def chat_refusal(runtime_paths: RuntimePaths, *, noun: str) -> str | None:
    """Return why MindRoom Chat could not answer a card here, or None: it signs in to this API with a client session.

    `noun` names what the card asks for, a key or a secret.
    """
    if not allowed_client_origins(runtime_paths, COMPUTER_ALLOWED_ORIGINS_ENV):
        return (
            "MindRoom Chat may not call this MindRoom (no origin in `MINDROOM_COMPUTER_ALLOWED_ORIGINS`), so nobody "
            "could answer a card and none was posted."
        )
    if openid_audience(runtime_paths) is None:
        return (
            "MindRoom has no origin to bind MindRoom Chat's sign-in to (set `MINDROOM_PUBLIC_URL`), so Chat cannot "
            f"send a {noun} here and no card was posted."
        )
    if await homeserver_openid_binding(runtime_paths) is not True:
        return (
            "MindRoom could not confirm that the homeserver binds OpenID tokens to this deployment, which MindRoom "
            f"Chat needs to send a {noun} here, so no card was posted."
        )
    return None


async def ask_with_card(
    context: ToolRuntimeContext,
    card: SecretRequestCard,
    *,
    agent_name: str,
) -> PendingSecretRequest | str:
    """Post `card` in the call's conversation and wait for the requester's answer, or say why no card was posted.

    The request returned is settled: its `outcome` is done and the posted card shows it.
    """
    thread_id = context.resolved_thread_id
    latest_thread_event_id = context.reply_to_event_id
    if thread_id is not None and latest_thread_event_id is None:
        latest_thread_event_id = await context.conversation_reader.latest_thread_event_id(
            room_id=context.room_id,
            thread_id=thread_id,
        )
        if latest_thread_event_id is None:
            return "The thread's latest message could not be found, so no card was posted."
    # A Stop of the reply that started this call's job, before the card is posted, posts none.
    job_checkpoint(approval=True)
    registry = secret_request_registry()
    request = registry.register(
        room_id=context.room_id,
        requester_id=card.requester_id,
        agent_name=agent_name,
        target=card.target,
        now=time.time(),
    )
    try:
        content = card.message(
            request,
            "pending",
            thread_id=thread_id,
            reply_to_event_id=context.reply_to_event_id if thread_id is not None else None,
            latest_thread_event_id=latest_thread_event_id if thread_id is not None else None,
        )
        delivered = await send_message_result(context.client, context.room_id, content, operation="secret_request")
        if delivered is None:
            return "The card could not be sent to the conversation."
        logger.info(
            "secret_request_posted",
            request_id=request.request_id,
            agent=agent_name,
            room_id=context.room_id,
            **secret_request_log_fields(card.target),
        )
        await _await_outcome(context, delivered.event_id, card, request)
    finally:
        registry.discard(request.request_id)
    return request


async def _edit_card(
    context: ToolRuntimeContext,
    event_id: str,
    card: SecretRequestCard,
    request: PendingSecretRequest,
    status: SecretRequestStatus,
) -> None:
    """Replace the posted card with its final `status`; a failed edit is only logged, never the reason."""
    content = card.message(request, status)
    try:
        delivered = await edit_message_result(context.client, context.room_id, event_id, content, content["body"])
    except Exception as exc:
        logger.warning(
            "secret_request_edit_failed",
            request_id=request.request_id,
            status=status,
            error_type=type(exc).__name__,
        )
    else:
        if delivered is None:
            logger.warning("secret_request_edit_failed", request_id=request.request_id, status=status)


async def _await_outcome(
    context: ToolRuntimeContext,
    event_id: str,
    card: SecretRequestCard,
    request: PendingSecretRequest,
) -> SecretRequestOutcome:
    """Wait for the requester's answer until the request expires, then edit the card; a stopped reply ends it too.

    An answer the route claimed before the wait ended gets up to `_SAVE_GRACE_SECONDS` more to be saved, so a key
    stored just after expiry is reported and shown as saved. The shield keeps the registry's future intact when a wait
    ends. A timeout or cancellation settles the request before the edit, so no answer is stored while the card turns
    expired, and a cancellation propagates after it.
    """
    try:
        async with _job_awaits_requester(context.runtime_paths):
            outcome = await asyncio.wait_for(
                asyncio.shield(request.outcome),
                max(0.0, request.expires_at - time.time()),
            )
    except TimeoutError:
        outcome = await _answer_in_flight(request)
        secret_request_registry().resolve(request.request_id, outcome)
    except asyncio.CancelledError:
        outcome = await _answer_in_flight(request)
        secret_request_registry().resolve(request.request_id, outcome)
        await _edit_card(context, event_id, card, request, outcome)
        raise
    await _edit_card(context, event_id, card, request, outcome)
    return outcome


@contextlib.asynccontextmanager
async def _job_awaits_requester(runtime_paths: RuntimePaths) -> AsyncIterator[None]:
    """Mark the background job this call runs in, if any, as awaiting a human decision while its card is open.

    A Stop of the reply then cancels the job, as it does a job waiting on approval cards; one that came first cancels
    it here. Outside a job, the Stop cancels the call itself.
    """
    job_id = current_job_id()
    runtime = None if job_id is None else get_background_runtime(runtime_paths)
    if job_id is None or runtime is None:
        yield
        return
    async with runtime.human_decision_wait(job_id):
        job_checkpoint(approval=True)
        yield


async def _answer_in_flight(request: PendingSecretRequest) -> SecretRequestOutcome:
    """Return the answer that arrived or is being saved as the wait ended, else `expired`.

    A save that fails gives its claim back, and after expiry nobody can claim again, so the wait ends there too.
    """
    if request.claimed and not request.outcome.done():
        released = asyncio.ensure_future(request.claim_released.wait())
        try:
            await asyncio.wait(
                {request.outcome, released},
                timeout=_SAVE_GRACE_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            released.cancel()
    return request.outcome.result() if request.outcome.done() else "expired"
