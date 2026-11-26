"""Unified triggers tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ApprovalAlias, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.custom_tools.triggers import TriggersTools


def _judge_watch_pr(_arguments: Mapping[str, object]) -> None:
    """Fail closed to ``triggers`` approval rules whenever they may ask, since ``watch_pr`` creates a trigger."""


@register_tool_with_metadata(
    name="triggers",
    file_access=ToolFileAccess.NONE,
    display_name="Triggers",
    description="Watch rooms and shared event sources (with optional LLM triage), thread tags, or direct signed webhooks to wake agents automatically",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    requires_primary_runtime=True,
    requires_room_context=True,
    icon="Webhook",
    icon_color="text-emerald-500",
    docs_url="https://docs.mindroom.chat/external-triggers/",
    function_names=("triggers", "watch_pr"),
    approval_aliases={"watch_pr": ApprovalAlias(judged_as="triggers", judge=_judge_watch_pr)},
)
def triggers_tools() -> type[TriggersTools]:
    """Return unified trigger management tools."""
    from mindroom.custom_tools.triggers import TriggersTools

    return TriggersTools
