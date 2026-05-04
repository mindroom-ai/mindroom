"""Matrix sync-loop selection helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    import nio

    from mindroom.config.main import Config

_SLIDING_SYNC_REQUIRED_STATE: list[list[str]] = [
    ["m.room.create", ""],
    ["m.room.name", ""],
    ["m.room.topic", ""],
    ["m.room.avatar", ""],
    ["m.room.encryption", ""],
    ["m.room.member", "$LAZY"],
]
_SLIDING_SYNC_LIST_ROOM_COUNT = 100


@runtime_checkable
class SlidingSyncClient(Protocol):
    """Client API provided by mindroom-nio for MSC4186 sync loops."""

    async def sliding_sync_forever(
        self,
        *,
        timeout: int,  # noqa: ASYNC109 - forwarded to matrix-nio for Matrix long-polling.
        conn_id: str,
        lists: dict[str, object],
        room_subscriptions: dict[str, object],
        extensions: dict[str, object],
    ) -> None:
        """Run a Simplified Sliding Sync loop."""
        ...


def _sliding_room_config(timeline_limit: int) -> dict[str, object]:
    """Return the shared room request config for Simplified Sliding Sync."""
    return {
        "timeline_limit": timeline_limit,
        "required_state": _SLIDING_SYNC_REQUIRED_STATE,
    }


def sliding_sync_lists(timeline_limit: int) -> dict[str, object]:
    """Return list subscriptions that preserve invite and recently-active-room ingress."""
    return {
        "mindroom": {
            "ranges": [[0, _SLIDING_SYNC_LIST_ROOM_COUNT - 1]],
            **_sliding_room_config(timeline_limit),
        },
    }


def sliding_sync_room_subscriptions(room_ids: list[str], timeline_limit: int) -> dict[str, object]:
    """Return explicit room subscriptions for resolved Matrix room IDs."""
    room_config = _sliding_room_config(timeline_limit)
    return {room_id: dict(room_config) for room_id in room_ids if room_id.startswith("!")}


def sliding_sync_extensions() -> dict[str, object]:
    """Return extension subscriptions required for a bot account sync loop."""
    return {
        "to_device": {"enabled": True},
        "e2ee": {"enabled": True},
        "account_data": {"enabled": True},
    }


async def _run_classic_sync_forever(
    client: nio.AsyncClient,
    *,
    timeout_ms: int,
    first_sync_done: bool,
) -> None:
    """Run the classic Matrix /v3/sync loop."""
    await client.sync_forever(timeout=timeout_ms, full_state=not first_sync_done)


async def _run_sliding_sync_forever(
    client: SlidingSyncClient,
    *,
    agent_name: str,
    room_ids: list[str],
    timeout_ms: int,
    timeline_limit: int,
) -> None:
    """Run the MSC4186 Simplified Sliding Sync loop."""
    await client.sliding_sync_forever(
        timeout=timeout_ms,
        conn_id=f"mindroom-{agent_name}",
        lists=sliding_sync_lists(timeline_limit),
        room_subscriptions=sliding_sync_room_subscriptions(room_ids, timeline_limit),
        extensions=sliding_sync_extensions(),
    )


async def run_matrix_sync_forever(
    client: nio.AsyncClient,
    *,
    config: Config,
    agent_name: str,
    room_ids: list[str],
    timeout_ms: int,
    first_sync_done: bool,
) -> None:
    """Run the configured Matrix sync loop for one bot account."""
    sync_mode = config.matrix_sync.mode
    if sync_mode == "classic":
        await _run_classic_sync_forever(client, timeout_ms=timeout_ms, first_sync_done=first_sync_done)
        return

    if sync_mode == "auto" and not isinstance(client, SlidingSyncClient):
        await _run_classic_sync_forever(client, timeout_ms=timeout_ms, first_sync_done=first_sync_done)
        return

    if not isinstance(client, SlidingSyncClient):
        msg = "matrix_sync.mode='sliding' requires mindroom-nio with sliding_sync_forever support"
        raise TypeError(msg)

    timeline_limit = config.matrix_sync.sliding_timeline_limit
    await _run_sliding_sync_forever(
        cast("SlidingSyncClient", client),
        agent_name=agent_name,
        room_ids=room_ids,
        timeout_ms=timeout_ms,
        timeline_limit=timeline_limit,
    )
