"""Visibility and recovery for undecryptable encrypted Matrix events.

nio surfaces encrypted timeline events it cannot decrypt as ``MegolmEvent``.
Without a registered callback those events vanish silently: the agent never
answers and the logs show nothing, which makes wedged encryption sessions
impossible to diagnose.
This module logs each failure, sends a Matrix room-key request so the session
can self-heal when the sender's client honors key requests, and posts one
rate-limited visible notice per (room, session) so the user is not left
talking to an agent that appears to ignore them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nio.exceptions import LocalProtocolError

from mindroom.constants import ROUTER_AGENT_NAME, tracking_dir
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_NOTICE_LEDGER_FILENAME = "e2ee_decrypt_notices.json"
_NOTICE_LEDGER_MAX_ENTRIES = 1000
_DECRYPT_FAILURE_NOTICE_BODY = (
    "⚠️ I couldn't decrypt your last message. "
    "I've requested the key; if this keeps happening, please send a new message."
)


@dataclass
class E2EEStats:
    """Process-wide counters for encrypted-event handling."""

    decrypt_failures: int = 0
    key_requests_sent: int = 0
    notices_sent: int = 0
    decrypt_failures_by_room: dict[str, int] = field(default_factory=dict)

    def record_failure(self, room_id: str) -> None:
        """Count one undecryptable event."""
        self.decrypt_failures += 1
        self.decrypt_failures_by_room[room_id] = self.decrypt_failures_by_room.get(room_id, 0) + 1

    def as_dict(self) -> dict[str, int]:
        """Return the global counters for health reporting."""
        return {
            "decrypt_failures": self.decrypt_failures,
            "key_requests_sent": self.key_requests_sent,
            "notices_sent": self.notices_sent,
        }


_stats = E2EEStats()


def e2ee_stats() -> E2EEStats:
    """Return the process-wide encrypted-event counters."""
    return _stats


def _notice_ledger_path(runtime_paths: RuntimePaths) -> Path:
    return tracking_dir(runtime_paths) / _NOTICE_LEDGER_FILENAME


_notice_ledgers: dict[Path, list[str]] = {}


def _load_notice_ledger(ledger_path: Path) -> list[str]:
    cached = _notice_ledgers.get(ledger_path)
    if cached is not None:
        return cached
    entries: list[str] = []
    if ledger_path.is_file():
        try:
            loaded = json.loads(ledger_path.read_text(encoding="utf-8"))
            if isinstance(loaded, list):
                entries = [entry for entry in loaded if isinstance(entry, str)]
        except (OSError, json.JSONDecodeError):
            logger.warning("e2ee_notice_ledger_unreadable", path=str(ledger_path))
    _notice_ledgers[ledger_path] = entries
    return entries


def _notice_already_sent(runtime_paths: RuntimePaths, room_id: str, session_id: str) -> bool:
    return f"{room_id}|{session_id}" in _load_notice_ledger(_notice_ledger_path(runtime_paths))


def _record_notice_sent(runtime_paths: RuntimePaths, room_id: str, session_id: str) -> None:
    ledger_path = _notice_ledger_path(runtime_paths)
    entries = _load_notice_ledger(ledger_path)
    entries.append(f"{room_id}|{session_id}")
    del entries[:-_NOTICE_LEDGER_MAX_ENTRIES]
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(entries), encoding="utf-8")
    except OSError:
        logger.warning("e2ee_notice_ledger_write_failed", path=str(ledger_path))


def _managed_bot_user_ids(runtime_paths: RuntimePaths) -> tuple[set[str], str | None]:
    """Return all managed bot Matrix IDs plus the router's Matrix ID from persisted state."""
    # why-lazy: matrix.state pulls in config helpers that are heavy at module import time.
    from mindroom.matrix.state import MatrixState  # noqa: PLC0415

    state = MatrixState.load(runtime_paths=runtime_paths)
    bot_user_ids: set[str] = set()
    router_user_id: str | None = None
    for key, account in state.accounts.items():
        if not account.domain:
            continue
        user_id = f"@{account.username}:{account.domain}"
        bot_user_ids.add(user_id)
        if key == f"agent_{ROUTER_AGENT_NAME}":
            router_user_id = user_id
    return bot_user_ids, router_user_id


def _is_elected_notifier(
    *,
    agent_name: str,
    own_user_id: str | None,
    room_member_ids: set[str],
    runtime_paths: RuntimePaths,
) -> bool:
    """Return whether this bot should post the visible decrypt-failure notice.

    The router speaks for rooms it is in; otherwise a bot only speaks when it
    is the sole managed bot in the room, so multi-agent rooms never storm.
    """
    bot_user_ids, router_user_id = _managed_bot_user_ids(runtime_paths)
    if router_user_id is not None and router_user_id in room_member_ids:
        return agent_name == ROUTER_AGENT_NAME
    other_bots = (room_member_ids & bot_user_ids) - ({own_user_id} if own_user_id else set())
    return not other_bots


async def _send_decrypt_failure_notice(client: nio.AsyncClient, room_id: str) -> bool:
    # why-lazy: client_delivery imports Matrix formatting helpers that import config at module import time.
    from mindroom.matrix.client_delivery import send_message_result  # noqa: PLC0415

    delivered = await send_message_result(
        client,
        room_id,
        {"msgtype": "m.notice", "body": _DECRYPT_FAILURE_NOTICE_BODY},
        operation="decrypt_failure_notice",
    )
    return delivered is not None


async def handle_decrypt_failure(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: nio.MegolmEvent,
    *,
    agent_name: str,
    runtime_paths: RuntimePaths,
) -> None:
    """Log one undecryptable Megolm event, request its key, and notify the room once."""
    session_id = event.session_id
    assert session_id is not None  # schema-required for parsed MegolmEvents
    already_requested = session_id in client.outgoing_key_requests
    _stats.record_failure(room.room_id)
    logger.warning(
        "matrix_event_decryption_failed",
        room_id=room.room_id,
        event_id=event.event_id,
        sender=event.sender,
        sender_device_id=event.device_id,
        session_id=session_id,
        agent=agent_name,
        key_request_already_sent=already_requested,
        hint=(
            "The sending client did not share this Megolm session with the bot's device. "
            "The bot requests the room key once per session; if the sender's client does "
            "not honor the request, ask the sender to send a new message."
        ),
    )
    if not already_requested:
        try:
            await client.request_room_key(event)
            _stats.key_requests_sent += 1
        except LocalProtocolError:
            # A concurrent callback for the same session already requested the key.
            logger.debug(
                "matrix_room_key_request_already_pending",
                room_id=room.room_id,
                session_id=session_id,
            )

    if _notice_already_sent(runtime_paths, room.room_id, session_id):
        return
    if not _is_elected_notifier(
        agent_name=agent_name,
        own_user_id=client.user_id,
        room_member_ids=set(room.users),
        runtime_paths=runtime_paths,
    ):
        return
    # Record before sending so a delivery crash cannot cause a notice loop.
    _record_notice_sent(runtime_paths, room.room_id, session_id)
    if await _send_decrypt_failure_notice(client, room.room_id):
        _stats.notices_sent += 1
        logger.info(
            "e2ee_decrypt_failure_notice_sent",
            room_id=room.room_id,
            session_id=session_id,
            agent=agent_name,
        )


__all__ = ["E2EEStats", "e2ee_stats", "handle_decrypt_failure"]
