"""The hooks an agent's run and a team leader's run install on the owned loop, in the order the loop applies them."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from mindroom import loop_hooks
from mindroom.agent_loop.build import AgentBuild, AgentSurface
from mindroom.agent_loop.prompt import PromptSpec
from mindroom.agent_loop.spec import AgentSpec
from mindroom.ai_runtime import queued_message_signal_context
from mindroom.config.main import Config
from mindroom.history.types import HistoryScope
from mindroom.llm.messages import Message
from mindroom.llm.provider import DeferredTools, ModelRequest
from mindroom.llm.tool_search import resolve_call_tool_calls
from mindroom.loop_hooks import HookTurn, agent_loop_hooks, leader_loop_hooks
from mindroom.mid_turn import MidTurnGate, MidTurnMode
from mindroom.participation import ParticipationGate
from mindroom.response_lifecycle import QueuedHumanNoticeReservation, _QueuedMessageState
from mindroom.skill_learning.capture import SkillReviewCapture
from mindroom.teams.members import BuiltTeam
from mindroom.tool_jobs.execution import JobActor
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel
from tests.history_helpers import replay_settings
from tests.teams.squad import team_spec

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.agent_loop.model_loop import BeforeRequestHook
    from mindroom.judgment.state import JudgmentRequest
    from mindroom.tool_system.invocation import ToolCall, ToolNext


async def _middleware(call: ToolCall, call_next: ToolNext) -> object:
    return await call_next(call.arguments)


def _stages(hooks: Sequence[object]) -> list[str]:
    """Name each hook by the factory that built it."""
    return [hook.__qualname__.split(".")[0] for hook in hooks]


def _agent_build(model: ScriptedModel) -> AgentBuild:
    return AgentBuild(
        spec=AgentSpec(id="general", name="General", model=model, system_messages=(), toolkits=()),
        prompt=PromptSpec(role="test", instructions=()),
        request_template=ModelRequest(
            messages=(),
            deferred_tools=DeferredTools(names=frozenset({"deferred_tool"}), portable=True),
        ),
        history_settings=replay_settings(),
        surface=AgentSurface(
            mode="standard",
            cli_shell=False,
            deferred=(),
            context_documents=(),
            context_files=(),
            knowledge_sources=(),
            output_file_policy=None,
        ),
        tool_policy=(_middleware,),
        tool_jobs=JobActor(name="general", team=False, depth=0, tools={}),
    )


def _built_team(model: ScriptedModel) -> BuiltTeam:
    return BuiltTeam(
        spec=team_spec("squad", "Squad", model, "You lead the squad.", ()),
        history_settings=replay_settings(),
        timezone="UTC",
        leader_middleware=(_middleware,),
        leader_jobs=JobActor(name="squad", team=True, depth=0, tools={}),
    )


def test_agent_and_leader_runs_install_their_hooks_in_order(tmp_path: Path) -> None:
    """Both runs share one policy; the leader has no gate, job definitions, tool search, or skill capture.

    The job definitions project first, so the participation decision and mid-turn compaction see the definitions the
    request sends. The approval receipt joins the request before mid-turn compaction sizes it, on every run. An
    agent's run publishes a delegated child's usage before any of them, so before a compaction the request waits for.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate({"agents": {"general": {"display_name": "General"}}}),
        runtime_paths,
    )
    model = ScriptedModel([])
    build = _agent_build(model)
    turn = HookTurn(
        config=config,
        runtime_paths=runtime_paths,
        model_name="default",
        storage=None,
        replay_plan=None,
        prompt_id=None,
    )

    agent = agent_loop_hooks(
        build,
        turn,
        participation=ParticipationGate(),
        skill_review_capture=SkillReviewCapture(),
    )
    leader = leader_loop_hooks(_built_team(model), turn)

    assert _stages(agent.before_request) == [
        "child_usage_hook",
        "job_definitions_hook",
        "participation_hook",
        "approval_receipt_hook",
        "mid_turn_compaction_hook",
    ]
    assert _stages(leader.before_request) == ["approval_receipt_hook", "mid_turn_compaction_hook"]
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


def test_each_run_compacts_as_its_history_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent's run compacts as the agent, and a leader's as its team."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate({"agents": {"general": {"display_name": "General"}}}),
        runtime_paths,
    )
    model = ScriptedModel([])
    scopes: list[HistoryScope] = []
    real_compaction_hook = loop_hooks.mid_turn_compaction_hook

    def capture_compaction_hook(**kwargs: Any) -> BeforeRequestHook:  # noqa: ANN401
        scopes.append(kwargs["scope"])
        return real_compaction_hook(**kwargs)

    monkeypatch.setattr(loop_hooks, "mid_turn_compaction_hook", capture_compaction_hook)
    turn = HookTurn(
        config=config,
        runtime_paths=runtime_paths,
        model_name="default",
        storage=None,
        replay_plan=None,
        prompt_id=None,
    )
    team = _built_team(model)

    agent_loop_hooks(_agent_build(model), turn)
    leader_loop_hooks(team, turn)

    assert scopes == [
        HistoryScope(kind="agent", scope_id="general"),
        HistoryScope(kind="team", scope_id=team.spec.leader.id),
    ]


