"""Runtime-derived config overlays that are not authored YAML."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any, cast

from mindroom.config.tool_entries import raw_tool_entry_name
from mindroom.constants import RuntimePaths

_APPROVED_EGRESS_ENABLED_ENV = "MINDROOM_APPROVED_EGRESS_ENABLED"
_APPROVED_EGRESS_TOOL_NAME = "approved_egress"
_APPROVED_EGRESS_TOOL_FUNCTION_NAME = "request_network_access"
_APPROVED_EGRESS_APPROVAL_RULE: dict[str, str] = {
    "match": _APPROVED_EGRESS_TOOL_FUNCTION_NAME,
    "action": "require_approval",
}


@dataclass(frozen=True)
class _RuntimeApprovedEgressOverlayResult:
    """Effective config data plus markers for runtime-derived entries."""

    data: object
    injected_default_tool: bool = False
    injected_approval_rule: bool = False


def _runtime_approved_egress_rule_present(rules: list[object]) -> bool:
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        raw_rule = cast("dict[object, object]", rule)
        match = raw_rule.get("match")
        if not isinstance(match, str) or not fnmatchcase(_APPROVED_EGRESS_TOOL_FUNCTION_NAME, match):
            continue
        return raw_rule.get("action") == "require_approval"
    return False


def apply_runtime_approved_egress_overlay(
    data: object,
    runtime_paths: RuntimePaths,
) -> _RuntimeApprovedEgressOverlayResult:
    """Add chart-managed approved egress config without requiring authored YAML edits."""
    if not runtime_paths.env_flag(_APPROVED_EGRESS_ENABLED_ENV) or not isinstance(data, dict):
        return _RuntimeApprovedEgressOverlayResult(data)

    config_data = cast("dict[object, object]", data.copy())
    raw_defaults = config_data.get("defaults")
    if "defaults" in config_data and not isinstance(raw_defaults, dict):
        return _RuntimeApprovedEgressOverlayResult(config_data)
    defaults = cast("dict[object, object]", raw_defaults).copy() if isinstance(raw_defaults, dict) else {}
    raw_tools = defaults.get("tools")
    if raw_tools is None:
        tools: list[object] = []
    elif isinstance(raw_tools, list):
        tools = list(raw_tools)
    else:
        config_data["defaults"] = defaults
        return _RuntimeApprovedEgressOverlayResult(config_data)
    injected_default_tool = False
    if all(raw_tool_entry_name(entry) != _APPROVED_EGRESS_TOOL_NAME for entry in tools):
        tools.append(_APPROVED_EGRESS_TOOL_NAME)
        injected_default_tool = True
    defaults["tools"] = tools
    config_data["defaults"] = defaults

    raw_tool_approval = config_data.get("tool_approval")
    if "tool_approval" in config_data and not isinstance(raw_tool_approval, dict):
        return _RuntimeApprovedEgressOverlayResult(
            config_data,
            injected_default_tool=injected_default_tool,
        )
    tool_approval = (
        cast("dict[object, object]", raw_tool_approval).copy() if isinstance(raw_tool_approval, dict) else {}
    )
    raw_rules = tool_approval.get("rules")
    if raw_rules is None:
        rules = []
    elif isinstance(raw_rules, list):
        rules = list(raw_rules)
    else:
        config_data["tool_approval"] = tool_approval
        return _RuntimeApprovedEgressOverlayResult(
            config_data,
            injected_default_tool=injected_default_tool,
        )
    injected_approval_rule = False
    if not _runtime_approved_egress_rule_present(rules):
        rules.insert(0, dict(_APPROVED_EGRESS_APPROVAL_RULE))
        injected_approval_rule = True
    tool_approval["rules"] = rules
    config_data["tool_approval"] = tool_approval
    return _RuntimeApprovedEgressOverlayResult(
        config_data,
        injected_default_tool=injected_default_tool,
        injected_approval_rule=injected_approval_rule,
    )


def _strip_runtime_approved_egress_default_tool(authored_payload: dict[str, Any]) -> None:
    defaults = authored_payload.get("defaults")
    if not isinstance(defaults, dict):
        return
    tools = defaults.get("tools")
    if not isinstance(tools, list):
        return
    defaults["tools"] = [entry for entry in tools if raw_tool_entry_name(entry) != _APPROVED_EGRESS_TOOL_NAME]
    if not defaults["tools"]:
        defaults.pop("tools", None)
    if not defaults:
        authored_payload.pop("defaults", None)


def _strip_runtime_approved_egress_approval_rule(authored_payload: dict[str, Any]) -> None:
    tool_approval = authored_payload.get("tool_approval")
    if not isinstance(tool_approval, dict):
        return
    rules = tool_approval.get("rules")
    if not isinstance(rules, list):
        return
    for index, rule in enumerate(rules):
        if rule == _APPROVED_EGRESS_APPROVAL_RULE:
            rules.pop(index)
            break
    if not rules:
        tool_approval.pop("rules", None)


def strip_runtime_approved_egress_overlay_from_dump(
    payload: dict[str, Any],
    *,
    injected_default_tool: bool,
    injected_approval_rule: bool,
) -> dict[str, Any]:
    """Remove runtime-derived approved egress entries from an authored config dump."""
    authored_payload = deepcopy(payload)
    if injected_default_tool:
        _strip_runtime_approved_egress_default_tool(authored_payload)
    if injected_approval_rule:
        _strip_runtime_approved_egress_approval_rule(authored_payload)
    return authored_payload
