"""Build a copied agent request for compaction without running an agent turn.

Agno's message/tool assembly is isolated here; no hooks, persistence, or tool
execution loop runs. The live model remains unchanged, including its cache and
conversation routing identity. Only inert function schemas leave this module.
"""

from __future__ import annotations

from copy import copy, deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from agno.agent import _messages as agent_messages
from agno.agent._tools import determine_tools_for_model
from agno.learn import LearningMachine
from agno.models.message import Message
from agno.run import RunContext
from agno.run.agent import RunInput, RunOutput
from agno.session.agent import AgentSession
from agno.tools.function import Function

from mindroom.claude_prompt_cache import as_anthropic_claude
from mindroom.history_run_visibility import is_model_history_visible_run
from mindroom.model_instance_checks import isinstance_of_loaded
from mindroom.prompts import COMPACTION_MODE_INSTRUCTION, COMPACTION_SUMMARY_PROMPT
from mindroom.token_budget import stable_serialize

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.agent import Agent
    from agno.models.base import Model
    from agno.models.openai import OpenAIChat, OpenAIResponses
    from agno.run.team import TeamRunOutput
    from agno.session.team import TeamSession


@dataclass(frozen=True)
class WarmPrefixRequest:
    """One immutable request snapshot; model settings are borrowed, never changed."""

    model: Model
    messages: tuple[Message, ...]
    tools: tuple[dict, ...]
    tool_choice: str | dict | None
    input_estimate: int


async def build_warm_prefix_request(
    *,
    agent: Agent,
    session: AgentSession | TeamSession,
    included_runs: Sequence[RunOutput | TeamRunOutput],
    summary_prompt: str,
    max_input_tokens: int,
    token_estimator: Callable[[str], int],
    supplemental_context: str,
) -> WarmPrefixRequest | None:
    """Reuse an eligible agent prefix, or leave this chunk to the standalone caller.

    Agno 3 makes per-run copies of Functions during tool preparation. We copy the
    Agent and session too, so synthetic run state never reaches the live turn.
    Request size includes the entire prefix, not just the serialized conversation.
    """
    model = agent.model
    if (
        model is None
        or not isinstance(session, AgentSession)
        or not all(isinstance(run, RunOutput) for run in included_runs)
        or not all(is_model_history_visible_run(run) for run in included_runs)
        or summary_prompt != COMPACTION_SUMMARY_PROMPT
        or not isinstance(agent.instructions, list)
        or COMPACTION_MODE_INSTRUCTION not in agent.instructions
        or agent.system_message is not None
        or agent.output_schema is not None
        # Reply replay caps tool calls across history; compaction caps per run.
        # Use the standalone projection so selected tool facts cannot disappear.
        or agent.max_tool_calls_from_history is not None
        or (session.summary is not None and not agent.add_session_summary_to_context)
    ):
        return None
    if not _model_supports_warm_compaction(model):
        return None

    request_agent = copy(agent)
    request_agent.num_history_runs = None
    request_agent.num_history_messages = None
    request_agent._tool_instructions = deepcopy(agent._tool_instructions)
    if isinstance(agent.learning, LearningMachine):
        # Copy mutable learning configuration/state, retaining connection owners.
        # Agno initialization and store resolution inject model/db dependencies.
        shared = (agent.learning.db, agent.learning.model, agent.learning.knowledge)
        request_agent.learning = deepcopy(agent.learning, {id(value): value for value in shared if value is not None})
    request_agent.initialize_agent()
    fork = replace(deepcopy(session), runs=deepcopy(list(included_runs)))
    run_id = str(uuid4())
    final_message = Message(
        role="user",
        content=(
            "<mindroom_compaction_request>\n"
            "Summarize the preceding conversation and previous summary using the handoff protocol.\n"
            f"{supplemental_context}\n</mindroom_compaction_request>"
        ),
    )
    run_context = RunContext(run_id=run_id, session_id=fork.session_id, user_id=fork.user_id, session_state={})
    run_response = RunOutput(
        run_id=run_id,
        session_id=fork.session_id,
        agent_id=agent.id,
        user_id=fork.user_id,
        input=RunInput(input_content=final_message),
        session_state={},
    )
    processed_tools = await request_agent.aget_tools(
        run_response=run_response,
        run_context=run_context,
        session=fork,
        user_id=fork.user_id,
    )
    # Dict tools may execute on the provider (web search, remote MCP, code).
    # Never send them in a compaction request with tool choice left unchanged.
    if any(isinstance(tool, dict) for tool in processed_tools):
        return None
    prepared_tools = determine_tools_for_model(
        agent=request_agent,
        model=model,
        processed_tools=processed_tools,
        run_response=run_response,
        run_context=run_context,
        session=fork,
        async_mode=True,
    )
    tools = tuple(
        {"type": "function", "function": deepcopy(tool.to_dict())}
        for tool in prepared_tools
        if isinstance(tool, Function)
    )
    run_messages = await agent_messages.aget_run_messages(
        request_agent,
        run_response=run_response,
        run_context=run_context,
        input=final_message,
        session=fork,
        user_id=fork.user_id,
        tools=prepared_tools,
        add_history_to_context=True,
        add_dependencies_to_context=False,
        add_session_state_to_context=False,
    )
    messages = tuple(message.model_copy(deep=True) for message in run_messages.messages)
    # Vertex's request fitting may drop from_history messages on overflow.
    # Every selected run must reach the summary model before it is tombstoned.
    for message in messages:
        message.from_history = False
    serialized = stable_serialize({"messages": [message.to_dict() for message in messages], "tools": tools})
    input_estimate = token_estimator(serialized)
    if input_estimate > max_input_tokens:
        return None
    return WarmPrefixRequest(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice=agent.tool_choice,
        input_estimate=input_estimate,
    )


def _model_supports_warm_compaction(model: Model) -> bool:
    """Exclude provider features that can execute tools or silently drop input."""
    claude = as_anthropic_claude(model)
    if claude is not None:
        return bool(claude.cache_system_prompt) and not (
            claude.mcp_servers or claude.skills or claude.context_management or claude.request_params
        )
    if not isinstance_of_loaded(
        model,
        ("agno.models.openai.chat", "OpenAIChat"),
        ("agno.models.openai.responses", "OpenAIResponses"),
    ):
        return False
    openai_model = cast("OpenAIChat | OpenAIResponses", model)
    # Raw request overrides can introduce hosted tools or replace messages.
    if openai_model.request_params or openai_model.extra_body or model.id.endswith("deep-research"):
        return False
    if isinstance_of_loaded(model, ("agno.models.openai.responses", "OpenAIResponses")):
        responses_model = cast("OpenAIResponses", model)
        if responses_model.truncation == "auto":
            return False

    return True
