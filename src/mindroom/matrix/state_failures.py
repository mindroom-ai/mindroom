"""Classify Matrix state-event read and write failures by HTTP status."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio

_FALLBACK_STATUS = {"M_FORBIDDEN": 403, "M_NOT_FOUND": 404}
TRANSIENT_HOMESERVER_HTTP_STATUSES = frozenset({502, 503, 504})


class StateFailure(StrEnum):
    """How a Matrix state read or write error should be handled."""

    REFUSED = "refused"
    MISSING = "missing"
    TRANSIENT = "transient"


def state_failure(response: nio.ErrorResponse) -> StateFailure:
    """Classify a state read/write error: 403 is REFUSED, 404 M_NOT_FOUND on a read is MISSING, else TRANSIENT.

    A 404 without M_NOT_FOUND, such as a proxy's, proves no absence, which could read as an unencrypted room. An error
    built by hand (tests, snapshots) has no ``http_status``, so its errcode stands in for it.
    """
    import nio  # noqa: PLC0415 - room_settings imports this module without loading nio

    status = (
        response.http_status
        if isinstance(response.http_status, int)
        else _FALLBACK_STATUS.get(response.status_code or "")
    )
    if status == 403:
        return StateFailure.REFUSED
    if status == 404 and response.status_code == "M_NOT_FOUND" and isinstance(response, nio.RoomGetStateEventError):
        return StateFailure.MISSING
    return StateFailure.TRANSIENT


def response_http_status(response: object) -> int | None:
    """Return the integer HTTP status of a Matrix response, if known."""
    http_status = getattr(response, "http_status", None)
    return http_status if isinstance(http_status, int) and not isinstance(http_status, bool) else None


def is_transient_homeserver_status(http_status: int | None) -> bool:
    """Return whether an HTTP status indicates a transient homeserver gateway/restart failure."""
    return http_status in TRANSIENT_HOMESERVER_HTTP_STATUSES
