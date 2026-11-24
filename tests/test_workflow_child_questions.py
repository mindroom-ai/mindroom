"""Tests for an agent's own `ask_user` tool in `workflow_peers`, including restart recovery and dedupe."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.matrix.event_types import WORKFLOW_QUESTION_CONTENT_KEY
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.answers import answer_question
from mindroom.workflow_runs.models import WorkflowNodeState
from tests.conftest import SETTLE_TIMEOUT_SECONDS
from tests.fakes.scripted_model import ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.workflow_helpers import WorkflowStack, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelRequest, StreamEvent

_ROOM = "!room:example.test"
_THREAD = "$thread:example.test"
_ALICE = "@alice:example.test"


class _RoutingModel(ScriptedModel):
    """Deterministic scripted model choosing steps per request."""

    def __init__(self, handler: Callable[[ModelRequest], list[ScriptStep]]) -> None:
        super().__init__(turns=[])
        self._handler = handler

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.turns.append(self._handler(request))
        async for event in super().stream(request):
            yield event


def _unwrap_prompt(text: str) -> str:
    if "<![CDATA[" in text and "]]></msg>" in text:
        return text.split("<![CDATA[", 1)[1].rsplit("]]></msg>", 1)[0]
    return text


def _tool_texts(request: ModelRequest) -> list[str]:
    return [m.content for m in request.messages if m.role == "tool" and isinstance(m.content, str)]


def _open_ask_nodes(stack: WorkflowStack) -> list[Any]:
    return [
        node
        for node in stack.store.nodes(stack.run_id)
        if node.kind == "ask" and node.state is WorkflowNodeState.WAITING and "event_id" in node.links
    ]


def _card_posts(stack: WorkflowStack) -> list[dict[str, Any]]:
    assert stack.room is not None
    return [
        content[WORKFLOW_QUESTION_CONTENT_KEY]
        for _event_id, _kind, content in stack.room.events
        if "m.new_content" not in content and WORKFLOW_QUESTION_CONTENT_KEY in content
    ]


async def _answer_open(stack: WorkflowStack, *, option: str | None = None, text: str | None = None) -> str:
    open_nodes = _open_ask_nodes(stack)
    assert len(open_nodes) == 1
    node = open_nodes[0]
    return await answer_question(
        stack.engine,
        run_id=stack.run_id,
        node_id=f"n{node.seq}",
        sender=_ALICE,
        room_id=_ROOM,
        thread_id=_THREAD,
        card_event_id=node.links["event_id"],
        option=option,
        text=text,
    )


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """Engine harness with room, job runtime, scripted agents, and one started run."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        stack.show()
        stack.continues = lambda job: job.kind == "delegation"
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start(max_concurrent=1)
        yield stack
        await stack.close()


@pytest.mark.asyncio
async def test_child_question_waits_while_another_lane_keeps_running(stack: WorkflowStack) -> None:
    """An agent waiting in ask_user frees its slot, marks adapter wait='user', and lets another lane complete."""

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _unwrap_prompt(str(next(m.content for m in req.messages if m.role == "user")))
        outs = _tool_texts(req)
        if prompt == "ask-lane":
            if not outs:
                return calls(tool_call("ask_user", "q1", question="Which branch?", options=["main", "dev"]))
            return answer(f"chose:{outs[0]}")
        return answer("other-lane-done")

    stack.models["watcher"] = _RoutingModel(_handle)
    await stack.submit("/0:p0@0", "agent", {"prompt": "ask-lane", "name": "asker"}, meta={"name": "asker"})
    await stack.submit("/0:p1@0", "agent", {"prompt": "other-lane", "name": "other"}, meta={"name": "other"})

    # The other lane must finish even at max_concurrent=1 while asker waits for the user!
    assert (
        await asyncio.wait_for(
            stack.result("/0:p1@0", "agent", {"prompt": "other-lane", "name": "other"}, meta={"name": "other"}),
            timeout=SETTLE_TIMEOUT_SECONDS,
        )
        == "other-lane-done"
    )
    await stack.until(lambda: bool(_open_ask_nodes(stack)))

    # Verify adapter wait="user", job status awaiting_approval, and node waiting on "user".
    asker_node = stack.store.get_node(stack.run_id, "/0:p0@0")
    assert asker_node is not None
    await stack.until(
        lambda: (
            (n := stack.store.get_node(stack.run_id, "/0:p0@0")) is not None
            and (n.state, n.waiting_on) == (WorkflowNodeState.WAITING, "user")
        ),
    )
    job = stack.node_job("/0:p0@0")
    assert job.status == "awaiting_approval"
    assert job.adapter.get("wait") == "user"

    # Answer the question card and verify the asker finishes with that answer.
    assert await _answer_open(stack, option="main") == "accepted"
    res = await asyncio.wait_for(
        stack.result("/0:p0@0", "agent", {"prompt": "ask-lane", "name": "asker"}, meta={"name": "asker"}),
        timeout=SETTLE_TIMEOUT_SECONDS,
    )
    assert res == "chose:main"
    assert len(_card_posts(stack)) == 1


