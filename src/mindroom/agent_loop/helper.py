"""One-shot helper model calls: one request without tools, optionally read into a structured-output schema.

Routing, team-mode selection, schedule parsing, thread summaries, room topics, knowledge-graph extraction, memory
flushes, and voice normalization each send one request like this.
Provider errors propagate as ``mindroom.llm.errors`` exceptions instead of becoming an errored run.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import BaseModel

from mindroom.agent_loop.structured import parse_structured
from mindroom.llm.provider import ModelRequest, RequestOverrides, complete
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.agent_loop.structured import ResponseFormat
    from mindroom.llm.messages import Message
    from mindroom.llm.provider import ModelProvider

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class HelperResult[T: BaseModel]:
    """One helper call's answer and the run record its usage is saved from.

    ``content`` is the answer read into the requested schema, or its text when no schema was requested or the text
    does not validate, so callers check ``isinstance`` as they did with Agno.
    """

    content: T | str
    record: RunRecord


async def complete_once[T: BaseModel](
    provider: ModelProvider,
    messages: Sequence[Message],
    *,
    response_format: ResponseFormat[T] | None = None,
    overrides: RequestOverrides | None = None,
    run_id: str | None = None,
) -> HelperResult[T]:
    """Send ``messages`` as one request and return the answer with a completed run record.

    ``response_format`` comes from ``structured.select_response_format``; its prompt must already end the system
    message.
    """
    started = perf_counter()
    turn = await complete(
        provider,
        ModelRequest(
            messages=tuple(messages),
            response_format=None if response_format is None else response_format.request_format,
            overrides=overrides or RequestOverrides(),
            stream=False,
        ),
    )
    identity = provider.identity
    metrics = RunMetrics(duration=perf_counter() - started)
    metrics.add(turn.usage, model_id=identity.id, provider=identity.provider_name)
    text = turn.content
    parsed = None if response_format is None else parse_structured(text, response_format.schema)
    if response_format is not None and parsed is None:
        logger.warning("helper_structured_output_unparsed", schema=response_format.schema.__name__)
    record = RunRecord(
        run_id=run_id or str(uuid4()),
        status=RunStatus.completed,
        content=parsed.model_dump(mode="json", exclude_none=True) if parsed is not None else text or None,
        model=identity.id,
        model_provider=identity.provider,
        messages=[*messages, turn.message],
        metrics=metrics,
    )
    return HelperResult(content=text if parsed is None else parsed, record=record)


__all__ = ["HelperResult", "complete_once"]
