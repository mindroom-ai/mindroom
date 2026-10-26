"""Classic and Simplified Sliding settings for the durable Matrix session."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nio.durable import DurableSyncConfig, SlidingSyncConfig

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nio.durable import DurableSync

    from mindroom.config.main import Config


_SLIDING_SYNC_REQUIRED_STATE: tuple[tuple[str, str], ...] = (
    ("m.room.create", ""),
    ("m.room.name", ""),
    ("m.room.topic", ""),
    ("m.room.avatar", ""),
    ("m.room.encryption", ""),
    ("m.room.member", "$LAZY"),
)


def _sliding_room_config(timeline_limit: int) -> dict[str, object]:
    return {
        "timeline_limit": timeline_limit,
        "required_state": [list(entry) for entry in _SLIDING_SYNC_REQUIRED_STATE],
    }


def _sliding_sync_room_subscriptions(room_ids: Iterable[str], timeline_limit: int) -> dict[str, object]:
    """Subscribe to the given resolved rooms, including rooms outside discovery."""
    return {room_id: _sliding_room_config(timeline_limit) for room_id in room_ids if room_id.startswith("!")}


async def _awaits_room_state(session: DurableSync, room_id: str) -> bool:
    """Return whether nio still waits for a sync to deliver this joined room's state; otherwise it answers at once."""
    try:
        async with asyncio.timeout(0):
            await session.wait_for_room_ready(room_id)
    except TimeoutError:
        return True
    return False


async def subscribe_sliding_rooms(session: DurableSync, config: Config, room_ids: Iterable[str]) -> None:
    """Subscribe a Sliding source to these rooms and to each joined room whose state nio still waits for.

    Sliding Sync reports a room only through its room list or a subscription, so a joined room outside the list,
    such as one a join just reset or one whose join a restart left unobserved, needs a subscription until its state
    arrives; a room nio can already send to needs none. A Classic source syncs every room.
    """
    if config.matrix_sync.mode != "sliding":
        return
    awaited = [room_id for room_id in tuple(session.client.rooms) if await _awaits_room_state(session, room_id)]
    await session.update_sliding_subscriptions(
        _sliding_sync_room_subscriptions((*room_ids, *awaited), config.matrix_sync.sliding_timeline_limit),
    )


def bot_ingestion_config(
    config: Config,
    *,
    agent_name: str,
    room_ids: list[str],
    timeout_ms: int,
    sync_filter: dict[str, object],
) -> DurableSyncConfig:
    """Build source settings while both transports retain durable batch ownership."""
    sliding = None
    if config.matrix_sync.mode == "sliding":
        timeline_limit = config.matrix_sync.sliding_timeline_limit
        sliding = SlidingSyncConfig(
            conn_id=f"mindroom-{agent_name}",
            lists={"mindroom": {"ranges": [[0, 99]], **_sliding_room_config(timeline_limit)}},
            room_subscriptions=_sliding_sync_room_subscriptions(room_ids, timeline_limit),
            extensions={
                "to_device": {"enabled": True},
                "e2ee": {"enabled": True},
                "account_data": {"enabled": True},
            },
        )
    return DurableSyncConfig(
        max_response_bytes=config.matrix_sync.max_response_bytes,
        max_pending_bytes=config.matrix_sync.max_pending_bytes,
        sync_timeout_ms=timeout_ms,
        sync_filter=sync_filter,
        sliding=sliding,
    )
