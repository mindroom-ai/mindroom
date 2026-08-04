"""Matrix response transport-status validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, cast

if TYPE_CHECKING:
    import nio


class _HttpStatusResponse(Protocol):
    """Transport response surface attached by nio's AsyncClient."""

    status: int


def matrix_response_transport_succeeded(response: nio.Response) -> bool:
    """Return whether an AsyncClient response crossed a successful HTTP boundary."""
    # nio's base annotation uses its sync transport shape, but AsyncClient attaches an aiohttp-style response.
    transport: _HttpStatusResponse | None = cast("Any", response.transport_response)
    return transport is None or transport.status in range(200, 300)


__all__ = ["matrix_response_transport_succeeded"]
