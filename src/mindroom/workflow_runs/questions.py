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
# How much of the question's text the card shows: the first that fits, with the options and an answer beside it.
_TEXT_CHARS = (2000, 500, 100)
_ANSWER_CHARS = 500


def question_id(run_id: str, seq: int, generation: int) -> str:
    """Return the short ID a person answers a question by: no two runs, and no question and its rerun, share one."""
    return "q" + hashlib.sha256(f"{run_id}:{seq}:{generation}".encode()).hexdigest()[:6]


def question_content(
    run: WorkflowRun,
    node: WorkflowNode,
    status: Literal["open", "answered", "cancelled"] = "open",
    answer: str | None = None,
    chars: int = _TEXT_CHARS[0],
) -> dict[str, Any]:
    """Return what the card of the `ask` call *node* says: the question, its options, and where it stands.

    The card shows the first *chars* characters of the question and the start of a long answer; the call keeps
    both whole.
    """
    options = [str(option) for option in node.request.get("options") or ()]
    return {
        "run": run.run_id,
        "node": f"n{node.seq}",
        "question_id": question_id(run.run_id, node.seq, node.generation),
        "requester": run.owner_user_id,
        "text": clip(str(node.request.get("question")), chars),
        "options": options,
        "free_text": not options,
        "status": status,
        "answer": None if answer is None else clip(answer, _ANSWER_CHARS),
    }


def options_text(options: list[str]) -> str:
    """Return a question's options as a numbered line, which `!answer` takes the numbers of."""
    return " ".join(f"{number}. {option}" for number, option in enumerate(options, 1)) + "."


def _message(run: WorkflowRun, content: dict[str, Any]) -> dict[str, Any]:
    """Return the card as a notice in the run's thread, with a text for a client that does not draw it."""
    text, qid, options = f"Question {content['question_id']}: {content['text']}", content["question_id"], ""
    if content["status"] == "open":
        options = f" {options_text(content['options'])}" if content["options"] else ""
        text += f"{options} Answer with !answer {qid} <{'number or text' if options else 'text'}>."
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
    for chars in _TEXT_CHARS:
        message = _message(run, question_content(run, node, status, answer, chars))
        if notice_fits(client, run, message, event_id):
            break
    posted = f"wfq-{run.run_id}-{node.seq}-{node.generation}"
    return await send_notice(client, run, message, event_id=event_id, transaction_id=posted, retries=retries)
