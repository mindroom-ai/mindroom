"""A workflow run's question to its requester: the card in the run's thread, its text for other clients, its edits."""

from __future__ import annotations

import hashlib
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from mindroom.matrix.client_delivery import edit_message_result, send_message_result
from mindroom.matrix.event_types import WORKFLOW_QUESTION_CONTENT_KEY
from mindroom.matrix.message_builder import build_message_content

if TYPE_CHECKING:
    import nio

    from mindroom.workflow_runs.models import WorkflowNode, WorkflowRun


def question_id(run_id: str, seq: int, generation: int) -> str:
    """Return the short ID a person answers a question by: no two runs, and no question and its rerun, share one."""
    return "q" + hashlib.sha256(f"{run_id}:{seq}:{generation}".encode()).hexdigest()[:6]


def question_content(
    run: WorkflowRun,
    node: WorkflowNode,
    status: Literal["open", "answered", "cancelled"] = "open",
    answer: str | None = None,
) -> dict[str, Any]:
    """Return what the card of the `ask` call *node* says: the question, its options, and where it stands."""
    options = [str(option) for option in node.request.get("options") or ()]
    return {
        "run": run.run_id,
        "node": f"n{node.seq}",
        "question_id": question_id(run.run_id, node.seq, node.generation),
        "requester": run.owner_user_id,
        "text": str(node.request.get("question")),
        "options": options,
        "free_text": not options,
        "status": status,
        "answer": answer,
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
    return build_message_content(
        text,
        thread_event_id=run.thread_root_event_id,
        latest_thread_event_id=run.thread_root_event_id,
        extra_content={"msgtype": "m.notice", WORKFLOW_QUESTION_CONTENT_KEY: content},
    )


async def post_question(client: nio.AsyncClient, run: WorkflowRun, node: WorkflowNode) -> str | None:
    """Send the card of a question and return its event, None when it could not be sent.

    The transaction ID is made from the run, the call and its generation, so a primary that stopped before it saved
    the event sends again and gets the same event.
    """
    message = _message(run, question_content(run, node))
    send = partial(send_message_result, client, run.room_id, message, operation="send_workflow_question")
    delivered = await send(transaction_id=f"wfq-{run.run_id}-{node.seq}-{node.generation}")
    return None if delivered is None else delivered.event_id


async def close_question(
    client: nio.AsyncClient,
    run: WorkflowRun,
    node: WorkflowNode,
    status: Literal["answered", "cancelled"],
    answer: str | None = None,
) -> None:
    """Edit the card of a question that was answered or that nobody can answer any more; best effort."""
    event_id = node.links.get("event_id")
    if isinstance(event_id, str):
        message = _message(run, question_content(run, node, status, answer))
        await edit_message_result(client, run.room_id, event_id, message, message["body"])
