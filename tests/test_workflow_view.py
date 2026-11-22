"""What people see of a workflow run: its card in the thread and its live frames (engine harness level)."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio

from mindroom.config.models import ModelPricing
from mindroom.matrix.event_types import WORKFLOW_RUN_CONTENT_KEY
from mindroom.matrix.large_messages import fits_one_event
from mindroom.script_runs.models import ScriptRunState
from mindroom.workflow_runs import view as view_module
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from tests.fakes.scripted_model import Await, answer, calls, tool_call, usage
from tests.workflow_helpers import StackRoom, WorkflowStack, set_run, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_TITLE = "Audit the invented module"
_NOW = {"op": "now"}


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """The harness with a room, and one started run that has a job and a title."""
    with workflow_stack(tmp_path, require_approval=()) as stack:
        stack.show()
        await stack.start(title=_TITLE, job_id="job-1")
        yield stack
        await stack.engine.close()


def _room(stack: WorkflowStack) -> StackRoom:
    assert stack.room is not None
    return stack.room


def _cards(stack: WorkflowStack) -> list[dict[str, Any]]:
    """Return the card of the stack's run as each post or edit of it said it, in order."""
    return _room(stack).carried(WORKFLOW_RUN_CONTENT_KEY)


async def _shown(stack: WorkflowStack) -> dict[str, Any]:
    """Build the view of the stack's run now, and return its card as the room last got it."""
    await stack.views.show(stack.run_id)
    return _cards(stack)[-1]


def _frames(stack: WorkflowStack, monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Turn live frames on, and return the list that gets what each view queues: account, room, run, meta, graph."""
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(view_module, "publish_run", lambda *args, **_options: queued.append(args))
    stack.views.live_frames = lambda: True
    return queued


@pytest.mark.asyncio
async def test_card_has_exactly_the_pinned_content_and_a_text_for_other_clients(stack: WorkflowStack) -> None:
    """The card is one notice of the run's agent in the run's thread, with the run's numbers and a plain text."""
    assert await stack.result("@0", "value", _NOW)
    card = await _shown(stack)
    event_id, event_type, content = _room(stack).events[0]

    assert card == {
        "run": stack.run_id,
        "job_id": "job-1",
        "requester": "@alice:example.test",
        "title": _TITLE,
        "status": "running",
        "counts": {
            "queued": 0,
            "running": 0,
            "waiting": 0,
            "completed": 1,
            "failed": 0,
            "indeterminate": 0,
            "cached": 0,
        },
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_calls": 0,
            "cost_usd": 0.0,
            "attempt_cost_usd": 0.0,
            "projected_cost_usd": 0.0,
            "cost_unknown": False,
        },
        "phases": [],
        "attempt": 1,
        "started_at": card["started_at"],
        "ended_at": None,
        "error": None,
        "large": False,
    }
    assert isinstance(card["started_at"], int)
    assert (event_type, content["msgtype"]) == ("m.room.message", "m.notice")
    assert content["body"] == (
        f'Workflow "{_TITLE}" running: 1 done, 0 running, 0 waiting. Stop it with !cancel_job job-1.'
    )
    assert content["m.relates_to"]["event_id"] == "$thread:example.test"
    assert stack.store.get_run(stack.run_id).card_event_id == event_id


