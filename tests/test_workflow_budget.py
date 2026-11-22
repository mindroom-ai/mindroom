"""A run's cost limit: what it holds, the question it asks, and the large-run notice (engine harness, priced model)."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.config.models import ModelPricing
from mindroom.matrix.event_types import WORKFLOW_QUESTION_CONTENT_KEY
from mindroom.script_runs import manager as manager_module
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.engine import WorkflowLimits
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.fakes.scripted_model import Await, answer, usage
from tests.workflow_helpers import CallFailed, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.types import MessageMetrics

_CONTINUE, _STOP = "Continue with $1.00 more", "Stop"
_LIMIT = {"max_cost_usd": 1.0, "max_concurrent": 2}


def _usd(amount: float) -> MessageMetrics:
    """Return the usage of one request that costs *amount*: the model costs 1 USD per million input tokens."""
    return usage(round(amount * 1_000_000), 0)


def _turn(amount: float = 0.6) -> list[Any]:
    return answer("done", _usd(amount))


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with a room, the real job runtime and scripted agents on a priced model; no run is started."""
    options = {"extra_tools": ("workflow",), "require_approval": (), "pricing": ModelPricing(input=1, output=2)}
    with workflow_stack(tmp_path, other_agents=("helper",), delegate_to=("helper",), **options) as stack:
        monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
        monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
        monkeypatch.setattr(manager_module, "background_script_supervision_supported", lambda: True)
        stack.show()
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        yield stack
        await stack.close()


async def _agent(stack: WorkflowStack, index: int) -> str:
    """Make the agent call at `@index` and return its state as the call was answered."""
    return (await stack.submit(f"@{index}", "agent", {"prompt": f"Look into part {index}."})).state


def _states(stack: WorkflowStack) -> dict[str, str]:
    return {node.key: node.state.value for node in stack.store.nodes(stack.run_id)}


def _questions(stack: WorkflowStack) -> list[dict[str, Any]]:
    assert stack.room is not None
    return stack.room.carried(WORKFLOW_QUESTION_CONTENT_KEY)


async def _settled(stack: WorkflowStack, *keys: str) -> None:
    done = (WorkflowNodeState.COMPLETED, WorkflowNodeState.FAILED, WorkflowNodeState.CANCELLED)
    await stack.until(lambda: all(stack.store.get_node(stack.run_id, key).state in done for key in keys))


async def _over_the_limit(stack: WorkflowStack, later: asyncio.Event | None = None) -> asyncio.Event:
    """Start four agents of a run with a limit of 1 USD: one spends 1.20, one still runs, two wait for a place.

    The two that wait spend nothing before *later* is set, when it is given.
    """
    await stack.start(**_LIMIT)
    gate = asyncio.Event()
    held = [] if later is None else [Await(later.wait)]
    model = stack.child_script(_turn(1.2), [Await(gate.wait), *_turn()], [*held, *_turn()], [*held, *_turn()])
    assert [await _agent(stack, index) for index in range(4)] == ["running", "running", "queued", "queued"]
    # The second agent asks its model when its job comes to it, which no call waits for.
    await stack.until(lambda: "$budget:1" in _states(stack) and len(model.requests) >= 2)
    assert len(model.requests) == 2
    return gate


async def _answer(stack: WorkflowStack, key: str, answer: str) -> bool:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return await stack.engine.answer(stack.run_id, key, node.generation, answer)


@pytest.mark.asyncio
async def test_budget_holds_queued_nodes_and_asks_and_the_run_waits_once_nothing_runs(stack: WorkflowStack) -> None:
    """At the limit no queued call starts, a running one finishes, and the requester is asked once."""
    gate = await _over_the_limit(stack)
    model = stack.models["watcher"]

    # One agent still runs: the run does too.
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING
    gate.set()
    await _settled(stack, "@1")
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.WAITING)

    assert _states(stack) == {
        "@0": "completed",
        "@1": "completed",
        "@2": "queued",
        "@3": "queued",
        "$budget:1": "waiting",
    }
    (question,) = _questions(stack)
    assert (question["text"], question["options"], question["status"]) == (
        "This run has spent $1.20 of its $1.00 limit. Continue?",
        [_CONTINUE, _STOP],
        "open",
    )
    assert len(model.requests) == 2
    assert stack.engine.run_usage(stack.run_id)["cost_usd"] == pytest.approx(1.8)
    # A call the script makes now waits too, whatever places are free.
    assert await _agent(stack, 4) == "queued"


