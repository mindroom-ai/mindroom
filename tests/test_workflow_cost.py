"""Tests for what a workflow run cost: the usage ledger and its two sums, the whole run and the current attempt.

Level: engine harness (`WorkflowStack`) with the real job runtime and scripted agents on a priced model.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.config.models import ModelPricing
from mindroom.llm.events import TextDelta
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from mindroom.workflow_runs.models import WorkflowNodeState
from mindroom.workflow_runs.store import SupersededAttemptError
from tests.fakes.scripted_model import Await, ScriptedModel, answer, calls, tool_call, usage
from tests.tool_job_helpers import wait_for_status
from tests.workflow_helpers import NodeFailed, WorkflowStack, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.types import MessageMetrics

_PROMPT = {"prompt": "Look into it."}
# The model costs 1 USD per million input tokens, so a request of 100,000 tokens costs ten cents.
_PRICE = ModelPricing(input=1, output=2)


def _usd(amount: float) -> MessageMetrics:
    """Return the usage of one request that costs *amount* at `_PRICE`."""
    return usage(round(amount * 1_000_000), 0)


async def _opened(stack: WorkflowStack, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    monkeypatch.setattr(engine_module, "RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "RECHECK_SECONDS", 0.05)
    await stack.open_jobs()
    stack.script_agents(monkeypatch)
    await stack.start()
    yield stack
    await stack.close()


@pytest_asyncio.fixture
async def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The harness with one started run whose agents run on a priced model; the owner may delegate to `helper`."""
    options = {"other_agents": ("helper",), "delegate_to": ("helper",), "pricing": _PRICE}
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=(), **options) as stack:
        async for opened in _opened(stack, monkeypatch):
            yield opened


@pytest_asyncio.fixture
async def unpriced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[WorkflowStack]:
    """The same harness on a model that has no price."""
    with workflow_stack(tmp_path, extra_tools=("workflow",), require_approval=()) as stack:
        async for opened in _opened(stack, monkeypatch):
            yield opened


def _costs(stack: WorkflowStack) -> tuple[float, float, bool]:
    used = stack.engine.run_usage(stack.run_id)
    return used["cost_usd"], used["attempt_cost_usd"], used["cost_unknown"]


def _ledger(stack: WorkflowStack) -> list[tuple[str, int, str]]:
    """Return the key, the generation and the turn of each entry of the run's ledger."""
    with closing(sqlite3.connect(stack.store.database_path)) as connection:
        rows = connection.execute(
            "SELECT key, generation, CAST(turn AS TEXT) FROM workflow_usage WHERE run_id = ? ORDER BY rowid",
            (stack.run_id,),
        ).fetchall()
    return [(key, generation, turn.partition(":")[0]) for key, generation, turn in rows]


@pytest.mark.asyncio
async def test_rerun_does_not_erase_what_the_first_generation_cost(stack: WorkflowStack) -> None:
    """A call that runs again is paid again, and the run still shows what its first generation cost."""
    stack.child_script(answer("first", _usd(0.40)), answer("second", _usd(0.10)))
    assert await stack.result("@0", "agent", _PROMPT) == "first"
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id, rerun=["n1"])

    assert await stack.result("@0", "agent", _PROMPT) == "second"

    assert _costs(stack) == (pytest.approx(0.50), pytest.approx(0.10), False)
    used = stack.engine.run_usage(stack.run_id)
    assert (used["input_tokens"], used["output_tokens"], used["tool_calls"]) == (500_000, 0, 0)


@pytest.mark.asyncio
async def test_cache_hit_adds_nothing(stack: WorkflowStack) -> None:
    """A saved answer that a resume reuses costs nothing: the attempt's sum stays at zero and the run's as it was."""
    stack.child_script(answer("first", _usd(0.40)))
    assert await stack.result("@0", "agent", _PROMPT) == "first"
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)

    assert (await stack.submit("@0", "agent", _PROMPT)).cached

    assert _costs(stack) == (pytest.approx(0.40), 0.0, False)


