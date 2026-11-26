"""Workflow notices, question cards, and answers (client answer events and `!answer`).

The one way a run's card and its questions reach the room. Every notice is content a homeserver signs, so it
holds no float, and it is measured as the edit it is or will get, which carries its content twice. Also covers
a question card's text and edits, which answers count, a client's answer event, and `!answer`.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import TYPE_CHECKING, Any, Literal, cast

from mindroom.budgets.pricing import format_cost_usd
from mindroom.matrix.client_delivery import build_edit_event_content, edit_message_result, send_message_result
from mindroom.matrix.event_types import WORKFLOW_ANSWER_REJECTED_EVENT_TYPE, WORKFLOW_QUESTION_CONTENT_KEY
from mindroom.matrix.large_messages import fits_one_event
from mindroom.matrix.message_builder import build_message_content
from mindroom.workflow_runs.models import WorkflowNodeState
from mindroom.workflow_runs.store import WorkflowRunNotFoundError

if TYPE_CHECKING:
    from collections.abc import Callable

    import nio

    from mindroom.workflow_runs.engine import WorkflowEngine
    from mindroom.workflow_runs.models import WorkflowNode, WorkflowRun

type _AnswerOutcome = Literal[
    "accepted",
    "not_for_you",
    "wrong_place",
    "no_longer_open",
    "already_answered",
    "not_an_option",
]

# How long a send that failed waits before each further try: a homeserver that restarts, or a rate limit.
RETRY_SECONDS = (0.5, 2.0)
# What a question may offer, so that its card always is one event: a card cannot cut an option short, since the
# answer is the option's own text.
MAX_OPTIONS, MAX_OPTION_BYTES = 20, 200
# What stands for a notice's event while one that was not posted yet is measured as the edit it will get.
_NO_EVENT = "$" + "0" * 43
_CUT = "... [truncated]"
# How much of the question, answer and options the card shows: (chars, answer_chars, option_chars, body_option_chars).
_ANSWER_CHARS = 500
_ANSWER = re.compile(r"(?:(q[0-9a-f]{6})\s+)?(.+)", re.DOTALL)
_OPEN_FITS: tuple[tuple[int, int, int | None, int | None], ...] = (
    (2000, _ANSWER_CHARS, None, None),
    (500, _ANSWER_CHARS, None, None),
    (100, _ANSWER_CHARS, None, None),
    (100, _ANSWER_CHARS, None, 40),
    (100, _ANSWER_CHARS, None, 0),
)
_CLOSED_FITS: tuple[tuple[int, int, int | None, int | None], ...] = (
    (2000, 500, None, None),
    (500, 500, None, None),
    (100, 500, None, None),
    (100, 200, 80, None),
    (100, 100, 40, None),
    (100, 50, 20, None),
    (50, 30, 0, None),
)


def clip(text: str, chars: int) -> str:
    """Return *text* in at most *chars* characters, with a mark at the end of one that was cut."""
    if len(text) <= chars:
        return text
    return text[: chars - len(_CUT)] + _CUT if chars > len(_CUT) else text[:chars]


def _signable(value: object) -> object:
    """Return *value* as an event a homeserver signs may hold it: a float as a decimal string of six places at most."""
    if isinstance(value, float):
        return format_cost_usd(value)
    if isinstance(value, dict):
        return {name: _signable(part) for name, part in value.items()}
    return [_signable(part) for part in value] if isinstance(value, list | tuple) else value


def notice(run: WorkflowRun, text: str, key: str, content: dict[str, Any]) -> dict[str, Any]:
    """Return a notice in the run's thread that says *text* and carries *content* under *key*."""
    return build_message_content(
        text,
        thread_event_id=run.thread_root_event_id,
        latest_thread_event_id=run.thread_root_event_id,
        extra_content={"msgtype": "m.notice", key: _signable(content)},
    )


