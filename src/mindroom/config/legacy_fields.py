"""Diagnostics for retired configuration fields."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping, Sequence

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
            msg = (
                "Agent field 'memory_dir' was removed. Every agent works in its own workspace: list the files it "
                "always loads in 'context_files', and set memory_backend=file to keep its memory files there."
            )
            raise ValueError(msg)
        if "memory_file_path" in value:
            msg = (
                "Agent field 'memory_file_path' was removed. With memory_backend=file, an agent's memory files "
                "live in its workspace root; configure context_files relative to that workspace."
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


# LEGACY_COMPAT: Root knowledge_reranker setting.
# Legacy format: the root reranker was configured as knowledge_reranker, when only search_knowledge_base used it.
# Last legacy release: v2026.11.415; replacement: v2026.11.416 renamed it; memory searches can rerank too, so the setting is reranker.
# Handling: Rename it to reranker before validation, so a config that still sets it loads unchanged and the next
#   dashboard save writes the new name; a config that sets both fails, naming both.
# Coverage: tests/test_config_validation_helpers.py::test_knowledge_reranker_key_still_loads,
#   tests/test_config_validation_helpers.py::test_both_reranker_keys_rejected.
def rename_legacy_knowledge_reranker(data: dict[str, object]) -> dict[str, object]:
    """Rename a retired ``knowledge_reranker`` key of root config ``data`` to ``reranker`` in place, and return ``data``."""
    if "knowledge_reranker" not in data:
        return data
    if "reranker" in data:
        msg = (
            "Both 'knowledge_reranker' and 'reranker' are set; 'knowledge_reranker' is the retired name, so remove it."
        )
        raise ValueError(msg)
    data["reranker"] = data.pop("knowledge_reranker")
    return data


# LEGACY_COMPAT: Retired push-model auto-flush settings.
# Legacy format: memory.auto_flush.max_dirty_age_seconds and memory.auto_flush.max_cross_session_reprioritize, which
#   the dashboard wrote on every save.
# Last legacy release: v2026.10.335; replacement: v2026.10.336 flushes quiet conversations from stored sessions and
#   removes both fields.
# Handling: Drop both before validation, so a config that still sets them loads unchanged while every other unknown
#   memory.auto_flush key fails, and the next dashboard save removes them.
# Coverage: tests/test_memory_config.py::TestMemoryConfig::test_retired_auto_flush_fields_still_load.
RETIRED_AUTO_FLUSH_FIELDS = frozenset({"max_dirty_age_seconds", "max_cross_session_reprioritize"})


# LEGACY_COMPAT: Retired memory.team_reads_member_memory switch.
# Legacy format: memory.team_reads_member_memory, which the dashboard wrote on every save, let memory reads made for a
#   whole team also read its members' agent scopes; nothing made such reads, so it changed nothing.
# Last legacy release: v2026.10.732; replacement: unreleased; each team scope is written once, below `team_memory/`,
#   and a team turn only writes to it, so the field is removed.
# Handling: Drop it before validation, so a config that still sets it loads unchanged and the next dashboard save
#   removes it.
# Coverage: tests/test_memory_config.py::TestMemoryConfig::test_retired_team_memory_fields_still_load.
#
# LEGACY_COMPAT: Retired memory.file section.
# Legacy format: memory.file held path, a fallback root that changed nothing (team and agent file memory always lived
#   in their canonical roots), and max_entrypoint_lines and max_entrypoint_tokens, the MEMORY.md preload caps, all
#   written by the dashboard on every save.
# Last legacy release: v2026.11.480; replacement: unreleased; every file inlined into the system prompt, MEMORY.md
#   included, is cut by defaults.max_preload_file_chars, so the section is removed.
# Handling: Drop the whole section before validation, so a config that still sets it loads and the next dashboard
#   save removes it; the old caps are not mapped to the new limit.
# Coverage: tests/test_memory_config.py::TestMemoryConfig::test_retired_team_memory_fields_still_load,
#   tests/test_agents.py::test_config_ignores_the_retired_shared_preload_budget.
RETIRED_MEMORY_FIELDS = frozenset({"team_reads_member_memory", "file"})


# LEGACY_COMPAT: Retired defaults.max_preload_chars shared budget.
# Legacy format: defaults.max_preload_chars capped all context files together, so one growing file shortened or
#   dropped the others.
# Last legacy release: v2026.11.480; replacement: unreleased; defaults.max_preload_file_chars cuts each file on its own.
# Handling: Drop it before validation, so a config that still sets it loads with the per-file default; a total budget
#   is not mapped to a per-file limit.
# Coverage: tests/test_agents.py::test_config_ignores_the_retired_shared_preload_budget.
RETIRED_DEFAULTS_FIELDS = frozenset({"max_preload_chars"})


def drop_retired_fields(value: object, fields: frozenset[str]) -> object:
    """Return a settings mapping without the retired ``fields``; any other value as it is."""
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key not in fields}
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
# LEGACY_COMPAT: Retired per-tool file_access override of the script tool.
# Legacy format: a script tool entry authored `file_access: unconfined`, the only value its unconfined class accepted.
# Last legacy release: v2026.11.71; replacement: unreleased; the next release declares script with no file access,
#   because it reads only its workspace source and runs its scripts on the script backend.
# Handling: Reject the key on script entries, saying to remove it.
# Coverage: tests/test_tools_metadata.py::test_script_file_access_override_is_rejected_with_removal_hint.
_REJECTED_TOOL_OVERRIDES_BY_TOOL = {
    "script": {
        "file_access": "script reads only its workspace source and declares no file access, so remove the setting",
    },
}


# LEGACY_COMPAT: Agno FileTools settings of the file tool.
# Legacy format: file tool entries took Agno FileTools settings: enable_<function> switches, max_file_length,
#   max_file_lines, line_separator, exclude_patterns, all, and expose_base_directory, and include_tools or
#   exclude_tools naming save_file, read_file_chunk, replace_file_chunk, list_files, search_files, or search_content.
# Last legacy release: v2026.11.447; replacement: unreleased; file and coding both name the filesystem toolkit.
# Handling: Reject each one, whatever its value, on a filesystem entry authored under any of its names in defaults or
#   on an agent, and in settings stored for filesystem, naming the filesystem setting to write instead. None is
#   translated, so a function that was switched off never comes back on.
# Coverage:
#   tests/test_filesystem_tool_config.py::test_an_old_agno_setting_on_a_file_entry_fails_config_load_naming_its_replacement,
#   tests/api/test_plugin_room_agents.py::test_old_settings_copied_under_filesystem_stop_the_toolkit_and_its_workspace_grant.
RETIRED_FILE_FUNCTIONS = {
    "save_file": "write_file",
    "replace_file_chunk": "edit_file",
    "read_file_chunk": "read_file",
    "list_files": "ls",
    "search_files": "find_files",
    "search_content": "grep",
}
_RETIRED_FILE_SETTINGS = {
    **{
        f"enable_{old}": f"use `filesystem: {{exclude_tools: [{new}]}}` to switch {new} off"
        for old, new in {"read_file": "read_file", **RETIRED_FILE_FUNCTIONS}.items()
    },
    "all": "remove it; set `filesystem: {enable_delete_file: true}` only if it enabled deletion and you still want that",
    **dict.fromkeys(
        ("max_file_length", "max_file_lines", "line_separator", "exclude_patterns", "expose_base_directory"),
        "filesystem has no such setting, so remove it",
    ),
}


# LEGACY_COMPAT: Approval rules written for the file tool's retired function names.
# Legacy format: a tool_approval.rules[].match glob matching save_file, replace_file_chunk, read_file_chunk,
#   list_files, search_files, or search_content, the functions of Agno's FileTools.
# Last legacy release: v2026.11.447; replacement: unreleased; the filesystem toolkit names them write_file,
#   edit_file, read_file, ls, find_files, and grep.
# Handling: Reject the rules when the first one matching a retired name is not the first one matching its
#   replacement, unless another registered toolkit still offers the retired name, naming that rule and the
#   replacement, so the rule that decided a call still decides it.
# Coverage:
#   tests/test_filesystem_tool_config.py::test_an_approval_rule_that_stops_deciding_a_renamed_function_fails,
#   tests/test_filesystem_tool_config.py::test_an_earlier_approval_rule_that_takes_the_replacement_from_its_gate_fails,
#   tests/test_filesystem_tool_config.py::test_approval_rules_that_decide_the_new_names_as_they_did_the_old_load.
def approval_rule_a_file_rename_escapes(globs: Sequence[str], offered_elsewhere: Collection[str]) -> str | None:
    """Return the rule that decided a retired file function but not its replacement, as text, or None."""
    for old, new in RETIRED_FILE_FUNCTIONS.items():
        first_old, first_new = (
            next((index for index, glob in enumerate(globs) if fnmatchcase(name, glob)), None) for name in (old, new)
        )
        if old not in offered_elsewhere and first_old is not None and first_old != first_new:
            return f"tool_approval.rules[{first_old}].match {globs[first_old]!r} decided {old}; {old} is now {new}"
    return None


def retired_file_setting(settings: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first Agno FileTools setting or retired function filter in ``settings``, and what replaces it."""
    for key in ("include_tools", "exclude_tools"):
        value = settings.get(key)
        names = value.replace("\n", ",").split(",") if isinstance(value, str) else value
        for name in names if isinstance(names, list) else ():
            if isinstance(name, str) and (new := RETIRED_FILE_FUNCTIONS.get(name.strip())):
                return f"{key}: {name.strip()}", f"use `filesystem: {{{key}: [{new}]}}`"
    return next(((field, guidance) for field, guidance in _RETIRED_FILE_SETTINGS.items() if field in settings), None)


