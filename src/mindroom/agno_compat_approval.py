"""Agno continuation bindings for persisted tool denials."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

from agno.agent._tools import reject_tool_call
from agno.run.agent import RunOutput
from agno.run.messages import RunMessages
from agno.tools.function import Function

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from agno.agent import Agent
    from agno.models.message import Message
    from agno.models.response import ToolExecution
    from agno.team import Team

# AGNO_COMPAT: Persisted tool denial requires live tools and private continuation hooks.
# Reason: Agno resolves live Functions even for denied persisted calls, and offers
# no continuation callback before tool lookup and resumed-message construction.
# Upstream issue: No matching public continuation/removed-tool denial issue identified.
# Upstream PR: None identified for this extension point.
# Remove when: Public Agent/Team continuation APIs can reject persisted calls without
# a live Function and invoke the owner's callback against the canonical resumed run.
# Coverage: tests/test_approval_response.py::test_denial_context_matches_run_identity_and_restores_lookup;
# tests/test_team_approval_dynamic_tools.py.


def append_denied_tool_result(
    actor: Agent | Team,
    messages: list[Message],
    tool: ToolExecution,
    *,
    tool_name: str,
) -> None:
    """Append Agno's rejection message without requiring the removed live tool."""
    reject_tool_call(
        cast("Agent", actor),
        RunMessages(messages=messages),
        tool,
        functions={tool_name: Function(name=tool_name, stop_after_tool_call=tool.stop_after_tool_call)},
    )


# AGNO_COMPAT: Continuation runs every confirmed call in the stored run, not only the supplied requirements.
# Reason: Agent and Team continuation keep stored tool entries that the supplied requirements do not replace,
# and run each one that is confirmed without a result or that requires user input; a run continued by ID is
# re-read from session storage first, so storage that worker code can write could add calls nobody approved.
# Upstream issue: No matching issue identified; tracking gap for continuing exactly the supplied requirements.
# Upstream PR: None identified.
# Remove when: Agent and Team continuation run only the caller's supplied requirements, or expose a public
# callback over the exact calls they are about to run; MindRoom's argument digest check must remain.
# Coverage: tests/test_response_runner_focused.py::test_agent_continuation_runs_only_approved_calls;
# tests/test_delegation_approval_ownership.py::test_saved_child_approval_preserves_executable_ownership;
# tests/test_team_approval_dynamic_tools.py::test_real_team_member_pause_reopens_with_exact_toolkit_owner.
def continuation_executes(tool: ToolExecution) -> bool:
    """Return whether Agno's continuation would run this tool entry."""
    return (
        tool.requires_confirmation is True and tool.confirmed is True and tool.result is None
    ) or tool.requires_user_input is True


@contextmanager
def before_tool_lookup(actor: Agent, callback: Callable[[RunOutput], None]) -> Iterator[None]:
    """Invoke the owner against each canonical run before Agno looks up its tools."""
    original = cast("Callable[..., Awaitable[list[object]]]", actor.aget_tools)

    async def tools_after_callback(*args: object, **kwargs: object) -> list[object]:
        run = kwargs.get("run_response")
        if isinstance(run, RunOutput):
            callback(run)
        return await original(*args, **kwargs)

    actor.__dict__["aget_tools"] = tools_after_callback
    try:
        yield
    finally:
        actor.__dict__["aget_tools"] = original