@pytest.mark.asyncio
async def test_live_test_captures_the_card_as_each_post_and_edit_said_it(
    stack: WorkflowStack,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the fault directory of a live test set, the card's content is kept each time the room took it."""
    faults = tmp_path / "faults"
    faults.mkdir()
    monkeypatch.setenv("MINDROOM_WORKFLOW_FAULT_DIR", str(faults))
    await _shown(stack)
    await stack.finish_attempt("done")
    last = await _shown(stack)

    kept = [json.loads(line) for line in (faults / "workflow_cards.jsonl").read_text().splitlines()]
    assert [card["status"] for card in kept] == ["running", "completed"]
    assert kept[-1] == last


@pytest.mark.asyncio
async def test_card_is_posted_once_across_a_restart(stack: WorkflowStack) -> None:
    """A primary that stopped after it sent the card and before it saved the event sends under the same transaction."""
    await _shown(stack)
    set_run(stack.store, stack.run_id, card_event_id=None)
    stack = await stack.restart()
    await stack.recover()
    await _shown(stack)

    posts = [content for _, _, content in _room(stack).events if "m.new_content" not in content]
    assert len(posts) == 1
    assert stack.store.get_run(stack.run_id).card_event_id == _room(stack).events[0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["revoked", "pending"])
async def test_run_without_its_owners_access_is_shown_nothing(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
    access: str,
) -> None:
    """A card and a frame are acts of the run: while its owner's access is gone or unknown, none is sent."""
    queued = _frames(stack, monkeypatch)
    stack.access = access  # type: ignore[assignment]

    await stack.views.show(stack.run_id)

    assert (_cards(stack), queued) == ([], [])
    stack.access = None
    assert (await _shown(stack))["status"] == "running"
    assert len(queued) == 1


@pytest.mark.asyncio
async def test_frames_of_a_run_go_out_through_its_accounts_client_whatever_becomes_of_the_run(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One publisher sends every frame of an account in a room, and keeps the first client lookup it was given.

    So a run hands it the lookup an ordinary sub-agent does, by the account alone: a run whose own agent stopped
    must not end the frames of that account's other runs there.
    """
    lookups: list[Any] = []
    monkeypatch.setattr(view_module, "publish_run", lambda *_args, **options: lookups.append(options["client"]))
    stack.views.live_frames = lambda: True
    await stack.views.show(stack.run_id)
    [lookup] = lookups
    client = lookup()

    stack.views.bot = lambda _run: None

    assert client is _room(stack).client
    assert lookup() is client


@pytest.mark.asyncio
async def test_run_whose_start_was_refused_gets_no_card(stack: WorkflowStack) -> None:
    """A run that never got an attempt is shown nowhere: its call's failure says why."""
    refused = stack.reserve(title="Never started")
    await stack.views.show(refused.run_id)

    assert _cards(stack) == []


@pytest.mark.asyncio
async def test_card_is_edited_on_status_phase_and_waiting_changes_and_else_every_ten_minutes(
    stack: WorkflowStack,
) -> None:
    """The card follows the run's status, its phases and whether it waits at once, and its numbers every ten minutes."""
    await _shown(stack)
    assert len(_cards(stack)) == 1

    # A call that completed changes only numbers: no edit before ten minutes passed.
    await stack.result("@0", "value", _NOW)
    await _shown(stack)
    assert len(_cards(stack)) == 1
    stack.now += 601
    assert (await _shown(stack))["counts"]["completed"] == 1
    assert len(_cards(stack)) == 2

    # A new phase is shown at once.
    await stack.result("@1", "value", _NOW, {"phase": "Audit"})
    assert (await _shown(stack))["phases"] == ["Audit"]
    assert len(_cards(stack)) == 3

    # And so is the run's status.
    await stack.finish_attempt("done")
    last = await _shown(stack)
    assert (last["status"], len(_cards(stack))) == ("completed", 4)
    assert last["ended_at"] >= last["started_at"]
    assert "Stop it" not in _room(stack).events[-1][2]["m.new_content"]["body"]
    # Nothing changed since: no further edit.
    stack.now += 601
    await _shown(stack)
    assert len(_cards(stack)) == 4


@pytest.mark.asyncio
async def test_card_of_a_run_that_works_for_72_hours_is_edited_fewer_than_450_times(stack: WorkflowStack) -> None:
    """A run whose numbers change all the time and whose status does not edits its card six times an hour at most."""
    for step in range(72 * 12):
        if step % 2 == 0:
            await stack.result(f"@{step}", "value", _NOW)
        stack.now += 300
        await stack.views.show(stack.run_id)

    assert 400 < len(_cards(stack)) < 450


@pytest.mark.asyncio
async def test_call_that_a_resume_reused_is_counted_as_cached(stack: WorkflowStack) -> None:
    """A call whose saved result a later attempt read back is counted as completed and as cached."""
    await stack.result("@0", "value", _NOW)
    await stack.end_attempt(ScriptRunState.FAILED)
    await stack.resume(stack.run_id)
    assert (await stack.submit("@0", "value", _NOW)).cached
    await stack.result("@1", "value", _NOW)
    counts = (await _shown(stack))["counts"]

    assert (counts["completed"], counts["cached"]) == (2, 1)


@pytest.mark.asyncio
async def test_failed_run_card_carries_the_last_line_of_its_error(stack: WorkflowStack) -> None:
    """A failed run's card says why in one line: the last of its traceback, which names the error."""
    traceback = 'Traceback (most recent call last):\n  File "main.py", line 3, in main\nValueError: no topics\n'
    await stack.finish_attempt(error=traceback)
    card = await _shown(stack)

    assert (card["status"], card["error"]) == ("failed", "ValueError: no topics")


@pytest.mark.asyncio
async def test_final_card_edit_is_receivable_in_an_encrypted_room(tmp_path: Path) -> None:
    """The last edit of a large run's card carries the graph, and the whole edit event fits one event once encrypted."""
    with workflow_stack(tmp_path, require_approval=()) as stack:
        stack.show(encrypted=True)
        await stack.start(title="検査 " * 30, job_id="job-1")
        for index in range(300):
            await stack.submit(f"@{index}", "value", _NOW, {"phase": f"相 {index % 7}", "label": "検査 " * 16})
        await stack.views.show(stack.run_id)
        await stack.finish_attempt("done")
        await stack.views.show(stack.run_id)
        room = _room(stack)
        edit = room.events[-1][2]
        card = edit["m.new_content"][WORKFLOW_RUN_CONTENT_KEY]

        assert card["status"] == "completed"
        assert card["counts"]["completed"] == 300
        assert 0 < len(card["graph"]["nodes"]) < 300
        assert len(card["graph"]["nodes"]) + card["graph"]["omitted"]["completed"] == 300
        assert card["graph"]["phases"] == card["phases"]
        assert edit[WORKFLOW_RUN_CONTENT_KEY] == card
        assert fits_one_event(room.client, room.room_id, edit, room_encrypted=True)
        assert room.encrypted_size(edit) < 65_536
        await stack.engine.close()


@pytest.mark.asyncio
async def test_large_run_is_marked_once_and_its_card_says_so(stack: WorkflowStack) -> None:
    """A run with more than 25 agent calls is marked large once: one log line, and the card is edited at once."""
    await _shown(stack)
    for index in range(26):
        node = await stack.submit(f"@{index}", "agent", {"prompt": f"Look into part {index}."})
        assert node.state in ("failed", "queued", "running")
    card = await _shown(stack)
    await _shown(stack)

    assert card["large"] is True
    assert stack.store.get_run(stack.run_id).large is True
    assert stack.store.log(stack.run_id) == ["Large run: 26 agents, about $0.00 projected."]
    assert len(_cards(stack)) == 2


@pytest.mark.asyncio
async def test_running_node_usage_is_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent that still runs shows what it used so far, though its turn is in no ledger yet."""
    options = {"extra_tools": ("workflow",), "require_approval": (), "pricing": ModelPricing(input=1, output=2)}
    with workflow_stack(tmp_path, **options) as stack:
        stack.show()
        await stack.open_jobs()
        stack.script_agents(monkeypatch)
        await stack.start(title=_TITLE, job_id="job-1")
        queued = _frames(stack, monkeypatch)
        wrote, gate = asyncio.Event(), asyncio.Event()

        async def held() -> None:
            wrote.set()
            await gate.wait()

        # Held in its second request: the feed has what the first one used by then.
        first = calls(tool_call("append", "c1", line="a"), metrics=usage(100_000, 0))
        stack.child_script(first, [Await(held), *answer("done")])
        await stack.submit("@0", "agent", {"prompt": "Append a line."})
        await asyncio.wait_for(wrote.wait(), 5)
        await stack.views.show(stack.run_id)
        (row,) = queued[-1][4].nodes
        meta = queued[-1][3]

        assert (row["status"], row["agent"]) == ("running", "watcher")
        assert row["usage"] == {"input_tokens": 100_000, "output_tokens": 0, "tool_calls": 1, "cost_usd": 0.1}
        assert meta["counts"]["running"] == 1
        gate.set()
        await stack.until(lambda: stack.store.get_node(stack.run_id, "@0").state is WorkflowNodeState.COMPLETED)
        await stack.close()


@pytest.mark.asyncio
async def test_views_follow_the_writes_of_a_run_by_themselves(stack: WorkflowStack) -> None:
    """Once started, the views show each run that is written: no caller asks for a view."""
    stack.views.start()
    await stack.result("@0", "value", _NOW)
    await stack.until(lambda: len(_cards(stack)) == 1)
    await stack.finish_attempt("done")
    await stack.until(lambda: _cards(stack)[-1]["status"] == "completed")
    await stack.views.close()

    assert "graph" in _cards(stack)[-1]
    assert stack.store.on_change is None


@pytest.mark.asyncio
async def test_run_inside_a_run_gets_no_card_and_shows_as_a_group(
    stack: WorkflowStack,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested run is shown in the run around it, as a group that counts its calls."""
    await stack.submit("@0", "tool", tool_args("counter", "text", size=1))
    await stack.until(lambda: stack.store.get_node(stack.run_id, "@0").state is WorkflowNodeState.COMPLETED)
    outer = stack.run_id
    inner = stack.reserve(parent_run_id=outer, parent_node_key="@0", title="Inner")
    stack.run_id = outer
    await stack.engine.nest(inner)
    await stack.engine.launch(inner.run_id, stack.context)
    set_run(stack.store, inner.run_id, state=WorkflowRunState.RUNNING)
    published = _frames(stack, monkeypatch)
    await stack.views.show(inner.run_id)
    await stack.views.show(outer)

    assert [card["run"] for card in _cards(stack)] == [outer]
    assert [args[2] for args in published] == [outer]
    (group,) = published[0][4].groups
    assert (group["kind"], group["parent"], group["total"]) == ("workflow", "n1", 0)