def notice_fits(
    client: nio.AsyncClient,
    run: WorkflowRun,
    message: dict[str, Any],
    event_id: str | None,
    *,
    as_edit: bool = True,
) -> bool:
    """Return whether *message* is one event in the run's room, as an edit of *event_id* when *as_edit*."""
    room = client.rooms.get(run.room_id)
    payload = (
        build_edit_event_content(event_id=event_id or _NO_EVENT, new_content=message, new_text=message["body"])
        if as_edit
        else message
    )
    return fits_one_event(client, run.room_id, payload, room_encrypted=room is None or bool(room.encrypted))


async def send_notice(
    client: nio.AsyncClient,
    run: WorkflowRun,
    message: dict[str, Any],
    *,
    event_id: str | None = None,
    transaction_id: str | None = None,
    retries: tuple[float, ...] = RETRY_SECONDS,
) -> str | None:
    """Post *message*, or edit the notice at *event_id* to say it, and return the notice's event.

    A post is sent under *transaction_id*, so one that is sent again is the same event. A send that fails is tried
    again after each of *retries*; None says that it could not be sent now, and the caller's next look sends it.
    """
    for wait in (*retries, None):
        if event_id is None:
            sent = await send_message_result(
                client,
                run.room_id,
                message,
                operation="send_workflow_notice",
                transaction_id=transaction_id,
            )
        else:
            sent = await edit_message_result(client, run.room_id, event_id, message, message["body"])
        if sent is not None:
            return event_id or sent.event_id
        if wait is not None:
            await asyncio.sleep(wait)
    return None


def question_id(run_id: str, seq: int, generation: int) -> str:
    """Return the short ID a person answers a question by: no two runs, and no question and its rerun, share one."""
    return "q" + hashlib.sha256(f"{run_id}:{seq}:{generation}".encode()).hexdigest()[:6]


def options_text(options: list[str]) -> str:
    """Return a question's options as a numbered line, which `!answer` takes the numbers of."""
    return " ".join(f"{number}. {option}" for number, option in enumerate(options, 1)) + "."


def _clip_options(options: list[str], limit: int | None) -> list[str]:
    if limit is None:
        return options
    return [] if limit == 0 else [clip(option, limit) for option in options]


def _question_content(
    run: WorkflowRun,
    node: WorkflowNode,
    status: Literal["open", "answered", "cancelled"] = "open",
    answer: str | None = None,
    chars: int = 2000,
    *,
    answer_chars: int = _ANSWER_CHARS,
    option_chars: int | None = None,
) -> dict[str, Any]:
    """Return what the card of the `ask` call *node* says: the question, its options, and where it stands.

    The card shows the first *chars* characters of the question and the start of a long answer; the call keeps
    both whole. A closed edit may also clip its repeated options to fit one encrypted event.
    """
    given = node.request.get("options")
    raw_options = [str(option) for option in given] if isinstance(given, list | tuple) else []
    return {
        "run": run.run_id,
        "node": f"n{node.seq}",
        "question_id": question_id(run.run_id, node.seq, node.generation),
        "requester": run.owner_user_id,
        "text": clip(str(node.request.get("question")), chars),
        "options": _clip_options(raw_options, option_chars),
        "free_text": not raw_options,
        "status": status,
        "answer": None if answer is None else clip(answer, answer_chars),
    }


def _question_message(
    run: WorkflowRun,
    content: dict[str, Any],
    *,
    body_option_chars: int | None = None,
) -> dict[str, Any]:
    """Return the card as a notice in the run's thread, with a text for a client that does not draw it."""
    qid = content["question_id"]
    text = f"Question {qid}: {content['text']}"
    if content["status"] == "open":
        shown = _clip_options(content["options"], body_option_chars)
        options = f" {options_text(shown)}" if shown else ""
        hint = "number or text" if content["options"] else "text"
        text += f"{options} Answer with !answer {qid} <{hint}>."
    else:
        text += f" Answered: {content['answer']}." if content["status"] == "answered" else " Cancelled."
    return notice(run, text, WORKFLOW_QUESTION_CONTENT_KEY, content)


