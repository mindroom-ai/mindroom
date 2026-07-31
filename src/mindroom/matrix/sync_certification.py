"""State machine for Matrix sync-token cache certification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from mindroom.matrix.sync_token_values import normalize_sync_token

if TYPE_CHECKING:
    import nio


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
    cache_generation: str | None = None


@dataclass(frozen=True)
class SyncCacheWriteResult:
    """Durable sync-timeline cache write outcome for one sync response."""

    complete: bool
    limited_room_ids: tuple[str, ...] = ()
    recovered_room_ids: frozenset[str] = frozenset()
    unrecovered_room_ids: frozenset[str] = frozenset()
    errors: tuple[BaseException, ...] = ()
    runtime_available: bool | None = None
    task_count: int | None = None
    runtime_diagnostics: dict[str, object] | None = None

    @classmethod
    def from_sync_response(
        cls,
        response: nio.SyncResponse | nio.SlidingSyncResponse,
        *,
        complete: bool,
        limited_room_ids: tuple[str, ...] = (),
        errors: tuple[BaseException, ...] = (),
        runtime_available: bool | None = None,
        task_count: int | None = None,
        runtime_diagnostics: dict[str, object] | None = None,
    ) -> SyncCacheWriteResult:
        """Build a cache result carrying nio's authoritative recovery outcome."""
        return cls(
            complete=complete,
            limited_room_ids=limited_room_ids,
            recovered_room_ids=response.recovered_room_ids,
            unrecovered_room_ids=response.unrecovered_room_ids,
            errors=errors,
            runtime_available=runtime_available,
            task_count=task_count,
            runtime_diagnostics=runtime_diagnostics,
        )

    @property
    def unclassified_limited_room_ids(self) -> tuple[str, ...]:
        """Return current limited rooms missing an authoritative recovery outcome."""
        classified_room_ids = self.recovered_room_ids | self.unrecovered_room_ids
        return tuple(room_id for room_id in self.limited_room_ids if room_id not in classified_room_ids)

    @property
    def has_recovery_obligation(self) -> bool:
        """Return whether nio reports unresolved recovery or misses a current limited-room classification."""
        return bool(self.unclassified_limited_room_ids or self.unrecovered_room_ids)

    @property
    def certified(self) -> bool:
        """Return whether local cache work completed without errors or recovery obligations."""
        return self.complete and not self.errors and not self.has_recovery_obligation


@dataclass(frozen=True)
class SyncCertificationDecision:
    """Action returned by the certification state machine."""

    state: SyncTrustState
    checkpoint_to_save: SyncCheckpoint | None = None
    clear_saved_token: bool = False
    reset_client_token: bool = False
    reason: str | None = None
    cache_scope_epoch: int | None = None


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
    if normalize_sync_token(next_batch) is None:
        return "missing_next_batch"
    if cache_result.errors:
        return "cache_write_failed"
    if cache_result.unclassified_limited_room_ids:
        return "limited_sync_timeline"
    if cache_result.unrecovered_room_ids:
        return "sync_recovery_incomplete"
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

    token = normalize_sync_token(next_batch)
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
        "cache_error_count": len(cache_result.errors),
    }
    diagnostics.update(_recovery_diagnostics(cache_result))
    if cache_result.runtime_available is not None:
        diagnostics["cache_runtime_available"] = cache_result.runtime_available
    if cache_result.task_count is not None:
        diagnostics["cache_task_count"] = cache_result.task_count
    if cache_result.runtime_diagnostics:
        diagnostics.update(cache_result.runtime_diagnostics)
    if cache_result.errors:
        diagnostics["cache_error_types"] = tuple(type(error).__name__ for error in cache_result.errors[:5])
        diagnostics["cache_error_messages"] = tuple(str(error)[:200] for error in cache_result.errors[:5])
    return diagnostics


def _recovery_diagnostics(cache_result: SyncCacheWriteResult) -> dict[str, Any]:
    """Return recovery-specific diagnostics for one cache write."""
    diagnostics: dict[str, Any] = {
        "cache_limited_room_count": len(cache_result.limited_room_ids),
        "cache_recovered_room_count": len(cache_result.recovered_room_ids),
        "cache_unrecovered_room_count": len(cache_result.unrecovered_room_ids),
        "cache_unclassified_limited_room_count": len(cache_result.unclassified_limited_room_ids),
    }
    if cache_result.limited_room_ids:
        diagnostics["cache_limited_room_ids"] = cache_result.limited_room_ids[:5]
    if cache_result.recovered_room_ids:
        diagnostics["cache_recovered_room_ids"] = tuple(sorted(cache_result.recovered_room_ids))[:5]
    if cache_result.unrecovered_room_ids:
        diagnostics["cache_unrecovered_room_ids"] = tuple(sorted(cache_result.unrecovered_room_ids))[:5]
    if cache_result.unclassified_limited_room_ids:
        diagnostics["cache_unclassified_limited_room_ids"] = cache_result.unclassified_limited_room_ids[:5]
    return diagnostics
