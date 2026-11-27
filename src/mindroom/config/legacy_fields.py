"""Diagnostics for retired configuration fields."""

from __future__ import annotations

from fnmatch import fnmatchcase
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
    from typing import Protocol

    class _ApprovalRule(Protocol):
        @property
        def match(self) -> str: ...
        @property
        def action(self) -> str | None: ...
        @property
        def script(self) -> str | None: ...
        @property
        def timeout_days(self) -> float | None: ...

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


# LEGACY_COMPAT: Retired preload truncation marker prompts.
# Legacy format: prompts.CONTEXT_TRUNCATION_MARKER_TEMPLATE, prompts.CONTEXT_CHUNK_OMITTED_MARKER_TEMPLATE, and
#   prompts.FILE_MEMORY_ENTRYPOINT_TRUNCATION_TEMPLATE overrode the markers of the shared context budget and the
#   MEMORY.md line and token caps.
# Last legacy release: v2026.11.480; replacement: unreleased; prompts.PRELOAD_FILE_CUT_MARKER_TEMPLATE marks every
#   cut file, and its placeholders differ, so an old override is not carried over.
# Handling: Drop them before validation, so a config that still overrides them loads and the next dashboard save
#   removes them.
# Coverage: tests/test_agents.py::test_config_ignores_overrides_of_the_retired_cut_markers.
#
# LEGACY_COMPAT: Retired natural-language workflow schedule parser prompt.
# Legacy format: prompts.WORKFLOW_SCHEDULE_PARSE_PROMPT_TEMPLATE overrode the LLM prompt used to parse `!schedule`
#   and `scheduler` natural-language strings into `ScheduledWorkflow` JSON.
# Last legacy release: v2026.11.574; replacement: unreleased; structured scheduler tool calls parse no model prompt.
# Handling: Drop it before validation, so a config that still overrides it loads and the next dashboard save removes it.
# Coverage: tests/test_agents.py::test_config_ignores_overrides_of_the_retired_schedule_parse_prompt.
RETIRED_PROMPT_NAMES = frozenset(
    {
        "CONTEXT_CHUNK_OMITTED_MARKER_TEMPLATE",
        "CONTEXT_TRUNCATION_MARKER_TEMPLATE",
        "FILE_MEMORY_ENTRYPOINT_TRUNCATION_TEMPLATE",
        "WORKFLOW_SCHEDULE_PARSE_PROMPT_TEMPLATE",
    },
)


def drop_retired_fields(value: object, fields: frozenset[str]) -> object:
    """Return a settings mapping without the retired ``fields``; any other value as it is."""
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key not in fields}
    return value


# LEGACY_COMPAT: Retired overlapping built-in toolkits.
# Legacy format: Authored tool lists accepted web_browser_tools, jira, confluence, and python.
# Last legacy release: v2026.11.615; replacement: unreleased; use browser, atlassian, or shell and filesystem.
# Handling: Reject the old names with explicit migration guidance; never grant replacement capabilities implicitly.
# Coverage: tests/test_retired_tools.py.
_RETIRED_TOOL_REPLACEMENTS = {
    "web_browser_tools": "choose and configure `browser` explicitly if you want Chromium or desktop browser access",
    "jira": "configure the `atlassian` tool and connect an Atlassian account",
    "confluence": "configure the `atlassian` tool and connect an Atlassian account",
    "python": "use `shell` to run Python and `filesystem` to read and write files",
}


def retired_tool_message(name: str) -> str | None:
    """Return migration guidance for a retired toolkit or one of its functions."""
    toolkit = name.partition(".")[0]
    if toolkit in _RETIRED_TOOL_REPLACEMENTS:
        return f"Tool {toolkit!r} was removed; {_RETIRED_TOOL_REPLACEMENTS[toolkit]}."
    return None


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
_RETIRED_FILE_FUNCTIONS = {
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
        for old, new in {"read_file": "read_file", **_RETIRED_FILE_FUNCTIONS}.items()
    },
    "all": "remove it; set `filesystem: {enable_delete_file: true}` only if it enabled deletion and you still want that",
    **dict.fromkeys(
        ("max_file_length", "max_file_lines", "line_separator", "exclude_patterns", "expose_base_directory"),
        "filesystem has no such setting, so remove it",
    ),
}


# LEGACY_COMPAT: Approval rules written for Agno FileTools function names.
# Legacy format: a tool_approval.rules[].match glob matching save_file, replace_file_chunk, read_file_chunk,
#   list_files, search_files, or search_content, the functions of Agno's FileTools.
# Last legacy release: v2026.11.447; replacement: unreleased; the filesystem toolkit names them write_file,
#   edit_file, read_file, ls, find_files, and grep.
# Handling: When the config enables `filesystem`, reject the rules when the first one matching a retired name is
#   not the first one matching its replacement, unless another registered toolkit still offers the retired name,
#   naming that rule and the replacement, so the rule that decided a call still decides it.
# Coverage:
#   tests/test_filesystem_tool_config.py::test_an_approval_rule_that_stops_deciding_a_renamed_function_fails,
#   tests/test_filesystem_tool_config.py::test_an_earlier_approval_rule_that_takes_the_replacement_from_its_gate_fails,
#   tests/test_filesystem_tool_config.py::test_approval_rules_that_decide_the_new_names_as_they_did_the_old_load,
#   tests/test_filesystem_tool_config.py::test_approval_rules_for_a_retired_file_name_offered_elsewhere_load,
#   tests/test_filesystem_tool_config.py::test_retired_file_approval_check_skips_configs_without_filesystem_and_checks_presets.
def approval_rule_a_file_rename_escapes(globs: Sequence[str], offered_elsewhere: Collection[str]) -> str | None:
    """Return the rule that decided a retired file function but not its replacement, as text, or None."""
    for old, new in _RETIRED_FILE_FUNCTIONS.items():
        first_old, first_new = (
            next((index for index, glob in enumerate(globs) if fnmatchcase(name, glob)), None) for name in (old, new)
        )
        if old not in offered_elsewhere and first_old is not None and first_old != first_new:
            return f"tool_approval.rules[{first_old}].match {globs[first_old]!r} decided {old}; {old} is now {new}"
    return None


