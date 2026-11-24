"""Tests for workflow peer messages, shared state, slot-freeing waits, deadlock notice, and SDK/tool integration."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.agents import create_agent
from mindroom.api.script_gateway import serve_listener
from mindroom.approval import manager as approval_manager
from mindroom.approval.manager import BackgroundApprovalDecision
from mindroom.approval.tool_owners import required_approval_tool_names
from mindroom.config.approval import ToolApprovalConfig
from mindroom.custom_tools.workflow import WorkflowTools
from mindroom.custom_tools.workflow_peers import WorkflowPeerTools
from mindroom.delegation.lifecycle import (
    _CHILD_RUN,
    ChildRunObservation,
    child_run_context,
    prepare_child_turn,
    start_child_turn,
)
from mindroom.delegation.state import DelegationChild, child_execution_identity
from mindroom.event_journal import ApprovalCall, ApprovalDecision
from mindroom.llm.messages import Message
from mindroom.llm.provider import TurnCompleted
from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.tool_jobs.execution import wait_mode
from mindroom.tool_jobs.runtime import JobAccessError
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.tool_function import ToolGate
from mindroom.turn_model import ModelScope, resolve_turn_model
from mindroom.workflow_runs import agent_nodes, peers
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.graph import graph_rows
from mindroom.workflow_runs.models import TERMINAL_RUN_STATES, WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.peers import (
    NodeRef,
    PeerTooLargeError,
    PeerUnreachableError,
    peer_op_id,
)
from mindroom.workflow_runs.store import WorkflowRunNotFoundError, WorkflowRunStore, WorkflowRunStoreError
from tests.conftest import SETTLE_TIMEOUT_SECONDS, bound_worker_launch_config
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.test_workflow_local_integration import LocalWorkflowStack
from tests.tool_job_helpers import JobApprovalCards, toolkit_job_tools
from tests.workflow_helpers import NodeFailed, WorkflowStack, set_run, workflow_stack
from tests.workflow_live import blind_authoring
from tests.workflow_live.kill_after_write import Deployment

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelRequest, StreamEvent


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """Engine harness with real delegation, job runtime, scripted agents, and one started run."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()
        yield stack
        await stack.close()


