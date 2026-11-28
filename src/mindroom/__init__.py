"""MindRoom: A universal interface for AI agents with persistent memory."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.vendor_telemetry import disable_vendor_telemetry as _disable_vendor_telemetry

if TYPE_CHECKING:
    __version__: str


def __getattr__(name: str) -> str:
    if name == "__version__":
        from importlib.metadata import version  # noqa: PLC0415

        globals()["__version__"] = resolved = version("mindroom")
        return resolved
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)


def __dir__() -> list[str]:
    return sorted({*globals(), "__version__"})


_disable_vendor_telemetry()
