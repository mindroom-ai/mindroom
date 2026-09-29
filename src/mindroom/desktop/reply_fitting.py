"""Build desktop success replies and fit them within one encrypted to-device message."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.desktop.protocol import MAX_INLINE_RESPONSE_BYTES, DesktopResponse

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.desktop.protocol import DesktopCommand, EncryptedDesktopMedia


def response_metrics(*, elapsed_ms: int, structured_bytes: int, screenshot_bytes: int) -> dict[str, int]:
    """Build the metrics every executed reply carries."""
    return {"elapsed_ms": elapsed_ms, "structured_bytes": structured_bytes, "screenshot_bytes": screenshot_bytes}


# Replies are fitted before their metrics are known, so fitting reserves the widest value of each metric;
# JSON consumers hold integers exactly only up to 2**53 - 1.
_WIDEST_METRICS = response_metrics(elapsed_ms=2**53 - 1, structured_bytes=2**53 - 1, screenshot_bytes=2**53 - 1)


def success_response(
    command: DesktopCommand,
    *,
    result: dict[str, object],
    screenshot: EncryptedDesktopMedia | None = None,
) -> DesktopResponse:
    """Build the successful reply to ``command``; ``fits_inline`` measures exactly this envelope."""
    return DesktopResponse(
        request_id=command.request_id,
        session_id=command.session_id,
        ok=True,
        result=result,
        screenshot=screenshot,
    )


def leftmost_fitting(
    command: DesktopCommand,
    low: int,
    high: int,
    build: Callable[[int], dict[str, object]],
) -> int:
    """Binary-search the smallest ``x`` in ``[low, high]`` whose enveloped ``build(x)`` reply still fits.

    Shared by every trimmed reply (shell output, listings, status), all measured by ``fits_inline``.
    Assumes ``build`` only shrinks the reply as ``x`` grows, and that ``build(high)`` fits.
    """
    while low < high:
        middle = (low + high) // 2
        if fits_inline(command, build(middle)):
            high = middle
        else:
            low = middle + 1
    return low


def fits_inline(command: DesktopCommand, result: dict[str, object]) -> bool:
    """Report whether ``result`` fits one to-device reply once execution adds its metrics."""
    reply = success_response(command, result={**result, "metrics": _WIDEST_METRICS})
    return reply.content_bytes() <= MAX_INLINE_RESPONSE_BYTES
