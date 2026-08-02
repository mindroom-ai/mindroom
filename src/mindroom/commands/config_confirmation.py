"""Configuration change confirmation system using Matrix reactions with persistence."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import CONFIG_CONFIRMATION_REACTION_KEY
from mindroom.delivery_gateway import SendTextRequest
from mindroom.logging_config import get_logger
from mindroom.matrix.client_thread_history import find_response_event_ids_via_room_messages
from mindroom.matrix.message_builder import build_reaction_content

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from mindroom.bot import AgentBot

logger = get_logger(__name__)

# Event type for pending config changes in Matrix state
_PENDING_CONFIG_EVENT_TYPE = "com.mindroom.pending.config"

# Maximum age for pending confirmations (24 hours)
_MAX_PENDING_AGE_HOURS = 24


@dataclass
class _PendingConfigChange:
    """Represents a pending configuration change awaiting confirmation."""

    room_id: str
    thread_id: str | None
    config_path: str
    old_value: Any
    new_value: Any
    requester: str  # User who requested the change
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decision_event_id: str | None = None
    decision_key: str | None = None
    decision_execution_started: bool = False
    decision_response_text: str | None = None

    def is_expired(self) -> bool:
        """Check if this pending change has expired."""
        if self.decision_event_id is not None:
            return False
        age = datetime.now(UTC) - self.created_at
        return age.total_seconds() > _MAX_PENDING_AGE_HOURS * 3600

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for Matrix state storage."""
        return {
            "room_id": self.room_id,
            "thread_id": self.thread_id,
            "config_path": self.config_path,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "requester": self.requester,
            "created_at": self.created_at.isoformat(),
            "decision_event_id": self.decision_event_id,
            "decision_key": self.decision_key,
            "decision_execution_started": self.decision_execution_started,
            "decision_response_text": self.decision_response_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _PendingConfigChange:
        """Create from dictionary retrieved from Matrix state."""
        # Parse the ISO format datetime
        created_at = datetime.fromisoformat(data["created_at"])

        return cls(
            room_id=data["room_id"],
            thread_id=data.get("thread_id"),
            config_path=data["config_path"],
            old_value=data["old_value"],
            new_value=data["new_value"],
            requester=data["requester"],
            created_at=created_at,
            decision_event_id=data.get("decision_event_id"),
            decision_key=data.get("decision_key"),
            decision_execution_started=data.get("decision_execution_started") is True,
            decision_response_text=data.get("decision_response_text"),
        )


# Track pending configuration changes by event_id
_pending_changes: dict[str, _PendingConfigChange] = {}


@dataclass
class _PendingChangeLock:
    """One borrowed per-preview serialization lock."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    borrowers: int = 0


_pending_change_locks: dict[str, _PendingChangeLock] = {}


@asynccontextmanager
async def _pending_change_lock(event_id: str) -> AsyncIterator[None]:
    """Serialize one preview while retaining locks only for active borrowers."""
    entry = _pending_change_locks.get(event_id)
    if entry is None:
        entry = _PendingChangeLock()
        _pending_change_locks[event_id] = entry
    entry.borrowers += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.borrowers -= 1
        if entry.borrowers == 0 and _pending_change_locks.get(event_id) is entry:
            _pending_change_locks.pop(event_id)


def _get_pending_change(event_id: str) -> _PendingConfigChange | None:
    """Get a pending configuration change by event ID.

    Args:
        event_id: The event ID of the confirmation message

    Returns:
        The pending change or None if not found

    """
    return _pending_changes.get(event_id)


def _remove_pending_change(event_id: str) -> _PendingConfigChange | None:
    """Remove and return a pending configuration change.

    Args:
        event_id: The event ID of the confirmation message

    Returns:
        The removed pending change or None if not found

    """
    return _pending_changes.pop(event_id, None)


async def _store_pending_change_in_matrix(
    client: nio.AsyncClient,
    event_id: str,
    pending_change: _PendingConfigChange,
) -> None:
    """Store pending config change in Matrix room state for persistence.

    Args:
        client: The Matrix client
        event_id: The event ID of the confirmation message
        pending_change: The pending configuration change

    """
    response = await client.room_put_state(
        room_id=pending_change.room_id,
        event_type=_PENDING_CONFIG_EVENT_TYPE,
        content=pending_change.to_dict(),
        state_key=event_id,
    )
    if not isinstance(response, nio.RoomPutStateResponse):
        msg = f"Failed to store pending config change in Matrix state: {response}"
        raise RuntimeError(msg)  # noqa: TRY004
    logger.info(
        "Stored pending config change in Matrix state",
        event_id=event_id,
        room_id=pending_change.room_id,
        config_path=pending_change.config_path,
    )


async def _commit_checkpoint(
    client: nio.AsyncClient,
    preview_event_id: str,
    checkpoint: _PendingConfigChange,
) -> _PendingConfigChange:
    """Persist one checkpoint before publishing its in-memory mirror."""
    await _store_pending_change_in_matrix(client, preview_event_id, checkpoint)
    _pending_changes[preview_event_id] = checkpoint
    return checkpoint


async def _remove_pending_change_from_matrix(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
) -> None:
    """Remove pending config change from Matrix room state.

    Args:
        client: The Matrix client
        room_id: The room ID
        event_id: The event ID of the confirmation message

    """
    response = await client.room_put_state(
        room_id=room_id,
        event_type=_PENDING_CONFIG_EVENT_TYPE,
        content={},
        state_key=event_id,
    )
    if not isinstance(response, nio.RoomPutStateResponse):
        msg = f"Failed to remove pending config change from Matrix state: {response}"
        raise RuntimeError(msg)  # noqa: TRY004
    logger.info(
        "Removed pending config change from Matrix state",
        event_id=event_id,
        room_id=room_id,
    )


async def _resolve_pending_change(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
) -> _PendingConfigChange | None:
    """Resolve one pending change from memory or its authoritative Matrix state."""
    pending_change = _get_pending_change(event_id)
    if pending_change is not None:
        return pending_change

    response = await client.room_get_state_event(
        room_id,
        _PENDING_CONFIG_EVENT_TYPE,
        event_id,
    )
    if isinstance(response, nio.RoomGetStateEventError) and response.status_code == "M_NOT_FOUND":
        return None
    if not isinstance(response, nio.RoomGetStateEventResponse):
        msg = f"Failed to resolve pending config change from Matrix state: {response}"
        raise RuntimeError(msg)  # noqa: TRY004
    if not response.content:
        return None
    return await _restore_pending_change(client, room_id, event_id, response.content)


async def resolve_reaction_pending_change(
    client: nio.AsyncClient,
    room_id: str,
    event: nio.ReactionEvent,
    *,
    enabled: bool,
) -> _PendingConfigChange | None:
    """Resolve config state only for confirmation-shaped router reactions."""
    if not enabled or event.key not in {"✅", "❌"}:
        return None
    return await _resolve_pending_change(client, room_id, event.reacts_to)


async def _restore_pending_change(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    content: dict[str, Any],
) -> _PendingConfigChange | None:
    """Restore one unexpired Matrix-backed pending change into memory."""
    pending_change = _PendingConfigChange.from_dict(content)
    if pending_change.is_expired():
        logger.info(
            "Skipping expired pending config change",
            event_id=event_id,
            created_at=pending_change.created_at,
        )
        await _remove_pending_change_from_matrix(client, room_id, event_id)
        return None
    _pending_changes[event_id] = pending_change
    logger.info(
        "Restored pending config change",
        event_id=event_id,
        config_path=pending_change.config_path,
        requester=pending_change.requester,
    )
    return pending_change


async def restore_pending_changes(client: nio.AsyncClient, room_id: str) -> int:
    """Restore pending config changes from Matrix state after bot restart.

    Args:
        client: The Matrix client
        room_id: The room ID to restore from

    Returns:
        Number of pending changes restored

    """
    try:
        response = await client.room_get_state(room_id)
        if not isinstance(response, nio.RoomGetStateResponse):
            logger.warning(
                "Failed to get room state for pending config changes",
                room_id=room_id,
                error=str(response),
            )
            return 0

        restored_count = 0
        expired_count = 0

        for event in response.events:
            if event.get("type") != _PENDING_CONFIG_EVENT_TYPE:
                continue

            state_key = event.get("state_key")
            content = event.get("content", {})

            # Skip empty content (deleted state events)
            if not content:
                continue

            try:
                pending_change = await _restore_pending_change(client, room_id, state_key, content)
                if pending_change is None:
                    expired_count += 1
                else:
                    restored_count += 1
            except Exception:
                logger.exception(
                    "Error restoring pending config change",
                    event_id=state_key,
                )

        if restored_count > 0 or expired_count > 0:
            logger.info(
                "Completed restoration of pending config changes",
                room_id=room_id,
                restored=restored_count,
                expired=expired_count,
            )

        return restored_count  # noqa: TRY300

    except Exception:
        logger.exception("Error restoring pending config changes from Matrix state")
        return 0


async def _add_confirmation_reactions(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
) -> None:
    """Add confirmation reaction buttons to a config change message.

    Args:
        client: The Matrix client
        room_id: The room ID
        event_id: The event ID of the message to add reactions to

    """
    for reaction_name, reaction_key in (("confirm", "✅"), ("cancel", "❌")):
        transaction_id = (
            "mindroom-config-reaction-"
            + hashlib.sha256(
                f"{event_id}\0{reaction_key}".encode(),
            ).hexdigest()
        )
        response = await client.room_send(
            room_id=room_id,
            message_type="m.reaction",
            content=build_reaction_content(event_id, reaction_key),
            tx_id=transaction_id,
            ignore_unverified_devices=True,
        )
        if isinstance(response, nio.RoomSendError) and response.status_code == "M_DUPLICATE_ANNOTATION":
            continue
        if not isinstance(response, nio.RoomSendResponse):
            msg = f"Failed to add {reaction_name} config confirmation reaction: {response}"
            raise RuntimeError(msg)  # noqa: TRY004


async def _confirmation_response_ids(
    client: nio.AsyncClient,
    room_id: str,
    preview_event_id: str,
    *,
    decision_event_id: str | None = None,
) -> tuple[str, ...]:
    """Return config-decision responses visibly owned by one preview."""
    if client.user_id is None:
        msg = "Config confirmation recovery requires a Matrix user ID"
        raise RuntimeError(msg)
    response_ids = await find_response_event_ids_via_room_messages(
        client,
        room_id,
        response_sender=client.user_id,
        source_event_ids=(preview_event_id,),
        response_source_filter=lambda source: (
            isinstance(content := source.get("content"), dict)
            and isinstance(reaction_id := content.get(CONFIG_CONFIRMATION_REACTION_KEY), str)
            and bool(reaction_id)
            and (decision_event_id is None or reaction_id == decision_event_id)
        ),
    )
    if len(response_ids) > 1:
        msg = "Config confirmation recovery found multiple visible responses"
        raise RuntimeError(msg)
    return tuple(response_ids)


async def _has_visible_confirmation_response(
    client: nio.AsyncClient,
    room_id: str,
    event: nio.ReactionEvent,
) -> bool:
    """Return whether durable Matrix output proves this decision was consumed."""
    return bool(
        await _confirmation_response_ids(
            client,
            room_id,
            event.reacts_to,
            decision_event_id=event.event_id,
        ),
    )


async def recover_confirmation_setup(
    client: nio.AsyncClient,
    room_id: str,
    preview_event_id: str,
) -> bool:
    """Recover a preview whose pending state or completed decision already exists."""
    async with _pending_change_lock(preview_event_id):
        pending_change = await _resolve_pending_change(client, room_id, preview_event_id)
        if pending_change is not None:
            if pending_change.decision_event_id is None:
                await _add_confirmation_reactions(client, room_id, preview_event_id)
            return True
        return bool(await _confirmation_response_ids(client, room_id, preview_event_id))


async def ensure_pending_change(
    client: nio.AsyncClient,
    *,
    event_id: str,
    room_id: str,
    thread_id: str | None,
    config_path: str,
    old_value: Any,  # noqa: ANN401
    new_value: Any,  # noqa: ANN401
    requester: str,
) -> None:
    """Persist one preview exactly once before exposing its reaction buttons."""
    async with _pending_change_lock(event_id):
        pending_change = await _resolve_pending_change(client, room_id, event_id)
        if pending_change is not None:
            if pending_change.decision_event_id is None:
                await _add_confirmation_reactions(client, room_id, event_id)
            return
        if await _confirmation_response_ids(client, room_id, event_id):
            return

        pending_change = _PendingConfigChange(
            room_id=room_id,
            thread_id=thread_id,
            config_path=config_path,
            old_value=old_value,
            new_value=new_value,
            requester=requester,
        )
        await _commit_checkpoint(client, event_id, pending_change)
        await _add_confirmation_reactions(client, room_id, event_id)
        logger.info(
            "Registered pending config change",
            event_id=event_id,
            path=config_path,
            requester=requester,
        )


async def _ensure_decision_checkpoint(
    bot: AgentBot,
    event: nio.ReactionEvent,
    pending_change: _PendingConfigChange,
) -> _PendingConfigChange | None:
    """Freeze the winning reaction and its authorization before mutation."""
    if pending_change.decision_event_id is not None:
        return pending_change if pending_change.decision_event_id == event.event_id else None

    assert bot.client is not None
    authorization = bot.config.authorization
    resolved_sender = authorization.resolve_alias(event.sender)
    if resolved_sender != pending_change.requester:
        logger.debug(
            "Ignoring config reaction from non-requester",
            sender=event.sender,
            requester=pending_change.requester,
            resolved_sender=resolved_sender,
        )
        return None
    if event.sender == bot.client.user_id or event.key not in {"✅", "❌"}:
        return None

    response_text = None
    if event.key == "❌":
        response_text = "❌ Configuration change cancelled."
    elif not authorization.config_command_enabled:
        response_text = "❌ Config command disabled."
    elif resolved_sender not in authorization.global_users:
        response_text = "❌ Admin only."

    checkpoint = replace(
        pending_change,
        decision_event_id=event.event_id,
        decision_key=event.key,
        decision_response_text=response_text,
    )
    return await _commit_checkpoint(bot.client, event.reacts_to, checkpoint)


async def _response_for_checkpointed_decision(
    bot: AgentBot,
    preview_event_id: str,
    pending_change: _PendingConfigChange,
) -> tuple[_PendingConfigChange, str]:
    """Checkpoint one decision result without repeating an ambiguous config write."""
    if pending_change.decision_response_text is not None:
        return pending_change, pending_change.decision_response_text
    if pending_change.decision_key != "✅":
        msg = "Config confirmation decision checkpoint is incomplete"
        raise RuntimeError(msg)
    assert bot.client is not None
    if pending_change.decision_execution_started:
        response_text = (
            "⚠️ The configuration change was interrupted after application began, so its outcome is uncertain. "
            "Inspect the current configuration before making another change."
        )
        checkpoint = await _commit_checkpoint(
            bot.client,
            preview_event_id,
            replace(pending_change, decision_response_text=response_text),
        )
        return checkpoint, response_text

    started_checkpoint = await _commit_checkpoint(
        bot.client,
        preview_event_id,
        replace(pending_change, decision_execution_started=True),
    )
    from mindroom.commands.config_commands import apply_config_change  # noqa: PLC0415

    response_text = await apply_config_change(
        pending_change.config_path,
        pending_change.new_value,
        runtime_paths=bot.runtime_paths,
    )
    completed_checkpoint = await _commit_checkpoint(
        bot.client,
        preview_event_id,
        replace(started_checkpoint, decision_response_text=response_text),
    )
    return completed_checkpoint, response_text


async def resume_committed_confirmation(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
    pending_change: _PendingConfigChange,
) -> None:
    """Resume only a decision already committed before current authorization changed."""
    if pending_change.decision_event_id == event.event_id:
        await handle_confirmation_reaction(bot, room, event)


async def handle_confirmation_reaction(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
) -> None:
    """Serialize and durably complete one config-confirmation decision."""
    assert bot.client is not None
    assert bot.client.user_id is not None
    preview_event_id = event.reacts_to
    async with _pending_change_lock(preview_event_id):
        pending_change = await _resolve_pending_change(bot.client, room.room_id, preview_event_id)
        if pending_change is None:
            return
        if pending_change.decision_event_id is not None and pending_change.decision_event_id != event.event_id:
            return

        if await _has_visible_confirmation_response(bot.client, room.room_id, event):
            await _remove_pending_change_from_matrix(bot.client, pending_change.room_id, preview_event_id)
            _remove_pending_change(preview_event_id)
            return

        pending_change = await _ensure_decision_checkpoint(bot, event, pending_change)
        if pending_change is None:
            return
        pending_change, response_text = await _response_for_checkpointed_decision(
            bot,
            preview_event_id,
            pending_change,
        )

        target = bot._conversation_resolver.build_message_target(
            room_id=room.room_id,
            thread_id=pending_change.thread_id,
            reply_to_event_id=preview_event_id,
        )
        response_event_id = await bot._delivery_gateway.send_text(
            SendTextRequest(
                target=target,
                response_text=response_text,
                skip_mentions=True,
                extra_content={CONFIG_CONFIRMATION_REACTION_KEY: event.event_id},
            ),
        )
        if response_event_id is None:
            msg = "Failed to send config confirmation response"
            raise RuntimeError(msg)

        await _remove_pending_change_from_matrix(bot.client, pending_change.room_id, preview_event_id)
        _remove_pending_change(preview_event_id)