# LEGACY_COMPAT: Retired individual functions of the triggers (external_trigger_manager) toolkit.
# Legacy format: triggers / external_trigger_manager exposed 8 separate functions (watch_room, watch_thread,
#   subscribe_event, create_trigger, list_triggers, disable_trigger, delete_trigger, rotate_trigger_key) and
#   later a separate watch_pr function, which could appear in tool_approval.rules[].match globs or
#   include_tools/exclude_tools overrides.
# Last legacy release: v2026.11.480 (original 8) and v2026.11.633 (watch_pr); replacement: unreleased; all 9 were
#   collapsed into a single `triggers` function taking an `action` parameter.
# Handling: When the config enables `triggers` (or its alias `external_trigger_manager`), allow configs with an
#   explicit exact `triggers` rule deciding `triggers`, and otherwise reject tool_approval.rules when any retired
#   function's effective decision differs from `triggers`'s effective decision (regardless of other toolkits);
#   also reject include_tools or exclude_tools naming a retired trigger function on a triggers entry, naming `triggers`.
# Coverage:
#   tests/test_triggers_tool.py::test_an_approval_rule_that_stops_deciding_a_retired_trigger_function_fails,
#   tests/test_triggers_tool.py::test_an_earlier_approval_rule_that_takes_triggers_from_a_retired_gate_fails,
#   tests/test_triggers_tool.py::test_approval_rules_that_decide_triggers_load,
#   tests/test_triggers_tool.py::test_retired_trigger_approval_check_skips_configs_without_triggers,
#   tests/test_triggers_tool.py::test_retired_trigger_approval_rule_weakening_require_approval_default_fails,
#   tests/test_triggers_tool.py::test_retired_trigger_approval_rule_changing_timeout_days_fails,
#   tests/test_triggers_tool.py::test_retired_trigger_include_exclude_overrides_fail_config_load.
_RETIRED_TRIGGER_FUNCTIONS: dict[str, str] = {
    "watch_room": "triggers",
    "watch_thread": "triggers",
    "watch_pr": "triggers",
    "subscribe_event": "triggers",
    "create_trigger": "triggers",
    "list_triggers": "triggers",
    "disable_trigger": "triggers",
    "delete_trigger": "triggers",
    "rotate_trigger_key": "triggers",
}

# LEGACY_COMPAT: Retired individual functions of consolidated toolkits.
# Legacy format: scheduler, searxng, memory, todo, Wave 3b native toolkits (gmail, google_calendar, github,
#   google_drive, google_sheets, homeassistant, including prefixed secondary Google Workspace plugin copies),
#   Wave 5 native toolkits (file_generation, visualization, unsplash), Wave 6 native toolkits (csv, duckdb, sql,
#   postgres, redshift, neo4j, yfinance, openbb, financial_datasets_api, openweather, docker, e2b, daytona),
#   Wave 7 comms toolkits (email, twilio, discord, telegram, whatsapp, webex, zoom, x), and Wave 8 native toolkits
#   (linear, notion, todoist, clickup, trello, bitbucket, cal_com, shopify, spotify, reddit, google_maps, brandfetch,
#   mem0, zep), skills, and attachments previously exposed individual per-operation function names that could appear in
#   tool_approval.rules[].match globs.
# Last legacy release: v2026.11.610 (Waves 3b, 5-8: v2026.11.611-v2026.11.625; skills/attachments: v2026.11.657);
#   replacement: unreleased; consolidated into action-dispatched and risk-scoped functions.
# Handling: When the config enables one of these toolkits (or a prefixed Google Workspace clone, or unconditionally
#   for generated skills), allow configs with an explicit exact rule deciding the replacement function, and otherwise
#   reject tool_approval.rules when a retired skills/attachments function is decided by an approval script or any
#   retired function's effective decision differs from the replacement's effective decision (regardless of other
#   toolkits); also reject include_tools or exclude_tools naming a retired attachment function on an attachments entry,
#   directing operators to `attachment` / `view_file` and `tool_approval.rules`.
# Coverage:
#   tests/test_tool_approval_grants.py::test_retired_consolidated_approval_rules_fail_closed,
#   tests/test_tool_approval_grants.py::test_retired_consolidated_approval_rules_fail_closed_across_agents_sharing_name,
#   tests/test_tool_approval_grants.py::test_secondary_google_workspace_retired_approval_rules_fail_closed,
#   tests/test_tool_approval_grants.py::test_wildcard_approval_script_rule_fails_closed_for_retired_attachment_and_skill_names,
#   tests/test_tool_approval_grants.py::test_retired_attachments_include_exclude_overrides_fail_config_load,
#   tests/test_wave6_data_tools.py::test_wave6_batch1_action_scoped_grants_and_fail_closed_migration,
#   tests/test_wave6_db_tools.py::test_wave6_batch2_action_scoped_grants_and_fail_closed_migration,
#   tests/test_wave6_finance_weather_tools.py::test_wave6_batch3_action_scoped_grants_and_fail_closed_migration,
#   tests/test_wave6_sandbox_tools.py::test_wave6_batch4_action_scoped_grants_and_fail_closed_migration.
# LEGACY_COMPAT: Retired `slack_watch` function of the `slack` toolkit.
# Legacy format: `slack: {include_tools: [slack_watch]}` / `exclude_tools` and `tool_approval.rules` matching `slack_watch`.
# Last legacy release: v2026.11.651; replacement: `triggers(action="subscribe_event", source_id="slack", ...)`.
# Handling: Reject `slack_watch` in `include_tools`/`exclude_tools` on `slack` and fail closed in `tool_approval.rules`
#   whenever both `slack` and `triggers` toolkits are enabled and `slack_watch` conflicts with `triggers` (allowing broad
#   `slack_*` globs paired with an explicit exact `triggers` rule).
# Coverage: tests/slack/test_event_source.py::test_retired_slack_watch_config_validation_fails_closed.
_RETIRED_SLACK_FUNCTIONS: dict[str, str] = {
    "slack_watch": "triggers",
}