@pytest_asyncio.fixture
async def local_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LocalWorkflowStack]:
    """Real child-process SDK over a live local HTTP gateway with scripted agents."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    with (
        workflow_stack(
            tmp_path,
            stack_type=LocalWorkflowStack,
            extra_tools=("workflow",),
            require_approval=(),
        ) as stack,
        bound_worker_launch_config(stack.context.config),
    ):
        assert isinstance(stack, LocalWorkflowStack)
        stack.show()
        stack.gateway_url = stack.scripts.gateway_url = f"http://127.0.0.1:{port}/api/script-gateway"
        stack.served = stack.app()
        stack.continues = coordinator_module._continues
        stack.engine.agents = stack._agents
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        async with serve_listener(
            stack.served,
            name="workflow_peers_test_gateway",
            host="127.0.0.1",
            port=port,
            log_level="WARNING",
            ws="none",
            access_log=False,
        ):
            yield stack
        await stack.close()


def _ref(stack: WorkflowStack, key: str) -> NodeRef:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return NodeRef(key=node.key, generation=node.generation, name=node.name or f"n{node.seq}")


def _unwrap_prompt(text: str) -> str:
    if "<![CDATA[" in text and "]]></msg>" in text:
        return text.split("<![CDATA[", 1)[1].rsplit("]]></msg>", 1)[0]
    return text


def _user_texts(request: ModelRequest) -> list[str]:
    return [_unwrap_prompt(str(message.content)) for message in request.messages if message.role == "user"]


def _tool_outputs(request: ModelRequest) -> list[dict[str, Any]]:
    return [json.loads(m.content) for m in request.messages if m.role == "tool" and isinstance(m.content, str)]


class _RoutingModel(ScriptedModel):
    """Deterministic scripted model that chooses each turn's steps from the request's prompt and tool history."""

    def __init__(self, handler: Callable[[ModelRequest], list[ScriptStep]]) -> None:
        super().__init__(turns=[])
        self._handler = handler

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.turns.append(self._handler(request))
        async for event in super().stream(request):
            yield event


def _route_agents(stack: WorkflowStack, handler: Callable[[ModelRequest], list[ScriptStep]]) -> _RoutingModel:
    model = _RoutingModel(handler)
    stack.models["watcher"] = model
    return model


@pytest.mark.asyncio
async def test_message_to_a_name_nobody_has_yet_is_held_and_delivered_when_it_starts(stack: WorkflowStack) -> None:
    """A message to a name with no agent yet is held and delivered once that agent starts."""
    hold = asyncio.Event()
    _route_agents(stack, lambda req: [Await(hold.wait), *answer("b done" if _user_texts(req)[0] == "be" else "f done")])
    await stack.submit("/0:p0@0", "agent", {"prompt": "be", "name": "backend"}, meta={"name": "backend"})
    backend = _ref(stack, "/0:p0@0")

    sent = await stack.engine.ask.peer_send(
        stack.run_id,
        op_id="op-1",
        sender=backend,
        to="frontend",
        text="contract v1",
    )
    assert sent == {"delivered": 0, "held": True}

    await stack.submit("/0:p1@0", "agent", {"prompt": "fe", "name": "frontend"}, meta={"name": "frontend"})
    frontend = _ref(stack, "/0:p1@0")
    got = peers.read_call(stack.store, stack.run_id, op_id="op-2", reader=frontend, names=("frontend", "*"))
    assert got is not None
    messages = got["messages"]
    assert [m["text"] for m in messages] == ["contract v1"]
    assert messages[0]["from"] == "backend"
    assert "sent_at" in messages[0]

    hold.set()
    assert (
        await stack.result("/0:p0@0", "agent", {"prompt": "be", "name": "backend"}, meta={"name": "backend"})
        == "b done"
    )
    assert (
        await stack.result("/0:p1@0", "agent", {"prompt": "fe", "name": "frontend"}, meta={"name": "frontend"})
        == "f done"
    )


@pytest.mark.asyncio
async def test_a_retried_tool_call_sends_once_and_reads_the_same_messages(stack: WorkflowStack) -> None:
    """A provider retry with the same op_id sends once and reads the exact same message batch."""
    hold = asyncio.Event()
    stack.child_script([Await(hold.wait), *answer("a done")], [Await(hold.wait), *answer("b done")])
    await stack.submit("/0:p0@0", "agent", {"prompt": "a", "name": "a"}, meta={"name": "a"})
    await stack.submit("/0:p1@0", "agent", {"prompt": "b", "name": "b"}, meta={"name": "b"})
    a, b = _ref(stack, "/0:p0@0"), _ref(stack, "/0:p1@0")

    send_op = peer_op_id(a.key, a.generation, "mrun-1", "call-1", "send_peer_message", {"to": "b", "text": "hello"})
    first_send = peers.send(stack.store, stack.run_id, op_id=send_op, sender=a, to="b", text="hello")
    second_send = peers.send(stack.store, stack.run_id, op_id=send_op, sender=a, to="b", text="hello")
    assert first_send == second_send == {"delivered": 1, "held": False}
    assert len(peers.all_messages(stack.store, stack.run_id)) == 1

    read_op1 = peer_op_id(b.key, b.generation, "mrun-2", "call-2", "read_peer_messages", {"wait_seconds": 0})
    read_op2 = peer_op_id(b.key, b.generation, "mrun-2", "call-3", "read_peer_messages", {"wait_seconds": 0})
    first_read = peers.read_call(stack.store, stack.run_id, op_id=read_op1, reader=b, names=("b", "*"))
    assert first_read is not None
    assert peers.read_call(stack.store, stack.run_id, op_id=read_op1, reader=b, names=("b", "*")) == first_read
    assert peers.read_call(stack.store, stack.run_id, op_id=read_op2, reader=b, names=("b", "*")) == {"messages": []}
    assert [m["text"] for m in first_read["messages"]] == ["hello"]

    hold.set()
    await stack.result("/0:p0@0", "agent", {"prompt": "a", "name": "a"}, meta={"name": "a"})
    await stack.result("/0:p1@0", "agent", {"prompt": "b", "name": "b"}, meta={"name": "b"})


@pytest.mark.asyncio
async def test_script_send_is_not_repeated_on_resume_and_inbox_replays(stack: WorkflowStack) -> None:
    """On resume, a cached script send sends nothing and a cached inbox returns its saved list."""
    stack.child_script(
        calls(tool_call("send_peer_message", "c1", to="script", text="ping from worker")),
        answer("worker done"),
    )
    sent = await stack.result("@0", "send", {"to": "worker", "text": "start"})
    assert sent == {"delivered": 0, "held": True}
    assert (
        await stack.result("@1", "agent", {"prompt": "work", "name": "worker"}, meta={"name": "worker"})
        == "worker done"
    )
    msgs = await stack.result("@2", "inbox", {"wait_seconds": 0})
    assert [m["text"] for m in msgs["messages"]] == ["ping from worker"]
    assert len(peers.all_messages(stack.store, stack.run_id)) == 2

    await stack.finish_attempt(result="done")
    await stack.resume(stack.run_id)
    replayed_send = await stack.submit("@0", "send", {"to": "worker", "text": "start"})
    assert replayed_send.cached is True
    assert len(peers.all_messages(stack.store, stack.run_id)) == 2

    replayed_inbox = await stack.submit("@2", "inbox", {"wait_seconds": 0})
    assert replayed_inbox.cached is True
    assert replayed_inbox.result == msgs


@pytest.mark.asyncio
async def test_state_write_is_not_reapplied_on_resume_over_a_newer_value(stack: WorkflowStack) -> None:
    """A replayed script state.set hits cache and never overwrites a newer value written later."""
    await stack.result("@0", "state", {"op": "set", "key": "phase", "value": "v1"})
    stack.child_script(
        calls(tool_call("set_shared_state", "c1", key="phase", value="v2")),
        answer("updated"),
    )
    assert (
        await stack.result("@1", "agent", {"prompt": "update", "name": "worker"}, meta={"name": "worker"}) == "updated"
    )
    assert peers.all_state(stack.store, stack.run_id) == {"phase": "v2"}

    await stack.finish_attempt(result="done")
    await stack.resume(stack.run_id)
    replayed = await stack.submit("@0", "state", {"op": "set", "key": "phase", "value": "v1"})
    assert replayed.cached is True
    assert peers.all_state(stack.store, stack.run_id) == {"phase": "v2"}


@pytest.mark.asyncio
async def test_waiting_reader_frees_the_only_slot_for_its_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At max_concurrent=1, a reader waiting in read_peer_messages(600) frees its slot so the queued sender runs."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start(max_concurrent=1)
        both_submitted = asyncio.Event()

        def _handle(req: ModelRequest) -> list[ScriptStep]:
            prompt = _user_texts(req)[0]
            outs = _tool_outputs(req)
            if prompt == "fe":
                if not outs:
                    return [Await(both_submitted.wait), *calls(tool_call("read_peer_messages", "r1", wait_seconds=600))]
                assert [m["text"] for m in outs[0]["messages"]] == ["contract v1"]
                return answer("frontend got contract")
            if not outs:
                return calls(tool_call("send_peer_message", "s1", to="frontend", text="contract v1"))
            return answer("backend sent contract")

        _route_agents(stack, _handle)
        r_fe = await stack.submit("/0:p0@0", "agent", {"prompt": "fe", "name": "frontend"}, meta={"name": "frontend"})
        r_be = await stack.submit("/0:p1@0", "agent", {"prompt": "be", "name": "backend"}, meta={"name": "backend"})
        assert (r_fe.state, r_be.state) == ("running", "queued")
        both_submitted.set()

        fe_done, be_done = await asyncio.wait_for(
            asyncio.gather(
                stack.result("/0:p0@0", "agent", {"prompt": "fe", "name": "frontend"}, meta={"name": "frontend"}),
                stack.result("/0:p1@0", "agent", {"prompt": "be", "name": "backend"}, meta={"name": "backend"}),
            ),
            timeout=SETTLE_TIMEOUT_SECONDS,
        )
        assert (fe_done, be_done) == ("frontend got contract", "backend sent contract")
        await stack.close()


@pytest.mark.asyncio
async def test_waiting_reader_returns_timeout_note_and_retries_persist_it(stack: WorkflowStack) -> None:
    """A reader whose sender never sends returns the timeout note at wait_seconds (fake clock), persisted under op_id."""
    seen_tool_results: list[dict[str, Any]] = []

    def _handle(request: ModelRequest) -> list[ScriptStep]:
        outs = _tool_outputs(request)
        if not outs:
            return calls(tool_call("read_peer_messages", "r-timeout", wait_seconds=30))
        seen_tool_results.extend(outs)
        return answer("timed-out")

    _route_agents(stack, _handle)
    receipt = await stack.submit("@0", "agent", {"prompt": "wait", "name": "reader"}, meta={"name": "reader"})
    await stack.until(
        lambda: (n := stack.store.get_node(stack.run_id, "@0")) is not None and n.waiting_on == "peer",
    )
    # Before wait_seconds elapses, no deadlock notice fires; advancing fake clock past 30s ends the wait with the note.
    stack.now += 10.0
    async with stack.engine.live.condition(stack.run_id):
        stack.engine.live.condition(stack.run_id).notify_all()
    await asyncio.sleep(0.02)
    assert not seen_tool_results

    stack.now += 25.0
    async with stack.engine.live.condition(stack.run_id):
        stack.engine.live.condition(stack.run_id).notify_all()
    await stack.until(
        lambda: (n := stack.store.get_node(stack.run_id, "@0")) is not None and n.state is WorkflowNodeState.COMPLETED,
    )
    assert receipt.id == "n1"
    assert seen_tool_results == [{"messages": [], "note": "No message arrived within 30 s."}]

    # Retrying an identical timed-out wait op_id returns the exact persisted outcome including note (Astra round 2 Medium).
    reader = _ref(stack, "@0")
    retry_op = peer_op_id(
        reader.key,
        reader.generation,
        "mrun-retry",
        "call-retry",
        "read_peer_messages",
        {"wait_seconds": 15},
    )
    wait_task = asyncio.create_task(
        stack.engine.ask.wait_for_messages(
            stack.run_id,
            reader,
            15.0,
            op_id=retry_op,
            names=("reader", "*"),
        ),
    )
    await asyncio.sleep(0.02)
    stack.now += 20.0
    async with stack.engine.live.condition(stack.run_id):
        stack.engine.live.condition(stack.run_id).notify_all()
    first = await asyncio.wait_for(wait_task, timeout=SETTLE_TIMEOUT_SECONDS)
    expected = {"messages": [], "note": "No message arrived within 15 s."}
    assert first == expected
    replayed = await stack.engine.ask.wait_for_messages(
        stack.run_id,
        reader,
        15.0,
        op_id=retry_op,
        names=("reader", "*"),
    )
    assert replayed == expected

    # Script inbox(wait_seconds=12) also returns the timeout note when no message arrives.
    inbox_receipt = await stack.submit("@1", "inbox", {"wait_seconds": 12})
    assert inbox_receipt.state is WorkflowNodeState.WAITING
    stack.now += 15.0
    async with stack.engine.live.condition(stack.run_id):
        stack.engine.live.condition(stack.run_id).notify_all()
    await stack.until(
        lambda: (n := stack.store.get_node(stack.run_id, "@1")) is not None and n.state is WorkflowNodeState.COMPLETED,
    )
    inbox_node = stack.store.get_node(stack.run_id, "@1")
    assert inbox_node is not None
    assert inbox_node.result == {"messages": [], "note": "No message arrived within 12 s."}


@pytest.mark.asyncio
async def test_rerun_agent_sees_the_messages_its_predecessor_read_and_its_old_unread_messages_are_withdrawn(
    stack: WorkflowStack,
) -> None:
    """A rerun agent starts at cursor 0 (sees predecessor messages) and its old unread sends are withdrawn."""
    # Script sends a message to "a" before "a" starts.
    await stack.result("@0", "send", {"to": "a", "text": "briefing"})
    # Gen 1 of "a" reads "briefing", then sends an unread message to "b", then finishes.
    m1 = stack.child_script(
        calls(tool_call("read_peer_messages", "a-r1", wait_seconds=0)),
        calls(tool_call("send_peer_message", "a-s1", to="b", text="stale from gen1")),
        answer("a done"),
    )
    assert await stack.result("@1", "agent", {"prompt": "a", "name": "a"}, meta={"name": "a"}) == "a done"
    gen1_reads = [o for o in _tool_outputs(m1.requests[-1]) if "messages" in o]
    assert [[m["text"] for m in o["messages"]] for o in gen1_reads] == [["briefing"]]

    # Resume and explicitly rerun "a" (n2): its old unread message to "b" must be withdrawn,
    # and gen 2 of "a" must see "briefing" again.
    await stack.finish_attempt(result="r1")
    await stack.resume(stack.run_id, rerun=["n2"])
    m2 = stack.child_script(
        calls(tool_call("read_peer_messages", "a-r2", wait_seconds=0)),
        answer("a done"),
    )
    await stack.submit("@0", "send", {"to": "a", "text": "briefing"})
    assert await stack.result("@1", "agent", {"prompt": "a", "name": "a"}, meta={"name": "a"}) == "a done"
    gen2_reads = [o for o in _tool_outputs(m2.requests[-1]) if "messages" in o]
    assert [[m["text"] for m in o["messages"]] for o in gen2_reads] == [["briefing"]]

    # Now "b" starts and reads: it must NOT see "stale from gen1"!
    b_model = stack.child_script(
        calls(tool_call("read_peer_messages", "b-r2", wait_seconds=0)),
        answer("b done"),
    )
    await stack.result("@2", "agent", {"prompt": "b", "name": "b"}, meta={"name": "b"})
    b_tool_outputs = [
        json.loads(m.content)
        for req in b_model.requests
        for m in req.messages
        if m.role == "tool" and isinstance(m.content, str)
    ]
    assert b_tool_outputs == [{"messages": []}]


@pytest.mark.asyncio
async def test_two_agents_sharing_a_name_each_get_a_copy_and_star_reaches_all_unfinished(stack: WorkflowStack) -> None:
    """Two agents sharing a name each receive a copy; '*' reaches every unfinished agent other than sender."""
    hold = asyncio.Event()
    stack.child_script(
        [Await(hold.wait), *answer("r1")],
        [Await(hold.wait), *answer("r2")],
        [Await(hold.wait), *answer("lead")],
    )
    await stack.submit("/0:p0@0", "agent", {"prompt": "r1", "name": "reviewer"}, meta={"name": "reviewer"})
    await stack.submit("/0:p1@0", "agent", {"prompt": "r2", "name": "reviewer"}, meta={"name": "reviewer"})
    await stack.submit("/0:p2@0", "agent", {"prompt": "lead", "name": "lead"}, meta={"name": "lead"})
    r1, r2, lead = _ref(stack, "/0:p0@0"), _ref(stack, "/0:p1@0"), _ref(stack, "/0:p2@0")

    assert peers.send(stack.store, stack.run_id, op_id="s1", sender=lead, to="reviewer", text="check diff") == {
        "delivered": 2,
        "held": False,
    }
    assert peers.send(stack.store, stack.run_id, op_id="s2", sender=lead, to="*", text="all hands") == {
        "delivered": 2,
        "held": False,
    }
    rd1 = peers.read_call(stack.store, stack.run_id, op_id="rd1", reader=r1, names=("reviewer", "*"))
    rd2 = peers.read_call(stack.store, stack.run_id, op_id="rd2", reader=r2, names=("reviewer", "*"))
    assert rd1 is not None
    assert rd2 is not None
    assert [m["text"] for m in rd1["messages"]] == ["check diff", "all hands"]
    assert [m["text"] for m in rd2["messages"]] == ["check diff", "all hands"]
    # Sender does not receive its own broadcast.
    assert peers.read_call(stack.store, stack.run_id, op_id="rd3", reader=lead, names=("lead", "*")) == {
        "messages": [],
    }

    hold.set()
    for key, name in (("/0:p0@0", "reviewer"), ("/0:p1@0", "reviewer"), ("/0:p2@0", "lead")):
        await stack.result(key, "agent", {"prompt": name, "name": name}, meta={"name": name})


@pytest.mark.asyncio
async def test_message_to_a_finished_agent_is_refused_with_the_hint(stack: WorkflowStack) -> None:
    """Sending to a name whose agents have all finished fails with the spec spawn() hint."""
    stack.child_script(answer("critic done"))
    assert (
        await stack.result("@0", "agent", {"prompt": "critique", "name": "critic"}, meta={"name": "critic"})
        == "critic done"
    )

    expected = "'critic' has finished and will not read this. Use spawn() to keep an agent you want to talk to."
    with pytest.raises(PeerUnreachableError, match=r"'critic' has finished and will not read this\. Use spawn\(\)"):
        peers.send(
            stack.store,
            stack.run_id,
            op_id="s-dead",
            sender=NodeRef(key="@1", generation=1, name="script"),
            to="critic",
            text="hello?",
        )

    # Through script send() node too: node fails with the exact hint.
    with pytest.raises(NodeFailed) as exc_info:
        await stack.result("@1", "send", {"to": "critic", "text": "hello?"})
    assert exc_info.value.receipt.error is not None
    assert exc_info.value.receipt.error["message"] == expected


@pytest.mark.asyncio
async def test_session_gets_queued_messages_in_front_of_its_next_turn(stack: WorkflowStack) -> None:
    """Messages sent to a session between turns are prepended to its next ask prompt."""
    model = stack.child_script(answer("turn 1 done"), answer("turn 2 done"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    assert (
        await stack.result("@1", "turn", {"session": "@0", "turn": 1, "message": "Step one"}, meta={"name": "dev"})
        == "turn 1 done"
    )
    # Between turn 1 and turn 2, send two messages to the session (must NOT be rejected as finished!).
    res = await stack.result("@2", "send", {"to": "dev", "text": "Watch out for edge case"})
    assert res == {"delivered": 1, "held": False}
    await stack.result("@3", "send", {"to": "dev", "text": "Also add tests"})

    assert (
        await stack.result("@4", "turn", {"session": "@0", "turn": 2, "message": "Step two"}, meta={"name": "dev"})
        == "turn 2 done"
    )
    second_turn_prompt = _user_texts(model.requests[-1])[-1]
    assert second_turn_prompt.startswith(
        "Messages received since your last turn:\nscript: Watch out for edge case\nscript: Also add tests\n\nStep two",
    )


@pytest.mark.asyncio
async def test_peer_tools_reach_only_the_childs_own_run(stack: WorkflowStack) -> None:
    """A child of another workflow run resolves only its own run and cannot read this run state or messages."""
    await stack.result("@0", "state", {"op": "set", "key": "secret", "value": "run1-only"})
    run1_id = stack.run_id

    run2 = await stack.start()
    assert run2.run_id != run1_id
    model = stack.child_script(
        calls(tool_call("get_shared_state", "g1")),
        answer("checked"),
    )
    assert await stack.result("@0", "agent", {"prompt": "read state", "name": "spy"}, meta={"name": "spy"}) == "checked"
    assert _tool_outputs(model.requests[-1]) == [{}]


@pytest.mark.asyncio
async def test_workflow_children_get_the_peer_tools_and_other_children_do_not(stack: WorkflowStack) -> None:
    """Workflow children receive workflow_peers automatically; plain subagents and top-level agents do not."""
    config, paths = stack.context.config, stack.context.runtime_paths
    plugins = config_plugins(config, paths)
    owner = stack.context.execution_identity

    turn_model = resolve_turn_model(config, paths, ModelScope(entity="watcher"), None)
    provider = ScriptedModel([])

    # Top-level agent: no workflow_peers.
    top_build = create_agent(
        "watcher",
        config,
        paths,
        execution_identity=owner,
        plugins=plugins,
        knowledge=None,
        model=turn_model,
        provider=provider,
    )
    top_names = {fn.name for tk in top_build.spec.toolkits for fn in tk.functions}
    assert "send_peer_message" not in top_names

    # Plain subagent (workflow_run_id is None): no workflow_peers.
    plain_child = prepare_child_turn(
        "watcher",
        "watcher",
        "plain task",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )
    token = _CHILD_RUN.set(ChildRunObservation(child=plain_child, audit=None))
    try:
        plain_build = create_agent(
            "watcher",
            config,
            paths,
            execution_identity=owner,
            plugins=plugins,
            knowledge=None,
            model=turn_model,
            provider=provider,
            delegation_depth=1,
        )
    finally:
        _CHILD_RUN.reset(token)
    plain_names = {fn.name for tk in plain_build.spec.toolkits for fn in tk.functions}
    assert "send_peer_message" not in plain_names

    # Workflow child (workflow_run_id is set): gets workflow_peers!
    wf_child = prepare_child_turn(
        "watcher",
        "watcher",
        "wf task",
        owner=owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )
    wf_child.workflow_run_id, wf_child.workflow_node_key, wf_child.workflow_generation = stack.run_id, "@0", 1
    token = _CHILD_RUN.set(ChildRunObservation(child=wf_child, audit=None))
    try:
        wf_build = create_agent(
            "watcher",
            config,
            paths,
            execution_identity=owner,
            plugins=plugins,
            knowledge=None,
            model=turn_model,
            provider=provider,
            delegation_depth=1,
        )
    finally:
        _CHILD_RUN.reset(token)
    wf_names = {fn.name for tk in wf_build.spec.toolkits for fn in tk.functions}
    assert {
        "send_peer_message",
        "read_peer_messages",
        "list_peers",
        "get_shared_state",
        "set_shared_state",
    } <= wf_names


@pytest.mark.asyncio
async def test_message_edges_carry_counts_and_the_full_history_is_owner_checked(stack: WorkflowStack) -> None:
    """Frames and cards carry only edge counts (no message text); get_workflow(messages=True) is owner-checked."""
    views = stack.show()
    payload_text = "TOP-SECRET-CONTRACT-PAYLOAD"
    secret_state = {"token": "SUPER-SECRET-STATE-VALUE"}
    hold = asyncio.Event()

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        if _user_texts(req)[0] == "fe":
            return [Await(hold.wait), *answer("fe done")]
        if not _tool_outputs(req):
            return calls(
                tool_call("send_peer_message", "s1", to="frontend", text=payload_text),
                tool_call("send_peer_message", "s2", to="frontend", text="second message"),
                tool_call("set_shared_state", "st1", key="contract", value=secret_state),
            )
        return answer("be done")

    _route_agents(stack, _handle)
    await stack.submit("/0:p0@0", "agent", {"prompt": "be", "name": "backend"}, meta={"name": "backend"})
    await stack.submit("/0:p1@0", "agent", {"prompt": "fe", "name": "frontend"}, meta={"name": "frontend"})
    assert (
        await stack.result("/0:p0@0", "agent", {"prompt": "be", "name": "backend"}, meta={"name": "backend"})
        == "be done"
    )
    hold.set()
    assert (
        await stack.result("/0:p1@0", "agent", {"prompt": "fe", "name": "frontend"}, meta={"name": "frontend"})
        == "fe done"
    )
    await stack.finish_attempt(result="all done")
    await views.show(stack.run_id)

    assert stack.room is not None
    # Graph frames and run card must have edges with counts only and ZERO message/state payload text.
    raw_room = json.dumps(stack.room.events) + json.dumps(stack.room.sent)
    assert payload_text not in raw_room
    assert "SUPER-SECRET-STATE-VALUE" not in raw_room
    built = views._build(stack.run_id)
    assert built is not None
    _run, _nodes, _usage, graph = built
    assert graph.edges == [{"from": "n1", "to": "n2", "kind": "message", "count": 2}]

    # get_workflow(messages=True) returns full uncapped text and state to owner.
    tools = WorkflowTools()
    with tool_runtime_context(stack.context):
        without_msgs = json.loads(await tools.get_workflow(stack.run_id))
        assert "messages" not in without_msgs
        assert "shared_state" not in without_msgs
        with_msgs = json.loads(await tools.get_workflow(stack.run_id, messages=True))
    assert {m["text"] for m in with_msgs["messages"]} == {payload_text, "second message"}
    assert len(with_msgs["messages"]) == 2
    assert with_msgs["shared_state"] == {"contract": secret_state}

    # Another requester gets WorkflowRunNotFoundError (reads as unknown).
    other = stack.context.__class__(**{**stack.context.__dict__, "requester_id": "@mallory:example.test"})
    with tool_runtime_context(other), pytest.raises(WorkflowRunNotFoundError):
        await tools.get_workflow(stack.run_id, messages=True)


@pytest.mark.asyncio
async def test_reused_call_id_in_a_continued_turn_or_with_other_arguments_is_a_new_operation(
    stack: WorkflowStack,
) -> None:
    """Reusing tool_call_id under a different model run ID or different arguments creates a distinct op_id."""
    op_a = peer_op_id("@0", 1, "model-run-1", "call-1", "send_peer_message", {"to": "b", "text": "one"})
    op_continued = peer_op_id("@0", 1, "model-run-2", "call-1", "send_peer_message", {"to": "b", "text": "one"})
    op_other_args = peer_op_id("@0", 1, "model-run-1", "call-1", "send_peer_message", {"to": "b", "text": "two"})
    assert len({op_a, op_continued, op_other_args}) == 3

    sender = NodeRef(key="@0", generation=1, name="a")
    peers.send(stack.store, stack.run_id, op_id=op_a, sender=sender, to="b", text="one")
    peers.send(stack.store, stack.run_id, op_id=op_continued, sender=sender, to="b", text="one")
    peers.send(stack.store, stack.run_id, op_id=op_other_args, sender=sender, to="b", text="two")
    assert [m["text"] for m in peers.all_messages(stack.store, stack.run_id)] == ["one", "one", "two"]


@pytest.mark.skipif(sys.platform != "linux", reason="Local workflow runs are Linux only.")
@pytest.mark.asyncio
async def test_sender_lane_not_yet_started_is_not_a_deadlock(local_stack: LocalWorkflowStack) -> None:
    """Real SDK foreach(..., max_concurrent=1): reader waits before sender lane submits; no false deadlock."""
    script = """\
