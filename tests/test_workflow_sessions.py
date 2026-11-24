"""Tests for workflow sessions: `spawn()` and `Session.ask()`.

Level: pure SDK against `FakeWorkflowGateway` plus engine harness (`WorkflowStack`) with real delegation and jobs.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

import mindroom.workflow as workflow_sdk
from mindroom.delegation import execution as delegation_execution
from mindroom.delegation.background import delegation_child
from mindroom.delegation.sessions import subagent_sessions_dir
from mindroom.delegation.state import SubagentPersona
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.agent_nodes import turn_job_id
from mindroom.workflow_runs.graph import graph_rows
from mindroom.workflow_runs.models import WorkflowNodeState
from tests.fakes.scripted_model import Await, answer, usage
from tests.scripted_agent_helpers import raising
from tests.workflow_helpers import FakeWorkflowGateway, NodeFailed, WorkflowStack, completed, sdk_env, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator
    from pathlib import Path
    from types import ModuleType

    from mindroom.llm.model import ModelRequest


@pytest.fixture
def gateway(tmp_path: Path) -> Iterator[FakeWorkflowGateway]:
    """Serve a fake workflow gateway for one SDK test."""
    with FakeWorkflowGateway(tmp_path / "capability") as fake:
        yield fake


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch, gateway: FakeWorkflowGateway) -> ModuleType:
    """Return the SDK module, pointed at the fake gateway."""
    sdk_env(monkeypatch, gateway)
    return workflow_sdk


@pytest.fixture
def in_main(sdk: ModuleType) -> Iterator[None]:
    """Run the test as if inside `main(args)`: the root lane exists."""
    sdk._local.lane = sdk._Lane("")
    yield
    sdk._local.lane = None


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The engine harness with real delegation, job runtime, scripted agents, and one started run."""
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start()
        yield stack
        await stack.close()


def _unwrap_prompt(text: str) -> str:
    """Extract the inner prompt from MindRoom's `<msg ...><![CDATA[...]]></msg>` wrapper when present."""
    if "<![CDATA[" in text and "]]></msg>" in text:
        return text.split("<![CDATA[", 1)[1].rsplit("]]></msg>", 1)[0]
    return text


def _user_texts(request: ModelRequest) -> list[str]:
    """Return the user message texts sent in one model request, in order."""
    return [_unwrap_prompt(str(message.content)) for message in request.messages if message.role == "user"]


def _turn_args(session: str, turn: int, message: str, *, schema: object = None) -> dict[str, object]:
    args: dict[str, object] = {"session": session, "turn": turn, "message": message}
    if schema is not None:
        args["schema"] = schema
    return args


@pytest.mark.usefixtures("in_main")
def test_sdk_turn_numbers_count_per_session(gateway: FakeWorkflowGateway, sdk: ModuleType) -> None:
    """`spawn()` creates a session node; `Session.ask()` counts turn numbers independently per session."""
    answers = iter([completed(None), completed(None), completed("d1"), completed("r1"), completed({"ok": True})])
    gateway.respond = lambda _request: next(answers)
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}

    dev = sdk.spawn("dev", model="haiku", system="Write code.")
    reviewer = sdk.spawn("reviewer", system="Review diffs.")
    assert dev.ask("Implement feature") == "d1"
    assert reviewer.ask("Review diff") == "r1"
    assert dev.ask("Fix tests", schema=schema, timeout=30) == {"ok": True}

    assert [(n["key"], n["kind"], n["args"]) for n in gateway.nodes] == [
        ("@0", "session", {"name": "dev", "model": "haiku", "system": "Write code."}),
        ("@1", "session", {"name": "reviewer", "system": "Review diffs."}),
        ("@2", "turn", {"session": "@0", "turn": 1, "message": "Implement feature"}),
        ("@3", "turn", {"session": "@1", "turn": 1, "message": "Review diff"}),
        ("@4", "turn", {"session": "@0", "turn": 2, "message": "Fix tests", "schema": schema}),
    ]
    assert gateway.nodes[4]["meta"] == {"cached": True, "name": "dev", "timeout": 30}

    with pytest.raises(sdk.WorkflowError, match=r"spawn\(\) has no option 'system_prompt'\. Did you mean system\?"):
        sdk.spawn("bad", system_prompt="Oops")