@pytest.mark.asyncio
async def test_child_question_unanswered_across_a_restart_is_asked_once(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restarting while a child waits in ask_user continues the child without posting a second card."""
    stack.child_script(
        calls(tool_call("ask_user", "q1", question="Which branch?")),
        answer("unexpected"),
    )
    await stack.submit("@0", "agent", {"prompt": "x"}, meta={"name": "worker"})
    await stack.until(lambda: bool(_open_ask_nodes(stack)))
    assert len(_card_posts(stack)) == 1

    # Restart MindRoom while the question is still unanswered.
    stack = await stack.restart()
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    stack.script_agents(monkeypatch)
    stack.child_script(
        calls(tool_call("ask_user", "q2", question="Which branch?")),
        answer("main"),
    )
    await stack.recover()
    await stack.continue_agents()
    # Let the continued child re-call ask_user and attach to the existing open card.
    await asyncio.sleep(0.2)
    assert len(_card_posts(stack)) == 1

    assert await _answer_open(stack, text="main") == "accepted"
    assert (
        await asyncio.wait_for(
            stack.result("@0", "agent", {"prompt": "x"}, meta={"name": "worker"}),
            timeout=SETTLE_TIMEOUT_SECONDS,
        )
        == "main"
    )
    assert len(_card_posts(stack)) == 1


@pytest.mark.asyncio
async def test_answer_arriving_with_no_process_attached_is_returned_later(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An answer recorded while no agent process is attached is returned immediately when the child asks again."""
    stack.child_script(
        calls(tool_call("ask_user", "q1", question="Which branch?")),
        answer("unexpected"),
    )
    await stack.submit("@0", "agent", {"prompt": "x"}, meta={"name": "worker"})
    await stack.until(lambda: bool(_open_ask_nodes(stack)))
    assert len(_card_posts(stack)) == 1

    # Restart: now no child process is attached yet.
    stack = await stack.restart()
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    stack.script_agents(monkeypatch)

    # Answer arrives BEFORE recover / continue_agents!
    assert await _answer_open(stack, text="release-1") == "accepted"

    def _after(req: ModelRequest) -> list[ScriptStep]:
        q2_outs = [m.content for m in req.messages if m.role == "tool" and m.tool_call_id == "q2"]
        if not q2_outs:
            return calls(tool_call("ask_user", "q2", question="Which branch?"))
        return answer(f"got:{q2_outs[0]}")

    stack.models["watcher"] = _RoutingModel(_after)
    await stack.recover()
    await stack.continue_agents()
    assert (
        await asyncio.wait_for(
            stack.result("@0", "agent", {"prompt": "x"}, meta={"name": "worker"}),
            timeout=SETTLE_TIMEOUT_SECONDS,
        )
        == "got:release-1"
    )
    assert len(_card_posts(stack)) == 1


@pytest.mark.asyncio
async def test_cancelling_run_closes_open_child_question_card(stack: WorkflowStack) -> None:
    """Cancelling the workflow run while a child waits in ask_user closes the question card as cancelled."""
    stack.child_script(
        calls(tool_call("ask_user", "q1", question="Proceed?", options=["Yes", "No"])),
        answer("done"),
    )
    await stack.submit("@0", "agent", {"prompt": "x"}, meta={"name": "worker"})
    await stack.until(lambda: bool(_open_ask_nodes(stack)))

    await stack.engine.cancel(stack.run_id, reason="Stopped by user.")
    assert stack.room is not None
    cards = stack.room.carried(WORKFLOW_QUESTION_CONTENT_KEY)
    assert cards[-1]["status"] == "cancelled"
    assert json.loads(json.dumps(cards[-1]))["question_id"] == cards[0]["question_id"]


@pytest.mark.asyncio
async def test_rerun_agent_with_new_generation_asks_user_again(stack: WorkflowStack) -> None:
    """Opus N1: rerunning an agent (generation 2) asks the user again instead of reusing generation 1's answer."""

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        tools = [m.content for m in req.messages if m.role == "tool" and isinstance(m.content, str)]
        if not tools:
            return calls(tool_call("ask_user", "q1", question="Which branch?"))
        return answer(f"branch:{tools[0]}")

    stack.models["watcher"] = _RoutingModel(_handle)
    await stack.submit("@0", "agent", {"prompt": "x"}, meta={"name": "worker"})
    await stack.until(lambda: bool(_open_ask_nodes(stack)))
    assert await _answer_open(stack, text="main") == "accepted"
    assert await stack.result("@0", "agent", {"prompt": "x"}, meta={"name": "worker"}) == "branch:main"
    assert len(_card_posts(stack)) == 1

    # Finish attempt 1 and resume with rerun=["n1"] -> generation 2 must post a new question card.
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    await stack.finish_attempt(result="done-1")
    await stack.resume(stack.run_id, rerun=[f"n{node.seq}"])

    await stack.submit("@0", "agent", {"prompt": "x"}, meta={"name": "worker"})
    await stack.until(lambda: bool(_open_ask_nodes(stack)))
    assert len(_card_posts(stack)) == 2
    assert await _answer_open(stack, text="release") == "accepted"
    assert await stack.result("@0", "agent", {"prompt": "x"}, meta={"name": "worker"}) == "branch:release"
