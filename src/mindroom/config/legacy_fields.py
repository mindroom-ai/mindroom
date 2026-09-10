"""Diagnostics for retired configuration fields."""

from __future__ import annotations

# Legacy format: Agent config accepted scalar knowledge_base instead of knowledge_bases.
# Last legacy release: v2026.2.32; replacement: v2026.2.33 removed the scalar field.
# Handling: Reject the retired spelling with a directed replacement instead of silently discarding it.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_agent_knowledge_base_field.

# Legacy format: Agent config accepted memory_dir for file-backed memory.
# Last legacy release: v2026.2.166; replacement: v2026.2.167 removed memory_dir.
# Handling: Reject it with guidance to current context files and file-memory configuration.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_agent_memory_dir_field.

# Legacy format: Agent config accepted memory_file_path as a file-memory location.
# Last legacy release: v2026.3.100; replacement: v2026.3.101 moved file memory to canonical workspaces.
# Handling: Reject it for every memory backend and direct authored paths to the workspace model.
# Coverage: tests/test_agents.py::test_config_rejects_memory_file_path_even_with_file_backend.

# Legacy format: Agent and defaults config accepted sandbox_tools instead of worker_tools.
# Last legacy release: v2026.3.71; replacement: v2026.3.72 renamed the field to worker_tools.
# Handling: Reject both retired locations with their current field name.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_defaults_sandbox_tools_field.

# Legacy format: Agent config accepted allowed_toolkits and initial_toolkits.
# Last legacy release: v2026.6.11; replacement: v2026.6.12 removed both agent fields.
# Handling: Reject them with guidance to expand bundles into per-tool configuration.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_agent_toolkit_fields_with_bundle_safe_hint.


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


# Legacy format: Defaults allowed_toolkits and initial_toolkits are unversioned authored input, not native fields.
# Last legacy release: no tagged native model; v2026.4.19 first rejected these previously ignored defaults keys.
# Handling: Reject both spellings with guidance to defaults.tools; do not infer a native removal release.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_defaults_toolkit_fields.
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