def _batch(call_id: str, *, answered: bool) -> list[Message]:
    """Return a request that ends with one call, answered or still waiting for its result."""
    call = {"id": call_id, "type": "function", "function": {"name": "work", "arguments": "{}"}}
    messages = [Message(role="user", content="Do the task"), Message(role="assistant", tool_calls=[call])]
    if answered:
        messages.append(Message(role="tool", content="Done", tool_call_id=call_id, tool_name="work"))
    return messages


@pytest.mark.asyncio
@pytest.mark.parametrize("judge", [False, True], ids=["no_judge", "judge"])
async def test_delegated_child_keeps_owner_steer_for_parent_batch(tmp_path: Path, *, judge: bool) -> None:
    """A delegated child's batch never ends its run for the requester's Steer; the owning parent's resumed batch does.

    The child keeps the notice behavior it had, so the Steer stays queued for the parent, which ends its run once its
    batch records the child's result. A team leader owns its reply as a top-level agent does.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate({"agents": {"general": {"display_name": "General"}}}),
        runtime_paths,
    )
    model = ScriptedModel([])
    turn = HookTurn(
        config=config,
        runtime_paths=runtime_paths,
        model_name="default",
        storage=None,
        replay_plan=None,
        prompt_id=None,
    )
    build = _agent_build(model)
    child_build = replace(build, tool_jobs=JobActor(name="general", team=False, depth=1, tools={}))
    parent = agent_loop_hooks(build, turn, steering=True)
    child = agent_loop_hooks(child_build, turn)
    leader = leader_loop_hooks(_built_team(model), turn)
    queue = _QueuedMessageState()
    queue.bind_active_requester("@alice:localhost")
    judged: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judged.append(request)
        return True

    gate = MidTurnGate(active_text="Do the task", evaluate=evaluate, requester_id="@alice:localhost") if judge else None
    queue.mid_turn_gate = gate
    queue.add_waiting_human_message(
        "$steer",
        text="Also print the date",
        requester_id="@alice:localhost",
        mode=MidTurnMode.STEER,
    )
    with queued_message_signal_context(queue, mid_turn_gate=gate):
        assert await child.after_tool_batch[0](_batch("child-work", answered=True), ()) is not True
        assert await child.on_resume_entry[0](_batch("child-work", answered=True), ()) is not True
        assert [message.event_id for message in queue.pending_message_snapshot()] == ["$steer"]
        # The parent's batch waits on the child's result, so its Steer waits for the resume that records it.
        assert await parent.after_tool_batch[0](_batch("delegate", answered=False), ()) is not True
        resumed = _batch("delegate", answered=True)
        assert await parent.on_resume_entry[0](resumed, resumed[-1:]) is True
        # The run ends as its batch stands: no notice joins it and no result changes.
        assert [(message.role, message.content, message.stop_after_tool_call) for message in resumed] == [
            ("user", "Do the task", False),
            ("assistant", None, False),
            ("tool", "Done", False),
        ]
        assert await leader.after_tool_batch[0](_batch("leader-work", answered=True), ()) is True
    assert [message.event_id for message in queue.pending_message_snapshot()] == ["$steer"]
    assert QueuedHumanNoticeReservation(queue, "$steer").stopped_reply


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["after_tool_batch", "on_resume_entry"])
async def test_a_childs_pause_notice_leaves_the_owner_its_cannot_wait(tmp_path: Path, boundary: str) -> None:
    """A child told to pause for a "cannot wait" leaves the verdict to its owner, whose completed batch it ends.

    The owner reuses the verdict the child's judgment cached: no second judgment, no notice of its own, results intact.
    """
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config.model_validate({"agents": {"general": {"display_name": "General"}}}),
        runtime_paths,
    )
    model = ScriptedModel([])
    turn = HookTurn(
        config=config,
        runtime_paths=runtime_paths,
        model_name="default",
        storage=None,
        replay_plan=None,
        prompt_id=None,
    )
    build = _agent_build(model)
    parent = agent_loop_hooks(build, turn, steering=True)
    child = agent_loop_hooks(replace(build, tool_jobs=JobActor(name="general", team=False, depth=1, tools={})), turn)
    queue = _QueuedMessageState()
    queue.bind_active_requester("@alice:localhost")
    judged: list[JudgmentRequest] = []

    async def evaluate(request: JudgmentRequest) -> bool | None:
        judged.append(request)
        return False

    gate = MidTurnGate(
        active_text="Do the task",
        evaluate=evaluate,
        requester_id="@alice:localhost",
        on_stop=queue.mark_stopped_reply,
    )
    queue.mid_turn_gate = gate
    queue.add_waiting_human_message("$stop", text="Stop, wrong file", requester_id="@alice:localhost")
    with queued_message_signal_context(queue, mid_turn_gate=gate):
        child_batch = _batch("child-work", answered=True)
        assert await child.after_tool_batch[0](child_batch, ()) is not True
        assert child_batch[-1].role == "user"
        owner_batch = _batch("delegate", answered=True)
        hook = parent.after_tool_batch[0] if boundary == "after_tool_batch" else parent.on_resume_entry[0]
        assert await hook(owner_batch, owner_batch[-1:]) is True
    assert [(message.role, message.content) for message in owner_batch] == [
        ("user", "Do the task"),
        ("assistant", None),
        ("tool", "Done"),
    ]
    assert len(judged) == 1
    assert QueuedHumanNoticeReservation(queue, "$stop").stopped_reply