@pytest.mark.asyncio
async def test_second_turn_continues_the_same_child_as_its_own_job(stack: WorkflowStack) -> None:
    """Turn 1 launches a fresh child named after the session; turn 2 continues it as its own job and usage entry."""
    model = stack.child_script(answer("first answer", usage(10, 4)), answer("second answer", usage(15, 6)))

    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    assert await stack.result("@1", "turn", _turn_args("@0", 1, "Step one"), meta={"name": "dev"}) == "first answer"
    assert await stack.result("@2", "turn", _turn_args("@0", 2, "Step two"), meta={"name": "dev"}) == "second answer"

    await stack.until(lambda: stack.node_job("@1").consumed and stack.node_job("@2").consumed)
    session = stack.store.get_node(stack.run_id, "@0")
    turn1 = stack.store.get_node(stack.run_id, "@1")
    turn2 = stack.store.get_node(stack.run_id, "@2")
    assert session is not None
    assert turn1 is not None
    assert turn2 is not None

    run = stack.store.get_run(stack.run_id)
    job1, job2 = stack.node_job("@1"), stack.node_job("@2")
    child1, child2 = delegation_child(job1), delegation_child(job2)

    assert job1.job_id == turn_job_id(run, "@1", 1, 1)
    assert job2.job_id == turn_job_id(run, "@2", 1, 2)
    assert job1.job_id != job2.job_id
    assert child1.subagent_id == child2.subagent_id
    assert child1.session_id == child2.session_id
    assert child1.task == "dev"
    assert session.links["subagent_id"] == child1.subagent_id
    assert (turn1.session_key, turn2.session_key) == ("@0", "@0")

    # Turn 2 saw turn 1 in its conversation history and received its own prompt in order.
    assert _user_texts(model.requests[0]) == ["Step one"]
    assert _user_texts(model.requests[1]) == ["Step one", "Step two"]

    # Each turn recorded its own usage entry.
    usage_by_node = stack.store.node_usage(stack.run_id)
    assert (usage_by_node["@1"]["input_tokens"], usage_by_node["@1"]["output_tokens"]) == (10, 4)
    assert (usage_by_node["@2"]["input_tokens"], usage_by_node["@2"]["output_tokens"]) == (15, 6)

    # Graph: turn nodes carry the session ID, and the session row reports the turn count.
    graph = graph_rows(stack.store.nodes(stack.run_id), usage_by_node, (), ())
    by_id = {row["id"]: row for row in graph.nodes}
    session_row, turn1_row, turn2_row = by_id[f"n{session.seq}"], by_id[f"n{turn1.seq}"], by_id[f"n{turn2.seq}"]
    assert (session_row["kind"], session_row["turns"], session_row["subagent"]) == ("session", 2, child1.subagent_id)
    assert (turn1_row["kind"], turn1_row["session"], turn2_row["session"]) == (
        "turn",
        session_row["id"],
        session_row["id"],
    )