from mindroom.workflow import agent, foreach


def run_role(role):
    if role == "reader":
        return agent("wait for contract", name="reader")
    return agent("send contract", name="sender")


def main(args):
    return foreach(["reader", "sender"], run_role, max_concurrent=1)
"""
    got_messages: list[dict[str, Any]] = []

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[0]
        outs = _tool_outputs(req)
        if "wait for contract" in prompt:
            if not outs:
                return calls(tool_call("read_peer_messages", "r1", wait_seconds=600))
            got_messages.extend(outs)
            return answer("reader done")
        if not outs:
            return calls(tool_call("send_peer_message", "s1", to="reader", text="from-sender"))
        return answer("sender done")

    _route_agents(local_stack, _handle)
    run = await local_stack.start(source=script, max_concurrent=1)
    await asyncio.wait_for(
        local_stack.until(lambda: local_stack.store.get_run(run.run_id).state is WorkflowRunState.COMPLETED),
        timeout=SETTLE_TIMEOUT_SECONDS,
    )
    ended = local_stack.store.get_run(run.run_id)
    assert ended.result == ["reader done", "sender done"]
    assert len(got_messages) == 1
    assert "note" not in got_messages[0]
    assert [m["text"] for m in got_messages[0]["messages"]] == ["from-sender"]


@pytest.mark.asyncio
async def test_oversized_text_is_refused_with_the_file_hint(stack: WorkflowStack) -> None:
    """A message or state value over 1 MiB is refused with the workspace-file hint."""
    big = "x" * (1024 * 1024 + 1)
    sender = NodeRef(key="@0", generation=1, name="a")
    with pytest.raises(
        PeerTooLargeError,
        match=r"Too large \(1,048,577 bytes, limit 1 MiB\)\. Write it to a workspace file and send the path\.",
    ):
        peers.send(stack.store, stack.run_id, op_id="big-msg", sender=sender, to="b", text=big)

    with pytest.raises(
        PeerTooLargeError,
        match=r"Too large \(.*bytes, limit 1 MiB\)\. Write it to a workspace file and send the path\.",
    ):
        peers.set_state(stack.store, stack.run_id, op_id="big-state", key="k", value=big)


@pytest.mark.asyncio
async def test_prune_removes_peer_tables(stack: WorkflowStack) -> None:
    """Pruning an old finished run deletes its rows in all four peer tables."""
    sender = NodeRef(key="@0", generation=1, name="a")
    reader = NodeRef(key="@1", generation=1, name="b")
    peers.send(stack.store, stack.run_id, op_id="p1", sender=sender, to="b", text="hi")
    peers.read_call(stack.store, stack.run_id, op_id="p2", reader=reader, names=("b", "*"))
    peers.set_state(stack.store, stack.run_id, op_id="p3", key="k", value=1)

    old_time = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    set_run(stack.store, stack.run_id, state=WorkflowRunState.COMPLETED, finished_at=old_time)
    assert stack.store.prune(cutoff, held=set()) == 1

    with closing(sqlite3.connect(stack.store.database_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM workflow_messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflow_cursors").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflow_state").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM workflow_peer_calls").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_woken_peer_waiting_for_slot_or_unread_message_is_not_deadlock(stack: WorkflowStack) -> None:
    """Ping-pong between two peers at max_concurrent=1 never falsely triggers deadlock when the reader starts first."""
    set_run(stack.store, stack.run_id, max_concurrent=1)
    both_submitted = asyncio.Event()
    got_backend: list[dict[str, Any]] = []
    got_frontend: list[dict[str, Any]] = []

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[0]
        outs = _tool_outputs(req)
        if prompt == "frontend":
            if len(outs) == 0:
                return [Await(both_submitted.wait), *calls(tool_call("read_peer_messages", "f1", wait_seconds=600))]
            got_frontend.append(outs[0])
            if len(outs) == 1:
                return calls(tool_call("send_peer_message", "f2", to="backend", text="ack"))
            return answer("fe-done")
        if len(outs) == 0:
            return calls(tool_call("send_peer_message", "b1", to="frontend", text="proposal"))
        if len(outs) == 1:
            return calls(tool_call("read_peer_messages", "b2", wait_seconds=600))
        got_backend.append(outs[1])
        return answer("be-done")

    _route_agents(stack, _handle)
    # Submit frontend FIRST (holds the sole slot), then backend (queued); once frontend waits, backend sends and
    # immediately waits before frontend finishes reacquiring the slot.
    r_fe = await stack.submit("/0:p0@0", "agent", {"prompt": "frontend", "name": "frontend"}, meta={"name": "frontend"})
    r_be = await stack.submit("/0:p1@0", "agent", {"prompt": "backend", "name": "backend"}, meta={"name": "backend"})
    assert (r_fe.state, r_be.state) == ("running", "queued")
    both_submitted.set()

    fe_done, be_done = await asyncio.wait_for(
        asyncio.gather(
            stack.result("/0:p0@0", "agent", {"prompt": "frontend", "name": "frontend"}, meta={"name": "frontend"}),
            stack.result("/0:p1@0", "agent", {"prompt": "backend", "name": "backend"}, meta={"name": "backend"}),
        ),
        timeout=SETTLE_TIMEOUT_SECONDS,
    )
    assert (fe_done, be_done) == ("fe-done", "be-done")
    assert [m["text"] for m in got_frontend[0]["messages"]] == ["proposal"]
    assert "note" not in got_frontend[0]
    assert [m["text"] for m in got_backend[0]["messages"]] == ["ack"]
    assert "note" not in got_backend[0]


@pytest.mark.skipif(sys.platform != "linux", reason="Local workflow runs are Linux only.")
@pytest.mark.asyncio
@pytest.mark.parametrize("pause", [0.0, 0.3])
async def test_real_sdk_script_sends_after_agent_finishes_no_false_deadlock(
    local_stack: LocalWorkflowStack,
    pause: float,
) -> None:
    """Opus B1: a script lane finishing agent() and then calling send() does not falsely deadlock a waiting peer."""
    seen: list[dict[str, Any]] = []

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[0]
        outs = _tool_outputs(req)
        if prompt == "quick a":
            return answer("a-done")
        if not outs:
            return calls(tool_call("read_peer_messages", "r1", wait_seconds=600))
        seen.append(outs[0])
        return answer("b-done")

    _route_agents(local_stack, _handle)
    script = (
        "import time\n"
        "from mindroom.workflow import agent, parallel, send\n"
        "def lane_a():\n"
        "    r = agent('quick a', name='a')\n"
        f"    time.sleep({pause})\n"
        "    send('b', 'hi from script')\n"
        "    return r\n"
        "def main(args):\n"
        "    return parallel([lane_a, lambda: agent('wait b', name='b')])\n"
    )
    run = await local_stack.start(source=script)
    await asyncio.wait_for(
        local_stack.until(lambda: local_stack.store.get_run(run.run_id).state in TERMINAL_RUN_STATES),
        timeout=SETTLE_TIMEOUT_SECONDS,
    )
    ended = local_stack.store.get_run(run.run_id)
    assert ended.state is WorkflowRunState.COMPLETED, ended.error
    assert len(seen) == 1
    assert "note" not in seen[0], seen
    assert [m["text"] for m in seen[0]["messages"]] == ["hi from script"]


@pytest.mark.skipif(sys.platform != "linux", reason="Local workflow runs are Linux only.")
@pytest.mark.asyncio
async def test_nested_foreach_reader_releases_outer_slot_for_sender(local_stack: LocalWorkflowStack) -> None:
    """Astra blocker 3: a nested foreach whose reader parks on peer releases the outer max_concurrent=1 slot."""
    got: list[dict[str, Any]] = []

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        outs = _tool_outputs(req)
        if not outs:
            return calls(tool_call("read_peer_messages", "r1", wait_seconds=600))
        got.append(outs[0])
        return answer("read-ok")

    _route_agents(local_stack, _handle)
    script = (
        "from mindroom.workflow import agent, foreach, send\n"
        "def role(kind):\n"
        "    if kind == 'read':\n"
        "        return foreach([1], lambda _: agent('wait', name='reader'))[0]\n"
        "    send('reader', 'go')\n"
        "    return 'sent'\n"
        "def main(args):\n"
        "    return foreach(['read', 'send'], role, max_concurrent=1)\n"
    )
    run = await local_stack.start(source=script, max_concurrent=1)
    await asyncio.wait_for(
        local_stack.until(lambda: local_stack.store.get_run(run.run_id).state in TERMINAL_RUN_STATES),
        timeout=SETTLE_TIMEOUT_SECONDS,
    )
    ended = local_stack.store.get_run(run.run_id)
    assert ended.state is WorkflowRunState.COMPLETED, ended.error
    assert ended.result == ["read-ok", "sent"]
    assert len(got) == 1
    assert "note" not in got[0]
    assert [m["text"] for m in got[0]["messages"]] == ["go"]


@pytest.mark.asyncio
async def test_opening_pre_peer_v1_store_adds_peer_tables_idempotently(stack: WorkflowStack) -> None:
    """Astra blocker 1: opening a v1 store without the 4 peer tables adds them so viewing a plain run's graph and sending work."""
    _route_agents(stack, lambda _req: answer("plain-ok"))
    assert await stack.result("@0", "agent", {"prompt": "hi", "name": "solo"}, meta={"name": "solo"}) == "plain-ok"
    await stack.finish_attempt(result="done")
    db_path = stack.store.database_path

    # Simulate a DB created by an earlier v1 build (no peer tables, schema version still 1).
    with closing(sqlite3.connect(db_path)) as conn:
        for table in ("workflow_messages", "workflow_cursors", "workflow_state", "workflow_peer_calls"):
            conn.execute(f"DROP TABLE {table}")
        conn.commit()

    reopened = WorkflowRunStore(db_path)
    nodes = reopened.nodes(stack.run_id, light=True)
    graph = graph_rows(
        nodes,
        reopened.usage_summary(stack.run_id).by_node,
        peers.message_edges(reopened, stack.run_id, nodes),
        reopened.log(stack.run_id, 50),
    )
    assert graph.edges == []
    assert any(row["label"] for row in graph.nodes)
    sent = peers.send(
        reopened,
        stack.run_id,
        op_id="post-upgrade",
        sender=NodeRef("@s", 1, "script"),
        to="future",
        text="hi",
    )
    assert sent == {
        "delivered": 0,
        "held": True,
    }
    with closing(sqlite3.connect(db_path)) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"workflow_messages", "workflow_cursors", "workflow_state", "workflow_peer_calls"} <= tables