@pytest.mark.asyncio
async def test_call_whose_start_was_under_way_when_the_limit_was_reached_waits_again_first(
    stack: WorkflowStack,
) -> None:
    """A queued call that had got its place just before the run reached its limit starts nothing and keeps its turn."""
    gate = await _over_the_limit(stack)
    places = stack.engine._places[stack.run_id]
    (third, start), fourth = places.queued.popleft(), places.queued[0][0]
    places.working.add(third)

    await stack.engine._start_queued(stack.run_id, third, start)

    assert ([key for key, _ in places.queued], third in places.working) == ([third, fourth], False)
    assert (_states(stack)[third], len(stack.models["watcher"].requests)) == ("queued", 2)
    gate.set()


@pytest.mark.asyncio
async def test_continue_adds_the_limit_and_releases_queued_nodes(stack: WorkflowStack) -> None:
    """Continue gives the run its limit once more: the held calls start, and the next limit asks again."""
    both = asyncio.Event()
    gate = await _over_the_limit(stack, both)
    gate.set()
    await _settled(stack, "@1")

    assert await _answer(stack, "$budget:1", _CONTINUE)
    # Both held calls run before either has spent anything: one that started after the other's 0.60 would be held.
    await stack.until(lambda: len(stack.models["watcher"].requests) == 4)
    both.set()
    await _settled(stack, "@2", "@3")
    # 1.80 before, and two more turns of 0.60: over the 2.00 the run has now.
    await stack.until(lambda: _questions(stack)[-1]["text"].endswith("of its $2.00 limit. Continue?"))

    assert _questions(stack)[-1]["options"] == [_CONTINUE, _STOP]
    assert (_states(stack)["$budget:1"], _states(stack)["$budget:2"]) == ("completed", "waiting")
    assert await _agent(stack, 4) == "queued"


@pytest.mark.asyncio
async def test_further_turn_of_an_agent_waits_at_the_limit_as_a_new_call_does(stack: WorkflowStack) -> None:
    """A turn that corrects an answer is new spending: at the limit it starts only once the requester continues."""
    await stack.start(max_cost_usd=0.5, max_concurrent=1)
    good = '```json\n{"ok": true}\n```'
    model = stack.child_script(answer("no json here", _usd(0.6)), answer(good, _usd(0.1)))
    ask = {"prompt": "Does it hold?", "schema": {"type": "object", "required": ["ok"]}}

    await stack.submit("@0", "agent", ask)
    await stack.until(lambda: "$budget:1" in _states(stack))
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.WAITING)

    assert (len(model.requests), _states(stack)["@0"]) == (1, "queued")
    assert stack.engine.run_usage(stack.run_id)["cost_usd"] == pytest.approx(0.6)
    assert await _answer(stack, "$budget:1", "Continue with $0.50 more")
    assert await stack.result("@0", "agent", ask) == {"ok": True}
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_agent_that_a_tool_call_starts_after_a_resume_waits_at_the_limit(stack: WorkflowStack) -> None:
    """The limit is read from what the run spent, not from what this primary remembers: a resume keeps it."""
    await stack.start(**_LIMIT)
    model = stack.child_script(_turn(1.2), _turn())
    assert await stack.result("@0", "agent", {"prompt": "Look into part 0."}) == "done"
    await stack.until(lambda: "$budget:1" in _states(stack))
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)
    assert (await stack.submit("@0", "agent", {"prompt": "Look into part 0."})).cached

    delegated = tool_args("delegate", "run_subagent", task="Look into part 1.")
    assert (await stack.submit("@1", "tool", delegated)).state == "queued"
    await asyncio.sleep(0.3)

    assert (len(model.requests), _states(stack)["@1"]) == (1, "queued")
    assert stack.engine.run_usage(stack.run_id)["cost_usd"] == pytest.approx(1.2)
    assert _questions(stack)[-1]["status"] == "open"


@pytest.mark.asyncio
async def test_stop_cancels_the_run_and_a_resume_asks_again_at_the_same_limit(stack: WorkflowStack) -> None:
    """Stop ends the run cancelled. Its limit is not raised: a resume that wants to work is asked again."""
    gate = await _over_the_limit(stack)
    gate.set()
    await _settled(stack, "@1")

    assert await _answer(stack, "$budget:1", _STOP)
    await stack.until(lambda: stack.store.get_run(stack.run_id).state is WorkflowRunState.CANCELLED)
    assert (_states(stack)["@2"], _states(stack)["@3"]) == ("cancelled", "cancelled")
    # A run that ended is asked nothing more, and holds nothing.
    assert await stack.engine._over_budget(stack.store.get_run(stack.run_id)) is False
    assert stack.run_id not in stack.engine._places

    await stack.resume(stack.run_id)
    assert await _agent(stack, 2) == "queued"
    assert _questions(stack)[-1]["text"] == "This run has spent $1.80 of its $1.00 limit. Continue?"
    assert _questions(stack)[-1]["question_id"] != _questions(stack)[0]["question_id"]


