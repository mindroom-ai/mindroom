"""Admission policy for historical Matrix callbacks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from mindroom.matrix.event_info import origin_server_ts_from_event_source

type _HistoricalDispatchFenceReason = Literal[
    "predates_startup_cutoff",
    "missing_origin_server_ts",
    "invalid_origin_server_ts",
]
type _HistoricalDispatchLogLevel = Literal["debug", "info"]


@dataclass(frozen=True, slots=True)
class _HistoricalDispatchDecision:
    """Describe whether one Matrix callback may enter user-visible dispatch."""

    dispatchable: bool
    reason: _HistoricalDispatchFenceReason | None = None
    log_level: _HistoricalDispatchLogLevel | None = None


@dataclass(slots=True)
class HistoricalDispatchFence:
    """Fence historical callbacks against one fixed construction-time cutoff."""

    startup_cutoff_ms: int
    armed: bool = False

    def set_armed(self, *, armed: bool) -> None:
        """Set whether timestamp admission is enforced."""
        self.armed = armed

    def classify(self, event_source: object) -> _HistoricalDispatchDecision:
        """Classify one raw Matrix event source for dispatch."""
        if not self.armed:
            return _HistoricalDispatchDecision(dispatchable=True)

        origin_server_ts = origin_server_ts_from_event_source(event_source)
        if isinstance(origin_server_ts, int) or (
            isinstance(origin_server_ts, float) and math.isfinite(origin_server_ts)
        ):
            if origin_server_ts >= self.startup_cutoff_ms:
                return _HistoricalDispatchDecision(dispatchable=True)
            return _HistoricalDispatchDecision(
                dispatchable=False,
                reason="predates_startup_cutoff",
                log_level="debug",
            )

        reason: _HistoricalDispatchFenceReason = (
            "missing_origin_server_ts" if origin_server_ts is None else "invalid_origin_server_ts"
        )
        return _HistoricalDispatchDecision(
            dispatchable=False,
            reason=reason,
            log_level="info",
        )
