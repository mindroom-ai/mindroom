"""A workflow run's questions to its requester: the card, the answers that count, and `!answer` (engine harness)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import nio
import pytest
import pytest_asyncio

from mindroom.matrix.event_types import (
    WORKFLOW_ANSWER_EVENT_TYPE,
    WORKFLOW_ANSWER_REJECTED_EVENT_TYPE,
    WORKFLOW_QUESTION_CONTENT_KEY,
)
from mindroom.workflow_runs.answers import answer_command, answer_question, handle_workflow_answer
from mindroom.workflow_runs.models import WorkflowNodeState, WorkflowRunState
from mindroom.workflow_runs.questions import question_id
from tests.workflow_helpers import StackRoom, WorkflowStack, tool_args, workflow_stack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

_ROOM = "!room:example.test"
_THREAD = "$thread:example.test"
_ALICE = "@alice:example.test"
_DEPLOY = {"question": "Deploy?\nIt is Friday.", "options": ["Yes", "No"]}
_WHEN = {"question": "When shall it run?", "options": None}


@pytest_asyncio.fixture
async def stack(tmp_path: Path) -> AsyncIterator[WorkflowStack]:
    """The harness with a room, and one started run that lets one call work at a time."""
    with workflow_stack(tmp_path, require_approval=()) as stack:
        stack.show()
        await stack.start(max_concurrent=1)
        yield stack
        await stack.engine.close()


def _room(stack: WorkflowStack) -> StackRoom:
    assert stack.room is not None
    return stack.room


def _cards(stack: WorkflowStack) -> list[dict[str, Any]]:
    """Return each question card as every post or edit of one said it, in order."""
    return _room(stack).carried(WORKFLOW_QUESTION_CONTENT_KEY)


def _posts(stack: WorkflowStack) -> list[tuple[str, dict[str, Any]]]:
    """Return the event ID and the content of each question card as it was posted, without its edits."""
    posts = [(event_id, content) for event_id, _, content in _room(stack).events if "m.new_content" not in content]
    return [(event_id, content) for event_id, content in posts if WORKFLOW_QUESTION_CONTENT_KEY in content]


async def _ask(stack: WorkflowStack, key: str = "@0", request: dict[str, Any] = _DEPLOY) -> dict[str, Any]:
    """Make an `ask_user` call of the stack's run and return what answers its card: the IDs a client sends."""
    receipt = await stack.submit(key, "ask", request)
    assert receipt.state == "waiting"
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    place = {"run_id": stack.run_id, "node_id": receipt.id, "sender": _ALICE, "room_id": _ROOM, "thread_id": _THREAD}
    return {**place, "card_event_id": node.links["event_id"]}


async def _answer(stack: WorkflowStack, card: dict[str, Any], **answer: object) -> str:
    answer = {"option": None, "text": None, **answer}
    return await answer_question(stack.engine, **{**card, **answer})  # type: ignore[arg-type]


def _node_state(stack: WorkflowStack, key: str = "@0") -> tuple[WorkflowNodeState, object]:
    node = stack.store.get_node(stack.run_id, key)
    assert node is not None
    return node.state, node.waiting_on


@pytest.mark.asyncio
async def test_ask_waits_then_returns_the_chosen_option(stack: WorkflowStack) -> None:
    """The question is one card in the run's thread; the requester's answer completes the call with the option."""
    card = await _ask(stack)
    run = stack.store.get_run(stack.run_id)
    qid = question_id(stack.run_id, 1, 1)
    (event_id, content), asked = _posts(stack)[0], _cards(stack)[0]

    assert asked == {
        "run": stack.run_id,
        "node": "n1",
        "question_id": qid,
        "requester": _ALICE,
        "text": "Deploy?\nIt is Friday.",
        "options": ["Yes", "No"],
        "free_text": False,
        "status": "open",
        "answer": None,
    }
    assert (qid[0], len(qid), event_id) == ("q", 7, card["card_event_id"])
    assert content["body"] == (
        f"Question {qid}: Deploy?\nIt is Friday. 1. Yes 2. No. Answer with !answer {qid} <number or text>."
    )
    assert (content["msgtype"], content["m.relates_to"]["event_id"]) == ("m.notice", _THREAD)
    assert _node_state(stack) == (WorkflowNodeState.WAITING, "user")
    assert run.state is WorkflowRunState.WAITING

    assert await _answer(stack, card, option="Yes") == "accepted"

    assert _cards(stack)[-1] == {**asked, "status": "answered", "answer": "Yes"}
    assert (
        _room(stack).events[-1][2]["m.new_content"]["body"] == f"Question {qid}: Deploy?\nIt is Friday. Answered: Yes."
    )
    assert await stack.result("@0", "ask", _DEPLOY) == "Yes"
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.RUNNING


