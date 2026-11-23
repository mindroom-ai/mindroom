"""A workflow run's question to its requester: the card in the run's thread, its text for other clients, its edits."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any, Literal

from mindroom.matrix.event_types import WORKFLOW_QUESTION_CONTENT_KEY
from mindroom.workflow_runs.notices import clip, notice, notice_fits, send_notice

if TYPE_CHECKING:
    import nio

    from mindroom.workflow_runs.models import WorkflowNode, WorkflowRun

# What a question may offer, so that its card always is one event: a card cannot cut an option short, since the
# answer is the option's own text.
MAX_OPTIONS, MAX_OPTION_BYTES = 20, 200
# How much of the question, answer and options the card shows: the first candidate that fits one event.
_TEXT_CHARS = (2000, 500, 100)
_ANSWER_CHARS = 500
_OPEN_FITS: tuple[tuple[int, int | None], ...] = ((2000, None), (500, None), (100, None), (100, 40), (100, 0))
_CLOSED_FITS: tuple[tuple[int, int, int | None], ...] = (
    (2000, 500, None),
    (500, 500, None),
    (100, 500, None),
    (100, 200, 80),
    (100, 100, 40),
    (100, 50, 20),
    (50, 30, 0),
)


def question_id(run_id: str, seq: int, generation: int) -> str:
    """Return the short ID a person answers a question by: no two runs, and no question and its rerun, share one."""
    return "q" + hashlib.sha256(f"{run_id}:{seq}:{generation}".encode()).hexdigest()[:6]


def question_content(
    run: WorkflowRun,
    node: WorkflowNode,
    status: Literal["open", "answered", "cancelled"] = "open",
    answer: str | None = None,
    chars: int = _TEXT_CHARS[0],
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
    if option_chars is None:
        options = raw_options
    elif option_chars == 0:
        options = []
    else:
        options = [clip(option, option_chars) for option in raw_options]
    return {
        "run": run.run_id,
        "node": f"n{node.seq}",
        "question_id": question_id(run.run_id, node.seq, node.generation),
        "requester": run.owner_user_id,
        "text": clip(str(node.request.get("question")), chars),
        "options": options,
        "free_text": not raw_options,
        "status": status,
        "answer": None if answer is None else clip(answer, answer_chars),
    }


def options_text(options: list[str]) -> str:
    """Return a question's options as a numbered line, which `!answer` takes the numbers of."""
    return " ".join(f"{number}. {option}" for number, option in enumerate(options, 1)) + "."


def _message(run: WorkflowRun, content: dict[str, Any], *, body_option_chars: int | None = None) -> dict[str, Any]:
    """Return the card as a notice in the run's thread, with a text for a client that does not draw it."""
    text, qid = f"Question {content['question_id']}: {content['text']}", content["question_id"]
    if content["status"] == "open":
        shown = content["options"]
        if body_option_chars == 0:
            shown = []
        elif body_option_chars is not None:
            shown = [clip(option, body_option_chars) for option in shown]
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
    event_id = node.links.get("event_id") if status != "open" else None
    if status != "open" and not isinstance(event_id, str):
        return None
    if status == "open":
        for chars, body_option_chars in _OPEN_FITS:
            content = question_content(run, node, status, answer, chars)
            message = _message(run, content, body_option_chars=body_option_chars)
            if notice_fits(client, run, message, None, as_edit=False):
                break
    else:
        for chars, answer_chars, option_chars in _CLOSED_FITS:
            content = question_content(
                run,
                node,
                status,
                answer,
                chars,
                answer_chars=answer_chars,
                option_chars=option_chars,
            )
            message = _message(run, content)
            if notice_fits(client, run, message, event_id):
                break
    posted = f"wfq-{run.run_id}-{node.seq}-{node.generation}"
    return await send_notice(client, run, message, event_id=event_id, transaction_id=posted, retries=retries)
