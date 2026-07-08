"""Visibility and recovery for undecryptable encrypted Matrix events.

nio surfaces encrypted timeline events it cannot decrypt as ``MegolmEvent``.
Without a registered callback those events vanish silently: the agent never
answers and the logs show nothing, which makes wedged encryption sessions
impossible to diagnose.
This module logs each failure, sends a best-effort room-key request (nio
delivers it to the bot account's own devices, so recovery normally needs the
sender to post a new message), and posts one visible notice per (room,
session) so the user is not left talking to an agent that appears to ignore
them.
All bots in the process share the disk-backed notice ledger: the first bot
that fails on a session claims the notice, so multi-agent rooms never storm.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from nio.exceptions import LocalProtocolError

from mindroom.constants import tracking_dir
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    import nio

    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)

_NOTICE_LEDGER_FILENAME = "e2ee_decrypt_notices.json"
_NOTICE_LEDGER_MAX_ENTRIES = 1000
_SEEN_EVENT_IDS_MAX = 4096
_DECRYPT_FAILURE_NOTICE_BODY = (
    "⚠️ I couldn't decrypt your last message — please send it again. "
    "If this keeps happening, run `!e2ee` for diagnostics."
)


@dataclass
class E2EEStats:
    """Process-wide counters for encrypted-event handling."""

    decrypt_failures: int = 0
    key_requests_sent: int = 0
    notices_sent: int = 0
    decrypt_failures_by_room: dict[str, int] = field(default_factory=dict)
    _seen_event_ids: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def record_failure(self, room_id: str, event_id: str) -> None:
        """Count one undecryptable event, at most once per event across all bots."""
        if event_id in self._seen_event_ids:
            return
        self._seen_event_ids[event_id] = None
        while len(self._seen_event_ids) > _SEEN_EVENT_IDS_MAX:
            self._seen_event_ids.popitem(last=False)
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


_notice_floors: dict[tuple[str, str | None], int] = {}


def raise_notice_floor(user_id: str, room_id: str | None = None) -> None:
    """Suppress visible decrypt-failure notices for events older than now.

    Bots call this when they join a room mid-flight (pre-join encrypted
    history is expected to be undecryptable) and when they start without sync
    continuity (a tokenless initial sync replays events an earlier device may
    already have handled). ``room_id=None`` applies to every room for the bot.
    """
    _notice_floors[(user_id, room_id)] = int(time.time() * 1000)


def _below_notice_floor(user_id: str | None, room_id: str, server_timestamp: int | None) -> bool:
    if user_id is None or not isinstance(server_timestamp, int):
        return False
    floor = max(
        _notice_floors.get((user_id, room_id), 0),
        _notice_floors.get((user_id, None), 0),
    )
    return server_timestamp < floor


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


def _write_notice_ledger(ledger_path: Path, entries: list[str]) -> None:
    try:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(json.dumps(entries), encoding="utf-8")
    except OSError:
        logger.warning("e2ee_notice_ledger_write_failed", path=str(ledger_path))


def _notice_already_sent(runtime_paths: RuntimePaths, room_id: str, session_id: str) -> bool:
    return f"{room_id}|{session_id}" in _load_notice_ledger(_notice_ledger_path(runtime_paths))


def _record_notice_sent(runtime_paths: RuntimePaths, room_id: str, session_id: str) -> None:
    ledger_path = _notice_ledger_path(runtime_paths)
    entries = _load_notice_ledger(ledger_path)
    entries.append(f"{room_id}|{session_id}")
    del entries[:-_NOTICE_LEDGER_MAX_ENTRIES]
    _write_notice_ledger(ledger_path, entries)


def _forget_notice_sent(runtime_paths: RuntimePaths, room_id: str, session_id: str) -> None:
    ledger_path = _notice_ledger_path(runtime_paths)
    entries = _load_notice_ledger(ledger_path)
    entry = f"{room_id}|{session_id}"
    if entry in entries:
        entries.remove(entry)
        _write_notice_ledger(ledger_path, entries)


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
    _stats.record_failure(room.room_id, event.event_id)
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
            "The room-key request only reaches the bot account's own devices, so "
            "recovery normally requires the sender to send a new message."
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

    if _below_notice_floor(client.user_id, room.room_id, event.server_timestamp):
        logger.debug(
            "e2ee_decrypt_notice_suppressed_old_event",
            room_id=room.room_id,
            event_id=event.event_id,
            session_id=session_id,
            agent=agent_name,
        )
        return
    # The check and the record run with no await between them, so concurrent
    # callbacks from other bots in this process cannot claim the same session:
    # the first bot that fails on a session posts the only notice.
    if _notice_already_sent(runtime_paths, room.room_id, session_id):
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
    else:
        # A cleanly failed send left no notice in the room; release the claim
        # so the next undecryptable event in this session can retry.
        _forget_notice_sent(runtime_paths, room.room_id, session_id)


__all__ = ["E2EEStats", "e2ee_stats", "handle_decrypt_failure", "raise_notice_floor"]
