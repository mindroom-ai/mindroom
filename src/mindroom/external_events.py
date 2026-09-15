"""Governed, durable external event delivery into the caller's Matrix room.

Provider plugins verify their own messages before calling this service. External
actor identifiers are descriptive provenance; the runtime requester owns every
resulting turn. Callers retry unfinished deliveries with the same source/event
identity. No autonomous replay worker or endpoint credential is required.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mindroom.authorization import is_sender_allowed_for_agent_reply_in_room
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import ORIGINAL_SENDER_KEY, PER_FIRE_THREAD_ROOT_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import EXTERNAL_TRIGGER_SOURCE_KIND
from mindroom.durable_write import create_directory_durable, write_json_file_durable
from mindroom.file_locks import async_exclusive_file_lock
from mindroom.matrix.client_delivery import DeliveredMatrixEvent, prepare_message_content, send_message_outcome
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.mentions import format_entity_mention
from mindroom.matrix.message_builder import build_message_content, markdown_to_html
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


class ExternalEventDeliveryError(RuntimeError):
    """Delivery was not confirmed; retain the source event for a later retry."""


class _Intent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any] | None
    transaction_id: str
    sender_id: str
    device_id: str
    matrix_event_id: str | None = None
    completed_at: int | None = None


class _Thread(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pending_event_key: str | None = None
    matrix_event_id: str | None = None
    updated_at: int


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    scope: tuple[str, str, str, str]
    events: dict[str, _Intent] = Field(default_factory=dict)
    threads: dict[str, _Thread] = Field(default_factory=dict)


_RETENTION_SECONDS = 8 * 86400


def _now() -> int:
    return int(time.time())


def _key(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def _text(value: str, name: str, limit: int) -> str:
    if not value.strip() or len(value) > limit:
        msg = f"{name} must be nonblank and at most {limit} characters."
        raise ExternalEventDeliveryError(msg)
    return value


def _read(path: Path, scope: tuple[str, str, str, str]) -> _State:
    if not path.exists():
        return _State(scope=scope)
    state = _State.model_validate_json(path.read_bytes())
    if (
        state.version != 1
        or state.scope != scope
        or any(
            thread.pending_event_key is not None and thread.pending_event_key not in state.events
            for thread in state.threads.values()
        )
        or any((intent.payload is None) != (intent.matrix_event_id is not None) for intent in state.events.values())
    ):
        msg = "External event store has invalid identity or thread state."
        raise ExternalEventDeliveryError(msg)
    cutoff = _now() - _RETENTION_SECONDS
    events = {
        key: intent
        for key, intent in state.events.items()
        if intent.completed_at is None or intent.completed_at >= cutoff
    }
    threads = {
        key: thread
        for key, thread in state.threads.items()
        if thread.pending_event_key is not None or thread.updated_at >= cutoff
    }
    if len(events) != len(state.events) or len(threads) != len(state.threads):
        state.events, state.threads = events, threads
        _write(path, state)
    return state


def _write(path: Path, state: _State) -> None:
    write_json_file_durable(path, state.model_dump(mode="json"), strict_atomic_replace=True)


async def _authorize(context: ToolRuntimeContext) -> None:
    config = context.current_config
    if (
        not context.requester_id.startswith("@")
        or ":" not in context.requester_id
        or context.requester_id == context.client.user_id
        or not is_human_requester_id(context.requester_id, config, context.runtime_paths)
        or not is_sender_allowed_for_agent_reply_in_room(
            context.requester_id,
            context.agent_name,
            config,
            context.room_id,
            context.runtime_paths,
            context.require_agent_reply_memberships(),
        )
    ):
        msg = "External event requester is not authorized for this agent and room."
        raise ExternalEventDeliveryError(msg)
    members = await get_room_members(context.client, context.room_id)
    canonical_members = {
        resolve_human_requester_alias(member, config, context.runtime_paths) for member in members or ()
    }
    if members is None or context.requester_id not in canonical_members or context.client.user_id not in members:
        msg = "External event requester and delivery agent must be joined to the room."
        raise ExternalEventDeliveryError(msg)


async def _send_intent(context: ToolRuntimeContext, path: Path, state: _State, event_key: str) -> str:
    intent = state.events[event_key]
    if intent.matrix_event_id is not None:
        return intent.matrix_event_id
    if (intent.sender_id, intent.device_id) != (context.client.user_id, context.client.device_id):
        msg = "Pending external event belongs to another Matrix device; delivery cannot be safely retried."
        raise ExternalEventDeliveryError(msg)
    assert intent.payload is not None
    outcome = await send_message_outcome(
        context.client,
        context.room_id,
        intent.payload,
        transaction_id=intent.transaction_id,
        content_is_prepared=True,
    )
    if not isinstance(outcome, DeliveredMatrixEvent):
        msg = "External event Matrix delivery is unconfirmed; retry the same source and event ID."
        raise ExternalEventDeliveryError(msg)
    intent.matrix_event_id = outcome.event_id
    intent.completed_at = _now()
    intent.payload = None
    for thread in state.threads.values():
        if thread.pending_event_key == event_key:
            thread.pending_event_key = None
            thread.matrix_event_id = outcome.event_id
            thread.updated_at = _now()
    await run_blocking_until_complete(_write, path, state)
    return outcome.event_id


def _content(
    context: ToolRuntimeContext,
    *,
    source: str,
    event_id: str,
    message: str,
    actor_id: str | None,
    title: str | None,
    data: dict[str, object] | None,
    thread_id: str | None,
) -> dict[str, Any]:
    plain_target, mentioned_ids, markdown_target = format_entity_mention(
        context.agent_name,
        context.current_config,
        context.runtime_paths,
    )
    body = f"{title}\n\n{message}" if title else message
    if data:
        body += (
            "\n\n```json\n" + json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False) + "\n```"
        )
    metadata: dict[str, object] = {
        SOURCE_KIND_KEY: EXTERNAL_TRIGGER_SOURCE_KIND,
        ORIGINAL_SENDER_KEY: context.requester_id,
        "io.mindroom.external_event.source": source,
        "io.mindroom.external_event.event_id": event_id,
    }
    if actor_id is not None:
        metadata["io.mindroom.external_event.actor_id"] = actor_id
    if thread_id is None:
        metadata[PER_FIRE_THREAD_ROOT_KEY] = True
    return build_message_content(
        body=f"{plain_target} {body}",
        formatted_body=markdown_to_html(f"{markdown_target} {body}"),
        mentioned_user_ids=mentioned_ids,
        thread_event_id=thread_id,
        latest_thread_event_id=thread_id,
        extra_content=metadata,
    )


async def _prepare_intent(
    context: ToolRuntimeContext,
    path: Path,
    state: _State,
    *,
    source: str,
    event_id: str,
    message: str,
    conversation_key: str | None,
    actor_id: str | None,
    title: str | None,
    data: dict[str, object] | None,
) -> _Intent:
    _text(message, "message", 32768)
    if conversation_key is not None:
        _text(conversation_key, "conversation_key", 256)
    if actor_id is not None:
        _text(actor_id, "actor_id", 256)
    if title is not None:
        _text(title, "title", 512)
    thread_key = _key(conversation_key) if conversation_key is not None else None
    thread_id = None
    if thread_key is not None and thread_key in state.threads:
        thread = state.threads[thread_key]
        thread_id = thread.matrix_event_id
        if thread.pending_event_key is not None:
            thread_id = await _send_intent(context, path, state, thread.pending_event_key)
        thread.updated_at = _now()
    content = _content(
        context,
        source=source,
        event_id=event_id,
        message=message,
        actor_id=actor_id,
        title=title,
        data=data,
        thread_id=thread_id,
    )
    if len(json.dumps(content, ensure_ascii=True, allow_nan=False).encode()) > 65536:
        msg = "External event payload exceeds 64 KiB."
        raise ExternalEventDeliveryError(msg)
    prepared = await prepare_message_content(context.client, context.room_id, content)
    if not isinstance(prepared, dict):
        msg = "External event payload could not be prepared."
        raise ExternalEventDeliveryError(msg)
    device_id = context.client.device_id
    if not device_id:
        msg = "External event delivery requires a known Matrix device."
        raise ExternalEventDeliveryError(msg)
    intent = _Intent(
        payload=prepared,
        transaction_id=f"external-event-{_key((state.scope, event_id))}",
        sender_id=context.client.user_id,
        device_id=device_id,
    )
    state.events[_key(event_id)] = intent
    if thread_key is not None and thread_id is None:
        state.threads[thread_key] = _Thread(pending_event_key=_key(event_id), updated_at=_now())
    await run_blocking_until_complete(_write, path, state)
    return intent


async def deliver_event(
    context: ToolRuntimeContext,
    *,
    source: str,
    event_id: str,
    message: str,
    conversation_key: str | None = None,
    actor_id: str | None = None,
    title: str | None = None,
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    """Deliver verified external text using the current requester's authority.

    ``source`` identifies a provider-qualified subscription. Event identity is
    scoped to requester, agent, room, and source, independently of script runs.
    The first accepted content is immutable, including on pending retries.
    Conversation keys are opaque and never interpreted as Matrix identifiers.
    Returns success only after Matrix acknowledgement and its durable receipt.
    Confirmed payloads are discarded immediately. Receipt tombstones last eight
    days; thread roots last eight days of activity. Pending intents never expire.
    """
    _text(source, "source", 128)
    _text(event_id, "event_id", 256)
    root = context.runtime_paths.control_state_root
    if root is None or context.orchestrator is None:
        msg = "External event delivery requires a managed primary runtime."
        raise ExternalEventDeliveryError(msg)
    if not context.client.user_id or not context.client.device_id:
        msg = "External event delivery requires a known Matrix sender and device."
        raise ExternalEventDeliveryError(msg)
    try:
        async with context.orchestrator.external_event_delivery_scope(
            context.transport_agent_name or context.agent_name,
            context.client,
        ):
            config = context.current_config
            context = replace(
                context,
                config=config,
                config_provider=None,
                requester_id=resolve_human_requester_alias(context.requester_id, config, context.runtime_paths),
            )
            scope = (context.requester_id, context.agent_name, context.room_id, source)
            directory = root / "external_events"
            path = directory / f"{_key(scope)}.json"
            await run_blocking_until_complete(lambda: create_directory_durable(directory, mode=0o700))
            async with async_exclusive_file_lock(path.with_suffix(".lock")):
                await _authorize(context)
                state = await run_blocking_until_complete(_read, path, scope)
                identity = _key(event_id)
                if identity in state.events:
                    intent = state.events[identity]
                    duplicate = intent.matrix_event_id is not None
                else:
                    intent = await _prepare_intent(
                        context,
                        path,
                        state,
                        source=source,
                        event_id=event_id,
                        message=message,
                        conversation_key=conversation_key,
                        actor_id=actor_id,
                        title=title,
                        data=data,
                    )
                    duplicate = False
                matrix_event_id = await _send_intent(context, path, state, identity)
                return {"status": "ok", "accepted": True, "duplicate": duplicate, "matrix_event_id": matrix_event_id}
    except (OSError, ValidationError, ValueError) as exc:
        msg = "External event state or payload is unavailable or invalid."
        raise ExternalEventDeliveryError(msg) from exc
