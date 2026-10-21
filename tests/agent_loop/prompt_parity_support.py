"""Run the prompt-golden scenarios and capture the owned prompt inputs of every system message they build.

While Agno still runs the scenarios' entry points, `PromptCapture` reads the inputs MindRoom hands Agno's
system-message builder, so owned prompt code is compared with the goldens through the same production builders.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from agno.agent import _messages as agno_messages
from agno.models.anthropic import Claude

from mindroom.agent_loop.prompt import PromptSpec, model_note_for, render_system_message
from mindroom.agent_loop.structured import select_response_format
from mindroom.llm.provider import Capabilities
from tests.agno_parity.prompt_scenarios import SCENARIOS, ModelCall, Recorder

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from agno.agent import Agent
    from agno.models.base import Model
    from agno.models.message import Message
    from agno.run import RunContext
    from agno.session.agent import AgentSession
    from pydantic import BaseModel


@dataclass(frozen=True)
class CapturedPrompt:
    """One system message Agno built: the owned prompt inputs MindRoom handed it, and what Agno rendered from them."""

    spec: PromptSpec
    agno_content: str | None
    capabilities: Capabilities
    output_schema: type[BaseModel] | None


def agno_model_capabilities(model: Model) -> Capabilities:
    """Return the capabilities an owned adapter reports for the route this Agno model serves, as the prompt reads them."""
    if model.supports_native_structured_outputs:
        structured_output = "native"
    elif model.supports_json_schema_outputs:
        structured_output = "json_schema"
    else:
        structured_output = "json_mode"
    return Capabilities(structured_output=structured_output, claude_family=isinstance(model, Claude))


def _check_modeled_inputs(agent: Agent, tools: list[Any] | None, *, add_session_state_to_context: bool | None) -> None:
    """Fail when an agent uses a system-message input of Agno's that ``PromptSpec`` does not model."""
    assert agent.build_context
    assert agent.system_message_role == "system"
    assert agent.system_message is None or isinstance(agent.system_message, str)
    assert not callable(agent.instructions)
    assert agent.model is not None
    assert agent.model.get_instructions_for_model(tools) is None
    assert agent.model.system_prompt is None
    assert not agent.use_instruction_tags
    assert not agent.add_datetime_to_context
    assert not agent.add_location_to_context
    assert agent.name is None or not agent.add_name_to_context
    assert agent._result_store is None
    assert agent.expected_output is None
    assert not agent.add_memories_to_context
    assert not agent.add_session_summary_to_context
    assert agent._learning is None
    assert agent.parser_model is None
    assert not agent.use_json_mode
    assert agent.structured_outputs is None
    assert not agent.enable_agentic_knowledge_filters
    assert not add_session_state_to_context


async def _prompt_spec(agent: Agent, run_context: RunContext | None, tools: list[Any] | None) -> PromptSpec:
    """Return the owned prompt inputs for the system message Agno is about to build for ``agent``."""
    if isinstance(agent.system_message, str):
        return PromptSpec(system_message=agent.system_message)
    assert agent.model is not None
    capabilities = agno_model_capabilities(agent.model)
    output_schema = run_context.output_schema if run_context is not None else None
    instructions = agent.instructions
    knowledge = agent.knowledge
    knowledge_block = (
        await knowledge.abuild_context(enable_agentic_filters=False)
        if knowledge is not None and agent.search_knowledge and agent.add_search_knowledge_instructions
        else None
    )
    return PromptSpec(
        description=agent.description,
        role=agent.role,
        instructions=(instructions,) if isinstance(instructions, str) else tuple(instructions or ()),
        markdown=agent.markdown and output_schema is None,
        tool_instructions=tuple(agent._tool_instructions or ()),
        additional_context=agent.additional_context,
        # MindRoom's agents carry the owned skills toolkit inside the Skills object Agno's Agent takes.
        skills_block=agent.skills.toolkit.prompt_block() if agent.skills is not None else None,
        knowledge_block=knowledge_block,
        model_note=model_note_for(capabilities, offers_tools=bool(tools)),
        output_format_prompt=None
        if output_schema is None
        else select_response_format(capabilities, output_schema).prompt,
    )


@dataclass
class PromptCapture:
    """Record the owned prompt inputs of every system message Agno builds while installed."""

    prompts: list[CapturedPrompt] = field(default_factory=list)

    @contextmanager
    def installed(self) -> Iterator[None]:
        """Wrap Agno's system-message builder, which agent runs and MindRoom's prompt exports both call."""
        build = agno_messages.aget_system_message

        async def capture(
            agent: Agent,
            session: AgentSession,
            run_context: RunContext | None = None,
            tools: list[Any] | None = None,
            add_session_state_to_context: bool | None = None,
            input: object = None,  # noqa: A002 - Agno's parameter name
        ) -> Message | None:
            _check_modeled_inputs(agent, tools, add_session_state_to_context=add_session_state_to_context)
            spec = await _prompt_spec(agent, run_context, tools)
            message = await build(
                agent,
                session=session,
                run_context=run_context,
                tools=tools,
                add_session_state_to_context=add_session_state_to_context,
                input=input,
            )
            assert agent.model is not None
            self.prompts.append(
                CapturedPrompt(
                    spec=spec,
                    agno_content=None if message is None else message.content,
                    capabilities=agno_model_capabilities(agent.model),
                    output_schema=run_context.output_schema if run_context is not None else None,
                ),
            )
            return message

        with patch.object(agno_messages, "aget_system_message", capture):
            yield


@dataclass(frozen=True)
class ScenarioRun:
    """What one scenario sent: its recorded input, exported values, model calls, and system-message captures."""

    input: dict[str, Any]
    exported: dict[str, Any] | None
    calls: list[ModelCall]
    prompts: list[CapturedPrompt]


async def run_scenario(name: str, root: Path) -> ScenarioRun:
    """Run one scenario under ``root`` against the recording model."""
    scenario = SCENARIOS[name]
    recorder = Recorder(responder=scenario.responder)
    prompts = PromptCapture()
    with recorder.installed(), prompts.installed():
        output = await scenario.run(root)
    return ScenarioRun(output.input, output.exported, recorder.ordered_calls(), prompts.prompts)


def rendered(prompt: CapturedPrompt) -> str:
    """Return the owned system message text for one capture."""
    content = render_system_message(prompt.spec).content
    assert isinstance(content, str)
    return content
