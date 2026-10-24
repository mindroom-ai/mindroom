"""The hooks an agent's run and a team leader's run install on the owned loop, in the order the loop applies them."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_loop.build import AgentBuild, AgentSurface
from mindroom.agent_loop.prompt import PromptSpec
from mindroom.agent_loop.spec import AgentSpec
from mindroom.config.main import Config
from mindroom.history.types import HistoryPolicy, ResolvedHistorySettings
from mindroom.llm.provider import Capabilities, DeferredTools, ModelRequest
from mindroom.llm.tool_search import resolve_call_tool_calls
from mindroom.loop_hooks import HookTurn, agent_loop_hooks, leader_loop_hooks
from mindroom.participation import ParticipationGate
from mindroom.skill_learning.capture import SkillReviewCapture
from mindroom.tool_jobs.execution import JobActor
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.tool_system.invocation import ToolCall, ToolNext


async def _middleware(call: ToolCall, call_next: ToolNext) -> object:
    return await call_next(call.arguments)


def _stages(hooks: Sequence[object]) -> list[str]:
    """Name each hook by the factory that built it."""
    return [hook.__qualname__.split(".")[0] for hook in hooks]


def test_agent_and_leader_runs_install_their_hooks_in_order(tmp_path: Path) -> None:
    """Both runs share one policy; the leader has no gate, job definitions, tool search, or skill capture.

    The job definitions project first, so the participation decision and mid-turn compaction see the definitions the
    request sends. The approval receipt hook comes right after mid-turn compaction on every run.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate({"agents": {"general": {"display_name": "General"}}}),
        runtime_paths,
    )
    model = ScriptedModel([], capabilities=Capabilities(claude_family=True))
    settings = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)
    build = AgentBuild(
        spec=AgentSpec(id="general", name="General", model=model, system_message=None),
        prompt=PromptSpec(role="test"),
        request_template=ModelRequest(
            messages=(),
            deferred_tools=DeferredTools(names=frozenset({"deferred_tool"}), portable=True),
        ),
        history_settings=settings,
        store=None,
        tool_owners={},
        surface=AgentSurface(mode="standard", cli_shell=False),
        tool_policy=(_middleware,),
        tool_jobs=JobActor(name="general", team=False, depth=0, tools={}),
    )

    turn = HookTurn(
        config=config,
        runtime_paths=runtime_paths,
        model_name="default",
        store=None,
        session=None,
        scope=None,
        replay_plan=None,
        prompt_id=None,
    )

    agent = agent_loop_hooks(
        build,
        turn,
        agent_name="general",
        participation=ParticipationGate(),
        skill_review_capture=SkillReviewCapture(),
        run_id="run",
    )
    leader = leader_loop_hooks(
        turn,
        entity_name=None,
        scope_id="squad",
        model=model,
        history_settings=settings,
        tool_middleware=(_middleware,),
        jobs=JobActor(name="squad", team=True, depth=0, tools={}),
    )

    assert _stages(agent.before_request) == [
        "job_definitions_hook",
        "participation_hook",
        "mid_turn_compaction_hook",
        "approval_receipt_hook",
    ]
    assert _stages(leader.before_request) == ["mid_turn_compaction_hook", "approval_receipt_hook"]
    assert agent.answer_tool_calls is not None
    assert leader.answer_tool_calls is not None
    assert agent.rewrite_tool_calls is resolve_call_tool_calls
    assert leader.rewrite_tool_calls is None
    # The agent's calls run through its tool-call policy behind the admission of its tool jobs.
    assert _stages(agent.tool_middleware) == ["with_job_admission", "_middleware"]
    assert agent.tool_middleware[1:] == leader.tool_middleware == (_middleware,)
    for hooks in (agent, leader):
        assert _stages(hooks.after_tool_batch) == ["queued_notice_after_tool_batch", "job_notice_hook"]
        assert _stages(hooks.on_resume_entry) == ["queued_notice_on_resume_entry"]
    assert agent.on_loop_end is not None
    assert leader.on_loop_end is None