RETIRED_CONSOLIDATED_FUNCTIONS: dict[str, dict[str, str]] = {
    "scheduler": {
        "schedule": "scheduler",
        "edit_schedule": "scheduler",
        "list_schedules": "scheduler",
        "cancel_schedule": "scheduler",
    },
    "searxng": {
        "search_web": "searxng_search",
        "image_search": "searxng_search",
        "it_search": "searxng_search",
        "map_search": "searxng_search",
        "music_search": "searxng_search",
        "news_search": "searxng_search",
        "science_search": "searxng_search",
        "video_search": "searxng_search",
    },
    "memory": {
        "add_memory": "memory",
        "search_memories": "memory",
        "list_memories": "memory",
        "get_memory": "memory",
        "update_memory": "memory",
        "delete_memory": "memory",
    },
    "todo": {
        "plan": "todo",
        "add_todo": "todo",
        "list_todos": "todo",
        "update_todo": "todo",
        "apply_template": "todo",
        "list_templates": "todo",
    },
    "gmail": {
        "get_latest_emails": "gmail_read",
        "get_emails_from_user": "gmail_read",
        "get_unread_emails": "gmail_read",
        "get_starred_emails": "gmail_read",
        "get_emails_by_context": "gmail_read",
        "get_emails_by_date": "gmail_read",
        "get_emails_by_thread": "gmail_read",
        "search_emails": "gmail_read",
        "get_message": "gmail_read",
        "get_thread": "gmail_read",
        "search_threads": "gmail_read",
        "list_custom_labels": "gmail_read",
        "get_draft": "gmail_read",
        "list_drafts": "gmail_read",
        "create_draft_email": "gmail_draft",
        "update_draft": "gmail_draft",
        "send_email": "gmail_send",
        "send_email_reply": "gmail_send",
        "send_email_to_self": "gmail_send_to_self",
        "mark_email_as_read": "gmail_manage",
        "mark_email_as_unread": "gmail_manage",
        "star_email": "gmail_manage",
        "unstar_email": "gmail_manage",
        "apply_label": "gmail_manage",
        "remove_label": "gmail_manage",
        "delete_custom_label": "gmail_manage",
    },
    "google_calendar": {
        "list_calendars": "google_calendar_read",
        "list_events": "google_calendar_read",
        "fetch_all_events": "google_calendar_read",
        "get_event": "google_calendar_read",
        "search_events": "google_calendar_read",
        "get_event_attendees": "google_calendar_read",
        "check_availability": "google_calendar_read",
        "find_available_slots": "google_calendar_read",
        "create_event": "google_calendar_write",
        "update_event": "google_calendar_write",
        "delete_event": "google_calendar_write",
        "quick_add_event": "google_calendar_write",
        "respond_to_event": "google_calendar_write",
        "move_event": "google_calendar_write",
    },
    "github": {
        "list_repositories": "github_read_repo",
        "search_repositories": "github_read_repo",
        "get_repository": "github_read_repo",
        "get_repository_with_stats": "github_read_repo",
        "get_repository_languages": "github_read_repo",
        "get_repository_stars": "github_read_repo",
        "list_branches": "github_read_repo",
        "get_file_content": "github_read_repo",
        "get_directory_content": "github_read_repo",
        "get_branch_content": "github_read_repo",
        "search_code": "github_read_repo",
        "list_issues": "github_read_issues_prs",
        "get_issue": "github_read_issues_prs",
        "list_issue_comments": "github_read_issues_prs",
        "get_pull_requests": "github_read_issues_prs",
        "get_pull_request": "github_read_issues_prs",
        "get_pull_request_changes": "github_read_issues_prs",
        "get_pull_request_comments": "github_read_issues_prs",
        "get_pull_request_with_details": "github_read_issues_prs",
        "get_pull_request_count": "github_read_issues_prs",
        "search_issues_and_prs": "github_read_issues_prs",
        "create_issue": "github_write_issues",
        "edit_issue": "github_write_issues",
        "comment_on_issue": "github_write_issues",
        "close_issue": "github_write_issues",
        "reopen_issue": "github_write_issues",
        "assign_issue": "github_write_issues",
        "label_issue": "github_write_issues",
        "create_pull_request": "github_write_pr",
        "create_pull_request_comment": "github_write_pr",
        "edit_pull_request_comment": "github_write_pr",
        "create_review_request": "github_write_pr",
        "create_repository": "github_write_repo",
        "delete_repository": "github_write_repo",
        "create_branch": "github_write_repo",
        "set_default_branch": "github_write_repo",
        "create_file": "github_write_repo",
        "update_file": "github_write_repo",
        "delete_file": "github_write_repo",
    },
    "google_drive": {
        "google_drive_list_files": "google_drive_read",
        "google_drive_search_files": "google_drive_read",
        "google_drive_read_file": "google_drive_read",
        "google_drive_download_file": "google_drive_read",
        "google_drive_upload_file": "google_drive_write",
        "google_drive_update_file": "google_drive_write",
        "google_drive_create_folder": "google_drive_write",
        "google_drive_move_file": "google_drive_write",
        "google_drive_trash_file": "google_drive_write",
    },
    "google_sheets": {
        "read_sheet": "google_sheets_read",
        "create_sheet": "google_sheets_write",
        "update_sheet": "google_sheets_write",
        "batch_update_sheet": "google_sheets_write",
        "create_duplicate_sheet": "google_sheets_write",
    },
    "homeassistant": {
        "list_entities": "homeassistant_read",
        "get_entity_state": "homeassistant_read",
        "turn_on": "homeassistant_control",
        "turn_off": "homeassistant_control",
        "toggle": "homeassistant_control",
        "set_brightness": "homeassistant_control",
        "set_color": "homeassistant_control",
        "set_temperature": "homeassistant_control",
        "activate_scene": "homeassistant_control",
        "trigger_automation": "homeassistant_control",
        "call_service": "homeassistant_control",
    },
    "csv": {
        "list_csv_files": "csv_inspect",
        "get_columns": "csv_inspect",
        "read_csv_file": "csv_inspect",
    },
    "duckdb": {
        "show_tables": "duckdb_inspect",
        "describe_table": "duckdb_inspect",
        "summarize_table": "duckdb_inspect",
        "inspect_query": "duckdb_inspect",
        "full_text_search": "duckdb_inspect",
        "create_table_from_path": "duckdb_manage",
        "load_local_path_to_table": "duckdb_manage",
        "load_local_csv_to_table": "duckdb_manage",
        "load_s3_path_to_table": "duckdb_manage",
        "load_s3_csv_to_table": "duckdb_manage",
        "export_table_to_path": "duckdb_manage",
        "create_fts_index": "duckdb_manage",
        "get_table_name_from_path": "duckdb_manage",
    },
    "sql": {
        "list_tables": "sql_inspect",
        "describe_table": "sql_inspect",
    },
    "postgres": {
        "show_tables": "postgres_inspect",
        "describe_table": "postgres_inspect",
        "summarize_table": "postgres_inspect",
        "inspect_query": "postgres_inspect",
    },
    "redshift": {
        "show_tables": "redshift_inspect",
        "describe_table": "redshift_inspect",
        "summarize_table": "redshift_inspect",
        "inspect_query": "redshift_inspect",
    },
    "neo4j": {
        "list_labels": "neo4j_inspect",
        "list_relationship_types": "neo4j_inspect",
        "get_schema": "neo4j_inspect",
    },
    "yfinance": {
        "get_current_stock_price": "yfinance_get",
        "get_company_info": "yfinance_get",
        "get_stock_fundamentals": "yfinance_get",
        "get_income_statements": "yfinance_get",
        "get_key_financial_ratios": "yfinance_get",
        "get_analyst_recommendations": "yfinance_get",
        "get_company_news": "yfinance_get",
        "get_technical_indicators": "yfinance_get",
        "get_historical_stock_prices": "yfinance_get",
    },
    "openbb": {
        "get_stock_price": "openbb_get",
        "search_company_symbol": "openbb_get",
        "get_company_news": "openbb_get",
        "get_company_profile": "openbb_get",
        "get_price_targets": "openbb_get",
    },
    "financial_datasets_api": {
        "get_income_statements": "financial_datasets_financials",
        "get_balance_sheets": "financial_datasets_financials",
        "get_cash_flow_statements": "financial_datasets_financials",
        "get_segmented_financials": "financial_datasets_financials",
        "get_financial_metrics": "financial_datasets_financials",
        "get_company_info": "financial_datasets_financials",
        "get_earnings": "financial_datasets_financials",
        "get_insider_trades": "financial_datasets_financials",
        "get_institutional_ownership": "financial_datasets_financials",
        "get_sec_filings": "financial_datasets_financials",
        "get_stock_prices": "financial_datasets_market",
        "get_crypto_prices": "financial_datasets_market",
        "get_news": "financial_datasets_market",
        "search_tickers": "financial_datasets_market",
    },
    "openweather": {
        "get_current_weather": "openweather_get",
        "get_forecast": "openweather_get",
        "get_air_pollution": "openweather_get",
        "geocode_location": "openweather_get",
    },
    "docker": {
        "list_containers": "docker_inspect",
        "inspect_container": "docker_inspect",
        "get_container_logs": "docker_inspect",
        "list_images": "docker_inspect",
        "inspect_image": "docker_inspect",
        "list_volumes": "docker_inspect",
        "inspect_volume": "docker_inspect",
        "list_networks": "docker_inspect",
        "inspect_network": "docker_inspect",
        "run_container": "docker_container",
        "exec_in_container": "docker_container",
        "start_container": "docker_container",
        "stop_container": "docker_container",
        "remove_container": "docker_container",
        "pull_image": "docker_resource",
        "build_image": "docker_resource",
        "tag_image": "docker_resource",
        "remove_image": "docker_resource",
        "create_volume": "docker_resource",
        "remove_volume": "docker_resource",
        "create_network": "docker_resource",
        "remove_network": "docker_resource",
        "connect_container_to_network": "docker_resource",
        "disconnect_container_from_network": "docker_resource",
    },
    "e2b": {
        "run_python_code": "e2b_run",
        "run_command": "e2b_run",
        "stream_command": "e2b_run",
        "run_background_command": "e2b_run",
        "kill_background_command": "e2b_run",
        "run_server": "e2b_run",
        "list_files": "e2b_inspect",
        "read_file_content": "e2b_inspect",
        "watch_directory": "e2b_inspect",
        "get_sandbox_status": "e2b_inspect",
        "list_running_sandboxes": "e2b_inspect",
        "get_public_url": "e2b_inspect",
        "write_file_content": "e2b_files",
        "upload_file": "e2b_files",
        "download_file_from_sandbox": "e2b_files",
        "download_png_result": "e2b_files",
        "download_chart_data": "e2b_files",
        "set_sandbox_timeout": "e2b_lifecycle",
        "shutdown_sandbox": "e2b_lifecycle",
    },
    "daytona": {
        "run_code": "daytona_run",
        "run_shell_command": "daytona_run",
        "read_file": "daytona_inspect",
        "list_files": "daytona_inspect",
        "create_file": "daytona_files",
        "delete_file": "daytona_files",
        "change_directory": "daytona_files",
    },
    "file_generation": {
        "generate_json_file": "generate_file",
        "generate_csv_file": "generate_file",
        "generate_pdf_file": "generate_file",
        "generate_docx_file": "generate_file",
        "generate_text_file": "generate_file",
        "generate_html_file": "generate_file",
        "generate_code_file": "generate_file",
    },
    "visualization": {
        "create_bar_chart": "create_chart",
        "create_line_chart": "create_chart",
        "create_pie_chart": "create_chart",
        "create_scatter_plot": "create_chart",
        "create_histogram": "create_chart",
    },
    "unsplash": {
        "search_photos": "unsplash",
        "get_photo": "unsplash",
        "get_random_photo": "unsplash",
        "download_photo": "unsplash",
    },
    "email": {"email_user": "smtp_send_email"},
    "twilio": {"get_call_details": "twilio_read", "list_messages": "twilio_read"},
    "discord": {
        "get_channel_messages": "discord_read",
        "get_channel_info": "discord_read",
        "list_channels": "discord_read",
        "send_message": "discord_send",
        "delete_message": "discord_manage",
    },
    "telegram": {
        "get_chat": "telegram_read",
        "get_file": "telegram_read",
        "send_message": "telegram_send",
        "send_photo": "telegram_send",
        "send_document": "telegram_send",
        "send_video": "telegram_send",
        "send_audio": "telegram_send",
        "send_animation": "telegram_send",
        "send_sticker": "telegram_send",
        "edit_message": "telegram_manage",
        "delete_message": "telegram_manage",
        "react_with_emoji": "telegram_manage",
        "pin_message": "telegram_manage",
    },
    "whatsapp": {
        "send_text_message": "whatsapp_send",
        "send_template_message": "whatsapp_send",
        "send_reply_buttons": "whatsapp_send",
        "send_list_message": "whatsapp_send",
        "send_image": "whatsapp_send",
        "send_document": "whatsapp_send",
        "send_location": "whatsapp_send",
        "send_reaction": "whatsapp_send",
    },
    "webex": {"list_rooms": "webex_read", "send_message": "webex_send"},
    "zoom": {
        "get_meeting": "zoom_read",
        "get_upcoming_meetings": "zoom_read",
        "list_meetings": "zoom_read",
        "get_meeting_recordings": "zoom_read",
        "schedule_meeting": "zoom_manage",
        "delete_meeting": "zoom_manage",
    },
    "x": {
        "get_user_info": "x_read",
        "get_home_timeline": "x_read",
        "search_posts": "x_read",
        "create_post": "x_write",
        "reply_to_post": "x_write",
        "send_dm": "x_write",
    },
    "linear": {
        "get_user_details": "linear_read",
        "get_teams_details": "linear_read",
        "get_issue_details": "linear_read",
        "get_user_assigned_issues": "linear_read",
        "get_workflow_issues": "linear_read",
        "get_high_priority_issues": "linear_read",
        "create_issue": "linear_write",
        "update_issue": "linear_write",
    },
    "notion": {
        "search_pages": "notion_search",
        "create_page": "notion_write",
        "update_page": "notion_write",
    },
    "todoist": {
        "get_task": "todoist_read",
        "get_active_tasks": "todoist_read",
        "get_projects": "todoist_read",
        "create_task": "todoist_write",
        "update_task": "todoist_write",
        "close_task": "todoist_write",
        "delete_task": "todoist_write",
    },
    "clickup": {
        "list_spaces": "clickup_read",
        "list_lists": "clickup_read",
        "list_tasks": "clickup_read",
        "get_task": "clickup_read",
        "create_task": "clickup_write",
        "update_task": "clickup_write",
        "delete_task": "clickup_write",
    },
    "trello": {
        "list_boards": "trello_read",
        "get_board_lists": "trello_read",
        "get_cards": "trello_read",
        "create_board": "trello_write",
        "create_list": "trello_write",
        "create_card": "trello_write",
        "move_card": "trello_write",
    },
    "bitbucket": {
        "list_repositories": "bitbucket_read",
        "get_repository_details": "bitbucket_read",
        "list_repository_commits": "bitbucket_read",
        "list_all_pull_requests": "bitbucket_read",
        "get_pull_request_details": "bitbucket_read",
        "get_pull_request_changes": "bitbucket_read",
        "list_issues": "bitbucket_read",
        "create_repository": "bitbucket_create_repository",
    },
    "cal_com": {
        "get_available_slots": "cal_com_read",
        "get_upcoming_bookings": "cal_com_read",
        "create_booking": "cal_com_write",
        "reschedule_booking": "cal_com_write",
        "cancel_booking": "cal_com_write",
    },
    "shopify": {
        "get_shop_info": "shopify_store",
        "get_products": "shopify_store",
        "get_orders": "shopify_store",
        "get_inventory_levels": "shopify_store",
        "get_low_stock_products": "shopify_store",
        "get_top_selling_products": "shopify_analytics",
        "get_products_bought_together": "shopify_analytics",
        "get_sales_by_date_range": "shopify_analytics",
        "get_order_analytics": "shopify_analytics",
        "get_product_sales_breakdown": "shopify_analytics",
        "get_customer_order_history": "shopify_analytics",
        "get_sales_trends": "shopify_analytics",
        "get_average_order_value": "shopify_analytics",
        "get_repeat_customers": "shopify_analytics",
    },
    "spotify": {
        "search_tracks": "spotify_search",
        "search_playlists": "spotify_search",
        "search_artists": "spotify_search",
        "search_albums": "spotify_search",
        "get_track_recommendations": "spotify_search",
        "get_artist_top_tracks": "spotify_search",
        "get_album_tracks": "spotify_search",
        "get_current_user": "spotify_library",
        "get_currently_playing": "spotify_library",
        "get_user_playlists": "spotify_library",
        "get_playlist": "spotify_library",
        "get_my_top_tracks": "spotify_library",
        "get_my_top_artists": "spotify_library",
        "create_playlist": "spotify_playlist_write",
        "add_tracks_to_playlist": "spotify_playlist_write",
        "update_playlist_details": "spotify_playlist_write",
        "remove_tracks_from_playlist": "spotify_playlist_write",
        "play_track": "spotify_play",
    },
    "reddit": {
        "get_user_info": "reddit_read",
        "get_top_posts": "reddit_read",
        "get_subreddit_info": "reddit_read",
        "get_trending_subreddits": "reddit_read",
        "get_subreddit_stats": "reddit_read",
        "create_post": "reddit_write",
        "reply_to_post": "reddit_write",
        "reply_to_comment": "reddit_write",
    },
    "google_maps": {
        "search_places": "google_maps_places",
        "geocode_address": "google_maps_places",
        "reverse_geocode": "google_maps_places",
        "validate_address": "google_maps_places",
        "get_elevation": "google_maps_places",
        "get_timezone": "google_maps_places",
        "get_directions": "google_maps_routes",
        "get_distance_matrix": "google_maps_routes",
    },
    "brandfetch": {
        "search_by_identifier": "brandfetch_lookup",
        "asearch_by_identifier": "brandfetch_lookup",
        "search_by_brand": "brandfetch_lookup",
        "asearch_by_brand": "brandfetch_lookup",
    },
    "mem0": {
        "search_memory": "mem0_read",
        "get_all_memories": "mem0_read",
        "add_memory": "mem0_add",
        "delete_all_memories": "mem0_delete_all",
    },
    "zep": {
        "get_zep_memory": "zep_read",
        "search_zep_memory": "zep_read",
        "add_zep_message": "zep_add_message",
    },
    "skills": {
        "get_skill_instructions": "skill",
        "get_skill_reference": "skill",
        "get_skill_script": "skill",
    },
    "attachments": {
        "list_attachments": "attachment",
        "get_attachment": "attachment",
        "register_attachment": "attachment",
        "inline_image": "attachment",
    },
    "atlassian": {
        "jira_search_issues": "jira_read",
        "jira_get_issue": "jira_read",
        "jira_create_issue": "jira_write",
        "jira_update_issue": "jira_write",
        "jira_add_comment": "jira_write",
        "jira_transition_issue": "jira_write",
        "confluence_search": "confluence_read",
        "confluence_get_page": "confluence_read",
        "confluence_list_attachments": "confluence_read",
        "confluence_download_attachment": "confluence_read",
        "confluence_create_page": "confluence_write",
        "confluence_update_page": "confluence_write",
        "confluence_add_comment": "confluence_write",
    },
    "slack": {
        "slack_search": "slack_read",
        "slack_recent": "slack_read",
        "slack_download": "slack_read",
        "slack_post": "slack_send",
        "slack_upload": "slack_send",
        "slack_react": "slack_manage",
        "slack_edit": "slack_manage",
        "slack_delete": "slack_manage",
    },
}
_STRICT_WILDCARD_SCRIPT_TOOLKITS = frozenset({"skills", "attachments", "atlassian", "slack"})
# Synthetic probe matching broad globs like `slack_*` without matching any retired Slack function name,
# so explicit retired rules (e.g. `slack_download` or `slack_post`) still conflict with reused `slack_read`/`slack_send`.
_BROAD_PROBE_BY_TOOLKIT: dict[str, str] = {"slack": "slack__probe"}


