"""One-shot helper model calls: one request without the caller's tools, optionally read into a structured-output schema.

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

from mindroom.agent_loop.prompt import join_sections
from mindroom.agent_loop.structured import output_format_prompt, parse_structured, structured_answer_text
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides, complete
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.llm.usage import RunMetrics
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.provider import ModelProvider

logger = get_logger(__name__)

_NO_OVERRIDES = RequestOverrides()


@dataclass(frozen=True, slots=True, kw_only=True)
class HelperResult[T: BaseModel]:
    """One helper call's answer and the run record its usage is saved from.

    ``content`` is the answer read into the requested schema, or its text when no schema was requested or the text
    does not validate, so callers check ``isinstance``.
    """

    content: T | str
    record: RunRecord


async def complete_prompt[T: BaseModel](
    provider: ModelProvider,
    prompt: str,
    *,
    instructions: Sequence[str] = (),
    schema: type[T] | None = None,
    overrides: RequestOverrides = _NO_OVERRIDES,
    run_id: str | None = None,
) -> HelperResult[T]:
    """Send ``prompt`` as one request under a system message of the caller's ``instructions``, each as written.

    The instructions join with one blank line between them. Returns the answer with a completed run record.
    ``schema`` asks for structured output the way ``provider`` supports it: a ``forced_tool`` route answers with a
    forced tool call, and a JSON-mode route also gets the JSON prompt at the end of the system message.
    """
    system = join_sections((*instructions, output_format_prompt(provider.capabilities, schema)))
    messages = [*([Message(role="system", content=system)] if system else []), Message(role="user", content=prompt)]
    started = perf_counter()
    turn = await complete(
        provider,
        ModelRequest(
            messages=tuple(messages),
            output_schema=schema,
            overrides=overrides,
        ),
    )
    identity = provider.identity
    metrics = RunMetrics(duration=perf_counter() - started)
    metrics.add(turn.usage, model_id=identity.id, provider=identity.provider_name)
    text = turn.content if schema is None else structured_answer_text(turn, provider.capabilities)
    lenient = provider.capabilities.structured_output == "json_mode"
    parsed = None if schema is None else parse_structured(text, schema, lenient=lenient)
    if schema is not None and parsed is None:
        logger.warning("helper_structured_output_unparsed", schema=schema.__name__)
    record = RunRecord(
        run_id=run_id or str(uuid4()),
        status=RunStatus.completed,
        content=text or None,
        model=identity.id,
        model_provider=identity.provider,
        messages=[*messages, turn.message],
        metrics=metrics,
    )
    return HelperResult(content=text if parsed is None else parsed, record=record)


__all__ = ["HelperResult", "complete_prompt"]
