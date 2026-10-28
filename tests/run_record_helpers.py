"""Expected run records for tests that assert what a run stored, as the typed values the agent loop writes.

A record's message and requirement ids are random, and its timestamps and run timings are clock readings, so a test
compares the ``settled`` copies of the record it got and the record it expects; every other field compares exactly.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.runs import RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import RunMetrics

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics

# The provider every scripted model of these tests reports.
PROVIDER = "Synthetic"
# What MindRoom's calculator answers for ``multiply(6, 7)``, the call the storage goldens' paused runs wait on.
PRODUCT = '{"operation": "multiplication", "result": 42.0}'


def reply(
    content: str | None,
    *,
    model: str,
    metrics: MessageMetrics,
    calls: Sequence[dict[str, Any]] = (),
) -> Message:
    """Return the assistant message ``model`` answered with: its text, its tool calls, and that request's usage."""
    return Message(
        role="assistant",
        content=content,
        tool_calls=list(calls) or None,
        provider_data=model_provenance(model, PROVIDER),
        metrics=metrics,
    )


def _called(call: dict[str, Any]) -> tuple[str, str, Any]:
    function = call["function"]
    return call["id"], function["name"], json.loads(function["arguments"])


def result(call: dict[str, Any], content: str, *, is_error: bool | None = False) -> Message:
    """Return the tool message answering ``call``, a tool call as ``tests.fakes.scripted_model.tool_call`` gives it.

    ``is_error`` is None for an externally executed call's result, which records no outcome of its own.
    """
    call_id, name, arguments = _called(call)
    return Message(
        role="tool",
        content=content,
        tool_call_id=call_id,
        tool_name=name,
        tool_args=arguments,
        tool_call_error=is_error,
    )


def execution(call: dict[str, Any], **fields: Any) -> ToolExecution:  # noqa: ANN401 - ToolExecution's own field types
    """Return the tool execution the run records for ``call``, with ``fields`` set, such as its result."""
    call_id, name, arguments = _called(call)
    return ToolExecution(tool_call_id=call_id, tool_name=name, tool_args=arguments, **fields)


def run_metrics(*requests: tuple[str, MessageMetrics], start: RunMetrics | None = None) -> RunMetrics:
    """Return a run's usage after ``requests``, each the model that answered it and its usage, counted onto ``start``.

    ``start`` is the usage a resumed run carried over from its pause; it is left unchanged.
    """
    metrics = RunMetrics() if start is None else deepcopy(start)
    for model, usage in requests:
        metrics.add(usage, model_id=model, provider=PROVIDER)
    return metrics


def waiting_call(paused: AnyRunRecord) -> tuple[dict[str, Any], ToolExecution]:
    """Return the one call ``paused`` waits on, as its last message asked for it, and its tool execution."""
    [call] = paused.messages[-1].tool_calls or ()
    [waiting] = paused.tools or ()
    return call, waiting


def approved(waiting: ToolExecution, answer: str) -> ToolExecution:
    """Return ``waiting`` after its approval, once it ran and answered ``answer``."""
    return replace(waiting, is_error=False, result=answer, requires_confirmation=False, confirmed=True)


def denied(waiting: ToolExecution, reason: str) -> ToolExecution:
    """Return ``waiting`` after its denial for ``reason``, which answers the model as a failed call."""
    return replace(waiting, is_error=True, requires_confirmation=False, confirmed=False, confirmation_note=reason)


def resumed[RecordT: AnyRunRecord](
    paused: RecordT,
    *,
    decided: ToolExecution,
    added: Sequence[Message],
    answered: tuple[str, MessageMetrics],
    **requirement_fields: str | bool,
) -> RecordT:
    """Return ``paused`` resumed into a completed run.

    Its one waiting call ends as ``decided``, its requirement records that call and ``requirement_fields``, ``added``
    follows its messages, and ``answered`` is the final reply's text and usage, from the model that ran it.
    """
    [requirement] = paused.requirements or ()
    text, metrics = answered
    assert paused.model is not None
    return replace(
        paused,
        status=RunStatus.completed,
        content=text,
        messages=[*paused.messages, *added, reply(text, model=paused.model, metrics=metrics)],
        tools=[decided],
        requirements=[replace(requirement, tool_execution=decided, **requirement_fields)],
        metrics=run_metrics((paused.model, metrics), start=paused.metrics),
    )


def as_stored[RecordT: AnyRunRecord](record: RecordT) -> RecordT:
    """Return a finished ``record`` as the store holds it: without its system messages."""
    return replace(record, messages=[message for message in record.messages if message.role != "system"])


def settled[RecordT: AnyRunRecord](record: RecordT) -> RecordT:
    """Return a copy of ``record`` whose random ids and clock readings are fixed, its member runs' too."""
    copy = deepcopy(record)
    copy.created_at = 0
    for message in copy.messages:
        message.id, message.created_at = "", 0
    for requirement in copy.requirements or ():
        requirement.id = ""
    if copy.metrics is not None:
        copy.metrics.duration = copy.metrics.time_to_first_token = None
    if isinstance(copy, TeamRunRecord):
        copy.member_responses = [settled(member) for member in copy.member_responses]
    return copy
