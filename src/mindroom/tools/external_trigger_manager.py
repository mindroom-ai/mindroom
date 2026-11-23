"""Backward-compatible Python import shim for the unified `triggers` toolkit."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mindroom.custom_tools.external_trigger_manager import ExternalTriggerManagerTools


def external_trigger_manager_tools() -> type[ExternalTriggerManagerTools]:
    """Return the backward-compatible external trigger manager toolkit class."""
    from mindroom.custom_tools.external_trigger_manager import ExternalTriggerManagerTools

    return ExternalTriggerManagerTools


__all__ = ["external_trigger_manager_tools"]