@pytest.mark.asyncio
async def test_session_turn_rerun_preserves_consumed_between_turn_mail(stack: WorkflowStack) -> None:
    """Astra blocker 4: rerunning turn 2 keeps the between-turn mail batch and replays it in later transcripts."""
    prompts_seen: list[str] = []

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        prompts_seen.append(_user_texts(req)[-1])
        return answer(f"reply-{len(prompts_seen)}")

    _route_agents(stack, _handle)
    await stack.result("@0", "session", {"name": "coder"}, meta={"name": "coder"})
    assert await stack.result("@1", "turn", {"session": "@0", "turn": 1, "message": "one"}) == "reply-1"
    await stack.result("@2", "send", {"to": "coder", "text": "use format v2"})
    assert await stack.result("@3", "turn", {"session": "@0", "turn": 2, "message": "two"}) == "reply-2"
    assert "use format v2" in prompts_seen[-1]

    # Finish attempt 1, then resume with only turn 2 in rerun.
    turn2_node = stack.store.get_node(stack.run_id, "@3")
    assert turn2_node is not None
    await stack.finish_attempt(result="done-1")
    await stack.resume(stack.run_id, rerun=[f"n{turn2_node.seq}"])

    await stack.result("@0", "session", {"name": "coder"}, meta={"name": "coder"})
    assert await stack.result("@1", "turn", {"session": "@0", "turn": 1, "message": "one"}) == "reply-1"
    await stack.result("@2", "send", {"to": "coder", "text": "use format v2"})
    assert await stack.result("@3", "turn", {"session": "@0", "turn": 2, "message": "two"}) == "reply-3"
    assert "use format v2" in prompts_seen[-1]
    assert "Earlier conversation:" in prompts_seen[-1]


