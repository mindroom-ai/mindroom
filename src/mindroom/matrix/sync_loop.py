"""Classic and Simplified Sliding settings for the durable Matrix session."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nio.durable import DurableSyncConfig, SlidingSyncConfig

if TYPE_CHECKING:
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


def sliding_sync_room_subscriptions(room_ids: list[str], timeline_limit: int) -> dict[str, object]:
    """Subscribe to configured resolved rooms, including rooms outside discovery."""
    return {room_id: _sliding_room_config(timeline_limit) for room_id in room_ids if room_id.startswith("!")}


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
            room_subscriptions=sliding_sync_room_subscriptions(room_ids, timeline_limit),
            extensions={
                "to_device": {"enabled": True},
                "e2ee": {"enabled": True},
                "account_data": {"enabled": True},
            },
        )
    return DurableSyncConfig(sync_timeout_ms=timeout_ms, sync_filter=sync_filter, sliding=sliding)