def retired_tool_override(tool_name: str, overrides: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first rejected retired field an entry of ``tool_name`` authors, and its replacement guidance."""
    if tool_name == "filesystem" and (retired := retired_file_setting(overrides)) is not None:
        return retired
    rejected = _REJECTED_TOOL_OVERRIDES | _REJECTED_TOOL_OVERRIDES_BY_TOOL.get(tool_name, {})
    return next(((field, guidance) for field, guidance in rejected.items() if field in overrides), None)


# LEGACY_COMPAT: Retired max_retries setting of OpenAI-family models.
# Legacy format: models.<name>.extra_kwargs.max_retries on an OpenAI-family route set the OpenAI SDK's own HTTP retries,
#   beneath the request pipeline's retries.
# Last legacy release: v2026.10.642; replacement: v2026.10.643; the request pipeline retries a failed model request
#   and the SDK clients' configurable retries are off.
# Handling: Reject it at config load on every provider, naming the key and saying to remove it.
# Coverage: tests/test_extra_kwargs.py::test_a_retired_max_retries_fails_config_load_with_its_reason.
# LEGACY_COMPAT: Retired role_map setting of Chat Completions models.
# Legacy format: models.<name>.extra_kwargs.role_map on a Chat Completions route, Agno's OpenAIChat field mapping each
#   message role to the role a request sends.
# Last legacy release: v2026.10.663, which accepted it as a typed Chat setting, as Agno's model classes did through
#   v2026.10.498; replacement: v2026.10.664; requests send the system prompt as developer to OpenAI's own API and as
#   system to every other Chat Completions endpoint, and every other role as it is.
# Handling: Reject it at config load on every provider, naming the key and saying what to do instead.
# Coverage: tests/test_extra_kwargs.py::test_a_retired_role_map_fails_config_load_with_its_reason.
_RETIRED_MODEL_SETTINGS = {
    "max_retries": "MindRoom retries a failed model request itself, so remove the setting",
    "role_map": (
        "Chat Completions requests send the system prompt as developer to OpenAI's own API and as system to every "
        "other endpoint, an OpenAI-compatible server set as provider openai with its base_url included, so remove the "
        "setting"
    ),
}


def retired_model_setting(keys: Iterable[str]) -> tuple[str, str] | None:
    """Return the first retired ``extra_kwargs`` setting among ``keys``, and its guidance."""
    return next(((key, _RETIRED_MODEL_SETTINGS[key]) for key in keys if key in _RETIRED_MODEL_SETTINGS), None)
