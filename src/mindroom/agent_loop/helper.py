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
from mindroom.llm.messages import Message, ModelProvenance, read_model_provenance, usage_sums_attempts
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
    reply = turn.message
    provenance = read_model_provenance(reply.provider_data)
    fallback_identity = turn.identity or provider.identity
    answering_id = provenance.model_id if provenance is not None else fallback_identity.id
    answering_provider = provenance.provider if provenance is not None else fallback_identity.provider
    capabilities = turn.capabilities or provider.capabilities
    metrics = RunMetrics(duration=perf_counter() - started)
    for prior in turn.prior_usage:
        metrics.add(prior.metrics, model_id=prior.identity.id, provider=prior.identity.provider)
    metrics.add(turn.usage, model_id=answering_id, provider=answering_provider)
    text = turn.content if schema is None else structured_answer_text(turn, capabilities)
    lenient = capabilities.structured_output == "json_mode"
    parsed = None if schema is None else parse_structured(text, schema, lenient=lenient)
    if schema is not None and parsed is None:
        logger.warning("helper_structured_output_unparsed", schema=schema.__name__)
    request_usages: list[RequestUsage] = [
        RequestUsage(
            created_at=reply.created_at,
            metrics=prior.metrics,
            model=ModelProvenance(model_id=prior.identity.id, provider=prior.identity.provider),
        )
        for prior in turn.prior_usage
        if not prior.sums_attempts
    ]
    if not usage_sums_attempts(reply.provider_data):
        answering_provenance = provenance or (
            ModelProvenance(model_id=answering_id, provider=answering_provider) if turn.identity is not None else None
        )
        request_usages.append(RequestUsage(created_at=reply.created_at, metrics=turn.usage, model=answering_provenance))
    usage = HelperUsage(
        run_id=run_id or str(uuid4()),
        model=answering_id,
        provider=answering_provider,
        metrics=metrics,
        requests=tuple(request_usages),
    )
    return HelperResult(content=text if parsed is None else parsed, usage=usage)


__all__ = ["HelperResult", "complete_prompt"]
