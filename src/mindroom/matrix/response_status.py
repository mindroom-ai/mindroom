"""Matrix response transport-status validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    import nio


class _HttpStatusResponse(Protocol):
    """Transport response surface attached by nio's AsyncClient."""

    status: int


def matrix_response_transport_status(response: nio.Response) -> int | None:
    """Return the attached AsyncClient HTTP status when one exists."""
    # nio's base annotation uses its sync transport shape, but AsyncClient attaches an aiohttp-style response.
    transport: _HttpStatusResponse | None = cast("Any", response.transport_response)
    return transport.status if transport is not None else None


def matrix_response_is_not_found(response: nio.ErrorResponse) -> bool:
    """Return whether an error authoritatively proves Matrix resource absence."""
    status = matrix_response_transport_status(response)
    return response.status_code == "M_NOT_FOUND" and status in (None, 404)


def matrix_response_transport_succeeded(response: nio.Response) -> bool:
    """Return whether an AsyncClient response crossed a successful HTTP boundary."""
    status = matrix_response_transport_status(response)
    return status is None or status in range(200, 300)


__all__ = [
    "matrix_response_is_not_found",
    "matrix_response_transport_status",
    "matrix_response_transport_succeeded",
]
