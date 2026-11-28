"""Diagnostics for retired configuration fields."""

from __future__ import annotations

import json
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from mindroom.config.retired_tool_renames import (
    RETIRED_CONSOLIDATED_FUNCTIONS,
    RETIRED_SPLIT_FUNCTIONS,
    SINGLE_VERB_RENAMED_TARGETS,
    SPLIT_APPROVAL_ONLY_FUNCTIONS,
    STRICT_WILDCARD_SCRIPT_RENAMES,
    STRICT_WILDCARD_SCRIPT_TOOLKITS,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
    from typing import Protocol

    class _ApprovalRule(Protocol):
        @property
        def match(self) -> str | list[str]: ...
        @property
        def agents(self) -> str | list[str] | None: ...
        @property
        def arguments(self) -> Mapping[str, Any] | None: ...
        @property
        def action(self) -> str | None: ...
        @property
        def script(self) -> str | None: ...
        @property
        def timeout_days(self) -> float | None: ...
        def match_patterns(self) -> tuple[str, ...]: ...
        def agent_patterns(self) -> tuple[str, ...] | None: ...
        def matches_tool(self, tool_name: str) -> bool: ...
        def matches_agent(self, agent_name: str) -> bool: ...
        def fixed_arguments_match(self, fixed_arguments: Mapping[str, Any]) -> str: ...

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


_REJECTED_AGENT_FIELDS: dict[str, str] = {
    "knowledge_base": "Agent field 'knowledge_base' was removed. Use 'knowledge_bases' (list) instead.",
    "memory_dir": (
        "Agent field 'memory_dir' was removed. Every agent works in its own workspace: list the files it "
        "always loads in 'context_files', and set memory_backend=file to keep its memory files there."
    ),
    "memory_file_path": (
        "Agent field 'memory_file_path' was removed. With memory_backend=file, an agent's memory files "
        "live in its workspace root; configure context_files relative to that workspace."
    ),
    "sandbox_tools": "Agent field 'sandbox_tools' was removed. Use 'worker_tools' instead.",
    "allowed_toolkits": (
        "Agent field 'allowed_toolkits' was removed. Expand toolkit/preset/bundle entries into individual "
        "tools before applying per-tool defer flags in tools."
    ),
    "initial_toolkits": (
        "Agent field 'initial_toolkits' was removed. Expand toolkit/preset/bundle entries into individual "
        "tools before applying per-tool initial flags in tools."
    ),
}

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
_REJECTED_DEFAULTS_FIELDS: dict[str, str] = {
    "auto_resume_after_restart": (
        "defaults.auto_resume_after_restart was removed. A reply interrupted by a restart now always "
        "continues in place in the same message; remove the setting."
    ),
    "sandbox_tools": "defaults.sandbox_tools was removed. Use defaults.worker_tools instead.",
    "allowed_toolkits": "defaults.allowed_toolkits was removed. Use defaults.tools instead.",
    "initial_toolkits": "defaults.initial_toolkits was removed. Use defaults.tools instead.",
}


def _reject_fields(value: object, rejected: Mapping[str, str]) -> object:
    if isinstance(value, dict):
        for field, msg in rejected.items():
            if field in value:
                raise ValueError(msg)
    return value


def reject_legacy_agent_fields(value: object) -> object:
    """Reject retired agent fields before regular model validation."""
    return _reject_fields(value, _REJECTED_AGENT_FIELDS)


def reject_legacy_defaults_fields(value: object) -> object:
    """Reject retired default fields before regular model validation."""
    return _reject_fields(value, _REJECTED_DEFAULTS_FIELDS)


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
_UNMATCHED_AGENT_PROBE = "\x00"


def _probe_agents(rules: Sequence[_ApprovalRule], agent_names: Collection[str] = ()) -> tuple[str, ...]:
    if not any(rule.agents is not None for rule in rules):
        return (_UNMATCHED_AGENT_PROBE,)
    probes = list(agent_names)
    for rule in rules:
        for pattern in rule.agent_patterns() or ():
            candidate = pattern.replace("*", "x").replace("?", "x")
            if fnmatchcase(candidate, pattern):
                probes.append(candidate)
    probes.append(_UNMATCHED_AGENT_PROBE)
    return tuple(dict.fromkeys(probes))


def _agent_rule_indices(
    rules: Sequence[_ApprovalRule],
    name: str,
    agent_name: str,
    *,
    moved_actions: Collection[str] | None = None,
) -> tuple[int, ...]:
    indices: list[int] = []
    for index, rule in enumerate(rules):
        if not rule.matches_tool(name) or not rule.matches_agent(agent_name):
            continue
        if (
            moved_actions is not None
            and rule.script is None
            and rule.arguments is not None
            and all(rule.fixed_arguments_match({"action": action}) == "no" for action in moved_actions)
        ):
            continue
        indices.append(index)
        if rule.arguments is None:
            break
    return tuple(indices)


def approval_rule_a_file_rename_escapes(
    rules: Sequence[_ApprovalRule],
    offered_elsewhere: Collection[str],
    agent_names: Collection[str] = (),
) -> str | None:
    """Return the rule that decided a retired file function but not its replacement, as text, or None."""
    probes = _probe_agents(rules, agent_names)
    for old, new in _RETIRED_FILE_FUNCTIONS.items():
        if old in offered_elsewhere:
            continue
        for agent in probes:
            old_indices = _agent_rule_indices(rules, old, agent)
            new_indices = _agent_rule_indices(rules, new, agent)
            if old_indices and new_indices[: len(old_indices)] != old_indices:
                report_index = next((index for index in old_indices if index not in new_indices), old_indices[0])
                return f"tool_approval.rules[{report_index}].match {rules[report_index].match!r} decided {old}; {old} is now {new}"
    return None


# LEGACY_COMPAT: Retired `slack_watch` function of the `slack` toolkit.
# Legacy format: `slack: {include_tools: [slack_watch]}` / `exclude_tools` and `tool_approval.rules` matching `slack_watch`.
# Last legacy release: v2026.11.651; replacement: `triggers_write(action="subscribe_event", source_id="slack", ...)`.
# Handling: Reject `slack_watch` in `include_tools`/`exclude_tools` on `slack` and fail closed in `tool_approval.rules`
#   whenever both `slack` and `triggers` toolkits are enabled and `slack_watch` conflicts with `triggers_write` (allowing
#   broad `slack_*` globs paired with an explicit exact `triggers_write` rule).
# Coverage: tests/slack/test_event_source.py::test_retired_slack_watch_config_validation_fails_closed.
_RETIRED_SLACK_FUNCTIONS: dict[str, str] = {"slack_watch": "triggers_write"}

# LEGACY_COMPAT: Retired function names of the deleted `dynamic_workflow` toolkit in `tool_approval.rules`.
# Legacy format: `tool_approval.rules[].match` could gate or allow the old Dynamic Workflows functions
#   (`run_workflow`, `create_workflow`, `update_workflow`, `get_workflow_run`, `list_workflows`).
# Last legacy release: v2026.11.590; replacement: unreleased; `dynamic_workflow` now aliases `workflow`, whose
#   functions are `workflow_read` (`list`, `get`) and `workflow_write` (`start`, `cancel`, `resume`, `save`).
# Handling: When the config authors `dynamic_workflow`, allow replacement functions when already decided by an exact
#   `match` rule, and otherwise reject `tool_approval.rules` when a retired function is decided by an approval script or
#   its effective approval decision differs from its replacement function's effective decision.
# Coverage: tests/test_dynamic_workflow_alias.py::test_retired_dynamic_workflow_approval_rules_fail,
#   tests/test_dynamic_workflow_alias.py::test_retired_dynamic_workflow_approval_rules_allowed,
#   tests/test_dynamic_workflow_alias.py::test_wildcard_approval_script_rule_fails_closed_for_retired_dynamic_workflow_names,
#   tests/test_dynamic_workflow_alias.py::test_canonical_workflow_wildcard_approval_rules_load_cleanly.
_RETIRED_DYNAMIC_WORKFLOW_FUNCTIONS: dict[str, str] = {
    "run_workflow": "workflow_write",
    "create_workflow": "workflow_write",
    "update_workflow": "workflow_write",
    "get_workflow_run": "workflow_read",
    "list_workflows": "workflow_read",
}


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


def _effective_approval_chain(
    rules: Sequence[_ApprovalRule],
    indices: tuple[int, ...],
    *,
    default: str,
    default_timeout_days: float,
) -> tuple[tuple[str, tuple[str | None, str | None, float | None]], ...]:
    chain = [
        (
            json.dumps(rules[index].arguments, sort_keys=True, separators=(",", ":")),
            _effective_approval_decision(
                rules[index],
                default=default,
                default_timeout_days=default_timeout_days,
            ),
        )
        for index in indices
    ]
    if not indices or rules[indices[-1]].arguments is not None:
        chain.append(
            (
                "null",
                _effective_approval_decision(None, default=default, default_timeout_days=default_timeout_days),
            ),
        )
    return tuple(chain)


def _is_broad_glob_for_old(rule: _ApprovalRule, *, old: str, broad_probe: str) -> bool:
    return any(fnmatchcase(old, pattern) and fnmatchcase(broad_probe, pattern) for pattern in rule.match_patterns())


_RetiredApprovalFailureKind = Literal["script", "conflict", "rem_old", "rem_new"]


def _split_approval_suffix(old: str) -> str:
    if old == "desktop":
        return " (and retain a desktop rule or add a run_desktop_shell rule if gating desktop shell commands)"
    return ""


def _retired_script_approval_error(
    rule: _ApprovalRule,
    *,
    script_index: int,
    old: str,
    news: Sequence[str],
    split_from_active: bool = False,
    all_replacements: tuple[str, ...] | None = None,
) -> str:
    prefix = f"tool_approval.rules[{script_index}].match {rule.match!r} runs approval script {rule.script!r}"
    if split_from_active:
        new = news[0]
        return (
            f"{prefix} for {old}; read actions from {old} moved to {new}; "
            f"update the script for {new} and add an explicit match: {new} rule."
        )
    if all_replacements and len(all_replacements) > 1:
        joined = " and ".join(all_replacements)
        suffix = _split_approval_suffix(old)
        return (
            f"{prefix} for retired tool {old}; {old} was split into {joined}; "
            f"update the script and add explicit rules for {joined}{suffix}."
        )
    joined = ", ".join(news)
    target = news[0] if len(news) == 1 else f"[{joined}]"
    return (
        f"{prefix} for retired tool {old}; {old} is now {joined}; "
        f"update the script for {joined} and add an explicit match: {target} rule."
    )


def _retired_split_mismatch_error(
    kind: Literal["rem_old", "rem_new"],
    index: int,
    *,
    rules: Sequence[_ApprovalRule],
    old: str,
    new: str,
    default: str,
    split_from_active: bool,
    all_replacements: tuple[str, ...] | None,
) -> str:
    if split_from_active:
        fix = f"add explicit approval rules for {new}."
        reason = f"read actions from {old} moved to {new}, which now gets a different approval decision"
        previous = f"read actions from {old}"
    else:
        assert all_replacements is not None
        joined = " and ".join(all_replacements)
        fix = f"rewrite the rules for {joined}{_split_approval_suffix(old)}."
        reason = f"{old} was split into {joined} ({new} now gets a different approval decision)"
        previous = f"{old} (now split into {joined})"
    if kind == "rem_old":
        return f"tool_approval.rules[{index}].match {rules[index].match!r} decided {old}; {reason}; {fix}"
    return (
        f"tool_approval.rules[{index}].match {rules[index].match!r} now decides {new}, "
        f"changing tool_approval.default {default!r} that previously decided {previous}; {fix}"
    )


def _retired_approval_mismatch_error(
    kind: Literal["rem_old", "rem_new"],
    index: int,
    *,
    rules: Sequence[_ApprovalRule],
    old: str,
    news: Sequence[str],
    default: str,
    kept_news: Sequence[str] = (),
    split_from_active: bool = False,
    all_replacements: tuple[str, ...] | None = None,
) -> str:
    if split_from_active or (all_replacements and len(all_replacements) > 1):
        return _retired_split_mismatch_error(
            kind,
            index,
            rules=rules,
            old=old,
            new=news[0],
            default=default,
            split_from_active=split_from_active,
            all_replacements=all_replacements,
        )
    joined = ", ".join(news)
    target = news[0] if len(news) == 1 else f"[{joined}]"
    if kind == "rem_old":
        verb = "gets" if len(news) == 1 else "get"
        if kept_news:
            kept_joined = ", ".join(kept_news)
            kept_patterns = [pattern for pattern in rules[index].match_patterns() if pattern != old]
            combined_match = json.dumps([*kept_patterns, *news])
            return (
                f"tool_approval.rules[{index}].match {rules[index].match!r} decided {old}; "
                f"{old} is now {joined} (while still matching {kept_joined}), which now {verb} a different "
                f"approval decision; keep the existing pattern and add {joined} "
                f"(for example match: {combined_match}) so {kept_joined} stays covered."
            )
        return (
            f"tool_approval.rules[{index}].match {rules[index].match!r} decided {old}; "
            f"{old} is now {joined}, which now {verb} a different approval decision; "
            f"rewrite the rules for {target}."
        )
    return (
        f"tool_approval.rules[{index}].match {rules[index].match!r} now decides {joined}, "
        f"changing tool_approval.default {default!r} that previously decided {old}; "
        f"rewrite the rules for {target}."
    )


def _format_retired_approval_error(
    kind: _RetiredApprovalFailureKind,
    index: int,
    *,
    rules: Sequence[_ApprovalRule],
    old: str,
    news: Sequence[str],
    default: str,
    kept_news: Sequence[str] = (),
    split_from_active: bool = False,
    all_replacements: tuple[str, ...] | None = None,
) -> str:
    if kind == "script":
        return _retired_script_approval_error(
            rules[index],
            script_index=index,
            old=old,
            news=news,
            split_from_active=split_from_active,
            all_replacements=all_replacements,
        )
    if kind == "conflict":
        joined = ", ".join(news)
        return (
            f"tool_approval.rules[{index}].match {rules[index].match!r} targets retired {old}, "
            f"which conflicts with {joined}; remove the retired {old} rule or reconcile it with {joined}."
        )
    return _retired_approval_mismatch_error(
        kind,
        index,
        rules=rules,
        old=old,
        news=news,
        default=default,
        kept_news=kept_news,
        split_from_active=split_from_active,
        all_replacements=all_replacements,
    )


def _retired_approval_pair_failure_for_agent(
    rules: Sequence[_ApprovalRule],
    *,
    old: str,
    new: str,
    agent: str,
    default: str,
    default_timeout_days: float,
    reject_wildcard_scripts: bool = False,
    broad_probe: str | None = None,
    moved_actions: Collection[str] | None = None,
) -> tuple[_RetiredApprovalFailureKind, int] | None:
    old_indices = _agent_rule_indices(rules, old, agent, moved_actions=moved_actions)
    new_indices = _agent_rule_indices(rules, new, agent, moved_actions=moved_actions)
    shared_prefix_len = 0
    for old_index, new_index in zip(old_indices, new_indices, strict=False):
        if old_index != new_index:
            break
        shared_prefix_len += 1
    shared_prefix = old_indices[:shared_prefix_len]
    rem_old = old_indices[shared_prefix_len:]
    rem_new = new_indices[shared_prefix_len:]
    rem_new_explicit_unconditional = (
        bool(rem_new)
        and all(new in rules[index].match_patterns() for index in rem_new)
        and rules[rem_new[-1]].arguments is None
    )
    if (
        reject_wildcard_scripts
        and not (rem_new_explicit_unconditional and all(rules[index].script is None for index in shared_prefix))
        and (
            script_index := next(
                (
                    index
                    for index in old_indices
                    if rules[index].script is not None and new not in rules[index].match_patterns()
                ),
                None,
            )
        )
        is not None
    ):
        return ("script", script_index)
    old_chain = _effective_approval_chain(
        rules,
        old_indices,
        default=default,
        default_timeout_days=default_timeout_days,
    )
    new_chain = _effective_approval_chain(
        rules,
        new_indices,
        default=default,
        default_timeout_days=default_timeout_days,
    )
    if old_chain == new_chain:
        return None
    extra_new = [index for index in rem_new if index not in rem_old]
    explicit_new_override = rem_new_explicit_unconditional or (
        bool(extra_new)
        and all(new in rules[index].match_patterns() for index in extra_new)
        and set(rem_old) <= set(rem_new)
    )
    if explicit_new_override:
        conflict_index = (
            next(
                (
                    index
                    for index in rem_old
                    if not _is_broad_glob_for_old(rules[index], old=old, broad_probe=broad_probe)
                ),
                None,
            )
            if broad_probe is not None
            else None
        )
        return ("conflict", conflict_index) if conflict_index is not None else None
    if rem_old:
        report_index = next((index for index in rem_old if index not in rem_new), rem_old[0])
        return ("rem_old", report_index)
    if rem_new:
        report_index = next(
            (index for index in rem_new if new not in rules[index].match_patterns()),
            rem_new[0],
        )
        return ("rem_new", report_index)
    return None


def _retired_approval_pair_error_for_agent(
    rules: Sequence[_ApprovalRule],
    *,
    old: str,
    new: str,
    agent: str,
    default: str,
    default_timeout_days: float,
    reject_wildcard_scripts: bool = False,
    broad_probe: str | None = None,
    split_from_active: bool = False,
    moved_actions: Collection[str] | None = None,
    all_replacements: tuple[str, ...] | None = None,
) -> str | None:
    failure = _retired_approval_pair_failure_for_agent(
        rules,
        old=old,
        new=new,
        agent=agent,
        default=default,
        default_timeout_days=default_timeout_days,
        reject_wildcard_scripts=reject_wildcard_scripts,
        broad_probe=broad_probe,
        moved_actions=moved_actions,
    )
    if failure is None:
        return None
    return _format_retired_approval_error(
        failure[0],
        failure[1],
        rules=rules,
        old=old,
        news=[new],
        default=default,
        split_from_active=split_from_active,
        all_replacements=all_replacements,
    )


def _first_retired_approval_error(
    pairs: Iterable[tuple[str, str]],
    *,
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float = 7.0,
    reject_wildcard_scripts: bool = False,
    broad_probe: str | None = None,
    agent_names: Collection[str] = (),
    split_from_active: bool = False,
    moved_actions_by_pair: Mapping[tuple[str, str], Collection[str]] | None = None,
    split_replacements: Mapping[str, tuple[str, ...]] | None = None,
) -> str | None:
    probes = _probe_agents(rules, agent_names)
    for old, new in pairs:
        all_replacements = split_replacements.get(old) if split_replacements is not None else None
        moved_actions = moved_actions_by_pair.get((old, new)) if moved_actions_by_pair is not None else None
        for agent in probes:
            if (
                error := _retired_approval_pair_error_for_agent(
                    rules,
                    old=old,
                    new=new,
                    agent=agent,
                    default=default,
                    default_timeout_days=default_timeout_days,
                    reject_wildcard_scripts=reject_wildcard_scripts,
                    broad_probe=broad_probe,
                    split_from_active=split_from_active,
                    moved_actions=moved_actions,
                    all_replacements=all_replacements,
                )
            ) is not None:
                return error
    return None


@dataclass(frozen=True)
class _RetiredApprovalSpec:
    renames: Mapping[str, str]
    reject_wildcard_scripts: bool = False
    broad_probe: str | None = None


_BUILTIN_RETIRED_APPROVALS: dict[str, _RetiredApprovalSpec] = {
    # Probe historical broad globs like `slack_*` without matching `slack_w*`.
    "slack_watch": _RetiredApprovalSpec(_RETIRED_SLACK_FUNCTIONS, broad_probe="slack_post"),
    "dynamic_workflow": _RetiredApprovalSpec(_RETIRED_DYNAMIC_WORKFLOW_FUNCTIONS, reject_wildcard_scripts=True),
}


def retired_builtin_approval_error(
    target: Literal["slack_watch", "dynamic_workflow"],
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float = 7.0,
    agent_names: Collection[str] = (),
) -> str | None:
    """Return a migration error for a built-in retired approval target (`slack_watch`, `dynamic_workflow`)."""
    spec = _BUILTIN_RETIRED_APPROVALS[target]
    return _first_retired_approval_error(
        spec.renames.items(),
        rules=rules,
        default=default,
        default_timeout_days=default_timeout_days,
        reject_wildcard_scripts=spec.reject_wildcard_scripts,
        broad_probe=spec.broad_probe,
        agent_names=agent_names,
    )


def retired_action_argument_approval_error(
    enabled_toolkits: Collection[str],
    rules: Sequence[_ApprovalRule],
) -> str | None:
    """Return a migration error when approval rules target moved or renamed action argument values."""
    check_matrix_user = "matrix_user" in enabled_toolkits
    check_matrix_room = "matrix_room" in enabled_toolkits
    if not (check_matrix_user or check_matrix_room):
        return None
    for index, rule in enumerate(rules):
        if rule.arguments is None or "action" not in rule.arguments:
            continue
        if (
            check_matrix_user
            and rule.matches_tool("matrix_user_read")
            and not rule.matches_tool("matrix_user_write")
            and rule.fixed_arguments_match({"action": "mark_read"}) != "no"
        ):
            return (
                f"tool_approval.rules[{index}] matches matrix_user_read with action 'mark_read'; "
                "mark_read moved from matrix_user_read to matrix_user_write; "
                "move 'mark_read' to matrix_user_write (preserving conditions and approval decision) "
                "and keep any remaining read actions on matrix_user_read."
            )
        if (
            check_matrix_room
            and rule.matches_tool("matrix_room")
            and rule.fixed_arguments_match({"action": "room-info"}) != "no"
            and rule.fixed_arguments_match({"action": "room_info"}) == "no"
        ):
            return (
                f"tool_approval.rules[{index}] matches matrix_room with retired action 'room-info'; "
                "action 'room-info' was renamed to 'room_info'; "
                "update the rule's arguments.action to 'room_info'."
            )
    return None


def _collect_consolidated_approval_targets(
    toolkit_checks: Sequence[tuple[str, Mapping[str, str], bool | None]],
) -> tuple[
    dict[str, dict[str, bool]],
    list[tuple[Mapping[str, tuple[str, ...]], bool]],
    list[tuple[Mapping[str, Mapping[str, frozenset[str]]], bool]],
]:
    targets_by_old: dict[str, dict[str, bool]] = {}
    split_checks: list[tuple[Mapping[str, tuple[str, ...]], bool]] = []
    split_only_checks: list[tuple[Mapping[str, Mapping[str, frozenset[str]]], bool]] = []
    for toolkit_name, renames, reject_wildcard_scripts in toolkit_checks:
        toolkit_strict = (
            toolkit_name in STRICT_WILDCARD_SCRIPT_TOOLKITS
            if reject_wildcard_scripts is None
            else reject_wildcard_scripts
        )
        strict_old_names = (
            STRICT_WILDCARD_SCRIPT_RENAMES.get(toolkit_name, frozenset())
            if reject_wildcard_scripts is None
            else frozenset()
        )
        for old, new in renames.items():
            reject_wildcards = toolkit_strict or old in strict_old_names
            targets = targets_by_old.setdefault(old, {})
            targets[new] = targets.get(new, False) or reject_wildcards
        if (split_renames := RETIRED_SPLIT_FUNCTIONS.get(toolkit_name)) is not None:
            split_checks.append((split_renames, toolkit_strict))
        if (split_only := SPLIT_APPROVAL_ONLY_FUNCTIONS.get(toolkit_name)) is not None:
            split_only_checks.append((split_only, toolkit_name in STRICT_WILDCARD_SCRIPT_TOOLKITS))
    return targets_by_old, split_checks, split_only_checks


def _normalize_consolidated_toolkit_checks(
    toolkit_or_checks: str | Sequence[tuple[str, Mapping[str, str], bool | None]],
    renames: Mapping[str, str] | None,
    reject_wildcard_scripts: bool | None,
) -> Sequence[tuple[str, Mapping[str, str], bool | None]]:
    if not isinstance(toolkit_or_checks, str):
        return toolkit_or_checks
    effective_renames = RETIRED_CONSOLIDATED_FUNCTIONS.get(toolkit_or_checks) if renames is None else renames
    if (
        not effective_renames
        and toolkit_or_checks not in RETIRED_SPLIT_FUNCTIONS
        and toolkit_or_checks not in SPLIT_APPROVAL_ONLY_FUNCTIONS
    ):
        return ()
    return [(toolkit_or_checks, effective_renames or {}, reject_wildcard_scripts)]


def _first_split_approval_error(
    split_checks: Sequence[tuple[Mapping[str, tuple[str, ...]], bool]],
    split_only_checks: Sequence[tuple[Mapping[str, Mapping[str, frozenset[str]]], bool]],
    *,
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float,
    agent_names: Collection[str],
) -> str | None:
    for split_renames, toolkit_strict in split_checks:
        if (
            split_error := _first_retired_approval_error(
                ((old, new) for old, replacements in split_renames.items() for new in replacements),
                rules=rules,
                default=default,
                default_timeout_days=default_timeout_days,
                reject_wildcard_scripts=toolkit_strict,
                agent_names=agent_names,
                split_replacements=split_renames,
            )
        ) is not None:
            return split_error
    for split_only, toolkit_strict in split_only_checks:
        moved_by_pair = {
            (old, new): moved_actions for old, targets in split_only.items() for new, moved_actions in targets.items()
        }
        if (
            split_only_error := _first_retired_approval_error(
                moved_by_pair,
                rules=rules,
                default=default,
                default_timeout_days=default_timeout_days,
                reject_wildcard_scripts=toolkit_strict,
                agent_names=agent_names,
                split_from_active=True,
                moved_actions_by_pair=moved_by_pair,
            )
        ) is not None:
            return split_only_error
    return None


def retired_consolidated_approval_error(
    toolkit_name: str | Sequence[tuple[str, Mapping[str, str], bool | None]],
    rules: Sequence[_ApprovalRule],
    default: str,
    default_timeout_days: float = 7.0,
    renames: Mapping[str, str] | None = None,
    agent_names: Collection[str] = (),
    *,
    reject_wildcard_scripts: bool | None = None,
) -> str | None:
    """Return a migration error when approval rules decide consolidated/split functions differently from retired names.

    Accepts either a single toolkit name (with optional ``renames``/``reject_wildcard_scripts`` for direct callers and
    plugins) or a sequence of ``(toolkit_name, renames, reject_wildcard_scripts)`` checks grouped across enabled
    toolkits so shared bare names (such as ``run_query`` across ``postgres`` and ``redshift``) name every failing
    replacement at once, and also validates 1-to-N split toolkits from ``RETIRED_SPLIT_FUNCTIONS`` and
    ``SPLIT_APPROVAL_ONLY_FUNCTIONS``.
    """
    toolkit_checks = _normalize_consolidated_toolkit_checks(toolkit_name, renames, reject_wildcard_scripts)
    targets_by_old, split_checks, split_only_checks = _collect_consolidated_approval_targets(toolkit_checks)

    probes = _probe_agents(rules, agent_names)
    for old, targets in targets_by_old.items():
        first_failure: tuple[_RetiredApprovalFailureKind, int] | None = None
        failing_news: list[str] = []
        for new, reject_wildcards in targets.items():
            for agent in probes:
                failure = _retired_approval_pair_failure_for_agent(
                    rules,
                    old=old,
                    new=new,
                    agent=agent,
                    default=default,
                    default_timeout_days=default_timeout_days,
                    reject_wildcard_scripts=reject_wildcards,
                )
                if failure is not None:
                    if first_failure is None:
                        first_failure = failure
                    failing_news.append(new)
                    break
        if first_failure is not None:
            kind, index = first_failure
            kept_news = (
                [new for new in targets if new not in failing_news and rules[index].matches_tool(new)]
                if kind == "rem_old"
                else []
            )
            return _format_retired_approval_error(
                kind,
                index,
                rules=rules,
                old=old,
                news=failing_news,
                default=default,
                kept_news=kept_news,
            )

    return _first_split_approval_error(
        split_checks,
        split_only_checks,
        rules=rules,
        default=default,
        default_timeout_days=default_timeout_days,
        agent_names=agent_names,
    )


def _iter_filter_names(settings: Mapping[str, object], key: str) -> Iterator[str]:
    value = settings.get(key)
    raw = value.replace("\n", ",").split(",") if isinstance(value, str) else value
    if not isinstance(raw, list):
        return
    for name in raw:
        if isinstance(name, str) and (stripped := name.strip()):
            yield stripped


def _retired_function_filter[T](
    settings: Mapping[str, object],
    renames: Mapping[str, T],
    *,
    guidance_for: Callable[[str, str, T], str],
) -> tuple[str, str] | None:
    for key in ("include_tools", "exclude_tools"):
        for old in _iter_filter_names(settings, key):
            if (target := renames.get(old)) is not None:
                return f"{key}: {old}", guidance_for(key, old, target)
    return None


def _is_single_verb_renamed_target(new: str) -> bool:
    return new in SINGLE_VERB_RENAMED_TARGETS or new.endswith("_google_docs_read")


def _single_verb_rename_filter_guidance(tool_name: str, key: str, old: str, new: str) -> str:
    return f"replace `{old}` with `{new}` in `{tool_name}.{key}` while preserving any other entries in the list"


def _retired_consolidated_filter_guidance(tool_name: str, key: str, old: str, new: str) -> str:
    if _is_single_verb_renamed_target(new):
        return _single_verb_rename_filter_guidance(tool_name, key, old, new)
    effect = (
        f"adding `{new}` to `include_tools` broadens to all sibling actions in `{new}`"
        if key == "include_tools"
        else f"adding `{new}` to `exclude_tools` removes all sibling actions in `{new}`"
    )
    return (
        f"replace `{old}` with `{new}` in `{tool_name}.{key}` while preserving any other entries in the list "
        f"(note that `{new}` covers all actions in that consolidated group, so {effect}; "
        f"for per-action gating, use a `tool_approval.rules` approval `script` matching `{new}`)"
    )


def _stored_consolidated_filter_guidance(tool_name: str, key: str, old: str, new: str) -> str:
    if _is_single_verb_renamed_target(new):
        return _single_verb_rename_filter_guidance(tool_name, key, old, new)
    return (
        f"use `{tool_name}: {{{key}: [{new}]}}` "
        f"(filtering by `{new}` {'enables' if key == 'include_tools' else 'disables'} all of `{new}`'s actions) "
        f"or remove the per-function filter and use `tool_approval.rules` matching `{new}` "
        "if per-action gating is needed"
    )


def _stored_toggle_filter_guidance(
    tool_name: str,
    toggle_hint: str,
    key: str,
    old: str,
    new: str,
) -> str:
    if _is_single_verb_renamed_target(new):
        return _single_verb_rename_filter_guidance(tool_name, key, old, new)
    return (
        f"`{new}` is now a consolidated function with an `action` parameter; migrate to `{new}` and "
        f"disable unwanted actions via `{tool_name}`'s {toggle_hint} (or `tool_approval.rules` "
        f"matching `{new}`) so permissions are not broadened"
    )


def _retired_fallback_filter_guidance(
    tool_name: str,
    key: str,
    old: str,
    new: str,
    *,
    has_enable_toggles: bool,
) -> str:
    if _is_single_verb_renamed_target(new):
        return _single_verb_rename_filter_guidance(tool_name, key, old, new)
    if has_enable_toggles:
        return (
            f"`{new}` is now a consolidated function with an `action` parameter; migrate to `{new}` and "
            f"disable unwanted actions via `{tool_name}`'s `enable_*` settings (or `tool_approval.rules` "
            f"matching `{new}`) so permissions are not broadened"
        )
    return (
        f"`{new}` is now a consolidated function with an `action` parameter and `{tool_name}` has no "
        f"per-action allowlist filter; remove the retired `{key}` entry and use `tool_approval.rules` "
        f"matching `{new}` if per-action gating is needed"
    )


def _retired_split_filter_guidance(tool_name: str, key: str, old: str, replacements: tuple[str, ...]) -> str:
    joined = ", ".join(f"`{name}`" for name in replacements)
    if tool_name == "workflow":
        return (
            f"`{old}` was split into {joined} and `{tool_name}` does not support `{key}`; "
            f"remove `{tool_name}.{key}` and use explicit `tool_approval.rules` matching {joined} instead"
        )
    extra = " (alongside `run_desktop_shell` for shell commands)" if tool_name == "desktop" else ""
    return (
        f"`{old}` was split into {joined}{extra}; replace `{old}` in `{tool_name}.{key}` with the desired "
        f"function(s) or remove the filter and use `tool_approval.rules` for per-action gating"
    )


def _retired_browser_filter(settings: Mapping[str, object]) -> tuple[str, str] | None:
    excluded = set(_iter_filter_names(settings, "exclude_tools"))
    included = set(_iter_filter_names(settings, "include_tools"))
    if "browser_control" in excluded and "browser_read" not in excluded and "browser_read" not in included:
        return (
            "exclude_tools: browser_control",
            "read-only browser actions moved to `browser_read`, so excluding `browser_control` alone no longer "
            "disables `browser_read`; exclude both `['browser_control', 'browser_read']` to disable all browser "
            "actions, or use `include_tools: ['browser_read']` for read-only browser access",
        )
    return None


_RETIRED_STORED_FILTERS: dict[str, tuple[tuple[Mapping[str, str], Callable[[str, str, str], str]], ...]] = {
    "filesystem": ((_RETIRED_FILE_FUNCTIONS, lambda key, _old, new: f"use `filesystem: {{{key}: [{new}]}}`"),),
    "triggers": (
        (
            RETIRED_CONSOLIDATED_FUNCTIONS["triggers"],
            partial(_retired_consolidated_filter_guidance, "triggers"),
        ),
    ),
    "attachments": (
        (
            RETIRED_CONSOLIDATED_FUNCTIONS["attachments"],
            lambda _key, _old, new: (
                f"`{new}` is now a single unified function with an `action` parameter (alongside standalone `view_file`); "
                f"remove the per-function filter and use `tool_approval.rules` matching `{new}` if per-action gating "
                "(such as restricting `register` or `inline_image`) is needed"
            ),
        ),
    ),
    "atlassian": (
        (
            RETIRED_CONSOLIDATED_FUNCTIONS["atlassian"],
            partial(_retired_consolidated_filter_guidance, "atlassian"),
        ),
    ),
    "slack": (
        (
            _RETIRED_SLACK_FUNCTIONS,
            lambda _key, _old, _new: (
                '`slack_watch` has been retired in favor of `triggers_write(action="subscribe_event", source_id="slack", ...)`; '
                "remove `slack_watch` from the `slack` tool filter and configure `triggers` instead"
            ),
        ),
        (
            RETIRED_CONSOLIDATED_FUNCTIONS["slack"],
            partial(_retired_consolidated_filter_guidance, "slack"),
        ),
    ),
    **{
        name: ((RETIRED_CONSOLIDATED_FUNCTIONS[name], partial(_stored_consolidated_filter_guidance, name)),)
        for name in (
            "shared_directories",
            "contacts",
            "knowledge_graph",
            "agent_manager",
            "email_invite",
            "terminal",
            "claude_agent",
            "thread_model",
            "thread_snooze",
            "thread_resolution",
            "usage_stats",
        )
    },
    **{
        name: ((RETIRED_CONSOLIDATED_FUNCTIONS[name], partial(_stored_toggle_filter_guidance, name, hint)),)
        for name, hint in (
            ("serper", "`enable_*` settings"),
            ("hackernews", "`enable_*` settings"),
            ("google_bigquery", "`list_tables`/`describe_table` settings"),
        )
    },
}


def _retired_custom_tool_setting(tool_name: str, settings: Mapping[str, object]) -> tuple[str, str] | None:
    if tool_name == "filesystem":
        return next(
            ((field, guidance) for field, guidance in _RETIRED_FILE_SETTINGS.items() if field in settings),
            None,
        )
    if tool_name == "browser":
        return _retired_browser_filter(settings)
    return None


def retired_stored_tool_setting(
    tool_name: str,
    settings: Mapping[str, object],
    *,
    retired_functions: Mapping[str, str] | None = None,
    has_enable_toggles: bool = False,
) -> tuple[str, str] | None:
    """Return the first retired stored setting or function filter for ``tool_name``, and its replacement guidance."""
    if (filters := _RETIRED_STORED_FILTERS.get(tool_name)) is not None:
        for renames, guidance_for in filters:
            if (retired := _retired_function_filter(settings, renames, guidance_for=guidance_for)) is not None:
                return retired
    elif (renames := RETIRED_CONSOLIDATED_FUNCTIONS.get(tool_name)) is not None and (
        retired := _retired_function_filter(
            settings,
            renames,
            guidance_for=partial(
                _retired_fallback_filter_guidance,
                tool_name,
                has_enable_toggles=has_enable_toggles,
            ),
        )
    ) is not None:
        return retired
    if (retired := _retired_custom_tool_setting(tool_name, settings)) is not None:
        return retired
    if (split_renames := RETIRED_SPLIT_FUNCTIONS.get(tool_name)) is not None and (
        retired := _retired_function_filter(
            settings,
            split_renames,
            guidance_for=partial(_retired_split_filter_guidance, tool_name),
        )
    ) is not None:
        return retired
    if retired_functions:
        return _retired_function_filter(
            settings,
            retired_functions,
            guidance_for=partial(_retired_consolidated_filter_guidance, tool_name),
        )
    return None


def retired_tool_override(
    tool_name: str,
    overrides: Mapping[str, object],
    *,
    retired_functions: Mapping[str, str] | None = None,
    has_enable_toggles: bool = False,
) -> tuple[str, str] | None:
    """Return the first rejected retired field an entry of ``tool_name`` authors, and its replacement guidance."""
    if (
        retired := retired_stored_tool_setting(
            tool_name,
            overrides,
            retired_functions=retired_functions,
            has_enable_toggles=has_enable_toggles,
        )
    ) is not None:
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
