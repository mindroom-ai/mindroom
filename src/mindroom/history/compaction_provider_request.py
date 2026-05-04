"""MindRoom compaction glue for Agno forked provider requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING
from uuid import uuid4

from agno.models.message import Message
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.history.agno_forked_request import (
    AgnoProviderRequest,
    PreparedTool,
    agent_tool_definition_payloads_for_logging,
    build_agent_provider_request_from_prefix,
    build_team_provider_request_from_prefix,
    prepare_agent_prompt_inputs_for_estimation,
    prepare_team_prompt_inputs_for_estimation,
    prepare_tools_for_estimation,
    prepared_tool_definition_payloads,
    preserve_tool_instructions,
    team_tool_definition_payloads_for_logging,
)
from mindroom.prepared_conversation_chain import CompactionSummaryRequest, compaction_summary_instruction
from mindroom.token_budget import estimate_text_tokens, stable_serialize

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.team import Team


CompactionProviderRequest = AgnoProviderRequest
type CompactionProviderRequestBuilder = Callable[
    [CompactionSummaryRequest, AgentSession | TeamSession],
    Awaitable[CompactionProviderRequest],
]

__all__ = [
    "CompactionProviderRequest",
    "CompactionProviderRequestBuilder",
    "agent_tool_definition_payloads_for_logging",
    "build_agent_compaction_provider_request",
    "build_team_compaction_provider_request",
    "compute_prompt_token_breakdown",
    "estimate_agent_static_tokens",
    "estimate_team_static_tokens",
    "estimate_tool_definition_tokens",
    "team_tool_definition_payloads_for_logging",
]


async def build_agent_compaction_provider_request(
    summary_request: CompactionSummaryRequest,
    session: AgentSession | TeamSession,
    *,
    agent: Agent,
) -> CompactionProviderRequest:
    """Build compaction as a normal agent request prefix plus one summary user turn."""
    if not isinstance(session, AgentSession):
        msg = "agent compaction request builder requires an AgentSession"
        raise TypeError(msg)
    return await build_agent_provider_request_from_prefix(
        agent=agent,
        source_session=session,
        prefix_messages=summary_request.chain.messages,
        final_user_message=_final_summary_message(
            summary_request,
            previous_summary_in_context=_agent_includes_session_summary(agent, session),
        ),
        synthetic_run_id=_synthetic_compaction_run_id(summary_request),
    )


async def build_team_compaction_provider_request(
    summary_request: CompactionSummaryRequest,
    session: AgentSession | TeamSession,
    *,
    team: Team,
) -> CompactionProviderRequest:
    """Build compaction as a normal team request prefix plus one summary user turn."""
    if not isinstance(session, TeamSession):
        msg = "team compaction request builder requires a TeamSession"
        raise TypeError(msg)
    return await build_team_provider_request_from_prefix(
        team=team,
        source_session=session,
        prefix_messages=summary_request.chain.messages,
        final_user_message=_final_summary_message(
            summary_request,
            previous_summary_in_context=_team_includes_session_summary(team, session),
        ),
        synthetic_run_id=_synthetic_compaction_run_id(summary_request),
    )


def _final_summary_message(
    summary_request: CompactionSummaryRequest,
    *,
    previous_summary_in_context: bool,
) -> Message:
    if previous_summary_in_context:
        return Message(
            role="user",
            content=compaction_summary_instruction(None, previous_summary_in_context=True),
        )
    return summary_request.messages[-1]


def _agent_includes_session_summary(agent: Agent, session: AgentSession) -> bool:
    return (
        _session_has_summary_text(session)
        and bool(agent.add_session_summary_to_context)
        and agent.system_message is None
        and agent.build_context
    )


def _team_includes_session_summary(team: Team, session: TeamSession) -> bool:
    return (
        _session_has_summary_text(session) and bool(team.add_session_summary_to_context) and team.system_message is None
    )


def _session_has_summary_text(session: AgentSession | TeamSession) -> bool:
    return session.summary is not None and bool(session.summary.summary.strip())


def estimate_agent_static_tokens(agent: Agent, full_prompt: str) -> int:
    """Estimate the non-history agent prompt using Agno's real system-message builder."""
    static_tokens = estimate_text_tokens(full_prompt)
    with preserve_tool_instructions(agent):
        session, run_context, prepared_tools = prepare_agent_prompt_inputs_for_estimation(agent)
        system_message = agent.get_system_message(
            session=session,
            run_context=run_context,
            tools=prepared_tools or None,
            add_session_state_to_context=False,
        )
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + _estimate_prepared_tool_definition_tokens(prepared_tools)


def estimate_tool_definition_tokens(agent: Agent) -> int:
    """Estimate the model-visible tool schema and tool instructions for one agent."""
    prepared_tools, tool_instructions = prepare_tools_for_estimation(agent.tools)
    return _estimate_prepared_tool_definition_tokens(
        prepared_tools,
        tool_instructions=tool_instructions,
    )


def estimate_team_static_tokens(team: Team, full_prompt: str) -> int:
    """Estimate the non-history team prompt using Agno's team system-message builder."""
    static_tokens = estimate_text_tokens(full_prompt)
    with preserve_tool_instructions(team):
        session, prepared_tools = prepare_team_prompt_inputs_for_estimation(team)
        system_message = team.get_system_message(
            session=session,
            tools=prepared_tools or None,
            add_session_state_to_context=False,
        )
    if system_message is not None and system_message.content is not None:
        static_tokens += estimate_text_tokens(str(system_message.content))
    return static_tokens + _estimate_prepared_tool_definition_tokens(prepared_tools)


def compute_prompt_token_breakdown(
    agent: Agent | None = None,
    team: Team | None = None,
    full_prompt: str | None = None,
) -> dict[str, int]:
    """Compute token breakdown for system prompt, tool defs, and current prompt."""
    breakdown: dict[str, int] = {}

    if agent is not None:
        sys_chars = len(agent.role or "")
        instructions = agent.instructions
        if isinstance(instructions, str):
            sys_chars += len(instructions)
        elif isinstance(instructions, list):
            for instruction in instructions:
                sys_chars += len(str(instruction))
        breakdown["role_instructions_tokens"] = sys_chars // 4

    tool_tokens = 0
    if agent is not None:
        tool_tokens = estimate_tool_definition_tokens(agent)
    elif team is not None:
        prepared_tools, _tool_instructions = prepare_tools_for_estimation(team.tools)
        tool_tokens = _estimate_prepared_tool_definition_tokens(prepared_tools)
    breakdown["tool_definition_tokens"] = tool_tokens

    if full_prompt is not None:
        breakdown["current_prompt_tokens"] = len(full_prompt) // 4

    return breakdown


def _estimate_prepared_tool_definition_tokens(
    prepared_tools: Sequence[PreparedTool],
    *,
    tool_instructions: Sequence[str] = (),
) -> int:
    tool_definitions = prepared_tool_definition_payloads(prepared_tools)
    tool_definition_tokens = len(stable_serialize(tool_definitions)) // 4 if tool_definitions else 0
    instruction_tokens = sum(estimate_text_tokens(instruction) for instruction in tool_instructions)
    return tool_definition_tokens + instruction_tokens


def _synthetic_compaction_run_id(summary_request: CompactionSummaryRequest) -> str:
    if summary_request.included_run_ids:
        return "+".join(summary_request.included_run_ids)
    return f"compaction-summary-{uuid4()}"