@pytest.mark.asyncio
async def test_subagent_of_a_workflow_agent_is_counted_once(stack: WorkflowStack) -> None:
    """What an agent's own subagent cost is an entry beside the agent's own turn, so the run's sum holds it once."""
    delegated = tool_call("run_subagent", "c1", task="Help.", agent_name="helper")
    stack.child_script(calls(delegated, metrics=_usd(0.10)), answer("done", _usd(0.10)))
    stack.child_script(answer("helped", _usd(0.30)), agent="helper")

    assert await stack.result("@0", "agent", _PROMPT) == "done"

    # The agent's two requests and its subagent's one: 0.50, not the 0.80 that its rolled-up total would add to.
    assert _costs(stack) == (pytest.approx(0.50), pytest.approx(0.50), False)
    assert _ledger(stack) == [("@0", 1, "1"), ("@0", 1, "sub")]
    assert stack.store.node_usage(stack.run_id)["@0"]["cost_usd"] == pytest.approx(0.50)


@pytest.mark.asyncio
async def test_agent_on_an_unpriced_model_with_a_priced_subagent_is_a_lower_bound(stack: WorkflowStack) -> None:
    """What the subagent cost is known and what the agent itself cost is not: the sum says so, and is no zero."""
    delegated = tool_call("run_subagent", "c1", task="Help.", agent_name="helper")
    # A model the operator's configuration has no price for.
    stack.models["watcher"] = ScriptedModel([calls(delegated, metrics=usage(1_000, 0)), answer("done")])
    stack.child_script(answer("helped", _usd(0.30)), agent="helper")

    assert await stack.result("@0", "agent", _PROMPT) == "done"

    assert _costs(stack) == (pytest.approx(0.30), pytest.approx(0.30), True)


@pytest.mark.asyncio
async def test_priced_agent_with_a_subagent_on_an_unpriced_model_is_a_lower_bound(stack: WorkflowStack) -> None:
    """What the agent itself cost is known and what its subagent cost is not: the sum is no exact twenty cents."""
    delegated = tool_call("run_subagent", "c1", task="Help.", agent_name="helper")
    stack.child_script(calls(delegated, metrics=_usd(0.10)), answer("done", _usd(0.10)))
    stack.models["helper"] = ScriptedModel([answer("helped", usage(1_000, 0))])

    assert await stack.result("@0", "agent", _PROMPT) == "done"

    assert _costs(stack) == (pytest.approx(0.20), pytest.approx(0.20), True)
    assert _ledger(stack) == [("@0", 1, "1"), ("@0", 1, "sub")]


@pytest.mark.asyncio
async def test_total_with_a_priced_and_an_unpriced_subagent_is_a_lower_bound_and_a_refused_one_changes_nothing(
    stack: WorkflowStack,
) -> None:
    """One helper has a price and one has none: the sum holds the first and says it is no exact amount.

    A delegation that was refused before any child ran spent nothing, so the total of its agent stays exact. The
    reproducer of the second full review (Astra, verification of M4).
    """

    def help_from(call_id: str, agent: str) -> dict[str, object]:
        return tool_call("run_subagent", call_id, task="Read.", agent_name=agent)

    stack.child_script(answer("priced", _usd(0.30)), agent="helper")

    async def unprice_helper() -> None:
        stack.models["helper"] = ScriptedModel([answer("unpriced", usage(1_000, 0))])

    stack.child_script(
        calls(help_from("c1", "helper"), metrics=_usd(0.10)),
        [Await(unprice_helper), *calls(help_from("c2", "helper"), metrics=_usd(0.10))],
        answer("both", _usd(0.10)),
        calls(help_from("c3", "nobody"), metrics=_usd(0.10)),
        answer("declined", _usd(0.10)),
    )

    assert await stack.result("@0", "agent", _PROMPT) == "both"
    assert await stack.result("@1", "agent", {"prompt": "Ask somebody who is not there."}) == "declined"

    by_node = stack.store.node_usage(stack.run_id)
    assert (by_node["@0"]["cost_usd"], by_node["@0"]["cost_unknown"]) == (pytest.approx(0.60), True)
    assert (by_node["@1"]["cost_usd"], by_node["@1"]["cost_unknown"]) == (pytest.approx(0.20), False)


