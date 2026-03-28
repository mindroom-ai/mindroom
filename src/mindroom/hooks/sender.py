"""Hook-to-Matrix message sender registration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

type HookMessageSender = Callable[
    [str, str, str | None, str, dict[str, Any] | None],
    Awaitable[str | None],
]

_active_sender: HookMessageSender | None = None


def set_hook_message_sender(fn: HookMessageSender) -> None:
    """Register the active hook message sender."""
    global _active_sender
    _active_sender = fn


def get_hook_message_sender() -> HookMessageSender | None:
    """Return the active hook message sender, if any."""
    return _active_sender


def clear_hook_message_sender() -> None:
    """Clear the active hook message sender."""
    global _active_sender
    _active_sender = None
