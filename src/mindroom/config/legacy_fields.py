"""Diagnostics for retired configuration fields."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# LEGACY_COMPAT: Scalar knowledge_base agent configuration.
# Legacy format: Agent config accepted scalar knowledge_base instead of knowledge_bases.
# Last legacy release: v2026.2.32; replacement: v2026.2.33 removed the scalar field.
# Handling: Reject the retired spelling with a directed replacement instead of silently discarding it.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_agent_knowledge_base_field.

# LEGACY_COMPAT: Retired memory_dir agent configuration.
# Legacy format: Agent config accepted memory_dir for file-backed memory.
# Last legacy release: v2026.2.166; replacement: v2026.2.167 removed memory_dir.
# Handling: Reject it with guidance to current context files and file-memory configuration.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_agent_memory_dir_field.

# LEGACY_COMPAT: Retired memory_file_path agent configuration.
# Legacy format: Agent config accepted memory_file_path as a file-memory location.
# Last legacy release: v2026.3.100; replacement: v2026.3.101 moved file memory to canonical workspaces.
# Handling: Reject it for every memory backend and direct authored paths to the workspace model.
# Coverage: tests/test_agents.py::test_config_rejects_memory_file_path_even_with_file_backend.

# LEGACY_COMPAT: Sandbox tools configured under sandbox_tools.
# Legacy format: Agent and defaults config accepted sandbox_tools instead of worker_tools.
# Last legacy release: v2026.3.71; replacement: v2026.3.72 renamed the field to worker_tools.
# Handling: Reject both retired locations with their current field name.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_defaults_sandbox_tools_field.

# LEGACY_COMPAT: Agent tool selection through allowed_toolkits and initial_toolkits.
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


# LEGACY_COMPAT: Unversioned toolkit-selection fields in defaults.
# Legacy format: Defaults allowed_toolkits and initial_toolkits are unversioned authored input, not native fields.
# Last legacy release: no tagged native model; v2026.4.19 first rejected these previously ignored defaults keys.
# Handling: Reject both spellings with guidance to defaults.tools; do not infer a native removal release.
# Coverage: tests/test_agents.py::test_config_rejects_legacy_defaults_toolkit_fields.
# LEGACY_COMPAT: Retired defaults.auto_resume_after_restart switch.
# Legacy format: Defaults accepted auto_resume_after_restart to allow router resume messages after restarts.
# Last legacy release: v2026.10.121; replacement: v2026.10.122 continues interrupted replies in place and removes the field.
# Handling: Reject it with a note that interrupted replies now always continue in place.
# Coverage: tests/test_agents.py::test_config_rejects_retired_auto_resume_after_restart.
def reject_legacy_defaults_fields(value: object) -> object:
    """Reject retired default fields before regular model validation."""
    if isinstance(value, dict):
        if "auto_resume_after_restart" in value:
            msg = (
                "defaults.auto_resume_after_restart was removed. A reply interrupted by a restart now always "
                "continues in place in the same message; remove the setting."
            )
            raise ValueError(msg)
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


# LEGACY_COMPAT: Retired push-model auto-flush settings.
# Legacy format: memory.auto_flush.max_dirty_age_seconds and memory.auto_flush.max_cross_session_reprioritize, which
#   the dashboard wrote on every save.
# Last legacy release: v2026.10.335; replacement: v2026.10.336 flushes quiet conversations from stored sessions and
#   removes both fields.
# Handling: Drop both before validation, so a config that still sets them loads unchanged while every other unknown
#   memory.auto_flush key fails, and the next dashboard save removes them.
# Coverage: tests/test_memory_config.py::TestMemoryConfig::test_retired_auto_flush_fields_still_load.
_RETIRED_AUTO_FLUSH_FIELDS = frozenset({"max_dirty_age_seconds", "max_cross_session_reprioritize"})


def drop_retired_auto_flush_fields(value: object) -> object:
    """Return memory.auto_flush settings without the retired dirty-queue fields."""
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key not in _RETIRED_AUTO_FLUSH_FIELDS}
    return value


# LEGACY_COMPAT: Retired per-tool restrict_to_base_dir override.
# Legacy format: file, coding, and python tool entries accepted an authored boolean restrict_to_base_dir.
# Last legacy release: v2026.9.279; replacement: v2026.9.280 removed the field for the agents.<name>.file_access and
#   defaults.file_access settings.
# Handling: Reject it on every tool with guidance to the agent or defaults file_access setting.
# Coverage: tests/test_tools_metadata.py::test_restrict_to_base_dir_is_rejected_with_file_access_hint.
# LEGACY_COMPAT: Retired per-tool add_instructions switch of Agno's toolkits.
# Legacy format: reasoning, zep, and daytona tool entries accepted an authored boolean add_instructions that put the
#   toolkit's instructions in the agent's prompt.
# Last legacy release: v2026.10.600; replacement: unreleased; the next release always shows a toolkit's instructions
#   whenever it offers a function and removes the field.
# Handling: Reject it on every tool, naming the key and saying to remove it.
# Coverage: tests/test_tools_metadata.py::test_add_instructions_is_rejected_because_instructions_always_show,
#   tests/test_tools_metadata.py::test_config_load_rejects_a_retired_add_instructions_setting.
_REJECTED_TOOL_OVERRIDES = {
    "restrict_to_base_dir": "use agents.<name>.file_access or defaults.file_access",
    "add_instructions": "a toolkit's instructions always reach the prompt, so remove the setting",
}


def retired_tool_override(overrides: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first rejected retired field an entry authors, and its replacement guidance."""
    return next(((field, guidance) for field, guidance in _REJECTED_TOOL_OVERRIDES.items() if field in overrides), None)
