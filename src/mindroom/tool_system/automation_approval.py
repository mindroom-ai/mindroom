"""Shared approval overlay for unattended tool execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.approval import ApprovalRuleConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.tools import Toolkit

    from mindroom.config.main import Config


def build_automation_approval_config(
    config: Config,
    *,
    toolkits_by_name: Mapping[str, Toolkit],
    preapproved_toolkits: frozenset[str],
    never_preapprove_toolkits: frozenset[str],
) -> Config:
    """Require approval by default and append safe function-level auto-approval rules."""
    allow_all = "*" in preapproved_toolkits
    owning_toolkits: dict[str, set[str]] = {}
    for toolkit_name, toolkit in toolkits_by_name.items():
        for function_name in (*toolkit.functions, *toolkit.async_functions):
            owning_toolkits.setdefault(function_name, set()).add(toolkit_name)

    safe_preapproved_toolkits = {
        toolkit_name
        for toolkit_name in toolkits_by_name
        if toolkit_name not in never_preapprove_toolkits and (allow_all or toolkit_name in preapproved_toolkits)
    }
    preapproved_functions = sorted(
        function_name for function_name, owners in owning_toolkits.items() if owners <= safe_preapproved_toolkits
    )
    tool_approval = config.tool_approval.model_copy(
        update={
            "default": "require_approval",
            "rules": [
                *config.tool_approval.rules,
                *(
                    ApprovalRuleConfig(match=function_name, action="auto_approve")
                    for function_name in preapproved_functions
                ),
            ],
        },
    )
    return config.model_copy(update={"tool_approval": tool_approval})