def _effective_approval_decision(
    rule: _ApprovalRule | None,
    *,
    default: str,
    default_timeout_days: float,
) -> tuple[str | None, str | None, float | None]:
    if rule is None:
        return (default, None, None if default == "auto_approve" else default_timeout_days)
    timeout_days = (
        None
        if rule.action == "auto_approve"
        else (rule.timeout_days if rule.timeout_days is not None else default_timeout_days)
    )
    return (rule.action, rule.script, timeout_days)


def _retired_approval_pair_error(
    rules: Sequence[_ApprovalRule],
    *,
    old: str,
    new: str,
    default: str,
    default_timeout_days: float,
    reject_wildcard_scripts: bool = False,
    broad_probe: str | None = None,
) -> str | None:
    first_old = next((index for index, rule in enumerate(rules) if fnmatchcase(old, rule.match)), None)
    first_new = next((index for index, rule in enumerate(rules) if fnmatchcase(new, rule.match)), None)
    explicit_new = first_new is not None and rules[first_new].match == new
    if not explicit_new and reject_wildcard_scripts and first_old is not None and rules[first_old].script is not None:
        return (
            f"tool_approval.rules[{first_old}].match {rules[first_old].match!r} runs approval script "
            f"{rules[first_old].script!r} for retired tool {old}; {old} is now {new}; "
            f"update the script for {new} and add an explicit match: {new} rule."
        )
    old_decision = _effective_approval_decision(
        rules[first_old] if first_old is not None else None,
        default=default,
        default_timeout_days=default_timeout_days,
    )
    new_decision = _effective_approval_decision(
        rules[first_new] if first_new is not None else None,
        default=default,
        default_timeout_days=default_timeout_days,
    )
    if old_decision == new_decision:
        return None
    if explicit_new:
        return (
            (
                f"tool_approval.rules[{first_old}].match {rules[first_old].match!r} targets retired {old}, "
                f"which conflicts with {new}; remove the retired {old} rule or reconcile it with {new}."
            )
            if broad_probe is not None
            and first_old is not None
            and not fnmatchcase(broad_probe, rules[first_old].match)
            else None
        )
    if first_old is not None:
        return (
            f"tool_approval.rules[{first_old}].match {rules[first_old].match!r} decided {old}; "
            f"{old} is now {new}, which now gets a different approval decision; "
            f"rewrite the rules for {new}."
        )
    if first_new is not None:
        return (
            f"tool_approval.rules[{first_new}].match {rules[first_new].match!r} now decides {new}, "
            f"changing tool_approval.default {default!r} that previously decided {old}; "
            f"rewrite the rules for {new}."
        )
    return None