@pytest.mark.asyncio
async def test_resume_appends_to_the_saved_session(stack: WorkflowStack) -> None:
    """On resume, saved turns hit cache and a new turn after them continues the saved child."""
    model = stack.child_script(answer("a1"), answer("a2"))

    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    assert await stack.result("@1", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"}) == "a1"
    assert await stack.result("@2", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"}) == "a2"
    saved_subagent = stack.store.get_node(stack.run_id, "@0").links["subagent_id"]  # type: ignore[union-attr]
    await stack.finish_attempt("round 1")

    await stack.resume(stack.run_id, source="def main(args): return 2")
    model2 = stack.child_script(answer("a3"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    r1 = await stack.submit("@1", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"})
    r2 = await stack.submit("@2", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"})
    assert (r1.cached, r1.result, r2.cached, r2.result) == (True, "a1", True, "a2")
    assert len(model.requests) == 2
    assert len(model2.requests) == 0

    assert await stack.result("@3", "turn", _turn_args("@0", 3, "m3"), meta={"name": "dev"}) == "a3"
    child3 = delegation_child(stack.node_job("@3"))
    assert child3.subagent_id == saved_subagent
    assert _user_texts(model2.requests[0]) == ["m1", "m2", "m3"]


@pytest.mark.asyncio
async def test_changed_middle_turn_starts_a_fresh_child_with_the_transcript(stack: WorkflowStack) -> None:
    """Editing a middle turn starts a fresh child with `Earlier conversation:` and later turns continue that new child."""
    stack.child_script(answer("a1"), answer("a2"), answer("a3"))

    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    assert await stack.result("@1", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"}) == "a1"
    assert await stack.result("@2", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"}) == "a2"
    assert await stack.result("@3", "turn", _turn_args("@0", 3, "m3"), meta={"name": "dev"}) == "a3"
    first_subagent = stack.store.get_node(stack.run_id, "@0").links["subagent_id"]  # type: ignore[union-attr]
    await stack.finish_attempt("v1")

    # Attempt 2 edits turn 2; turn 1 hits, turn 2 gets a fresh child with the transcript, turn 3 continues the new child.
    await stack.resume(stack.run_id, source="def main(args): return 2")
    model2 = stack.child_script(answer("a2_new"), answer("a3_new"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    r1 = await stack.submit("@1", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"})
    assert (r1.cached, r1.result) == (True, "a1")

    assert await stack.result("@2", "turn", _turn_args("@0", 2, "m2_edited"), meta={"name": "dev"}) == "a2_new"
    assert await stack.result("@3", "turn", _turn_args("@0", 3, "m3"), meta={"name": "dev"}) == "a3_new"

    child2 = delegation_child(stack.node_job("@2"))
    child3 = delegation_child(stack.node_job("@3"))
    assert child2.subagent_id != first_subagent
    assert child3.subagent_id == child2.subagent_id
    assert stack.store.get_node(stack.run_id, "@0").links["subagent_id"] == child2.subagent_id  # type: ignore[union-attr]

    prompt2 = _user_texts(model2.requests[0])[0]
    assert prompt2.startswith("Earlier conversation:")
    assert "m1" in prompt2
    assert "a1" in prompt2
    assert prompt2.endswith("\n\nm2_edited")
    assert _user_texts(model2.requests[1]) == [prompt2, "m3"]

    # Attempt 3 with the same edited script hits every turn without running a model.
    await stack.finish_attempt("v2")
    await stack.resume(stack.run_id, source="def main(args): return 2")
    model3 = stack.child_script()
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    for key, turn, msg, expected in (
        ("@1", 1, "m1", "a1"),
        ("@2", 2, "m2_edited", "a2_new"),
        ("@3", 3, "m3", "a3_new"),
    ):
        receipt = await stack.submit(key, "turn", _turn_args("@0", turn, msg), meta={"name": "dev"})
        assert (receipt.cached, receipt.result) == (True, expected)
    assert model3.requests == []


@pytest.mark.asyncio
async def test_changed_turn_in_one_lane_reruns_the_sessions_later_turn_in_another_lane(stack: WorkflowStack) -> None:
    """Changing turn 1 in lane /1:p0 invalidates turn 2 in sibling lane /1:p1 via session lineage."""
    stack.child_script(answer("a1"), answer("a2"))

    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    await stack.submit("@1", "group", {"op": "parallel"}, meta={"members": ["/1:p0", "/1:p1"]})
    assert await stack.result("/1:p0@0", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"}) == "a1"
    assert await stack.result("/1:p1@0", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"}) == "a2"
    await stack.finish_attempt("v1")

    await stack.resume(stack.run_id, source="def main(args): return 2")
    stack.child_script(answer("a1_new"), answer("a2_new"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    await stack.submit("@1", "group", {"op": "parallel"}, meta={"members": ["/1:p0", "/1:p1"]})
    assert await stack.result("/1:p0@0", "turn", _turn_args("@0", 1, "m1_new"), meta={"name": "dev"}) == "a1_new"
    assert await stack.result("/1:p1@0", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"}) == "a2_new"

    turn2 = stack.store.get_node(stack.run_id, "/1:p1@0")
    assert turn2 is not None
    assert (turn2.generation, turn2.cached) == (2, False)


@pytest.mark.asyncio
async def test_two_asks_at_once_on_one_session_are_refused_and_two_sessions_run_together(
    stack: WorkflowStack,
) -> None:
    """Concurrent asks on one session fail with the plan's message; two different sessions run at once."""
    gate_a, gate_b = asyncio.Event(), asyncio.Event()
    stack.child_script([Await(gate_a.wait), *answer("from_a")], [Await(gate_b.wait), *answer("from_b")])

    await stack.result("@0", "session", {"name": "alpha"}, meta={"name": "alpha"})
    await stack.result("@1", "session", {"name": "beta"}, meta={"name": "beta"})

    # Start alpha turn 1 and wait until it is running.
    receipt_a = await stack.submit("@2", "turn", _turn_args("@0", 1, "work a"), meta={"name": "alpha"})
    assert receipt_a.state == WorkflowNodeState.RUNNING.value

    # A second ask on alpha while turn 1 is live fails with the plan's message.
    with pytest.raises(NodeFailed) as overlap:
        await stack.result("@3", "turn", _turn_args("@0", 2, "overlap"), meta={"name": "alpha"})
    assert str(overlap.value) == "Session 'alpha' is already in a turn. Wait for it or spawn another agent."

    # A concurrent ask on beta runs at the same time as alpha.
    receipt_b = await stack.submit("@4", "turn", _turn_args("@1", 1, "work b"), meta={"name": "beta"})
    assert receipt_b.state == WorkflowNodeState.RUNNING.value
    assert stack.store.get_node(stack.run_id, "@2").state is WorkflowNodeState.RUNNING  # type: ignore[union-attr]
    assert stack.store.get_node(stack.run_id, "@4").state is WorkflowNodeState.RUNNING  # type: ignore[union-attr]

    gate_a.set()
    gate_b.set()
    assert (await stack.engine.node_receipt(stack.attempt, receipt_a.id, wait=5.0)).result == "from_a"
    assert (await stack.engine.node_receipt(stack.attempt, receipt_b.id, wait=5.0)).result == "from_b"


@pytest.mark.asyncio
async def test_rerun_of_turn_one_with_the_same_message_reruns_turn_two(stack: WorkflowStack) -> None:
    """When turn 1 failed and ran again as generation 2 with the same message, turn 2 misses because its lineage changed."""
    stack.child_script(raising(RuntimeError("boom")), answer("a2_after_failure"))

    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    await stack.submit("@1", "group", {"op": "parallel"}, meta={"members": ["/1:p0", "/1:p1"]})
    with pytest.raises(NodeFailed):
        await stack.result("/1:p0@0", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"})
    assert await stack.result("/1:p1@0", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"}) == "a2_after_failure"
    await stack.finish_attempt("v1")

    # Attempt 2 retries turn 1 (same message, now generation 2) -> turn 2 in sibling lane misses and continues turn 1.
    await stack.resume(stack.run_id)
    model2 = stack.child_script(answer("a1_ok"), answer("a2_ok"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    await stack.submit("@1", "group", {"op": "parallel"}, meta={"members": ["/1:p0", "/1:p1"]})
    assert await stack.result("/1:p0@0", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"}) == "a1_ok"
    assert await stack.result("/1:p1@0", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"}) == "a2_ok"

    turn1 = stack.store.get_node(stack.run_id, "/1:p0@0")
    turn2 = stack.store.get_node(stack.run_id, "/1:p1@0")
    assert turn1 is not None
    assert turn2 is not None
    assert (turn1.generation, turn2.generation, turn2.cached) == (2, 2, False)
    assert _user_texts(model2.requests[1]) == ["m1", "m2"]


@pytest.mark.asyncio
async def test_moved_earlier_turn_reruns_later_turn_in_sibling_lane(stack: WorkflowStack) -> None:
    """Inserting a call before turn 1 moves it to a new key; a later turn in a sibling lane must miss."""
    stack.child_script(answer("old findings"), answer("old summary"))

    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    await stack.submit("@1", "group", {"op": "parallel"}, meta={"members": ["/1:p0", "/1:p1"]})
    assert await stack.result("/1:p0@0", "turn", _turn_args("@0", 1, "Read current data"), meta={"name": "dev"}) == (
        "old findings"
    )
    assert await stack.result("/1:p1@0", "turn", _turn_args("@0", 2, "Summarize"), meta={"name": "dev"}) == (
        "old summary"
    )
    await stack.finish_attempt("v1")

    # Attempt 2 inserts a call at /1:p0@0 so turn 1 moves to /1:p0@1 (fresh generation 1 with the same message).
    await stack.resume(stack.run_id, source="def main(args): return 2")
    model2 = stack.child_script(answer("new findings"), answer("new summary"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    await stack.submit("@1", "group", {"op": "parallel"}, meta={"members": ["/1:p0", "/1:p1"]})
    await stack.result("/1:p0@0", "value", {"op": "now"})
    assert await stack.result("/1:p0@1", "turn", _turn_args("@0", 1, "Read current data"), meta={"name": "dev"}) == (
        "new findings"
    )
    summary = await stack.result("/1:p1@0", "turn", _turn_args("@0", 2, "Summarize"), meta={"name": "dev"})

    turn2 = stack.store.get_node(stack.run_id, "/1:p1@0")
    assert turn2 is not None
    assert (summary, len(model2.requests), turn2.cached) == ("new summary", 2, False)


@pytest.mark.asyncio
async def test_session_ask_supports_schema_correction_on_later_turns(stack: WorkflowStack) -> None:
    """A session turn with `schema` retries invalid JSON on the same child and returns parsed JSON."""
    schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}
    stack.child_script(answer("draft ready"), answer("not json"), answer('{"verdict": "APPROVE"}'))

    await stack.result("@0", "session", {"name": "reviewer"}, meta={"name": "reviewer"})
    assert (
        await stack.result("@1", "turn", _turn_args("@0", 1, "Read diff"), meta={"name": "reviewer"}) == "draft ready"
    )
    verdict = await stack.result(
        "@2",
        "turn",
        _turn_args("@0", 2, "Give verdict", schema=schema),
        meta={"name": "reviewer"},
    )
    assert verdict == {"verdict": "APPROVE"}
    assert (
        stack.store.get_node(stack.run_id, "@2").links["subagent_id"]
        == (  # type: ignore[union-attr]
            stack.store.get_node(stack.run_id, "@1").links["subagent_id"]  # type: ignore[union-attr]
        )
    )


@pytest.mark.asyncio
async def test_narrowed_resume_starts_a_capped_fresh_child_with_transcript(stack: WorkflowStack) -> None:
    """A resume with a narrower tool ceiling starts a capped fresh child with the transcript instead of continuing the wider child."""
    stack.child_script(answer("a1"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    assert await stack.result("@1", "turn", _turn_args("@0", 1, "Step one"), meta={"name": "dev"}) == "a1"
    child1 = delegation_child(stack.node_job("@1"))
    assert child1.persona is None
    await stack.finish_attempt("v1")

    narrow = SubagentPersona("inline", "narrow", "Only text.", tools=("counter.text",))
    stack.context = replace(stack.context, persona=narrow)
    await stack.resume(stack.run_id, source="def main(args): return 2")
    model2 = stack.child_script(answer("a2"), answer("a3"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    r1 = await stack.submit("@1", "turn", _turn_args("@0", 1, "Step one"), meta={"name": "dev"})
    assert (r1.cached, r1.result) == (True, "a1")

    assert await stack.result("@2", "turn", _turn_args("@0", 2, "Step two"), meta={"name": "dev"}) == "a2"
    assert await stack.result("@3", "turn", _turn_args("@0", 3, "Step three"), meta={"name": "dev"}) == "a3"

    child2 = delegation_child(stack.node_job("@2"))
    child3 = delegation_child(stack.node_job("@3"))
    assert child2.subagent_id != child1.subagent_id
    assert child2.persona is not None
    assert child2.persona.tools == ("counter.text",)
    assert child3.subagent_id == child2.subagent_id
    prompt2 = _user_texts(model2.requests[0])[0]
    assert prompt2.startswith("Earlier conversation:")
    assert "Step one" in prompt2
    assert "a1" in prompt2
    assert prompt2.endswith("\n\nStep two")
    assert _user_texts(model2.requests[1]) == [prompt2, "Step three"]


@pytest.mark.asyncio
async def test_session_turn_delegation_hooks_receive_turn_message_not_session_name(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delegation hooks see the turn's actual text while child job display labels stay the session name."""
    seen: list[tuple[str, dict[str, object]]] = []
    original = delegation_execution.before_delegation

    async def record(**call: object) -> object:
        seen.append((str(call["tool_name"]), dict(call["arguments"])))  # type: ignore[call-overload]
        return await original(**call)  # type: ignore[arg-type]

    monkeypatch.setattr(delegation_execution, "before_delegation", record)
    stack.child_script(answer("read it"), answer("summary"))

    await stack.result("@0", "session", {"name": "reviewer"}, meta={"name": "reviewer"})
    assert await stack.result("@1", "turn", _turn_args("@0", 1, "Read draft"), meta={"name": "reviewer"}) == "read it"
    assert (
        await stack.result("@2", "turn", _turn_args("@0", 2, "Summarize draft"), meta={"name": "reviewer"}) == "summary"
    )

    child1, child2 = delegation_child(stack.node_job("@1")), delegation_child(stack.node_job("@2"))
    assert (child1.task, child2.task) == ("reviewer", "reviewer")
    assert [(name, args.get("task") or args.get("message")) for name, args in seen] == [
        ("run_subagent", "Read draft"),
        ("continue_subagent", "Summarize draft"),
    ]


@pytest.mark.asyncio
async def test_refused_follow_up_falls_back_to_fresh_child_with_transcript(stack: WorkflowStack) -> None:
    """When the saved child cannot be continued (e.g. session storage gone), the turn starts a fresh child with the transcript."""
    model = stack.child_script(answer("a1"), answer("a2"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    assert await stack.result("@1", "turn", _turn_args("@0", 1, "Step one"), meta={"name": "dev"}) == "a1"
    child1 = delegation_child(stack.node_job("@1"))
    (subagent_sessions_dir(stack.context.runtime_paths) / f"{child1.subagent_id}.json").unlink()

    assert await stack.result("@2", "turn", _turn_args("@0", 2, "Step two"), meta={"name": "dev"}) == "a2"
    child2 = delegation_child(stack.node_job("@2"))
    assert child2.subagent_id != child1.subagent_id
    prompt2 = _user_texts(model.requests[1])[0]
    assert prompt2.startswith("Earlier conversation:")
    assert "Step one" in prompt2
    assert "a1" in prompt2
    assert prompt2.endswith("\n\nStep two")


@pytest.mark.asyncio
async def test_resumed_failed_last_turn_continues_same_child(stack: WorkflowStack) -> None:
    """When the last turn failed with no tool call, resuming continues the same child instead of starting fresh."""
    stack.child_script(answer("a1"), raising(RuntimeError("boom")))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    assert await stack.result("@1", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"}) == "a1"
    child1 = delegation_child(stack.node_job("@1"))
    with pytest.raises(NodeFailed):
        await stack.result("@2", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"})
    await stack.finish_attempt("v1")

    await stack.resume(stack.run_id)
    model2 = stack.child_script(answer("a2"))
    await stack.result("@0", "session", {"name": "dev"}, meta={"name": "dev"})
    r1 = await stack.submit("@1", "turn", _turn_args("@0", 1, "m1"), meta={"name": "dev"})
    assert (r1.cached, r1.result) == (True, "a1")
    assert await stack.result("@2", "turn", _turn_args("@0", 2, "m2"), meta={"name": "dev"}) == "a2"

    child2 = delegation_child(stack.node_job("@2"))
    assert child2.subagent_id == child1.subagent_id
    assert _user_texts(model2.requests[0]) == ["m1", "m2"]
