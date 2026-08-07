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

    @property
    def recovery_conclusive(self) -> bool:
        """Return whether this response settles whether nio closed every gap.

        A response that refused an event never reached its recovery verdict, so
        it may neither prove nor disprove that a room's rebuild has stopped
        converging.
        """
        return not self.admission_refused


@dataclass(frozen=True)
class SyncCertificationDecision:
    """Action returned by the certification state machine."""

    state: SyncTrustState
    checkpoint_to_save: SyncCheckpoint | None = None
    clear_saved_token: bool = False
    reset_client_token: bool = False
    reason: str | None = None
    # Rooms this checkpoint is about to move past without their history. The
    # decision carries them from planning to application because the durable
    # record of what was skipped has to be written before the checkpoint that
    # skips it, and only the applying side is allowed to write anything.
    skipped_recovery_room_ids: frozenset[str] = frozenset()


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


def _uncertain_reason(
    recovery: SyncRecoveryOutcome,
    *,
    token: str | None,
    skipped_recovery_room_ids: frozenset[str],
) -> str | None:
    """Return why one sync response cannot certify a checkpoint."""
    if token is None:
        reason = "missing_next_batch"
    elif recovery.admission_refused:
        reason = "admission_refused"
    elif recovery.unrecovered_room_ids - skipped_recovery_room_ids:
        reason = "sync_recovery_incomplete"
    else:
        reason = None
    return reason


def certify_sync_response(
    *,
    next_batch: str | None,
    recovery: SyncRecoveryOutcome,
    skipped_recovery_room_ids: frozenset[str] = frozenset(),
) -> SyncCertificationDecision:
    """Return the certifier decision for one sync response.

    ``skipped_recovery_room_ids`` names rooms whose unrecovered gap the caller
    has decided to give up on, trading that room's missed history for the
    forward progress a permanently rewinding cursor can never make.
    """
    token = normalize_sync_token(next_batch)
    reason = _uncertain_reason(
        recovery,
        token=token,
        skipped_recovery_room_ids=skipped_recovery_room_ids,
    )
    if reason is not None:
        return _uncertain_decision(
            reason=reason,
            reset_client_token=True,
        )

    checkpoint = SyncCheckpoint(token=cast("str", token))
    return SyncCertificationDecision(
        state=SyncTrustState.CERTIFIED,
        checkpoint_to_save=checkpoint,
        # Skipping a gap leaves nio holding recovery state for a room this
        # checkpoint has already moved past, and that state can block the
        # response from ever being acknowledged. Restart the client from the
        # newly certified position instead of acknowledging in place.
        reset_client_token=bool(skipped_recovery_room_ids),
        skipped_recovery_room_ids=skipped_recovery_room_ids,
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
