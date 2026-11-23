"""A person's answer to a workflow run's question: which answers count, a client's answer event, and `!answer`."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal, cast

from mindroom.matrix.client_delivery import send_message_result
from mindroom.matrix.event_types import WORKFLOW_ANSWER_REJECTED_EVENT_TYPE
from mindroom.workflow_runs.models import WorkflowNodeState
from mindroom.workflow_runs.questions import options_text, question_id
from mindroom.workflow_runs.store import WorkflowRunNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode

type AnswerOutcome = Literal[
    "accepted",
    "not_for_you",
    "wrong_place",
    "no_longer_open",
    "already_answered",
    "not_an_option",
]
_ANSWER = re.compile(r"(?:(q[0-9a-f]{6})\s+)?(.+)", re.DOTALL)


def _open(node: WorkflowNode | None) -> bool:
    """Return whether *node* is a question that waits and that somebody was shown: one whose card was posted."""
    waits = node is not None and (node.state, node.waiting_on) == (WorkflowNodeState.WAITING, "user")
    return waits and node.kind == "ask" and "event_id" in node.links


async def answer_question(
    engine: WorkflowEngine,
    *,
    run_id: str,
    node_id: str,
    sender: str,
    room_id: str,
    thread_id: str | None,
    card_event_id: str | None,
    option: str | None,
    text: str | None,
) -> AnswerOutcome:
    """Answer the question of one `ask` call of a run, and return whether the answer counted or why it did not.

    It counts only from the run's requester, in the run's room and thread, for the card of the call as it is now,
    while the call waits. A question with options takes one of them as *option*, any other one free *text*.
    """
    try:
        run = engine.store.get_run(run_id)
    except WorkflowRunNotFoundError:
        return "no_longer_open"
    node = engine.store.node_by_id(run_id, node_id)
    if sender != run.owner_user_id or (room_id, thread_id) != (run.room_id, run.thread_root_event_id):
        return "not_for_you" if sender != run.owner_user_id else "wrong_place"
    if node is None or node.kind != "ask" or card_event_id != node.links.get("event_id"):
        return "no_longer_open"
    if node.state is WorkflowNodeState.COMPLETED:
        return "already_answered"
    options = node.request.get("options") or ()
    answer = option if options else (text or "").strip()
    if not answer or (option is not None and not options) or (options and answer not in options):
        return "not_an_option" if _open(node) else "no_longer_open"
    return "accepted" if await engine.answer(run_id, node.key, node.generation, answer) else "no_longer_open"


async def handle_workflow_answer(
    room: nio.MatrixRoom,
    event: nio.UnknownEvent,
    *,
    agent_name: str,
    engine: Callable[[], WorkflowEngine],
) -> AnswerOutcome | None:
    """Answer the question a client's answer event names, and tell its sender when the answer did not count.

    The event is `{run, node, option | text}` in the run's thread, as a reply to the question's card. Every bot in
    the room gets it: only the bot that serves the run acts (the others return None), and sends `{run, node, reason}`
    as a reply to an answer that was not accepted.
    """
    content, sender, workflows = event.source.get("content"), event.source.get("sender"), engine()
    if not isinstance(content, dict) or not isinstance(sender, str):
        return None
    try:
        run = workflows.store.get_run(str(content.get("run")))
    except WorkflowRunNotFoundError:
        return None
    if workflows.served_by(run) != agent_name:
        return None
    relation = content.get("m.relates_to")
    relation = relation if isinstance(relation, dict) else {}
    card = relation.get("m.in_reply_to")
    option, text = content.get("option"), content.get("text")
    outcome = await answer_question(
        workflows,
        run_id=run.run_id,
        node_id=str(content.get("node")),
        sender=sender,
        room_id=room.room_id,
        thread_id=relation.get("event_id") if relation.get("rel_type") == "m.thread" else None,
        card_event_id=card.get("event_id") if isinstance(card, dict) else None,
        option=option if isinstance(option, str) else None,
        text=text if isinstance(text, str) else None,
    )
    if outcome != "accepted" and (client := workflows.poster(run)) is not None:
        rejection = {
            "run": run.run_id,
            "node": content.get("node"),
            "reason": outcome,
            "m.relates_to": {**relation, "m.in_reply_to": {"event_id": event.event_id}},
        }
        kind = WORKFLOW_ANSWER_REJECTED_EVENT_TYPE
        await send_message_result(client, room.room_id, rejection, message_type=kind, operation="reject_answer")
    return outcome


def _chosen(found: dict[str, tuple[str, str] | None], text: str) -> tuple[str, tuple[str, str], str] | str:
    """Return the ID and the place of the open question that `!answer`'s *text* answers, and the answer; else why none."""
    match = _ANSWER.fullmatch(text)
    named, answer = (match.group(1), match.group(2).strip()) if match else (None, "")
    if not answer or (named is None and _ANSWER.fullmatch(f"{answer} x").group(1)):  # type: ignore[union-attr]
        # No answer at all, or an ID alone: an ID is no answer to the only open question.
        return "Use !answer <id> <answer>."
    if named is None and len(found) != 1:
        several = f"Several questions are open: {', '.join(found)}. Use !answer <id> <answer>."
        return several if found else "No open question for you in this thread."
    qid = named or next(iter(found))
    if (asked := found.get(qid)) is None:
        shared = "Several questions are open. Answer on the card of the one you mean."
        return shared if qid in found else f"Question {qid} is no longer open."
    return qid, asked, answer


async def answer_command(engine: WorkflowEngine, *, room_id: str, thread_id: str | None, sender: str, text: str) -> str:
    """Answer an open question of *sender* in this thread for `!answer [id] <answer>`, and return the reply.

    The ID names one question; without it the answer goes to the sender's only open question here. An option is
    answered by its number or its text.
    """
    found: dict[str, tuple[str, str] | None] = {}
    runs = engine.store.list_runs(owner_user_id=sender, room_id=room_id, include_finished=False)
    for run in runs:
        for node in engine.store.nodes(run.run_id, light=True) if run.thread_root_event_id == thread_id else ():
            if _open(node):
                qid = question_id(run.run_id, node.seq, node.generation)
                # An ID that two open questions share names neither: nothing is answered by a guess.
                found[qid] = None if qid in found else (run.run_id, node.key)
    chosen = _chosen(found, text.strip())
    if isinstance(chosen, str):
        return chosen
    qid, (run_id, key), answer = chosen
    node = cast("WorkflowNode", engine.store.get_node(run_id, key))
    options = [str(option) for option in node.request.get("options") or ()]
    if options:
        number = int(answer) if len(answer) <= 6 and answer.isdecimal() else 0
        answer = options[number - 1] if 0 < number <= len(options) else answer
        if answer not in options:
            return f"Answer with one of: {options_text(options)}"
    if not await engine.answer(run_id, node.key, node.generation, answer):
        return f"Question {qid} is no longer open."
    return f"Answered {qid}: {answer}."
