"""Tests for what a workflow run cost: the usage ledger and its two sums, the whole run and the current attempt.

Level: engine harness (`WorkflowStack`) with the real job runtime and scripted agents on a priced model.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.config.models import ModelPricing
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs import agent_nodes
from mindroom.workflow_runs import engine as engine_module
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call, usage
from tests.workflow_helpers import WorkflowStack, workflow_stack

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
    monkeypatch.setattr(engine_module, "_RECHECK_SECONDS", 0.05)
    monkeypatch.setattr(agent_nodes, "_RECHECK_SECONDS", 0.05)
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
