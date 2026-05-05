"""Matrix homeserver health helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

_MATRIX_VERSIONS_PATH = "/_matrix/client/versions"
_MATRIX_SYNC_HEALTH_STALE_SECONDS = 180.0
MATRIX_SYNC_STARTUP_GRACE_SECONDS = 600.0
MATRIX_SYNC_WATCHDOG_TIMEOUT_SECONDS = 120.0


@dataclass(slots=True)
class _MatrixSyncState:
    """Mutable sync-health state for one active Matrix entity."""

    running: bool = False
    loop_started_time: datetime | None = None
    last_sync_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class _MatrixSyncHealthSnapshot:
    """Snapshot of aggregated Matrix sync health across active entities."""

    active_entities: tuple[str, ...]
    stale_entities: tuple[str, ...]
    last_sync_time: datetime | None

    @property
    def is_healthy(self) -> bool:
        """Return whether all active entities have a recent successful sync."""
        return not self.stale_entities


_matrix_sync_state: dict[str, _MatrixSyncState] = {}
_matrix_sync_lock = Lock()


def _normalize_sync_time(sync_time: datetime) -> datetime:
    """Return a timezone-aware UTC timestamp."""
    if sync_time.tzinfo is None:
        return sync_time.replace(tzinfo=UTC)
    return sync_time.astimezone(UTC)


def matrix_versions_url(homeserver_url: str) -> str:
    """Return the Matrix versions endpoint for a homeserver URL."""
    return f"{homeserver_url.rstrip('/')}{_MATRIX_VERSIONS_PATH}"


def response_has_matrix_versions(response: httpx.Response) -> bool:
    """Return whether a response is a successful Matrix `/versions` payload."""
    if not response.is_success:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    return isinstance(payload, dict) and "versions" in payload


def mark_matrix_sync_loop_started(entity_name: str) -> None:
    """Mark an entity as actively running a Matrix sync loop.

    Preserve the original startup grace window and last successful sync across
    watchdog restarts so a stuck loop cannot hide behind repeated restarts.
    """
    with _matrix_sync_lock:
        state = _matrix_sync_state.setdefault(entity_name, _MatrixSyncState())
        state.running = True
        if state.loop_started_time is None:
            state.loop_started_time = _normalize_sync_time(datetime.now(UTC))


def mark_matrix_sync_success(entity_name: str, sync_time: datetime | None = None) -> datetime:
    """Record a successful Matrix sync response for one entity."""
    resolved_sync_time = _normalize_sync_time(sync_time or datetime.now(UTC))
    with _matrix_sync_lock:
        state = _matrix_sync_state.setdefault(entity_name, _MatrixSyncState())
        state.running = True
        state.last_sync_time = resolved_sync_time
    return resolved_sync_time


def clear_matrix_sync_state(entity_name: str) -> None:
    """Remove one entity from the shared Matrix sync-health registry."""
    with _matrix_sync_lock:
        _matrix_sync_state.pop(entity_name, None)


def get_matrix_sync_health_snapshot(
    *,
    stale_after_seconds: float = _MATRIX_SYNC_HEALTH_STALE_SECONDS,
    startup_grace_seconds: float = MATRIX_SYNC_STARTUP_GRACE_SECONDS,
    now: datetime | None = None,
) -> _MatrixSyncHealthSnapshot:
    """Return the current Matrix sync-health snapshot.

    The reported `last_sync_time` is the oldest successful sync among active
    entities, because any stale entity should surface as unhealthy.
    """
    current_time = _normalize_sync_time(now or datetime.now(UTC))
    with _matrix_sync_lock:
        active_states = tuple(
            sorted(
                (
                    (entity_name, state.last_sync_time, state.loop_started_time)
                    for entity_name, state in _matrix_sync_state.items()
                    if state.running
                ),
                key=lambda item: item[0],
            ),
        )

    if not active_states:
        return _MatrixSyncHealthSnapshot(
            active_entities=(),
            stale_entities=(),
            last_sync_time=None,
        )

    active_entities = tuple(entity_name for entity_name, _, _ in active_states)
    stale_entities = tuple(
        entity_name
        for entity_name, last_sync_time, loop_started_time in active_states
        if (
            (last_sync_time is not None and (current_time - last_sync_time).total_seconds() > stale_after_seconds)
            or (
                last_sync_time is None
                and loop_started_time is not None
                and (current_time - loop_started_time).total_seconds() > startup_grace_seconds
            )
        )
    )
    if any(last_sync_time is None for _, last_sync_time, _ in active_states):
        oldest_last_sync_time = None
    else:
        oldest_last_sync_time = min(
            last_sync_time for _, last_sync_time, _ in active_states if last_sync_time is not None
        )
    return _MatrixSyncHealthSnapshot(
        active_entities=active_entities,
        stale_entities=stale_entities,
        last_sync_time=oldest_last_sync_time,
    )


def reset_matrix_sync_health() -> None:
    """Clear all shared Matrix sync-health state."""
    with _matrix_sync_lock:
        _matrix_sync_state.clear()