@pytest.mark.asyncio
async def test_star_broadcast_freezes_recipients_at_send_time(stack: WorkflowStack) -> None:
    """Astra Medium: '*' delivers and draws edges only to peers live at send time, never finished or later peers."""
    gate_live = asyncio.Event()
    gate_later = asyncio.Event()

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        p = _user_texts(req)[0]
        if p == "done":
            return answer("done-ok")
        if p == "live":
            return [Await(gate_live.wait), *answer("live-ok")]
        return [Await(gate_later.wait), *answer("later-ok")]

    _route_agents(stack, _handle)
    assert (
        await stack.result("/0:p0@0", "agent", {"prompt": "done", "name": "done"}, meta={"name": "done"}) == "done-ok"
    )
    await stack.submit("/0:p1@0", "agent", {"prompt": "live", "name": "live"}, meta={"name": "live"})
    await stack.until(lambda: stack.store.get_node(stack.run_id, "/0:p1@0") is not None)

    sender = NodeRef(key="@send", generation=1, name="script")
    # Put a real send node so message_edges has a sender id.
    receipt = await stack.result("@5", "send", {"to": "*", "text": "freeze-check"})
    assert receipt == {"delivered": 1, "held": False}
    # Also test held named delivery still works alongside frozen '*'.
    assert peers.send(stack.store, stack.run_id, op_id="named-hold", sender=sender, to="later", text="for-later") == {
        "delivered": 0,
        "held": True,
    }

    # Start 'later' AFTER the '*' broadcast.
    await stack.submit("/0:p2@0", "agent", {"prompt": "later", "name": "later"}, meta={"name": "later"})
    await stack.until(lambda: stack.store.get_node(stack.run_id, "/0:p2@0") is not None)

    live_msgs = peers.read_call(
        stack.store,
        stack.run_id,
        op_id="r-live",
        reader=_ref(stack, "/0:p1@0"),
        names=("live", "*"),
    )
    later_msgs = peers.read_call(
        stack.store,
        stack.run_id,
        op_id="r-later",
        reader=_ref(stack, "/0:p2@0"),
        names=("later", "*"),
    )
    assert live_msgs is not None
    assert later_msgs is not None
    assert [m["text"] for m in live_msgs["messages"]] == ["freeze-check"]
    assert [m["text"] for m in later_msgs["messages"]] == ["for-later"]

    send_node = stack.store.get_node(stack.run_id, "@5")
    live_node = stack.store.get_node(stack.run_id, "/0:p1@0")
    assert send_node is not None
    assert live_node is not None
    edges = peers.message_edges(stack.store, stack.run_id, stack.store.nodes(stack.run_id))
    assert [(f, t, c) for f, t, c in edges if f == f"n{send_node.seq}"] == [
        (f"n{send_node.seq}", f"n{live_node.seq}", 1),
    ]

    gate_live.set()
    gate_later.set()
    await stack.result("/0:p1@0", "agent", {"prompt": "live", "name": "live"}, meta={"name": "live"})
    await stack.result("/0:p2@0", "agent", {"prompt": "later", "name": "later"}, meta={"name": "later"})


