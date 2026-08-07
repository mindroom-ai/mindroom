"""State machine for Matrix sync-token cache certification."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, cast

from mindroom.matrix.sync_token_values import SyncCheckpoint, normalize_sync_token

if TYPE_CHECKING:
    import nio


class SyncTrustState(Enum):
    """Runtime state for restored sync-token cache trust."""

    COLD = "cold"
    PENDING = "pending"
    CERTIFIED = "certified"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class SyncRecoveryOutcome:
    """What one sync response settled about durable ownership of its events.

    Both facts are reported rather than measured after the fact. nio names the
    rooms whose gap it closed and the rooms it could not, and admission either
    accepted every event in the response or refused one -- refusing inside the
    callback nio awaits, which is what keeps the event for redelivery and the
    watermark where it was.

    This replaced a tally of background cache writes. That tally existed only
    because those writes happened outside nio's acceptance protocol, so
    something had to go back afterwards and ask whether they had landed.
    """

    recovered_room_ids: frozenset[str] = frozenset()
    unrecovered_room_ids: frozenset[str] = frozenset()
    admission_refused: bool = False

    @classmethod
    def from_sync_response(
        cls,
        response: nio.SyncResponse | nio.SlidingSyncResponse,
        *,
        admission_refused: bool,
    ) -> SyncRecoveryOutcome:
        """Build the outcome carrying nio's authoritative recovery verdict."""
        return cls(
            recovered_room_ids=response.recovered_room_ids,
            unrecovered_room_ids=response.unrecovered_room_ids,
            admission_refused=admission_refused,
        )


@dataclass(frozen=True)
class SyncCertificationDecision:
    """Action returned by the certification state machine."""

    state: SyncTrustState
    checkpoint_to_save: SyncCheckpoint | None = None
    clear_saved_token: bool = False
    reset_client_token: bool = False
    reason: str | None = None


def _uncertain_decision(
    *,
    reason: str,
    reset_client_token: bool,
    clear_saved_token: bool = False,
) -> SyncCertificationDecision:
    """Return a fail-closed uncertainty decision."""
    return SyncCertificationDecision(
        state=SyncTrustState.UNCERTAIN,
        clear_saved_token=clear_saved_token,
        reset_client_token=reset_client_token,
        reason=reason,
    )


def _uncertain_reason(recovery: SyncRecoveryOutcome, *, token: str | None) -> str | None:
    """Return why one sync response cannot certify a checkpoint.

    An unrecovered room is deliberately absent from this list. nio fences its
    own limited-timeline gaps per room and carries them across restarts, so a
    room it has not finished rebuilding holds back only its own events; the
    checkpoint is a statement about every other room, and withholding it over
    one room's gap is what used to ask for a strictly larger gap on each retry.

    Both remaining reasons are about this response rather than about a room. A
    response with no ``next_batch`` names no position to resume from, and a
    refused admission means some event in it has no durable owner, so resuming
    past it would skip work nobody is holding.
    """
    if token is None:
        return "missing_next_batch"
    if recovery.admission_refused:
        return "admission_refused"
    return None


def certify_sync_response(
    *,
    next_batch: str | None,
    recovery: SyncRecoveryOutcome,
) -> SyncCertificationDecision:
    """Return the certifier decision for one sync response."""
    token = normalize_sync_token(next_batch)
    reason = _uncertain_reason(recovery, token=token)
    if reason is not None:
        return _uncertain_decision(
            reason=reason,
            reset_client_token=True,
        )

    return SyncCertificationDecision(
        state=SyncTrustState.CERTIFIED,
        checkpoint_to_save=SyncCheckpoint(token=cast("str", token)),
    )


def handle_unknown_pos() -> SyncCertificationDecision:
    """Return the fail-closed decision for Matrix ``M_UNKNOWN_POS``."""
    return _uncertain_decision(
        reason="unknown_pos",
        clear_saved_token=True,
        reset_client_token=True,
    )


def sync_recovery_diagnostics(recovery: SyncRecoveryOutcome) -> dict[str, Any]:
    """Return structured log fields explaining one response's recovery outcome."""
    diagnostics: dict[str, Any] = {
        "sync_admission_refused": recovery.admission_refused,
        "sync_recovery_certified": not recovery.admission_refused and not recovery.unrecovered_room_ids,
        "sync_recovered_room_count": len(recovery.recovered_room_ids),
        "sync_unrecovered_room_count": len(recovery.unrecovered_room_ids),
    }
    if recovery.recovered_room_ids:
        diagnostics["sync_recovered_room_ids"] = tuple(sorted(recovery.recovered_room_ids))[:5]
    if recovery.unrecovered_room_ids:
        diagnostics["sync_unrecovered_room_ids"] = tuple(sorted(recovery.unrecovered_room_ids))[:5]
    return diagnostics
