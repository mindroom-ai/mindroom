"""Diagnostics for retired configuration fields."""

from __future__ import annotations


def reject_legacy_agent_fields(value: object) -> object:
    """Reject retired agent fields before regular model validation."""
    if isinstance(value, dict):
        if "knowledge_base" in value:
            msg = "Agent field 'knowledge_base' was removed. Use 'knowledge_bases' (list) instead."
            raise ValueError(msg)
        if "memory_dir" in value:
            msg = "Agent field 'memory_dir' was removed. Use 'context_files' and memory.backend=file instead."
            raise ValueError(msg)
        if "memory_file_path" in value:
            msg = (
                "Agent field 'memory_file_path' was removed. File-backed agent memory now lives in the "
                "canonical agent workspace root; keep memory_backend=file and configure context_files "
                "relative to that workspace."
            )
            raise ValueError(msg)
        if "sandbox_tools" in value:
            msg = "Agent field 'sandbox_tools' was removed. Use 'worker_tools' instead."
            raise ValueError(msg)
        if "allowed_toolkits" in value:
            msg = (
                "Agent field 'allowed_toolkits' was removed. Expand toolkit/preset/bundle entries into individual "
                "tools before applying per-tool defer flags in tools."
            )
            raise ValueError(msg)
        if "initial_toolkits" in value:
            msg = (
                "Agent field 'initial_toolkits' was removed. Expand toolkit/preset/bundle entries into individual "
                "tools before applying per-tool initial flags in tools."
            )
            raise ValueError(msg)
    return value


def reject_legacy_defaults_fields(value: object) -> object:
    """Reject retired default fields before regular model validation."""
    if isinstance(value, dict):
        if "sandbox_tools" in value:
            msg = "defaults.sandbox_tools was removed. Use defaults.worker_tools instead."
            raise ValueError(msg)
        if "allowed_toolkits" in value:
            msg = "defaults.allowed_toolkits was removed. Use defaults.tools instead."
            raise ValueError(msg)
        if "initial_toolkits" in value:
            msg = "defaults.initial_toolkits was removed. Use defaults.tools instead."
            raise ValueError(msg)
    return value