async def show_question(
    client: nio.AsyncClient,
    run: WorkflowRun,
    node: WorkflowNode,
    status: Literal["open", "answered", "cancelled"] = "open",
    answer: str | None = None,
    retries: tuple[float, ...] = (),
) -> str | None:
    """Post the card of a question, or edit it to say how the question ended; return its event, None when not sent.

    The card is posted under a transaction ID made from the run, the call and its generation, so a primary that
    stopped before it saved the event sends again and gets the same event. It shows as much of the question as
    keeps it one event.
    """
    as_edit = status != "open"
    event_id = node.links.get("event_id") if as_edit else None
    if as_edit and not isinstance(event_id, str):
        return None
    for chars, answer_chars, option_chars, body_option_chars in _CLOSED_FITS if as_edit else _OPEN_FITS:
        content = _question_content(
            run,
            node,
            status,
            answer,
            chars,
            answer_chars=answer_chars,
            option_chars=option_chars,
        )
        message = _question_message(run, content, body_option_chars=body_option_chars)
        if notice_fits(client, run, message, event_id, as_edit=as_edit):
            break
    txn_id = f"wfq-{run.run_id}-{node.seq}-{node.generation}"
    return await send_notice(client, run, message, event_id=event_id, transaction_id=txn_id, retries=retries)


def _open(node: WorkflowNode | None) -> bool:
    """Return whether *node* is a question that waits and that somebody was shown: one whose card was posted."""
    waits = node is not None and (node.state, node.waiting_on) == (WorkflowNodeState.WAITING, "user")
    return waits and node.kind == "ask" and "event_id" in node.links


def _options(node: WorkflowNode) -> list[str]:
    raw = node.request.get("options")
    return [str(option) for option in raw] if isinstance(raw, list | tuple) else []


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
) -> _AnswerOutcome:
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
    options = _options(node)
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
) -> _AnswerOutcome | None:
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
        await send_message_result(
            client,
            room.room_id,
            rejection,
            message_type=WORKFLOW_ANSWER_REJECTED_EVENT_TYPE,
            operation="reject_answer",
        )
    return outcome


async def answer_command(engine: WorkflowEngine, *, room_id: str, thread_id: str | None, sender: str, text: str) -> str:
    """Answer an open question of *sender* in this thread for `!answer [id] <answer>`, and return the reply.

    The ID names one question; without it the answer goes to the sender's only open question here. An option is
    answered by its number or its text.
    """
    match = _ANSWER.fullmatch(text.strip())
    named, answer = (match.group(1), match.group(2).strip()) if match else (None, "")
    probe = _ANSWER.fullmatch(f"{answer} x") if named is None and answer else None
    if not answer or (probe is not None and probe.group(1)):
        return "Use !answer <id> <answer>."
    matches = [
        (qid, run.run_id, node.key)
        for run in engine.store.list_runs(owner_user_id=sender, room_id=room_id, include_finished=False)
        if run.thread_root_event_id == thread_id
        for node in engine.store.nodes(run.run_id, light=True)
        if _open(node) and named in {None, (qid := question_id(run.run_id, node.seq, node.generation))}
    ]
    if not matches:
        return f"Question {named} is no longer open." if named else "No open question for you in this thread."
    if len(matches) > 1:
        return (
            "Several questions are open. Use !answer <id> <answer> with the ID shown on the card."
            if named is None
            else "Several questions are open. Answer on the card of the one you mean."
        )
    qid, run_id, key = matches[0]
    node = cast("WorkflowNode", engine.store.get_node(run_id, key))
    options = _options(node)
    if options:
        number = int(answer) if len(answer) <= 6 and answer.isdecimal() else 0
        answer = options[number - 1] if 0 < number <= len(options) else answer
        if answer not in options:
            return f"Answer with one of: {options_text(options)}"
    if not await engine.answer(run_id, node.key, node.generation, answer):
        return f"Question {qid} is no longer open."
    return f"Answered {qid}: {answer}."
