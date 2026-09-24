"""Durable preparation and receipts for explicitly keyed Matrix text sends."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.custom_tools.attachment_helpers import requester_joined_target_room
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.matrix.client_delivery import DeliveredMatrixEvent, prepare_message_content, send_message_outcome
from mindroom.requester_identity import resolve_human_requester_alias

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_RETENTION_SECONDS = 8 * 86400
_CLAIM_TIMEOUT_SECONDS = 60.0
_MAX_RETAINED_RECORDS = 10_000
_MAX_PENDING_RECORDS = 1_024
_MAX_STORE_BYTES = 16 * 1024 * 1024
# https://spec.matrix.org/latest/appendices/#event-ids limits IDs to 255 UTF-8 bytes.
_MAX_EVENT_ID_BYTES = 255
# Six JSON escape bytes per event-ID byte plus a float timestamp fit in 2 KiB.
# Reserve this for each pending row in addition to its current serialized size.
_RECEIPT_RESERVE_BYTES = 2048
_CAPACITY_ERROR = (
    "Matrix message send capacity is exhausted; retry existing keys or wait for completed receipts to expire."
)


class MatrixMessageIdempotencyError(RuntimeError):
    """Delivery has no confirmed durable receipt and must not be acknowledged."""


class _Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    payload: dict[str, Any] | None
    transaction_id: str
    sender_id: str
    device_id: str
    recipient: str | None
    recipient_user_id: str | None
    thread_id: str | None
    starts_thread: bool
    event_id: str | None = None
    completed_at: float | None = None


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = 1
    scope: tuple[str, str, str]
    sends: dict[str, _Intent] = Field(default_factory=dict)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def _serialized_size(payload: object) -> int:
    # Match write_json_file_durable's default JSON separators and ensure_ascii.
    return len(json.dumps(payload).encode("utf-8"))


def _pending_count(state: _State) -> int:
    return sum(intent.event_id is None for intent in state.sends.values())


def _check_admission(state: _State) -> None:
    if len(state.sends) >= _MAX_RETAINED_RECORDS or _pending_count(state) >= _MAX_PENDING_RECORDS:
        raise MatrixMessageIdempotencyError(_CAPACITY_ERROR)


def _write(path: Path, state: _State) -> None:
    payload = state.model_dump(mode="json")
    if _serialized_size(payload) > _MAX_STORE_BYTES:
        raise MatrixMessageIdempotencyError(_CAPACITY_ERROR)
    write_json_file_durable(path, payload, strict_atomic_replace=True)


def _read(path: Path, scope: tuple[str, str, str]) -> _State:
    if not path.exists():
        return _State(scope=scope)
    with path.open("rb") as source:
        raw = source.read(_MAX_STORE_BYTES + 1)
    if len(raw) > _MAX_STORE_BYTES:
        msg = "Matrix message receipt store exceeds the size limit."
        raise MatrixMessageIdempotencyError(msg)
    state = _State.model_validate_json(raw)
    if (
        state.version != 1
        or state.scope != scope
        or any(
            (intent.payload is None) != (intent.event_id is not None)
            or (intent.completed_at is not None) != (intent.event_id is not None)
            or not intent.sender_id
            or not intent.device_id
            or not intent.transaction_id
            for intent in state.sends.values()
        )
    ):
        msg = "Matrix message receipt store has invalid identity or delivery state."
        raise MatrixMessageIdempotencyError(msg)
    cutoff = time.time() - _RETENTION_SECONDS
    state.sends = {
        key: intent
        for key, intent in state.sends.items()
        if intent.completed_at is None or intent.completed_at >= cutoff
    }
    return state


@dataclass
class MatrixMessageSendClaim:
    """One locked send identity; the first prepared target and payload are immutable."""

    context: ToolRuntimeContext
    path: Path
    state: _State
    key: str

    @property
    def intent(self) -> _Intent | None:
        """Return the first prepared send, if one exists."""
        return self.state.sends.get(self.key)

    async def prepare(
        self,
        content: dict[str, Any],
        *,
        recipient: str | None,
        recipient_user_id: str | None,
        thread_id: str | None,
        starts_thread: bool,
    ) -> None:
        """Persist exactly the payload Matrix will receive before attempting delivery."""
        context = self.context
        assert context.client.device_id is not None
        prepared = await prepare_message_content(context.client, self.state.scope[2], content)
        if not isinstance(prepared, dict):
            msg = "Matrix message could not be prepared; retry with the same idempotency_key."
            raise MatrixMessageIdempotencyError(msg)
        self.state.sends[self.key] = _Intent(
            payload=prepared,
            transaction_id=f"matrix-message-{uuid4().hex}",
            sender_id=context.client.user_id,
            device_id=context.client.device_id,
            recipient=recipient,
            recipient_user_id=recipient_user_id,
            thread_id=thread_id,
            starts_thread=starts_thread,
        )
        reserved_size = (
            _serialized_size(self.state.model_dump(mode="json")) + _pending_count(self.state) * _RECEIPT_RESERVE_BYTES
        )
        if reserved_size > _MAX_STORE_BYTES:
            raise MatrixMessageIdempotencyError(_CAPACITY_ERROR)
        await run_blocking_until_complete(_write, self.path, self.state)

    async def deliver(self) -> tuple[str, str | None]:
        """Return success only after Matrix acknowledgement and durable receipt storage."""
        intent = self.intent
        assert intent is not None
        if (intent.sender_id, intent.device_id) != (self.context.client.user_id, self.context.client.device_id):
            msg = "Idempotent Matrix send belongs to another sender or device; refusing unsafe replay."
            raise MatrixMessageIdempotencyError(msg)
        if intent.event_id is None:
            # Seeing a pending row does not prove its original directory fsync
            # succeeded. Reaffirm its transaction before any transport attempt.
            await run_blocking_until_complete(_write, self.path, self.state)
            assert intent.payload is not None
            outcome = await send_message_outcome(
                self.context.client,
                self.state.scope[2],
                intent.payload,
                transaction_id=intent.transaction_id,
                content_is_prepared=True,
            )
            if not isinstance(outcome, DeliveredMatrixEvent):
                msg = "Matrix delivery is unconfirmed; retry with the same idempotency_key."
                raise MatrixMessageIdempotencyError(msg)
            if not outcome.event_id or len(outcome.event_id.encode("utf-8")) > _MAX_EVENT_ID_BYTES:
                msg = "Matrix returned an invalid event ID; retry with the same idempotency_key."
                raise MatrixMessageIdempotencyError(msg)
            intent.event_id = outcome.event_id
            intent.completed_at = time.time()
            intent.payload = None
        # A prior replace may have succeeded before its directory fsync failed.
        # Re-publish even an existing receipt before reporting durable success.
        await run_blocking_until_complete(_write, self.path, self.state)
        return intent.event_id, intent.event_id if intent.starts_thread else intent.thread_id


@asynccontextmanager
async def _claim_matrix_message_send(
    context: ToolRuntimeContext,
    room_id: str,
    idempotency_key: str,
) -> AsyncIterator[MatrixMessageSendClaim]:
    """Serialize one requester/agent/room store across tasks and processes."""
    root = context.runtime_paths.control_state_root
    if root is None or not context.client.user_id or not context.client.device_id:
        msg = "Idempotent Matrix sends require durable control storage and a known Matrix sender and device."
        raise MatrixMessageIdempotencyError(msg)
    config = context.current_config
    requester = resolve_human_requester_alias(context.requester_id, config, context.runtime_paths)
    scope = (requester, context.agent_name, room_id)
    directory = root / "matrix_message_sends"
    path = directory / f"{_digest(scope)}.json"
    await run_blocking_until_complete(lambda: create_directory_durable(directory, mode=0o700))
    async with async_exclusive_file_lock(path.with_suffix(".lock")):
        config = context.current_config
        if resolve_human_requester_alias(context.requester_id, config, context.runtime_paths) != requester:
            msg = "Requester identity changed while waiting to send; retry with current authorization."
            raise MatrixMessageIdempotencyError(msg)
        context = replace(context, config=config, config_provider=None, requester_id=requester)
        if not is_sender_allowed_for_responder(
            requester,
            context.agent_name,
            room_id,
            context.config,
            context.runtime_paths,
            context.require_agent_reply_memberships(),
        ) or not await requester_joined_target_room(context, room_id):
            msg = "Not authorized to send to the target room."
            raise MatrixMessageIdempotencyError(msg)
        state = await run_blocking_until_complete(_read, path, scope)
        key = _digest(idempotency_key)
        if key not in state.sends:
            _check_admission(state)
        yield MatrixMessageSendClaim(context, path, state, key)


@asynccontextmanager
async def claim_matrix_message_send(
    context: ToolRuntimeContext,
    room_id: str,
    idempotency_key: str,
) -> AsyncIterator[MatrixMessageSendClaim]:
    """Bound lock wait, preparation, and transport while allowing durable writes to settle."""
    try:
        async with asyncio.timeout(_CLAIM_TIMEOUT_SECONDS):
            async with _claim_matrix_message_send(context, room_id, idempotency_key) as claim:
                yield claim
    except TimeoutError as exc:
        msg = "Idempotent Matrix send timed out; retry with the same idempotency_key."
        raise MatrixMessageIdempotencyError(msg) from exc
