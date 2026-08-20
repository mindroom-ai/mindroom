"""Shared approval overlay for unattended tool execution."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.approval import ApprovalRuleConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.config.main import Config

NEVER_PREAPPROVE_TOOLKITS = frozenset({"claude_agent", "config_manager", "scheduler", "subagents"})


def build_automation_approval_config(
    config: Config,
    *,
    function_owners: Mapping[str, frozenset[str]],
    preapproved_toolkits: frozenset[str],
    never_preapprove_toolkits: frozenset[str],
) -> Config:
    """Require approval by default and append safe function-level auto-approval rules."""
    allow_all = "*" in preapproved_toolkits
    # A bare function name can belong to multiple toolkits. Auto-approve it only
    # when every owner is eligible; live operator rules remain first-match-wins.
    safe_preapproved_toolkits = {
        toolkit_name
        for owners in function_owners.values()
        for toolkit_name in owners
        if toolkit_name not in never_preapprove_toolkits and (allow_all or toolkit_name in preapproved_toolkits)
    }
    preapproved_functions = sorted(
        function_name for function_name, owners in function_owners.items() if owners <= safe_preapproved_toolkits
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
