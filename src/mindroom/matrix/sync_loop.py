"""Matrix sync-loop selection helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config

_SLIDING_SYNC_REQUIRED_STATE: tuple[tuple[str, str], ...] = (
    ("m.room.create", ""),
    ("m.room.name", ""),
    ("m.room.topic", ""),
    ("m.room.avatar", ""),
    ("m.room.encryption", ""),
    ("m.room.member", "$LAZY"),
)
_SLIDING_SYNC_LIST_ROOM_COUNT = 100


def _sliding_room_config(timeline_limit: int) -> dict[str, object]:
    """Return the shared room request config for Simplified Sliding Sync."""
    return {
        "timeline_limit": timeline_limit,
        "required_state": [list(entry) for entry in _SLIDING_SYNC_REQUIRED_STATE],
    }


def _sliding_sync_lists(timeline_limit: int) -> dict[str, object]:
    """Return list subscriptions that preserve invite and recently-active-room ingress."""
    return {
        "mindroom": {
            "ranges": [[0, _SLIDING_SYNC_LIST_ROOM_COUNT - 1]],
            **_sliding_room_config(timeline_limit),
        },
    }


def _sliding_sync_room_subscriptions(room_ids: list[str], timeline_limit: int) -> dict[str, object]:
    """Return explicit room subscriptions for resolved Matrix room IDs."""
    return {room_id: _sliding_room_config(timeline_limit) for room_id in room_ids if room_id.startswith("!")}


def _sliding_sync_extensions() -> dict[str, object]:
    """Return extension subscriptions required for a bot account sync loop."""
    return {
        "to_device": {"enabled": True},
        "e2ee": {"enabled": True},
        "account_data": {"enabled": True},
    }


async def run_matrix_sync_forever(
    client: nio.AsyncClient,
    *,
    config: Config,
    agent_name: str,
    room_ids: list[str],
    timeout_ms: int,
    sync_filter: dict[str, object],
    first_sync_done: bool,
) -> None:
    """Run the configured Matrix sync loop for one bot account."""
    if config.matrix_sync.mode == "classic":
        await client.sync_forever(timeout=timeout_ms, sync_filter=sync_filter, full_state=not first_sync_done)
        return

    timeline_limit = config.matrix_sync.sliding_timeline_limit
    await client.sliding_sync_forever(
        timeout=timeout_ms,
        conn_id=f"mindroom-{agent_name}",
        lists=_sliding_sync_lists(timeline_limit),
        room_subscriptions=_sliding_sync_room_subscriptions(room_ids, timeline_limit),
        extensions=_sliding_sync_extensions(),
    )