def _first_retired_approval_error(
    pairs: Iterable[tuple[str, str]],
    *,
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float = 7.0,
    reject_wildcard_scripts: bool = False,
    broad_probe: str | None = None,
) -> str | None:
    for old, new in pairs:
        if (
            error := _retired_approval_pair_error(
                rules,
                old=old,
                new=new,
                default=default,
                default_timeout_days=default_timeout_days,
                reject_wildcard_scripts=reject_wildcard_scripts,
                broad_probe=broad_probe,
            )
        ) is not None:
            return error
    return None


def retired_trigger_approval_error(
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float = 7.0,
) -> str | None:
    """Return a migration error when approval rules decide `triggers` differently from a retired trigger function."""
    return _first_retired_approval_error(
        _RETIRED_TRIGGER_FUNCTIONS.items(),
        rules=rules,
        default=default,
        default_timeout_days=default_timeout_days,
    )


def retired_slack_watch_approval_error(
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float = 7.0,
) -> str | None:
    """Return a migration error when approval rules decide `triggers` differently from retired `slack_watch`."""
    return _first_retired_approval_error(
        _RETIRED_SLACK_FUNCTIONS.items(),
        rules=rules,
        default=default,
        default_timeout_days=default_timeout_days,
        # Probe historical broad globs like `slack_*` without matching `slack_w*`.
        broad_probe="slack_post",
    )


