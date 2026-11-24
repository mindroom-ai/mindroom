"""Tests for workflow peer messages, shared state, slot-freeing waits, deadlock notice, and SDK/tool integration."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.agents import create_agent
from mindroom.api.script_gateway import serve_listener
from mindroom.custom_tools.workflow import WorkflowTools
from mindroom.delegation.lifecycle import _CHILD_RUN, ChildRunObservation, prepare_child_turn
from mindroom.orchestration import tool_job_runtime as coordinator_module
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.turn_model import ModelScope, resolve_turn_model
from mindroom.workflow_runs import agent_nodes, peers
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.models import WorkflowRunState
from mindroom.workflow_runs.peers import (
    DEADLOCK_NOTE,
    NodeRef,
    PeerTooLargeError,
    PeerUnreachableError,
    peer_op_id,
)
from mindroom.workflow_runs.store import WorkflowRunNotFoundError
from tests.conftest import SETTLE_TIMEOUT_SECONDS, bound_worker_launch_config
from tests.fakes.scripted_model import Await, ScriptedModel, ScriptStep, answer, calls, tool_call
from tests.test_workflow_local_integration import LocalWorkflowStack
from tests.workflow_helpers import NodeFailed, WorkflowStack, set_run, workflow_stack

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

    sent = await stack.engine.peer_send(
        stack.run_id,
        op_id="op-1",
        sender=backend,
        to="frontend",
        text="contract v1",
    )
    assert sent == {"delivered": 0, "held": True}

    await stack.submit("/0:p1@0", "agent", {"prompt": "fe", "name": "frontend"}, meta={"name": "frontend"})
    frontend = _ref(stack, "/0:p1@0")
    messages = peers.read(stack.store, stack.run_id, op_id="op-2", reader=frontend, names=("frontend", "*"))
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
    first_read = peers.read(stack.store, stack.run_id, op_id=read_op1, reader=b, names=("b", "*"))
    assert peers.read(stack.store, stack.run_id, op_id=read_op1, reader=b, names=("b", "*")) == first_read
    assert peers.read(stack.store, stack.run_id, op_id=read_op2, reader=b, names=("b", "*")) == []
    assert [m["text"] for m in first_read] == ["hello"]

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
    assert [m["text"] for m in msgs] == ["ping from worker"]
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
async def test_everyone_waiting_returns_with_the_note_instead_of_hanging(stack: WorkflowStack) -> None:
    """When every unfinished agent waits on peer and runnable==0, all waiters return [] with the deadlock note."""
    seen_tool_results: list[dict[str, Any]] = []

    both_submitted = asyncio.Event()

    def _handle(request: ModelRequest) -> list[ScriptStep]:
        outs = _tool_outputs(request)
        if not outs:
            name = _user_texts(request)[0]
            return [Await(both_submitted.wait), *calls(tool_call("read_peer_messages", f"r-{name}", wait_seconds=600))]
        seen_tool_results.extend(outs)
        return answer("unblocked")

    _route_agents(stack, _handle)
    await stack.submit("/0:p0@0", "agent", {"prompt": "a", "name": "a"}, meta={"name": "a"})
    await stack.submit("/0:p1@0", "agent", {"prompt": "b", "name": "b"}, meta={"name": "b"})
    both_submitted.set()

    res_a, res_b = await asyncio.wait_for(
        asyncio.gather(
            stack.result("/0:p0@0", "agent", {"prompt": "a", "name": "a"}, meta={"name": "a"}),
            stack.result("/0:p1@0", "agent", {"prompt": "b", "name": "b"}, meta={"name": "b"}),
        ),
        timeout=SETTLE_TIMEOUT_SECONDS,
    )
    assert (res_a, res_b) == ("unblocked", "unblocked")
    assert len(seen_tool_results) == 2
    assert all(r == {"messages": [], "note": DEADLOCK_NOTE} for r in seen_tool_results)


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
    assert [
        m["text"] for m in peers.read(stack.store, stack.run_id, op_id="rd1", reader=r1, names=("reviewer", "*"))
    ] == [
        "check diff",
        "all hands",
    ]
    assert [
        m["text"] for m in peers.read(stack.store, stack.run_id, op_id="rd2", reader=r2, names=("reviewer", "*"))
    ] == [
        "check diff",
        "all hands",
    ]
    # Sender does not receive its own broadcast.
    assert peers.read(stack.store, stack.run_id, op_id="rd3", reader=lead, names=("lead", "*")) == []

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
        peers.set_state(stack.store, stack.run_id, op_id="big-state", key="k", value=big, updated_by="a")


@pytest.mark.asyncio
async def test_prune_removes_peer_tables(stack: WorkflowStack) -> None:
    """Pruning an old finished run deletes its rows in all four peer tables."""
    sender = NodeRef(key="@0", generation=1, name="a")
    reader = NodeRef(key="@1", generation=1, name="b")
    peers.send(stack.store, stack.run_id, op_id="p1", sender=sender, to="b", text="hi")
    peers.read(stack.store, stack.run_id, op_id="p2", reader=reader, names=("b", "*"))
    peers.set_state(stack.store, stack.run_id, op_id="p3", key="k", value=1, updated_by="a")

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
