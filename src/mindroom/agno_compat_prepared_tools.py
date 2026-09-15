"""Shared private Agno tool preparation and temporary instruction-state workarounds.

Keep SDK internals here; prompt_tokens owns caching and token estimation.
Verified against the pinned Agno version by prompt-surface integration tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.agent._tools import determine_tools_for_model
from agno.team._tools import _determine_tools_for_model

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from agno.agent import Agent
    from agno.run import RunContext
    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from agno.session.agent import AgentSession
    from agno.session.team import TeamSession
    from agno.team import Team
    from agno.tools.function import Function
    from agno.tools.toolkit import Toolkit


# AGNO_COMPAT: Prompt inspection requires private tool preparation.
# Reason: Prompt estimation needs private tool preparation and temporary _tool_instructions.
# Upstream issue: https://github.com/agno-agi/agno/issues/7806
# Upstream PR: https://github.com/agno-agi/agno/pull/7807
# Remove when: Public prepared-request inspection supplies the actual messages, tools,
# and tool instructions without persisting a run or mutating live instruction state.
# Coverage: tests/test_agno_compat_prompt.py::test_prompt_builder_failure_restores_original_instruction_list;
# tests/test_history_prompt_tokens.py.


@contextmanager
def temporary_tool_instructions(entity: Agent | Team, instructions: Sequence[str]) -> Iterator[None]:
    """Restore the exact original instruction list, including when prompt building fails."""
    previous = entity._tool_instructions
    entity._tool_instructions = list(instructions)
    try:
        yield
    finally:
        entity._tool_instructions = previous


@dataclass(frozen=True)
class _PreparedTeamPromptTools:
    """SDK-prepared tools and instructions before restoring the team's live state."""

    tools: tuple[Function | dict, ...]
    tool_instructions: tuple[str, ...]


def prepare_team_prompt_tools(
    team: Team,
    *,
    session: TeamSession,
    run_response: TeamRunOutput,
    run_context: RunContext,
) -> _PreparedTeamPromptTools:
    """Use Agno's actual tool preparation without retaining its instruction mutation."""
    model = team.model
    assert model is not None
    with temporary_tool_instructions(team, team._tool_instructions or ()):
        tools = _determine_tools_for_model(
            team=team,
            model=model,
            run_response=run_response,
            run_context=run_context,
            team_run_context={},
            session=session,
            check_mcp_tools=False,
        )
        return _PreparedTeamPromptTools(tuple(tools), tuple(team._tool_instructions or ()))


# AGNO_COMPAT: Executable tool preparation lacks public run-context and media bindings.
# Reason: RTC needs prepared Functions with Agno's run context and media bindings,
# which Agent.aget_tools alone does not supply through a public preparation API.
# Upstream issue: https://github.com/agno-agi/agno/issues/7806
# Upstream PR: https://github.com/agno-agi/agno/pull/7807 is related inspection work;
# it must also support executable run-context/media bindings to replace this path.
# Remove when: A public Agent preparation API returns the same effective Functions
# for execution; RTC filtering and requester authorization remain with the owner.
# Coverage: tests/test_matrix_rtc_call_tools.py::test_build_call_tools_returns_same_agent_prompt_and_tools;
# tests/test_matrix_rtc_call_tools.py::test_build_call_tools_includes_async_only_toolkit_functions.
def prepare_agent_tools(
    agent: Agent,
    *,
    processed_tools: list[Toolkit | Callable | Function | dict],
    run_response: RunOutput,
    run_context: RunContext,
    session: AgentSession,
) -> list[Function | dict]:
    """Prepare executable async Agent tools with Agno's canonical context bindings."""
    assert agent.model is not None
    return determine_tools_for_model(
        agent,
        model=agent.model,
        processed_tools=processed_tools,
        run_response=run_response,
        run_context=run_context,
        session=session,
        async_mode=True,
    )
