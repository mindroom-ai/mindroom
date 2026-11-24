"""Workflow notices, question cards, and answers (client answer events and `!answer`)."""

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

type AnswerOutcome = Literal[
    "accepted",
    "not_for_you",
    "wrong_place",
    "no_longer_open",
    "already_answered",
    "not_an_option",
]

RETRY_SECONDS = (0.5, 2.0)
MAX_OPTIONS, MAX_OPTION_BYTES = 20, 200
_NO_EVENT = "$" + "0" * 43
_CUT = "... [truncated]"
_ANSWER_CHARS = 500
_ANSWER = re.compile(r"(?:(q[0-9a-f]{6})\s+)?(.+)", re.DOTALL)
# (question_chars, answer_chars, option_chars, body_option_chars) ladders to fit one encrypted Matrix event.
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
    """Return *text* in at most *chars* characters, marking truncation when cut."""
    if len(text) <= chars:
        return text
    return text[: chars - len(_CUT)] + _CUT if chars > len(_CUT) else text[:chars]


def _signable(value: object) -> object:
    """Convert floats to canonical decimal strings so homeservers can sign the event."""
    if isinstance(value, float):
        return format_cost_usd(value)
    if isinstance(value, dict):
        return {name: _signable(part) for name, part in value.items()}
    return [_signable(part) for part in value] if isinstance(value, list | tuple) else value


def notice(run: WorkflowRun, text: str, key: str, content: dict[str, Any]) -> dict[str, Any]:
    """Build a thread notice carrying signable *content* under *key*."""
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
    """Return whether *message* fits one Matrix event in *run*'s room (measured as an edit when *as_edit*)."""
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
    """Post *message* under *transaction_id* or edit *event_id*, retrying after *retries*; return its event ID."""
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
    """Return the short ID (`q<6 hex>`) scoped to one run, node sequence, and generation."""
    return "q" + hashlib.sha256(f"{run_id}:{seq}:{generation}".encode()).hexdigest()[:6]


def options_text(options: list[str]) -> str:
    """Return a numbered option line accepted by `!answer`."""
    return " ".join(f"{number}. {option}" for number, option in enumerate(options, 1)) + "."


def _clip_options(options: list[str], limit: int | None) -> list[str]:
    if limit is None:
        return options
    return [] if limit == 0 else [clip(option, limit) for option in options]


def question_content(
    run: WorkflowRun,
    node: WorkflowNode,
    status: Literal["open", "answered", "cancelled"] = "open",
    answer: str | None = None,
    chars: int = 2000,
    *,
    answer_chars: int = _ANSWER_CHARS,
    option_chars: int | None = None,
) -> dict[str, Any]:
    """Build the question card payload for *node* clipped to the given limits."""
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
    """Post an open question card (idempotent via transaction ID) or edit an existing card to its closed state."""
    as_edit = status != "open"
    event_id = node.links.get("event_id") if as_edit else None
    if as_edit and not isinstance(event_id, str):
        return None
    for chars, answer_chars, option_chars, body_option_chars in _CLOSED_FITS if as_edit else _OPEN_FITS:
        content = question_content(
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
    """Return whether *node* is a posted question still waiting for a person."""
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
    """Validate and record one requester answer to an open question card."""
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
    """Handle a client `io.mindroom.workflow_answer` event on the bot serving the run, replying on rejection."""
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
    """Handle `!answer [id] <answer>` for *sender* in the current thread and return the command reply."""
    match = _ANSWER.fullmatch(text.strip())
    named, answer = (match.group(1), match.group(2).strip()) if match else (None, "")
    if not answer or (named is None and _ANSWER.fullmatch(f"{answer} x").group(1)):  # type: ignore[union-attr]
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
    options = [str(option) for option in node.request.get("options") or ()]
    if options:
        number = int(answer) if len(answer) <= 6 and answer.isdecimal() else 0
        answer = options[number - 1] if 0 < number <= len(options) else answer
        if answer not in options:
            return f"Answer with one of: {options_text(options)}"
    if not await engine.answer(run_id, node.key, node.generation, answer):
        return f"Question {qid} is no longer open."
    return f"Answered {qid}: {answer}."
