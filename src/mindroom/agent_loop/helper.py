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
from mindroom.llm.messages import Message, read_model_provenance, usage_sums_attempts
from mindroom.llm.provider import ModelRequest, RequestOverrides, complete
from mindroom.llm.usage import HelperUsage, RequestUsage, RunMetrics
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.llm.provider import ModelProvider

logger = get_logger(__name__)

_NO_OVERRIDES = RequestOverrides()


@dataclass(frozen=True, slots=True, kw_only=True)
class HelperResult[T: BaseModel]:
    """One helper call's answer and what the call used.

    ``content`` is the answer read into the requested schema, or its text when no schema was requested or the text
    does not validate, so callers check ``isinstance``.
    """

    content: T | str
    usage: HelperUsage


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

    The instructions join with one blank line between them. Returns the answer with the call's usage, under ``run_id``
    when given, which the caller records (``helper_usage.py``).
    ``schema`` asks for structured output the way ``provider`` supports it: a ``forced_tool`` route answers with a
    forced tool call, and a JSON-mode route also gets the JSON prompt at the end of the system message.
    """
    system = join_sections((*instructions, output_format_prompt(provider.capabilities, schema)))
    messages = (*([Message(role="system", content=system)] if system else []), Message(role="user", content=prompt))
    started = perf_counter()
    turn = await complete(
        provider,
        ModelRequest(
            messages=messages,
            output_schema=schema,
            overrides=overrides,
        ),
    )
    identity = provider.identity
    metrics = RunMetrics(duration=perf_counter() - started)
    metrics.add(turn.usage, model_id=identity.id, provider=identity.provider)
    text = turn.content if schema is None else structured_answer_text(turn, provider.capabilities)
    lenient = provider.capabilities.structured_output == "json_mode"
    parsed = None if schema is None else parse_structured(text, schema, lenient=lenient)
    if schema is not None and parsed is None:
        logger.warning("helper_structured_output_unparsed", schema=schema.__name__)
    reply = turn.message
    requests: tuple[RequestUsage, ...] = ()
    if not usage_sums_attempts(reply.provider_data):
        model = read_model_provenance(reply.provider_data)
        requests = (RequestUsage(created_at=reply.created_at, metrics=turn.usage, model=model),)
    usage = HelperUsage(
        run_id=run_id or str(uuid4()),
        model=identity.id,
        provider=identity.provider,
        metrics=metrics,
        requests=requests,
    )
    return HelperResult(content=text if parsed is None else parsed, usage=usage)


__all__ = ["HelperResult", "complete_prompt"]