@pytest.mark.asyncio
async def test_job_that_completed_before_a_restart_is_no_cost_of_the_attempt_that_reads_it(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent's turn ended in the first attempt and MindRoom stopped before its node was settled.

    The next attempt reads the saved answer and asks no model: the twenty cents are the run's, and none of them the
    second attempt's. The reproducer of the second full review (Sol Major 1).
    """
    stack.continues = lambda job: job.kind == "delegation"
    settling, settle = asyncio.Event(), engine_module.WorkflowEngine._settle_agent
    first = stack.engine

    async def held(self: engine_module.WorkflowEngine, *args: object, **kwargs: object) -> None:
        if self is first:
            settling.set()
            await asyncio.Event().wait()
        await settle(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine_module.WorkflowEngine, "_settle_agent", held)
    stack.child_script(answer("done", _usd(0.20)))
    await stack.submit("@0", "agent", _PROMPT)
    await asyncio.wait_for(settling.wait(), 5)
    await stack.until(lambda: stack.node_job("@0").status == "completed")

    await stack.restart()
    assert await stack.recover() == [stack.run_id]

    assert await stack.result("@0", "agent", _PROMPT) == "done"
    assert (stack.store.get_run(stack.run_id).attempt, _costs(stack)) == (2, (pytest.approx(0.20), 0.0, False))


@pytest.mark.asyncio
async def test_unpriced_model_marks_the_total_unknown(unpriced: WorkflowStack) -> None:
    """A turn on a model with no price counts its tokens and marks the cost as a lower bound."""
    unpriced.child_script(answer("hi", usage(11, 3)))

    assert await unpriced.result("@0", "agent", _PROMPT) == "hi"

    used = unpriced.engine.run_usage(unpriced.run_id)
    assert (used["input_tokens"], used["output_tokens"]) == (11, 3)
    assert (used["cost_usd"], used["attempt_cost_usd"], used["cost_unknown"]) == (0.0, 0.0, True)


@pytest.mark.asyncio
async def test_get_workflow_shows_the_runs_cost_and_the_attempts(stack: WorkflowStack) -> None:
    """`get_workflow` reports both sums, so a resumed run shows what the resume itself cost."""
    stack.child_script(answer("first", _usd(0.40)), answer("more", _usd(0.10)))
    assert await stack.result("@0", "agent", _PROMPT) == "first"
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)
    assert await stack.result("@1", "agent", {"prompt": "And this."}) == "more"

    status = json.loads(await stack.call("get_workflow", run_id=stack.run_id, nodes="all"))

    shown = status["usage"]
    assert (shown["cost_usd"], shown["attempt_cost_usd"]) == (pytest.approx(0.50), pytest.approx(0.10))
    # Each call shows what it used itself.
    assert [node["usage"]["cost_usd"] for node in status["nodes"]] == [pytest.approx(0.40), pytest.approx(0.10)]


@pytest.mark.asyncio
async def test_failed_turn_without_a_model_request_writes_no_phantom_usage_row(stack: WorkflowStack) -> None:
    """An attempt-1 turn that ended before any model request ran leaves no null-cost ledger row when attempt 2 runs it."""
    stack.continues = lambda job: job.kind == "delegation"

    async def boom() -> None:
        msg = "connection closed during shutdown"
        raise RuntimeError(msg)

    stack.child_script([Await(boom), *answer("never")])
    await stack.submit("@0", "agent", _PROMPT)
    await wait_for_status(stack.jobs, stack.node_job("@0").job_id, "failed")

    await stack.restart()
    stack.child_script(answer("second try", _usd(0.20)))
    assert await stack.recover() == [stack.run_id]
    assert await stack.result("@0", "agent", _PROMPT) == "second try"

    assert _ledger(stack) == [("@0", 2, "1")]
    assert _costs(stack) == (pytest.approx(0.20), pytest.approx(0.20), False)


@pytest.mark.asyncio
async def test_completed_turn_around_restart_stays_generation_one_with_one_ledger_row(stack: WorkflowStack) -> None:
    """A job that completed in attempt 1 before settlement keeps generation 1 and one attempt-1 ledger row on replay."""
    stack.continues = lambda job: job.kind == "delegation"
    gate = asyncio.Event()
    stack.child_script([Await(gate.wait), *answer("done", _usd(0.20))])
    await stack.submit("@0", "agent", _PROMPT)
    await stack.engine.close()
    gate.set()
    job_id = stack.node_job("@0").job_id
    await wait_for_status(stack.jobs, job_id, "completed")
    # Even if attempt 1 wrote usage before crashing ahead of settle_node, attempt 2 stays idempotent.
    stack.store.add_usage(
        stack.run_id,
        "@0",
        1,
        1,
        1,
        {"input_tokens": 200_000, "output_tokens": 0, "tool_calls": 0, "cost_usd": 0.20},
    )

    await stack.restart()
    assert await stack.recover() == [stack.run_id]
    assert await stack.result("@0", "agent", _PROMPT) == "done"

    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    assert (node.state, node.generation) == (WorkflowNodeState.COMPLETED, 1)
    assert _ledger(stack) == [("@0", 1, "1")]
    assert _costs(stack) == (pytest.approx(0.20), 0.0, False)
    # A superseded epoch cannot append stale usage for a run that has moved on.
    with pytest.raises(SupersededAttemptError):
        stack.store.add_usage(stack.run_id, "@0", 2, 1, 1, {"cost_usd": 0.10}, epoch=0)
    assert _ledger(stack) == [("@0", 1, "1")]


@pytest.mark.asyncio
async def test_failed_unmetered_turn_after_a_tool_call_keeps_its_call_and_unknown_cost(
    unpriced: WorkflowStack,
) -> None:
    """An unpriced turn that answered with a tool call and no tokens before failing keeps its row and unknown cost."""

    async def boom() -> None:
        msg = "second request failed"
        raise RuntimeError(msg)

    unpriced.child_script(calls(tool_call("append", "c1", line="a")), [Await(boom), *answer("never")])

    with pytest.raises(NodeFailed) as stopped:
        await unpriced.result("@0", "agent", _PROMPT)

    assert (stopped.value.kind, unpriced.counting_tool.lines) == ("indeterminate", ["a"])
    assert _ledger(unpriced) == [("@0", 1, "1")]
    used = unpriced.engine.run_usage(unpriced.run_id)
    assert (used["tool_calls"], used["cost_usd"], used["cost_unknown"]) == (1, 0.0, True)


@pytest.mark.asyncio
async def test_cut_short_stream_with_output_and_no_usage_report_keeps_cost_unknown(stack: WorkflowStack) -> None:
    """A turn interrupted after streaming text and before usage reporting stays unknown instead of pricing at zero."""
    stack.continues = lambda job: job.kind == "delegation"
    streamed = asyncio.Event()

    async def hold() -> None:
        streamed.set()
        await asyncio.Event().wait()

    stack.child_script([TextDelta("partial output"), Await(hold), *answer("never")])
    await stack.submit("@0", "agent", _PROMPT)
    await asyncio.wait_for(streamed.wait(), 5)

    await stack.restart()
    assert await stack.recover() == [stack.run_id]
    stack.child_script(answer("continued", _usd(0.20)))
    await stack.continue_agents()
    assert await stack.result("@0", "agent", _PROMPT) == "continued"

    assert _costs(stack) == (pytest.approx(0.20), pytest.approx(0.20), True)
    assert stack.store.turn_costs(stack.run_id) == [pytest.approx(0.20)]
