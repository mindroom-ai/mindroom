"""State machine for Matrix sync-token cache certification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class SyncTrustState(Enum):
    """Runtime state for restored sync-token cache trust."""

    COLD = "cold"
    PENDING = "pending"
    CERTIFIED = "certified"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class SyncCheckpoint:
    """A sync token saved after its sync response was durably cached."""

    token: str


@dataclass(frozen=True)
class SyncCacheWriteResult:
    """Durable sync-timeline cache write outcome for one sync response."""

    complete: bool
    limited_room_ids: tuple[str, ...] = ()
    errors: tuple[BaseException, ...] = ()
    runtime_available: bool | None = None
    task_count: int | None = None
    runtime_diagnostics: dict[str, object] | None = None

    @property
    def certified(self) -> bool:
        """Return whether this result proves the sync delta reached durable cache."""
        return self.complete and not self.limited_room_ids and not self.errors


@dataclass(frozen=True)
class SyncCertificationDecision:
    """Action returned by the certification state machine."""

    state: SyncTrustState
    checkpoint_to_save: SyncCheckpoint | None = None
    clear_saved_token: bool = False
    reset_client_token: bool = False
    reason: str | None = None


@dataclass(frozen=True)
class SyncCertificationStart:
    """Initial runtime sync-token trust state."""

    state: SyncTrustState
    sync_token: str | None
    legacy_token: bool = False


def _non_empty_token(token: str | None) -> str | None:
    """Return a normalized token when it can be persisted or restored."""
    if not isinstance(token, str):
        return None
    normalized = token.strip()
    return normalized or None


def start_from_loaded_token(loaded: SyncCheckpoint | str | None) -> SyncCertificationStart:
    """Build initial certifier state from a loaded token or checkpoint."""
    if isinstance(loaded, SyncCheckpoint):
        token = _non_empty_token(loaded.token)
        if token is None:
            return SyncCertificationStart(
                state=SyncTrustState.COLD,
                sync_token=None,
            )
        return SyncCertificationStart(
            state=SyncTrustState.PENDING,
            sync_token=token,
        )

    token = _non_empty_token(loaded) if isinstance(loaded, str) else None
    return SyncCertificationStart(
        state=SyncTrustState.COLD,
        sync_token=token,
        legacy_token=token is not None,
    )


def _uncertain_decision(
    *,
    reason: str,
    reset_client_token: bool = False,
) -> SyncCertificationDecision:
    """Return a fail-closed uncertainty decision."""
    return SyncCertificationDecision(
        state=SyncTrustState.UNCERTAIN,
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


def certify_sync_response(
    state: SyncTrustState,
    *,
    next_batch: str | None,
    cache_result: SyncCacheWriteResult,
    first_sync: bool,
) -> SyncCertificationDecision:
    """Return the certifier decision for one sync response."""
    reason = _uncertain_reason(cache_result, next_batch=next_batch)
    if reason is not None:
        return _uncertain_decision(
            reason=reason,
            reset_client_token=state is SyncTrustState.PENDING and first_sync,
        )

    token = _non_empty_token(next_batch)
    if token is None:
        return _uncertain_decision(reason="missing_next_batch")

    checkpoint = SyncCheckpoint(token=token)
    return SyncCertificationDecision(
        state=SyncTrustState.CERTIFIED,
        checkpoint_to_save=checkpoint,
    )


def handle_unknown_pos() -> SyncCertificationDecision:
    """Return the fail-closed decision for Matrix ``M_UNKNOWN_POS``."""
    return _uncertain_decision(
        reason="unknown_pos",
        reset_client_token=True,
    )


def sync_cache_write_diagnostics(cache_result: SyncCacheWriteResult) -> dict[str, Any]:
    """Return structured log fields explaining one sync cache-write result."""
    diagnostics: dict[str, Any] = {
        "cache_write_complete": cache_result.complete,
        "cache_write_certified": cache_result.certified,
        "cache_limited_room_count": len(cache_result.limited_room_ids),
        "cache_error_count": len(cache_result.errors),
    }
    if cache_result.runtime_available is not None:
        diagnostics["cache_runtime_available"] = cache_result.runtime_available
    if cache_result.task_count is not None:
        diagnostics["cache_task_count"] = cache_result.task_count
    if cache_result.runtime_diagnostics:
        diagnostics.update(cache_result.runtime_diagnostics)
    if cache_result.limited_room_ids:
        diagnostics["cache_limited_room_ids"] = cache_result.limited_room_ids[:5]
    if cache_result.errors:
        diagnostics["cache_error_types"] = tuple(type(error).__name__ for error in cache_result.errors[:5])
        diagnostics["cache_error_messages"] = tuple(str(error)[:200] for error in cache_result.errors[:5])
    return diagnostics
