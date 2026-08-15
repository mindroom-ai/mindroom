"""Test-only constructors for detached authorization-aware runtimes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.commands.handler import CommandHandlerContext
    from mindroom.orchestration.external_trigger_runtime import ExternalTriggerRuntimeCoordinator
    from mindroom.scheduling import SchedulingRuntime
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.turn_policy import TurnPolicyDeps


def isolated_membership_index() -> AgentReplyMembershipIndex:
    """Return a fresh real index for one detached unit-test runtime."""
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex  # noqa: PLC0415

    return AgentReplyMembershipIndex()


def make_test_tool_runtime_context(*args: Any, **kwargs: Any) -> ToolRuntimeContext:  # noqa: ANN401
    """Build a detached tool context with an explicit isolated membership index."""
    from mindroom.tool_system.runtime_context import ToolRuntimeContext  # noqa: PLC0415

    kwargs.setdefault("agent_reply_memberships", isolated_membership_index())
    return ToolRuntimeContext(*args, **kwargs)


def make_test_turn_policy_deps(*args: Any, **kwargs: Any) -> TurnPolicyDeps:  # noqa: ANN401
    """Build detached turn-policy dependencies with an explicit isolated index."""
    from mindroom.turn_policy import TurnPolicyDeps  # noqa: PLC0415

    kwargs.setdefault("agent_reply_memberships", isolated_membership_index())
    return TurnPolicyDeps(*args, **kwargs)


def make_test_command_handler_context(*args: Any, **kwargs: Any) -> CommandHandlerContext:  # noqa: ANN401
    """Build a detached command context with an explicit isolated index."""
    from mindroom.commands.handler import CommandHandlerContext  # noqa: PLC0415

    kwargs.setdefault("agent_reply_memberships", isolated_membership_index())
    return CommandHandlerContext(*args, **kwargs)


def make_test_scheduling_runtime(*args: Any, **kwargs: Any) -> SchedulingRuntime:  # noqa: ANN401
    """Build a detached scheduling runtime with an explicit isolated index."""
    from mindroom.scheduling import SchedulingRuntime  # noqa: PLC0415

    kwargs.setdefault("agent_reply_memberships", isolated_membership_index())
    return SchedulingRuntime(*args, **kwargs)


def make_test_external_trigger_runtime_coordinator(
    *args: Any,  # noqa: ANN401
    **kwargs: Any,  # noqa: ANN401
) -> ExternalTriggerRuntimeCoordinator:
    """Build a detached trigger coordinator with an explicit isolated index."""
    from mindroom.orchestration.external_trigger_runtime import (  # noqa: PLC0415
        ExternalTriggerRuntimeCoordinator,
    )

    kwargs.setdefault("agent_reply_memberships", isolated_membership_index())
    return ExternalTriggerRuntimeCoordinator(*args, **kwargs)


def make_test_bot_for_entity(*args: Any, **kwargs: Any) -> AgentBot | TeamBot | None:  # noqa: ANN401
    """Build a detached entity bot with an explicit isolated membership index."""
    from mindroom.bot import create_bot_for_entity  # noqa: PLC0415

    kwargs.setdefault("agent_reply_memberships", isolated_membership_index())
    return create_bot_for_entity(*args, **kwargs)
