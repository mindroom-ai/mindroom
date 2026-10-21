"""Build the agent of a prompt-golden reply through ``create_agent`` and record the request the loop would send first.

Until Matrix replies run on the owned loop, their entry point cannot run, so this module builds each reply's agent
with the arguments ``ai`` hands ``create_agent``, composes the turn's context the way ``ai`` does, and sends the
first request through the recording provider; the recorder sees it as the provider adapter would, portable tool
search applied. Once ``ai`` runs ``run_agent``, the recorder alone records every request and this module goes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.agent_loop.build import prompt_for_turn
from mindroom.agent_loop.tools import prepare_run_tools
from mindroom.agents import create_agent
from mindroom.hooks import render_system_enrichment_block
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest
from mindroom.memory.functions import build_memory_prompt_parts
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context
from tests.agno_parity.prompt_scenarios import SCENARIOS, ModelCall, Recorder, reply_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild


@dataclass(frozen=True)
class FirstRequest:
    """The first request of one Matrix reply: the scenario's input, the agent built for it, and the recorded request."""

    input: dict[str, Any]
    build: AgentBuild
    call: ModelCall


async def first_request(name: str, root: Path) -> FirstRequest:
    """Build the reply that scenario ``name`` starts with, as ``ai`` would, and send its first request."""
    scenario = SCENARIOS[name]
    assert scenario.turn is not None
    turn = scenario.turn(root)
    recorder = Recorder()
    with reply_runtime(turn) as runtime, recorder.installed():
        execution_identity = build_execution_identity_from_runtime_context(runtime)
        runtime_model = turn.config.resolve_runtime_model(
            entity_name=turn.agent_name,
            room_id=runtime.target.room_id,
            thread_id=runtime.target.resolved_thread_id,
            runtime_paths=turn.runtime_paths,
        )
        build = create_agent(
            turn.agent_name,
            turn.config,
            turn.runtime_paths,
            active_model_name=runtime_model.model_name,
            knowledge=turn.knowledge,
            execution_identity=execution_identity,
            supports_native_tool_approval=True,
            agent_mode=turn.agent_mode,
            agent_cli_in_shell=True,
        )
        memory = await build_memory_prompt_parts(
            turn.prompt,
            turn.agent_name,
            turn.runtime_paths.storage_root,
            turn.config,
            turn.runtime_paths,
            execution_identity=execution_identity,
        )
        system_message = prompt_for_turn(
            build,
            memory.session_preamble,
            render_system_enrichment_block(turn.system_enrichment),
        )
        request = ModelRequest(
            messages=[system_message, Message(role="user", content=turn.prompt)],
            tools=prepare_run_tools(build.spec).specs(),
            deferred_tool_names=build.request_template.deferred_tool_names,
        )
        async for _event in build.spec.model.stream(request):
            pass
    [call] = recorder.calls
    return FirstRequest(turn.input, build, call)