@pytest.mark.asyncio
async def test_blind_authoring_harness_smoke_without_live_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Astra blocker: blind_authoring loads real config, uses adapted start_workflow description, markers, ended_run, and checks peer exchange."""
    slot_dir = tmp_path / "slot-9"
    storage_dir = slot_dir / "storage"
    (storage_dir / "agents" / "helper" / "workspace").mkdir(parents=True)
    (slot_dir / "config.yaml").write_text(
        """models:
  default:
    provider: anthropic
    id: claude-test
agents:
  helper:
    display_name: Helper
    role: Helper agent
    model: default
    rooms: ["!lobby:test"]
    tools: [workflow]
  deckhand:
    display_name: Deckhand
    role: Peer agent
    model: default
    rooms: ["!lobby:test"]
""",
        encoding="utf-8",
    )
    deployment = Deployment(
        slot=9,
        live={
            "homeserver": "http://127.0.0.1:8008",
            "lobby": {"roomId": "!lobby:test"},
            "userId": "@u:test",
            "accessToken": "tok",
        },
        directory=slot_dir,
    )

    config, runtime_paths, description = blind_authoring._build_start_workflow_description(deployment)
    assert "send(to, text)" in description
    assert "read_peer_messages" in description
    assert "Models: default." in description
    assert "Agents you can run with agent=: deckhand." in description

    class _FakeProvider:
        async def stream(self, request: ModelRequest) -> AsyncIterator[TurnCompleted]:
            assert "start_workflow" in str(request.messages[-1].content)
            yield TurnCompleted(
                message=Message(
                    role="assistant",
                    content="```python\nfrom mindroom.workflow import agent\ndef main(args):\n    return 'ok'\n```",
                ),
            )

    monkeypatch.setattr(blind_authoring, "build_model_provider", lambda *_a, **_kw: _FakeProvider())
    negotiate = next(t for t in blind_authoring.TASKS if t.name == "negotiate")
    script = await blind_authoring._author_script(config, runtime_paths, description, "default", negotiate)
    assert script.startswith("from mindroom.workflow import agent")

    # Exercise _start_with_args with a real SQLite store containing the marked source + peer messages + contract state.
    db_path = storage_dir / "workflow_runs" / "workflow_runs.sqlite3"
    store = WorkflowRunStore(db_path)
    marker = "blind_negotiate_abcd"

    def _fake_ask(text: str, _dev: Path | None) -> str:
        written = (deployment.workspace / f"blind_{marker}.py").read_text(encoding="utf-8")
        assert f"# blind_token={marker}\n" in written
        assert f'path="blind_{marker}.py"' in text
        with store.write_transaction() as conn:
            conn.execute(
                "INSERT INTO workflow_runs VALUES "
                "(?, ?, ?, NULL, NULL, 0, 1, 'owner', '{}', 'helper', '@u:test', '!lobby:test', NULL, '{}', '{}', NULL, "
                "?, 'd', NULL, '{}', 4, NULL, 0, NULL, NULL, 'completed', 1, 0, NULL, NULL, NULL, NULL, ?, NULL, "
                "'2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z')",
                ("wf-1", "job-1", "res-1", written, json.dumps("ok")),
            )
            for seq, (key, name) in enumerate([("@0", "backend"), ("@1", "frontend"), ("@2", "integrator")], start=1):
                conn.execute(
                    "INSERT INTO workflow_nodes VALUES "
                    "(?, ?, ?, 'agent', '', ?, 'd', '{}', 1, 1, 'running', NULL, '{}', 0, ?, ?, NULL, '[]', NULL, 0, "
                    "'2026-01-01T00:00:00Z', '2026-01-01T00:00:01Z', ?, NULL, '{}', '{}')",
                    ("wf-1", key, seq, seq, name, name, json.dumps("ok")),
                )
        peers.send(store, "wf-1", op_id="m1", sender=NodeRef("@0", 1, "backend"), to="frontend", text="propose v1")
        peers.send(store, "wf-1", op_id="m2", sender=NodeRef("@1", 1, "frontend"), to="backend", text="accept v1")
        peers.set_state(store, "wf-1", op_id="s1", key="contract", value={"endpoint": "/avatar"})
        with store.write_transaction() as conn:
            conn.execute("UPDATE workflow_nodes SET state = 'completed' WHERE run_id = 'wf-1'")
        return "$ev1"

    monkeypatch.setattr(deployment, "ask", _fake_ask)
    run, nodes = blind_authoring._start_with_args(deployment, script, negotiate.args, marker, None)
    messages, state = blind_authoring._peer_activity(deployment, str(run["run_id"]))
    assert blind_authoring._verify_task(negotiate, run, nodes, messages, state) == (True, "ok")

    # A no-op completed run or one-way exchange must fail verification for negotiate.
    passed_noop, _ = blind_authoring._verify_task(negotiate, run, nodes, [], {})
    assert not passed_noop
    passed_one_way, _ = blind_authoring._verify_task(negotiate, run, nodes, messages[:1], state)
    assert not passed_one_way


@pytest.mark.asyncio
async def test_resume_edited_send_resolves_reachability_against_current_attempt(stack: WorkflowStack) -> None:
    """Fresh Astra Major 2: on resume, send resolves reachability and '*' against current attempt's nodes only."""
    seen: list[list[str]] = []

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        outs = _tool_outputs(req)
        if not outs:
            return calls(tool_call("read_peer_messages", "r1", wait_seconds=0))
        seen.append([m["text"] for m in outs[0]["messages"]])
        return answer("reviewed")

    _route_agents(stack, _handle)
    assert await stack.result("@0", "send", {"to": "reviewer", "text": "draft 1"}) == {"delivered": 0, "held": True}
    assert (
        await stack.result("@1", "agent", {"prompt": "review", "name": "reviewer"}, meta={"name": "reviewer"})
        == "reviewed"
    )
    assert seen == [["draft 1"]]

    # Resume as attempt 2 with an edited send BEFORE the reviewer node is reached in attempt 2.
    await stack.finish_attempt(result="done-1")
    await stack.resume(stack.run_id)
    assert await stack.result("@0", "send", {"to": "reviewer", "text": "draft 2"}) == {"delivered": 0, "held": True}
    assert await stack.result("@2", "send", {"to": "*", "text": "bcast-before-start"}) == {"delivered": 0, "held": True}

    # Reviewer runs as generation 2 in attempt 2 and reads the new draft (not the broadcast frozen before it started).
    assert (
        await stack.result("@1", "agent", {"prompt": "review", "name": "reviewer"}, meta={"name": "reviewer"})
        == "reviewed"
    )
    assert seen[-1] == ["draft 1", "draft 2"]

    # After the current attempt's reviewer finishes, sending to it still fails with PeerUnreachableError.
    with pytest.raises(NodeFailed, match=r"'reviewer' has finished and will not read this\. Use spawn\(\)"):
        await stack.result("@3", "send", {"to": "reviewer", "text": "too late"})


