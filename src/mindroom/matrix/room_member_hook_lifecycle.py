"""Own room-member hook bootstrap, catch-up, and live phases."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

import nio

from mindroom.matrix.sync_certification import SyncCertificationDecision, SyncTrustState


class _RoomMemberHookPhase(Enum):
    """One room-member hook admission and baseline phase."""

    DISABLED = "disabled"
    BASELINE = "baseline"
    FULL_STATE_CATCHUP = "full_state_catchup"
    CATCHUP = "catchup"
    SLIDING_CATCHUP = "sliding_catchup"
    LIVE = "live"


@dataclass(frozen=True, slots=True)
class RoomMemberResponsePlan:
    """Room-member work that must precede one response's certification."""

    drain_captured_timeline: bool = False
    dispatch_state: bool = False


@dataclass(slots=True)
class RoomMemberHookLifecycle:
    """Own the phase transitions that fence room-member hook delivery."""

    enabled: bool
    _phase: _RoomMemberHookPhase = field(init=False)

    def __post_init__(self) -> None:
        """Start enabled owners behind a tokenless baseline fence."""
        self._phase = _RoomMemberHookPhase.BASELINE if self.enabled else _RoomMemberHookPhase.DISABLED

    @property
    def baseline_record_pending(self) -> bool:
        """Return whether the next full-state response needs baseline markers."""
        return self._phase in {
            _RoomMemberHookPhase.BASELINE,
            _RoomMemberHookPhase.FULL_STATE_CATCHUP,
        }

    @property
    def full_state_required(self) -> bool:
        """Return whether a Classic replacement loop must request full state."""
        return self._phase in {
            _RoomMemberHookPhase.BASELINE,
            _RoomMemberHookPhase.FULL_STATE_CATCHUP,
            _RoomMemberHookPhase.CATCHUP,
        }

    def admission_enabled(self, provenance: nio.TimelineEventProvenance) -> bool:
        """Return whether one member event should enter durable callback ownership."""
        if self._phase is _RoomMemberHookPhase.DISABLED:
            return False
        if provenance is nio.TimelineEventProvenance.LIVE:
            return True
        return self._phase in {
            _RoomMemberHookPhase.FULL_STATE_CATCHUP,
            _RoomMemberHookPhase.CATCHUP,
        }

    def prepare_startup(
        self,
        *,
        transport: Literal["classic", "sliding"],
        resuming_position: bool,
    ) -> None:
        """Initialize startup without carrying Classic history authority into Sliding."""
        if not self.enabled:
            self._phase = _RoomMemberHookPhase.DISABLED
            return
        if transport == "sliding":
            self._phase = _RoomMemberHookPhase.SLIDING_CATCHUP
            return
        self._phase = _RoomMemberHookPhase.FULL_STATE_CATCHUP if resuming_position else _RoomMemberHookPhase.BASELINE

    def begin_sync_loop(self) -> None:
        """Prepare a replacement Classic loop to reacquire full state."""
        if self._phase is _RoomMemberHookPhase.CATCHUP:
            self._phase = _RoomMemberHookPhase.FULL_STATE_CATCHUP

    def baseline_recorded(self) -> None:
        """Advance after the exact full-state baseline markers reach durability."""
        if self._phase not in {
            _RoomMemberHookPhase.BASELINE,
            _RoomMemberHookPhase.FULL_STATE_CATCHUP,
        }:
            msg = f"Cannot record a room-member baseline while phase is {self._phase.value!r}"
            raise RuntimeError(msg)
        self._phase = _RoomMemberHookPhase.CATCHUP

    def plan_response(
        self,
        decision: SyncCertificationDecision,
        *,
        first_sync_response: bool,
    ) -> RoomMemberResponsePlan:
        """Return lifecycle work required before applying one certification decision."""
        certified = decision.state is SyncTrustState.CERTIFIED and not decision.reset_client_token
        return RoomMemberResponsePlan(
            drain_captured_timeline=(certified and self._phase is not _RoomMemberHookPhase.DISABLED),
            dispatch_state=(certified and self._phase is _RoomMemberHookPhase.LIVE and not first_sync_response),
        )

    def certified(self) -> None:
        """Enter live hook operation after every pre-certification effect succeeds."""
        if self.enabled:
            self._phase = _RoomMemberHookPhase.LIVE

    def rewound(self, *, has_retry_token: bool) -> None:
        """Fence callbacks until the replacement position is safely consumed."""
        if not self.enabled:
            return
        self._phase = _RoomMemberHookPhase.CATCHUP if has_retry_token else _RoomMemberHookPhase.BASELINE

    def unknown_position(self) -> None:
        """Treat a server-rejected cursor as a fresh tokenless baseline."""
        if self.enabled:
            self._phase = _RoomMemberHookPhase.BASELINE

    def stop(self) -> None:
        """Disable all lifecycle admission after the owning bot stops."""
        self._phase = _RoomMemberHookPhase.DISABLED
