"""Unified triggers tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ApprovalAlias, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.custom_tools.triggers import TriggersTools


_NON_CODE_TRIGGER_ACTIONS = frozenset(
    {
        "watch_room",
        "watch_thread",
        "watch_pr",
        "subscribe_event",
        "create",
        "list",
        "set_enabled",
        "delete",
        "rotate_key",
        "history",
        "test",
    },
)


def _judge_triggers_script_check(arguments: Mapping[str, object]) -> list[dict[str, object]] | None:
    action = arguments.get("action")
    if not isinstance(action, str) or not action.strip():
        return None
    normalized = action.strip()
    if normalized == "watch_script":
        return [
            {
                "source": arguments.get("source"),
                "path": arguments.get("path"),
                "name": arguments.get("trigger_id") or arguments.get("description"),
            },
        ]
    if normalized == "update":
        if arguments.get("source") is not None or arguments.get("path") is not None:
            return [
                {
                    "source": arguments.get("source"),
                    "path": arguments.get("path"),
                    "name": arguments.get("trigger_id"),
                },
            ]
        return []
    if normalized in _NON_CODE_TRIGGER_ACTIONS:
        return []
    return None


@register_tool_with_metadata(
    name="triggers",
    file_access=ToolFileAccess.NONE,
    display_name="Triggers",
    description="Watch GitHub PRs, scheduled Python/tool checks, rooms, thread tags, shared event sources (with optional LLM triage), or direct signed webhooks to wake agents automatically",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_primary_runtime=True,
    requires_room_context=True,
    icon="Webhook",
    icon_color="text-emerald-500",
    docs_url="https://docs.mindroom.chat/external-triggers/",
    function_names=("triggers",),
    approval_aliases={"triggers": ApprovalAlias(judged_as="start_script", judge=_judge_triggers_script_check)},
)
def triggers_tools() -> type[TriggersTools]:
    """Return unified trigger management tools."""
    from mindroom.custom_tools.triggers import TriggersTools

    return TriggersTools