@pytest.mark.asyncio
async def test_session_turn_rerun_re_reads_explicitly_read_mail_from_predecessor_cursor(stack: WorkflowStack) -> None:
    """Fresh Astra Major 3: rerunning a session turn starts its cursor from its predecessor turn's ending cursor."""
    explicit_reads: list[list[str]] = []
    turn2_prompts: list[str] = []

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[-1]
        last_user = max(i for i, m in enumerate(req.messages) if m.role == "user")
        outs = [
            json.loads(m.content)
            for m in req.messages[last_user + 1 :]
            if m.role == "tool" and isinstance(m.content, str)
        ]
        if "turn-1" in prompt:
            if not outs:
                return calls(tool_call("read_peer_messages", "r-t1", wait_seconds=0))
            explicit_reads.append([m["text"] for m in outs[-1]["messages"]])
            return answer("t1-ok")
        if "turn-2" in prompt:
            if not outs:
                turn2_prompts.append(prompt)
                return calls(tool_call("read_peer_messages", "r-t2", wait_seconds=0))
            explicit_reads.append([m["text"] for m in outs[-1]["messages"]])
            return answer("t2-ok")
        return answer("ok")

    _route_agents(stack, _handle)
    await stack.result("@0", "session", {"name": "coder"}, meta={"name": "coder"})
    await stack.result("@1", "send", {"to": "coder", "text": "draft 1"})
    assert await stack.result("@2", "turn", {"session": "@0", "turn": 1, "message": "turn-1"}) == "t1-ok"
    assert explicit_reads == [["draft 1"]]

    await stack.result("@3", "send", {"to": "coder", "text": "draft 2"})
    assert await stack.result("@4", "turn", {"session": "@0", "turn": 2, "message": "turn-2"}) == "t2-ok"
    assert "draft 2" in turn2_prompts[-1]
    assert "draft 1" not in turn2_prompts[-1]
    assert explicit_reads == [["draft 1"], []]

    # Finish and rerun ONLY turn 1: generation 2 starts from cursor 0 and re-reads "draft 1" explicitly.
    turn1 = stack.store.get_node(stack.run_id, "@2")
    assert turn1 is not None
    await stack.finish_attempt(result="done-1")
    await stack.resume(stack.run_id, rerun=[f"n{turn1.seq}"])

    await stack.result("@0", "session", {"name": "coder"}, meta={"name": "coder"})
    await stack.result("@1", "send", {"to": "coder", "text": "draft 1"})
    assert await stack.result("@2", "turn", {"session": "@0", "turn": 1, "message": "turn-1"}) == "t1-ok"
    assert "draft 1" in explicit_reads[-1]


@pytest.mark.asyncio
async def test_workflow_peers_authorized_under_live_tool_job_runtime(stack: WorkflowStack) -> None:
    """Live path: workflow_peers calls succeed when ToolJobRuntime enforces coordinator._authorize_execution."""
    assert stack.coordinator is not None
    assert stack.context.config.background_tool_jobs.enabled
    stack.jobs.authorize_execution = stack.coordinator._authorize_execution

    raw_outputs: dict[str, list[str]] = {"backend": [], "frontend": []}

    def _handle(req: ModelRequest) -> list[ScriptStep]:
        prompt = _user_texts(req)[0]
        tool_msgs = [m.content for m in req.messages if m.role == "tool" and isinstance(m.content, str)]
        role = "backend" if "backend-role" in prompt else "frontend"
        if role == "backend":
            if len(tool_msgs) == 0:
                return calls(
                    tool_call("list_peers", "lp1"),
                    tool_call("set_shared_state", "st1", key="contract", value={"v": 1}),
                )
            if len(tool_msgs) == 2:
                return calls(tool_call("send_peer_message", "s1", to="frontend", text="hello-fe"))
            raw_outputs["backend"] = list(tool_msgs)
            return answer("backend-done")
        if len(tool_msgs) == 0:
            return calls(tool_call("read_peer_messages", "r1", wait_seconds=5))
        if len(tool_msgs) == 1:
            return calls(tool_call("get_shared_state", "g1", key="contract"))
        raw_outputs["frontend"] = list(tool_msgs)
        return answer("frontend-done")

    _route_agents(stack, _handle)
    await stack.submit("/0:p0@0", "agent", {"prompt": "backend-role", "name": "backend"}, meta={"name": "backend"})
    await stack.submit("/0:p1@0", "agent", {"prompt": "frontend-role", "name": "frontend"}, meta={"name": "frontend"})
    assert (
        await stack.result("/0:p0@0", "agent", {"prompt": "backend-role", "name": "backend"}, meta={"name": "backend"})
        == "backend-done"
    )
    assert (
        await stack.result(
            "/0:p1@0",
            "agent",
            {"prompt": "frontend-role", "name": "frontend"},
            meta={"name": "frontend"},
        )
        == "frontend-done"
    )
    for role, outputs in raw_outputs.items():
        assert outputs, f"{role} produced no tool outputs"
        for out in outputs:
            assert "Tool execution is no longer authorized" not in out, f"{role} got unauthorized: {out}"
    fe_msgs = json.loads(raw_outputs["frontend"][0])
    assert [m["text"] for m in fe_msgs["messages"]] == ["hello-fe"]
    fe_state = json.loads(raw_outputs["frontend"][1])
    assert fe_state == {"key": "contract", "found": True, "value": {"v": 1}}


async def _start_workflow_child(
    stack: WorkflowStack,
    *,
    run_id: str,
    node_key: str,
    parent_run_id: str,
) -> DelegationChild:
    config, paths, parent_owner = stack.context.config, stack.context.runtime_paths, stack.context.execution_identity
    child = prepare_child_turn(
        "watcher",
        "watcher",
        f"{run_id}:{node_key}",
        owner=parent_owner,
        config=config,
        runtime_paths=paths,
        depth=0,
        budget_monitor=None,
        model=None,
        agent_mode="standard",
        previous=None,
        persona=None,
    )
    child.workflow_run_id, child.workflow_node_key, child.workflow_generation = run_id, node_key, 1
    await start_child_turn(
        child,
        parent_run_id=parent_run_id,
        config=config,
        runtime_paths=paths,
        caller_execution_identity=parent_owner,
    )
    return child


