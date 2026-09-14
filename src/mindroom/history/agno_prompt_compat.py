"""Private Agno prompt-builder calls and temporary instruction-state workarounds.

Keep SDK internals here; prompt_tokens owns caching and token estimation.
Verified against the pinned Agno version by prompt-surface integration tests.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agno.team._tools import _determine_tools_for_model

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from agno.agent import Agent
    from agno.run import RunContext
    from agno.run.team import TeamRunOutput
    from agno.session.team import TeamSession
    from agno.team import Team
    from agno.tools.function import Function


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
