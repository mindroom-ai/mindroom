"""Classify Matrix state-event read and write failures by HTTP status."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio

_FALLBACK_STATUS = {"M_FORBIDDEN": 403, "M_NOT_FOUND": 404}


class StateFailure(StrEnum):
    """How a Matrix state read or write error should be handled."""

    REFUSED = "refused"
    MISSING = "missing"
    TRANSIENT = "transient"


def state_failure(response: nio.ErrorResponse) -> StateFailure:
    """Classify a state read/write error by HTTP status: 403 is REFUSED, 404 on a GET is MISSING, else TRANSIENT.

    When ``http_status`` is ``None`` (hand-built errors in tests or snapshots), infer 403/404 from ``status_code``.
    """
    import nio  # noqa: PLC0415 - room_settings imports this module without loading nio

    # No real HTTP answer reaches the errcode fallback; nio sets http_status on every parsed response.
    status = (
        response.http_status
        if isinstance(response.http_status, int)
        else _FALLBACK_STATUS.get(response.status_code or "")
    )
    if status == 403:
        return StateFailure.REFUSED
    if status == 404 and isinstance(response, nio.RoomGetStateEventError):
        return StateFailure.MISSING
    return StateFailure.TRANSIENT
