"""Backward-compatible alias for the unified `triggers` toolkit."""

from __future__ import annotations

from typing import ClassVar

from mindroom.custom_tools.triggers import TriggersTools


class ExternalTriggerManagerTools(TriggersTools):
    """Backward-compatible subclass preserving the legacy toolkit name and context label."""

    _TOOL_NAME: ClassVar[str] = "external_trigger_manager"
    _CONTEXT_LABEL: ClassVar[str] = "External trigger manager"


__all__ = ["ExternalTriggerManagerTools"]
