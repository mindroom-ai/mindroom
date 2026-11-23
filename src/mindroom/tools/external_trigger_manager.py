"""Backward-compatible Python import shim for the unified `triggers` toolkit."""

from __future__ import annotations

from mindroom.tools.triggers import triggers_tools

external_trigger_manager_tools = triggers_tools

__all__ = ["external_trigger_manager_tools"]