@pytest.mark.asyncio
async def test_workflow_peers_refuses_caller_outside_bound_run(stack: WorkflowStack) -> None:
    """Security: a peer toolkit bound to one run/node refuses callers outside it or from another run."""
    assert stack.coordinator is not None
    config, paths = stack.context.config, stack.context.runtime_paths
    child_a = await _start_workflow_child(stack, run_id=stack.run_id, node_key="@0", parent_run_id="parent-a")
    child_b = await _start_workflow_child(
        stack,
        run_id=f"{stack.run_id}-other",
        node_key="@0",
        parent_run_id="parent-b",
    )
    child_sibling = await _start_workflow_child(stack, run_id=stack.run_id, node_key="@1", parent_run_id="parent-s")
    owner_a, owner_b, owner_s = (
        child_execution_identity(child_a),
        child_execution_identity(child_b),
        child_execution_identity(child_sibling),
    )

    with tool_runtime_context(stack.context):
        async with child_run_context(child_a, config=config, runtime_paths=paths):
            peer_tools = WorkflowPeerTools.build(enabled=True)
            assert peer_tools is not None
            tools = toolkit_job_tools(peer_tools, authored_name="workflow_peers")
            job_tool = tools["send_peer_message"]
            assert wait_mode(job_tool, depth=0, context=stack.context) == "native"
            stack.coordinator._authorize_execution(owner_a, job_tool)
            stack.coordinator._authorize_execution(owner_a, tools["ask_user"])
            with pytest.raises(JobAccessError, match="no longer authorized"):
                stack.coordinator._authorize_execution(replace(owner_a, session_id="other-session"), job_tool)

        # Outside any workflow run: refused by both coordinator and toolkit itself.
        with pytest.raises(JobAccessError, match="no longer authorized"):
            stack.coordinator._authorize_execution(owner_a, job_tool)
        with pytest.raises(WorkflowRunStoreError, match="inside a workflow run"):
            await peer_tools.get_shared_state("contract")

        # Inside a different workflow run or different node: refused by both coordinator and toolkit itself.
        for wrong_child, wrong_owner in ((child_b, owner_b), (child_sibling, owner_s)):
            async with child_run_context(wrong_child, config=config, runtime_paths=paths):
                with pytest.raises(JobAccessError, match="no longer authorized"):
                    stack.coordinator._authorize_execution(wrong_owner, job_tool)
                with pytest.raises(WorkflowRunStoreError, match="bound workflow node"):
                    await peer_tools.set_shared_state("contract", {"stolen": True})
                with pytest.raises(WorkflowRunStoreError, match="bound workflow node"):
                    await peer_tools.send_peer_message("frontend", "spoofed")

        # Revoking the caller's workflow tool still revokes peer execution.
        original_tools = list(config.agents["watcher"].tools)
        try:
            config.agents["watcher"].tools = [t for t in original_tools if getattr(t, "name", t) != "workflow"]
            async with child_run_context(child_a, config=config, runtime_paths=paths):
                with pytest.raises(JobAccessError, match="no longer authorized"):
                    stack.coordinator._authorize_execution(owner_a, job_tool)
        finally:
            config.agents["watcher"].tools = original_tools

        # Removing the agent from config mid-run refuses peer execution directly in _authorize_execution.
        async with child_run_context(child_a, config=config, runtime_paths=paths):
            removed_agent = config.agents.pop("watcher")
            try:
                with pytest.raises(JobAccessError, match="no longer authorized"):
                    stack.coordinator._authorize_execution(owner_a, job_tool)
            finally:
                config.agents["watcher"] = removed_agent
        with pytest.raises(ValueError, match="non-empty"):
            WorkflowPeerTools(run_id="", node_key="@0")


@pytest.mark.asyncio
async def test_workflow_peers_approval_gates_and_withholding(stack: WorkflowStack) -> None:
    """Operator tool_approval rules gate workflow_peers identically to configured tools, and withhold when non-resumable."""
    config = stack.context.config.model_copy(
        update={
            "tool_approval": ToolApprovalConfig(
                default="auto_approve",
                rules=[
                    {"match": "append", "action": "require_approval"},
                    {"match": "set_shared_state", "action": "require_approval"},
                ],
            ),
        },
    )
    wildcard_config = config.model_copy(update={"tool_approval": ToolApprovalConfig(default="require_approval")})
    paths = stack.context.runtime_paths
    child = await _start_workflow_child(stack, run_id=stack.run_id, node_key="@check", parent_run_id="p-chk")
    owner = child_execution_identity(child)
    plugins = config_plugins(config, paths)
    turn_model = resolve_turn_model(config, paths, ModelScope(entity="watcher"), None)
    provider = ScriptedModel(lambda _req: answer("ok"))

    async def _build_fns(cfg: Any, *, resumable: bool) -> dict[str, Any]:  # noqa: ANN401
        async with child_run_context(child, config=cfg, runtime_paths=paths):
            built = create_agent(
                "watcher",
                cfg,
                paths,
                execution_identity=owner,
                plugins=plugins,
                knowledge=None,
                model=turn_model,
                provider=provider,
                delegation_depth=1,
                supports_native_tool_approval=resumable,
            )
        return {fn.name: fn for tk in built.spec.toolkits for fn in tk.functions}

    resumable_fns = await _build_fns(config, resumable=True)
    non_resumable_fns = await _build_fns(config, resumable=False)
    assert resumable_fns["append"].gate is ToolGate.POLICY
    assert resumable_fns["set_shared_state"].gate is ToolGate.POLICY
    assert resumable_fns["send_peer_message"].gate is None
    assert "append" not in non_resumable_fns
    assert "set_shared_state" not in non_resumable_fns
    assert "send_peer_message" in non_resumable_fns

    wildcard_fns = await _build_fns(wildcard_config, resumable=True)
    wildcard_non_resumable = await _build_fns(wildcard_config, resumable=False)
    for peer_fn in ("send_peer_message", "set_shared_state", "ask_user"):
        assert wildcard_fns[peer_fn].gate is ToolGate.POLICY
        assert peer_fn not in wildcard_non_resumable

    # Outside a workflow child (default runtime_toolkits=()), required_approval_tool_names refuses workflow_peers.
    saved = ApprovalCall(
        tool_call_id="c1",
        tool_name="set_shared_state",
        invoking_agent="watcher",
        toolkit_name="workflow_peers",
        arguments_digest="d",
        expires_at_ns=0,
        decision=ApprovalDecision.APPROVED,
    )
    with pytest.raises(RuntimeError, match="workflow_peers"):
        await required_approval_tool_names(
            "watcher",
            (saved,),
            config=config,
            runtime_paths=paths,
            tools=plugins.tools,
            execution_identity=owner,
            delegation_depth=1,
        )


@pytest.mark.asyncio
async def test_workflow_peers_respects_tool_approval_policy_pause_approve_deny(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live workflow child pauses gated peer writes until approved, and leaves state untouched before approval or on denial."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)

    with workflow_stack(
        tmp_path,
        extra_tools=("workflow",),
        require_approval=("append", "set_shared_state"),
    ) as gated:
        await gated.open_jobs()
        assert gated.coordinator is not None
        gated.jobs.authorize_execution = gated.coordinator._authorize_execution
        gated.script_agents(monkeypatch)
        cards = JobApprovalCards(asyncio.get_running_loop().create_future(), gated.jobs.journal)
        monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards)
        await gated.start()
        try:
            # 1. Pause + approve: no state changes before approval; executes once after approval.
            gated.child_script(
                calls(tool_call("set_shared_state", "st1", key="contract", value={"v": 1})),
                answer("saved"),
            )
            await gated.submit("@0", "agent", {"prompt": "save contract", "name": "backend"}, meta={"name": "backend"})
            await asyncio.wait_for(cards.posted.wait(), 10)
            await gated.until(lambda: gated.store.get_run(gated.run_id).state is WorkflowRunState.WAITING)

            node0 = gated.store.get_node(gated.run_id, "@0")
            assert node0 is not None
            assert (node0.state, node0.waiting_on) == (WorkflowNodeState.WAITING, "approval")
            assert peers.all_state(gated.store, gated.run_id) == {}

            cards.decision.set_result(BackgroundApprovalDecision("approved", None))
            assert (
                await gated.result(
                    "@0",
                    "agent",
                    {"prompt": "save contract", "name": "backend"},
                    meta={"name": "backend"},
                )
                == "saved"
            )
            assert peers.all_state(gated.store, gated.run_id) == {"contract": {"v": 1}}

            # 2. Deny: state remains untouched and the child finishes cleanly.
            cards2 = JobApprovalCards(asyncio.get_running_loop().create_future(), gated.jobs.journal)
            cards2.decision.set_result(BackgroundApprovalDecision("denied", "No overwrite"))
            monkeypatch.setattr(approval_manager, "get_approval_store", lambda: cards2)
            gated.child_script(
                calls(tool_call("set_shared_state", "st2", key="contract", value={"v": 2})),
                answer("denied-ok"),
            )
            assert (
                await gated.result(
                    "@1",
                    "agent",
                    {"prompt": "overwrite contract", "name": "backend2"},
                    meta={"name": "backend2"},
                )
                == "denied-ok"
            )
            assert peers.all_state(gated.store, gated.run_id) == {"contract": {"v": 1}}
        finally:
            if not cards.decision.done():
                cards.decision.cancel()
            await gated.close()