def retired_consolidated_approval_error(
    toolkit_name: str,
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float = 7.0,
    renames: Mapping[str, str] | None = None,
    *,
    reject_wildcard_scripts: bool | None = None,
    broad_probe: str | None = None,
) -> str | None:
    """Return a migration error when approval rules decide a consolidated function differently from a retired name."""
    effective_renames = RETIRED_CONSOLIDATED_FUNCTIONS.get(toolkit_name) if renames is None else renames
    if not effective_renames:
        return None
    return _first_retired_approval_error(
        effective_renames.items(),
        rules=rules,
        default=default,
        default_timeout_days=default_timeout_days,
        reject_wildcard_scripts=(
            toolkit_name in _STRICT_WILDCARD_SCRIPT_TOOLKITS
            if reject_wildcard_scripts is None
            else reject_wildcard_scripts
        ),
        broad_probe=_BROAD_PROBE_BY_TOOLKIT.get(toolkit_name) if broad_probe is None else broad_probe,
    )


def _retired_function_filter(
    settings: Mapping[str, object],
    renames: Mapping[str, str],
    *,
    guidance_for: Callable[[str, str], str],
) -> tuple[str, str] | None:
    for key in ("include_tools", "exclude_tools"):
        value = settings.get(key)
        names = value.replace("\n", ",").split(",") if isinstance(value, str) else value
        for name in names if isinstance(names, list) else ():
            if isinstance(name, str) and (new := renames.get(name.strip())):
                return f"{key}: {name.strip()}", guidance_for(key, new)
    return None


