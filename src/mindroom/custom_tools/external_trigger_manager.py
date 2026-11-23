"""Backward-compatible alias for the unified `triggers` toolkit."""

from __future__ import annotations

from typing import ClassVar

from mindroom.custom_tools.triggers import TriggersTools


class ExternalTriggerManagerTools(TriggersTools):
    """Backward-compatible subclass preserving the legacy toolkit name, context label, and sync disable_trigger."""

    _TOOL_NAME: ClassVar[str] = "external_trigger_manager"
    _CONTEXT_LABEL: ClassVar[str] = "External trigger manager"

    def disable_trigger(self, trigger_id: str, enabled: bool = False) -> str:  # ty: ignore[incompatible-method-override]  # Legacy sync callers
        """Enable or disable a direct external trigger or reactive trigger synchronously."""
        return self._disable_trigger_sync(trigger_id, enabled=enabled)


__all__ = ["ExternalTriggerManagerTools"]