@pytest.mark.asyncio
async def test_free_text_question_takes_any_text_and_no_option(stack: WorkflowStack) -> None:
    """A question without options is answered in the requester's words."""
    card = await _ask(stack, request=_WHEN)
    qid = question_id(stack.run_id, 1, 1)

    assert (_cards(stack)[0]["options"], _cards(stack)[0]["free_text"]) == ([], True)
    assert _posts(stack)[0][1]["body"] == f"Question {qid}: When shall it run? Answer with !answer {qid} <text>."
    assert await _answer(stack, card, option="Yes") == "not_an_option"
    assert await _answer(stack, card, text="  ") == "not_an_option"
    assert await _answer(stack, card, text="after lunch") == "accepted"
    assert await stack.result("@0", "ask", _WHEN) == "after lunch"


@pytest.mark.asyncio
async def test_restart_between_posting_and_saving_posts_one_card(stack: WorkflowStack) -> None:
    """A primary that sent the card and stopped before it saved the event sends again and gets the same event."""
    card = await _ask(stack)
    run = stack.store.get_run(stack.run_id)
    stack.store.update_node(stack.run_id, "@0", 1, run.epoch, links={})
    stack = await stack.restart()
    await stack.recover()

    again = await _ask(stack)

    assert len(_posts(stack)) == 1
    assert again["card_event_id"] == card["card_event_id"]


@pytest.mark.asyncio
async def test_answer_during_a_restart_is_returned_without_asking_twice(stack: WorkflowStack) -> None:
    """An answer that comes while no attempt runs is saved on the call, and the next attempt reads it back."""
    card = await _ask(stack, request=_WHEN)
    stack = await stack.restart()

    assert await _answer(stack, card, text="after lunch") == "accepted"
    await stack.recover()
    receipt = await stack.submit("@0", "ask", _WHEN)

    assert (receipt.state, receipt.cached, receipt.result) == ("completed", True, "after lunch")
    assert len(_posts(stack)) == 1


@pytest.mark.parametrize(
    ("change", "outcome"),
    [
        ({"sender": "@mallory:example.test"}, "not_for_you"),
        ({"room_id": "!other:example.test"}, "wrong_place"),
        ({"thread_id": "$other:example.test"}, "wrong_place"),
        ({"card_event_id": "$card-of-generation-1"}, "no_longer_open"),
        ({"option": "Maybe"}, "not_an_option"),
        ({"option": None, "text": "Yes, but later"}, "not_an_option"),
    ],
)
@pytest.mark.asyncio
async def test_rejected_answers_change_nothing_and_say_why(
    stack: WorkflowStack,
    change: dict[str, Any],
    outcome: str,
) -> None:
    """An answer counts only from the requester, in the run's room and thread, to the open card, with an option."""
    card = await _ask(stack)

    assert await _answer(stack, {**card, **change}, **{"option": "Yes", **change}) == outcome

    assert _node_state(stack) == (WorkflowNodeState.WAITING, "user")
    assert _cards(stack)[-1]["status"] == "open"


def _answer_event(card: dict[str, Any], content: dict[str, object], sender: str = _ALICE) -> nio.UnknownEvent:
    relation = {"rel_type": "m.thread", "event_id": _THREAD, "m.in_reply_to": {"event_id": card["card_event_id"]}}
    source = {
        "type": WORKFLOW_ANSWER_EVENT_TYPE,
        "sender": sender,
        "event_id": "$answer",
        "origin_server_ts": 1,
        "content": {},
    }
    event = nio.UnknownEvent.from_dict(source)
    assert isinstance(event, nio.UnknownEvent)
    event.source["content"] = {"run": card["run_id"], "node": card["node_id"], "m.relates_to": relation, **content}
    return event


@pytest.mark.asyncio
async def test_answer_event_answers_the_card_and_a_rejected_one_gets_the_rejection_event(stack: WorkflowStack) -> None:
    """A client's answer event names the run, the node and the card; one that does not count is told why, once."""
    card = await _ask(stack)
    room = nio.MatrixRoom(_ROOM, "@mindroom_watcher:example.test")
    handle = {"agent_name": stack.context.agent_name, "engine": lambda: stack.engine}

    # Every bot in the room gets the event: only the run's agent acts on it.
    assert (
        await handle_workflow_answer(room, _answer_event(card, {"option": "Yes"}), **{**handle, "agent_name": "x"})
        is None
    )
    assert await handle_workflow_answer(room, _answer_event(card, {"option": "Maybe"}), **handle) == "not_an_option"
    rejected = [content for _, kind, content in _room(stack).events if kind == WORKFLOW_ANSWER_REJECTED_EVENT_TYPE]
    assert rejected == [
        {
            "run": stack.run_id,
            "node": "n1",
            "reason": "not_an_option",
            "m.relates_to": {"rel_type": "m.thread", "event_id": _THREAD, "m.in_reply_to": {"event_id": "$answer"}},
        },
    ]
    assert await handle_workflow_answer(room, _answer_event(card, {"option": "No"}), **handle) == "accepted"
    assert await stack.result("@0", "ask", _DEPLOY) == "No"
    assert await handle_workflow_answer(room, _answer_event(card, {"option": "Yes"}), **handle) == "already_answered"