def retired_file_setting(settings: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first Agno FileTools setting or retired function filter in ``settings``, and what replaces it."""
    if (
        retired := _retired_function_filter(
            settings,
            _RETIRED_FILE_FUNCTIONS,
            guidance_for=lambda key, new: f"use `filesystem: {{{key}: [{new}]}}`",
        )
    ) is not None:
        return retired
    return next(((field, guidance) for field, guidance in _RETIRED_FILE_SETTINGS.items() if field in settings), None)


def _retired_trigger_setting(settings: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first retired trigger function filter in ``settings``, and its migration guidance."""
    return _retired_function_filter(
        settings,
        _RETIRED_TRIGGER_FUNCTIONS,
        guidance_for=lambda _key, new: (
            f"`{new}` is now a single unified function with an `action` parameter; "
            "remove the per-function filter and use `tool_approval.rules` matching `triggers` if gating is needed"
        ),
    )


def _retired_attachments_setting(settings: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first retired attachment function filter in ``settings``, and its migration guidance."""
    return _retired_function_filter(
        settings,
        RETIRED_CONSOLIDATED_FUNCTIONS["attachments"],
        guidance_for=lambda _key, new: (
            f"`{new}` is now a single unified function with an `action` parameter (alongside standalone `view_file`); "
            f"remove the per-function filter and use `tool_approval.rules` matching `{new}` if per-action gating "
            "(such as restricting `register` or `inline_image`) is needed"
        ),
    )


def _retired_consolidated_filter_guidance(tool_name: str, key: str, new: str) -> str:
    return (
        f"use `{tool_name}: {{{key}: [{new}]}}` "
        f"(or `tool_approval.rules` matching `{new}` if per-action gating is needed)"
    )


def _retired_slack_setting(settings: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first retired Slack function filter in ``settings``, and its migration guidance."""
    if (
        retired := _retired_function_filter(
            settings,
            _RETIRED_SLACK_FUNCTIONS,
            guidance_for=lambda _key, _new: (
                '`slack_watch` has been retired in favor of `triggers(action="subscribe_event", source_id="slack", ...)`; '
                "remove `slack_watch` from the `slack` tool filter and configure `triggers` instead"
            ),
        )
    ) is not None:
        return retired
    return _retired_function_filter(
        settings,
        RETIRED_CONSOLIDATED_FUNCTIONS["slack"],
        guidance_for=lambda key, new: _retired_consolidated_filter_guidance("slack", key, new),
    )


def _retired_atlassian_setting(settings: Mapping[str, object]) -> tuple[str, str] | None:
    """Return the first retired Atlassian function filter in ``settings``, and its migration guidance."""
    return _retired_function_filter(
        settings,
        RETIRED_CONSOLIDATED_FUNCTIONS["atlassian"],
        guidance_for=lambda key, new: _retired_consolidated_filter_guidance("atlassian", key, new),
    )


_RETIRED_TOOL_SETTING_CHECKERS: dict[str, Callable[[Mapping[str, object]], tuple[str, str] | None]] = {
    "filesystem": retired_file_setting,
    "triggers": _retired_trigger_setting,
    "attachments": _retired_attachments_setting,
    "atlassian": _retired_atlassian_setting,
    "slack": _retired_slack_setting,
}


def retired_tool_override(
    tool_name: str,
    overrides: Mapping[str, object],
    *,
    retired_functions: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """Return the first rejected retired field an entry of ``tool_name`` authors, and its replacement guidance."""
    if (checker := _RETIRED_TOOL_SETTING_CHECKERS.get(tool_name)) is not None and (
        retired := checker(overrides)
    ) is not None:
        return retired
    if (
        retired_functions
        and (
            retired := _retired_function_filter(
                overrides,
                retired_functions,
                guidance_for=lambda key, new: _retired_consolidated_filter_guidance(tool_name, key, new),
            )
        )
        is not None
    ):
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


# LEGACY_COMPAT: Retired `dynamic_workflow` toolkit alias and its `allowed_tools` override.
# Legacy format: `defaults.tools` and `agents.<name>.tools` accepted `dynamic_workflow` (optionally with an
#   `allowed_tools` override restricting participant tools under the deleted Dynamic Workflows engine).
# Last legacy release: v2026.11.590; replacement: unreleased; aliases `dynamic_workflow` to `workflow` and deletes
#   the old Dynamic Workflows engine.
# Handling: Canonicalize `dynamic_workflow` to `workflow` via TOOL_ALIASES, strip `allowed_tools` from entries
#   authored under the legacy alias, and log one concise deprecation warning per config load.
# Coverage: tests/test_dynamic_workflow_alias.py::test_dynamic_workflow_bare_agent_and_defaults_alias_with_warning,
#   tests/test_dynamic_workflow_alias.py::test_dynamic_workflow_strips_allowed_tools_and_warns,
#   tests/test_dynamic_workflow_alias.py::test_dynamic_workflow_deduplicates_with_workflow.
def strip_legacy_dynamic_workflow_overrides(
    tool_name: str,
    overrides: dict[str, object],
) -> tuple[dict[str, object], bool]:
    """Strip retired `allowed_tools` from entries authored as `dynamic_workflow`."""
    if tool_name != "dynamic_workflow" or "allowed_tools" not in overrides:
        return overrides, False
    return {key: value for key, value in overrides.items() if key != "allowed_tools"}, True


def dynamic_workflow_deprecation_warning(*, ignored_allowed_tools: bool) -> str:
    """Return the one-line deprecation warning for configs that still name `dynamic_workflow`."""
    if ignored_allowed_tools:
        return (
            "Tool 'dynamic_workflow' is deprecated and now aliases 'workflow' "
            "(ignoring 'allowed_tools' because a workflow inherits its caller's tools); "
            "rename it to 'workflow' in your config."
        )
    return "Tool 'dynamic_workflow' is deprecated and now aliases 'workflow'; rename it to 'workflow' in your config."


# LEGACY_COMPAT: Retired function names of the deleted `dynamic_workflow` toolkit in `tool_approval.rules`.
# Legacy format: `tool_approval.rules[].match` could gate or allow the old Dynamic Workflows functions
#   (`run_workflow`, `create_workflow`, `update_workflow`, `get_workflow_run`, `list_workflows`).
# Last legacy release: v2026.11.590; replacement: unreleased; `dynamic_workflow` now aliases `workflow`, whose
#   functions are `start_workflow`, `resume_workflow`, `save_workflow`, `get_workflow`, and `list_workflow_runs`.
# Handling: When the config authors `dynamic_workflow`, allow any replacement function already decided by an exact
#   `match` rule, and otherwise reject `tool_approval.rules` when a retired function's effective approval decision
#   differs from its replacement's effective decision.
# Coverage: tests/test_dynamic_workflow_alias.py::test_retired_dynamic_workflow_approval_rules_fail,
#   tests/test_dynamic_workflow_alias.py::test_retired_dynamic_workflow_approval_rules_allowed,
#   tests/test_dynamic_workflow_alias.py::test_canonical_workflow_wildcard_approval_rules_load_cleanly.
_RETIRED_DYNAMIC_WORKFLOW_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "run_workflow": ("start_workflow", "resume_workflow"),
    "create_workflow": ("save_workflow",),
    "update_workflow": ("save_workflow",),
    "get_workflow_run": ("get_workflow",),
    "list_workflows": ("list_workflow_runs",),
}


def retired_dynamic_workflow_approval_error(
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float = 7.0,
) -> str | None:
    """Return a migration error when approval rules decide a `workflow` function differently from its retired name."""
    return _first_retired_approval_error(
        ((old, new) for old, replacements in _RETIRED_DYNAMIC_WORKFLOW_FUNCTIONS.items() for new in replacements),
        rules=rules,
        default=default,
        default_timeout_days=default_timeout_days,
    )