@pytest.mark.asyncio
async def test_budget_counts_rerun_turns_and_cached_nodes_do_not_move_it(stack: WorkflowStack) -> None:
    """A saved answer that a resume reuses costs nothing; a call that runs again is paid again, and counts."""
    await stack.start(**_LIMIT)
    stack.child_script(_turn(), _turn())
    assert await stack.result("@0", "agent", {"prompt": "Look into it."}) == "done"
    await stack.end_attempt(ScriptRunState.FAILED)

    await stack.resume(stack.run_id)
    assert (await stack.submit("@0", "agent", {"prompt": "Look into it."})).cached
    assert "$budget:1" not in _states(stack)
    await stack.end_attempt(ScriptRunState.FAILED)

    await stack.resume(stack.run_id, rerun=["n1"])
    assert await stack.result("@0", "agent", {"prompt": "Look into it."}) == "done"
    await stack.until(lambda: "$budget:1" in _states(stack))
    assert _questions(stack)[-1]["text"] == "This run has spent $1.20 of its $1.00 limit. Continue?"


@pytest.mark.asyncio
async def test_open_budget_question_still_holds_the_run_after_a_restart(stack: WorkflowStack) -> None:
    """What holds the run is saved: the next primary starts none of its calls until the requester answers."""
    gate = await _over_the_limit(stack)
    gate.set()
    await _settled(stack, "@1")
    stack = await stack.restart()
    await stack.recover()

    assert await _agent(stack, 2) == "queued"
    assert len(_questions(stack)) == 1
    assert await _answer(stack, "$budget:1", _CONTINUE)
    await stack.until(lambda: _states(stack)["@2"] in ("running", "completed", "failed"))


@pytest.mark.asyncio
async def test_script_cannot_submit_the_cost_questions_key(stack: WorkflowStack) -> None:
    """The key of the run's own cost question is no key a script's call can have, so no script answers it for itself."""
    await stack.start(**_LIMIT)

    with pytest.raises(ValueError, match="invalid literal"):
        await stack.submit("$budget:1", "value", {"op": "now"})

    assert _states(stack) == {}


@pytest.mark.asyncio
async def test_without_a_limit_nothing_pauses(stack: WorkflowStack) -> None:
    """A run that names no limit spends what its agents cost, and asks nothing."""
    await stack.start(max_concurrent=2)
    stack.child_script(_turn(1.2), _turn(), _turn())

    for index in range(3):
        assert await stack.result(f"@{index}", "agent", {"prompt": f"Look into part {index}."}) == "done"

    assert _questions(stack) == []
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_start_workflow_saves_the_limit_and_refuses_one_that_is_no_amount(stack: WorkflowStack) -> None:
    """`max_cost_usd` is the run's, and anything but a positive number is refused before a run exists."""
    source = "def main(args):\n    return 'counted'\n"
    handle = json.loads(await stack.call("start_workflow", source=source, max_cost_usd=2.5))

    assert stack.store.get_run(handle["run_id"]).max_cost_usd == 2.5
    for wrong in (0, -1, "much"):
        with pytest.raises(CallFailed, match="max_cost_usd must be a positive amount in USD"):
            await stack.call("start_workflow", source=source, max_cost_usd=wrong, call_id=f"wrong-{wrong}")
    assert len(stack.store.list_runs()) == 1


@pytest.mark.parametrize(("notice_usd", "large"), [(10, True), (0, False)])
@pytest.mark.asyncio
async def test_large_notice_fires_when_the_projection_passes_ten_dollars(
    stack: WorkflowStack,
    notice_usd: float,
    large: bool,
) -> None:
    """A run whose projected cost passes the operator's amount is marked large, once; 0 turns that test off."""
    stack.limits = WorkflowLimits(large_run_notice_usd=notice_usd)
    await stack.start(title="Audit", job_id="job-1", max_concurrent=1)
    stack.child_script(_turn(6.0), [Await(asyncio.Event().wait), *_turn()])
    assert await stack.result("@0", "agent", {"prompt": "Look into part 0."}) == "done"
    # The first agent's place may still be its own for a moment: the second starts then or right after.
    assert await _agent(stack, 1) in ("running", "queued")
    await stack.until(lambda: _states(stack)["@1"] == "running")

    # One turn of 6 USD ended and one agent is still to end: 6 spent, and 6 more projected.
    await stack.views.show(stack.run_id)
    await stack.views.show(stack.run_id)

    log = ["Large run: 2 agents, about $12.00 projected."] if large else []
    assert (stack.store.get_run(stack.run_id).large, stack.store.log(stack.run_id)) == (large, log)
    await stack.engine.cancel(stack.run_id, reason="Done.")