@pytest.mark.asyncio
async def test_answer_command_answers_by_id_by_number_and_alone(stack: WorkflowStack) -> None:
    """`!answer` takes an option's number or its text, for the question it names or the only one that is open."""
    await _ask(stack)
    await _ask(stack, "@1", _WHEN)
    first, second = question_id(stack.run_id, 1, 1), question_id(stack.run_id, 2, 1)
    here = {"room_id": _ROOM, "thread_id": _THREAD, "sender": _ALICE}

    several = await answer_command(stack.engine, text="2", **here)
    assert several == (
        f"Several questions are open: {first} (Deploy?), {second} (When shall it run?). Use !answer <id> <answer>."
    )
    assert _node_state(stack) == (WorkflowNodeState.WAITING, "user")
    assert await answer_command(stack.engine, text=f"{first} 3", **here) == "Answer with one of: 1. Yes 2. No."
    assert await answer_command(stack.engine, text=f"{first} 2", **here) == f"Answered {first}: No."
    assert await answer_command(stack.engine, text=f"{first} 1", **here) == f"Question {first} is no longer open."
    assert await answer_command(stack.engine, text="q000000 1", **here) == "Question q000000 is no longer open."
    assert await answer_command(stack.engine, text="after lunch", **here) == f"Answered {second}: after lunch."
    assert await answer_command(stack.engine, text="again", **here) == "No open question for you in this thread."
    assert await answer_command(stack.engine, text="1", **{**here, "sender": "@mallory:example.test"}) == (
        "No open question for you in this thread."
    )
    assert await stack.result("@0", "ask", _DEPLOY) == "No"
    assert await stack.result("@1", "ask", _WHEN) == "after lunch"


@pytest.mark.asyncio
async def test_two_runs_ask_under_different_ids_and_a_replaced_question_answers_nothing(stack: WorkflowStack) -> None:
    """A question and the one that replaces it never share an ID, nor do two runs: a late answer answers nothing."""
    first_run = stack.run_id
    await stack.start()
    other = await _ask(stack)
    stack.run_id = first_run
    old = await _ask(stack)
    assert _cards(stack)[0]["question_id"] != _cards(stack)[1]["question_id"]
    assert await _answer(stack, other, option="No") == "accepted"
    # The next attempt asks something else at the same place: another call, with a card of its own.
    stack = await stack.restart()
    await stack.recover()
    new = await _ask(stack, request={**_DEPLOY, "question": "Deploy now?"})

    replaced = [card for card in _cards(stack) if card["question_id"] == question_id(first_run, 1, 1)]
    assert [card["status"] for card in replaced] == ["open", "cancelled"]
    assert question_id(first_run, 1, 2) == _cards(stack)[-1]["question_id"]
    assert await _answer(stack, old, option="Yes") == "no_longer_open"
    assert await _answer(stack, new, option="Yes") == "accepted"


@pytest.mark.asyncio
async def test_another_lane_keeps_running_while_a_question_is_open(stack: WorkflowStack) -> None:
    """A call that waits for an answer holds no place: the run's one working place is free for the next call."""
    card = await _ask(stack)

    assert await stack.result("@1", "tool", tool_args("counter", "append", line="a")) == "appended"
    assert stack.store.get_run(stack.run_id).state is WorkflowRunState.WAITING
    assert await _answer(stack, card, option="No") == "accepted"
    assert await _answer(stack, card, option="Yes") == "already_answered"


@pytest.mark.asyncio
async def test_cancel_closes_open_cards(stack: WorkflowStack) -> None:
    """A run that ends closes its open questions: the card says so, the call ends cancelled, an answer comes late."""
    card = await _ask(stack)

    await stack.engine.cancel(stack.run_id, reason="Stopped.")

    assert (_cards(stack)[-1]["status"], _node_state(stack)[0]) == ("cancelled", WorkflowNodeState.CANCELLED)
    assert await _answer(stack, card, option="Yes") == "no_longer_open"
    node = stack.store.get_node(stack.run_id, "@0")
    assert node is not None
    assert node.error == {"kind": "cancelled", "message": "The run ended before the question was answered."}


@pytest.mark.asyncio
async def test_question_that_cannot_be_posted_is_asked_again_by_the_script(stack: WorkflowStack) -> None:
    """While the card cannot be sent the call is not saved as asked: the script's SDK asks again."""
    _room(stack).refuse_sends = True
    with pytest.raises(Exception, match="could not be posted"):
        await stack.submit("@0", "ask", _DEPLOY)
    _room(stack).refuse_sends = False

    await _ask(stack)

    assert len(_posts(stack)) == 1
