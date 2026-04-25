"""State machine for Matrix sync-token cache certification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class SyncTrustState(Enum):
    """Runtime state for restored sync-token cache trust."""

    COLD = "cold"
    PENDING = "pending"
    CERTIFIED = "certified"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class SyncCheckpoint:
    """A sync token certified against a durable thread-cache boundary."""

    token: str
    thread_cache_valid_after: float


@dataclass(frozen=True)
class SyncCacheWriteResult:
    """Durable sync-timeline cache write outcome for one sync response."""

    complete: bool
    limited_room_ids: tuple[str, ...] = ()
    errors: tuple[BaseException, ...] = ()

    @property
    def certified(self) -> bool:
        """Return whether this result proves the sync delta reached durable cache."""
        return self.complete and not self.limited_room_ids and not self.errors


@dataclass(frozen=True)
class SyncCertificationDecision:
    """Action returned by the certification state machine."""

    state: SyncTrustState
    thread_cache_read_boundary: float
    checkpoint_to_save: SyncCheckpoint | None = None
    clear_saved_token: bool = False
    reset_client_token: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class SyncCertificationStart:
    """Initial runtime sync-token trust state."""

    state: SyncTrustState
    sync_token: str | None
    thread_cache_read_boundary: float
    checkpoint: SyncCheckpoint | None = None
    legacy_token: bool = False


def _non_empty_token(token: str | None) -> str | None:
    """Return a normalized token when it can be persisted or restored."""
    if not isinstance(token, str):
        return None
    normalized = token.strip()
    return normalized or None


def start_from_loaded_token(
    loaded: SyncCheckpoint | str | None,
    *,
    runtime_started_at: float,
) -> SyncCertificationStart:
    """Build initial certifier state from a loaded token or checkpoint."""
    if isinstance(loaded, SyncCheckpoint):
        token = _non_empty_token(loaded.token)
        if token is None:
            return SyncCertificationStart(
                state=SyncTrustState.COLD,
                sync_token=None,
                thread_cache_read_boundary=runtime_started_at,
            )
        checkpoint = SyncCheckpoint(
            token=token,
            thread_cache_valid_after=loaded.thread_cache_valid_after,
        )
        return SyncCertificationStart(
            state=SyncTrustState.PENDING,
            sync_token=token,
            thread_cache_read_boundary=runtime_started_at,
            checkpoint=checkpoint,
        )

    token = _non_empty_token(loaded) if isinstance(loaded, str) else None
    return SyncCertificationStart(
        state=SyncTrustState.COLD,
        sync_token=token,
        thread_cache_read_boundary=runtime_started_at,
        legacy_token=token is not None,
    )


def _uncertain_decision(
    *,
    now: float,
    reason: str,
    reset_client_token: bool = False,
) -> SyncCertificationDecision:
    """Return a fail-closed uncertainty decision."""
    return SyncCertificationDecision(
        state=SyncTrustState.UNCERTAIN,
        thread_cache_read_boundary=now,
        clear_saved_token=True,
        reset_client_token=reset_client_token,
        reason=reason,
    )


def _uncertain_reason(cache_result: SyncCacheWriteResult, *, next_batch: str | None) -> str | None:
    """Return why one sync response cannot certify a checkpoint."""
    if _non_empty_token(next_batch) is None:
        return "missing_next_batch"
    if cache_result.errors:
        return "cache_write_failed"
    if cache_result.limited_room_ids:
        return "limited_sync_timeline"
    if not cache_result.complete:
        return "cache_write_incomplete"
    return None


def _checkpoint_boundary(
    state: SyncTrustState,
    *,
    previous_checkpoint: SyncCheckpoint | None,
    current_read_boundary: float,
) -> float:
    """Return the boundary to carry into a newly certified checkpoint."""
    if state is SyncTrustState.PENDING and previous_checkpoint is not None:
        return previous_checkpoint.thread_cache_valid_after
    if state is SyncTrustState.CERTIFIED and previous_checkpoint is not None:
        return previous_checkpoint.thread_cache_valid_after
    return current_read_boundary


def certify_sync_response(
    state: SyncTrustState,
    *,
    previous_checkpoint: SyncCheckpoint | None,
    next_batch: str | None,
    cache_result: SyncCacheWriteResult,
    first_sync: bool,
    now: float,
    current_read_boundary: float,
) -> SyncCertificationDecision:
    """Return the certifier decision for one sync response."""
    reason = _uncertain_reason(cache_result, next_batch=next_batch)
    if reason is not None:
        return _uncertain_decision(
            now=now,
            reason=reason,
            reset_client_token=state is SyncTrustState.PENDING and first_sync,
        )

    token = _non_empty_token(next_batch)
    if token is None:
        return _uncertain_decision(now=now, reason="missing_next_batch")

    boundary = _checkpoint_boundary(
        state,
        previous_checkpoint=previous_checkpoint,
        current_read_boundary=current_read_boundary,
    )
    checkpoint = SyncCheckpoint(token=token, thread_cache_valid_after=boundary)
    return SyncCertificationDecision(
        state=SyncTrustState.CERTIFIED,
        thread_cache_read_boundary=boundary,
        checkpoint_to_save=checkpoint,
    )


def handle_unknown_pos(*, now: float) -> SyncCertificationDecision:
    """Return the fail-closed decision for Matrix ``M_UNKNOWN_POS``."""
    return _uncertain_decision(
        now=now,
        reason="unknown_pos",
        reset_client_token=True,
    )
