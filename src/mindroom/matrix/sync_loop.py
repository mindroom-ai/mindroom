"""Owned Matrix ingestion source configuration."""

from __future__ import annotations

from json import JSONEncoder
from typing import TYPE_CHECKING

from nio.ingest.config import ClassicSourceConfig, IngestionConfig, SlidingSourceConfig

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
_SLIDING_SYNC_LIST_ROOM_COUNT = 100
_INGESTION_JSON_ENCODER = JSONEncoder(
    ensure_ascii=False,
    allow_nan=False,
    separators=(",", ":"),
    sort_keys=True,
)


def _canonical_ingestion_json(value: object) -> bytes:
    """Encode one immutable source configuration exactly as nio expects."""
    return _INGESTION_JSON_ENCODER.encode(value).encode("utf-8")


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


def bot_ingestion_config(
    config: Config,
    *,
    agent_name: str,
    room_ids: list[str],
    timeout_ms: int,
    sync_filter: dict[str, object],
) -> IngestionConfig:
    """Freeze the live bot transport settings for the owned nio source."""
    if config.matrix_sync.mode == "classic":
        return IngestionConfig(
            ClassicSourceConfig(
                timeout_ms=timeout_ms,
                filter_json=_canonical_ingestion_json(sync_filter),
            ),
        )
    timeline_limit = config.matrix_sync.sliding_timeline_limit
    return IngestionConfig(
        SlidingSourceConfig(
            timeout_ms=timeout_ms,
            connection_name=f"mindroom-{agent_name}",
            lists_json=_canonical_ingestion_json(_sliding_sync_lists(timeline_limit)),
            room_subscriptions_json=_canonical_ingestion_json(
                _sliding_sync_room_subscriptions(room_ids, timeline_limit),
            ),
            extensions_json=_canonical_ingestion_json(_sliding_sync_extensions()),
        ),
    )
