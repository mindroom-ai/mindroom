"""The one model call that titles a thread, and tags it on its first refresh."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from mindroom.agent_loop.helper import complete_prompt
from mindroom.entity_resolution import resolve_room_scoped_model_override
from mindroom.helper_usage import record_system_usage
from mindroom.llm.provider import RequestOverrides
from mindroom.providers import registry as provider_registry
from mindroom.thread_summary.state import THREAD_SUMMARY_MAX_LENGTH
from mindroom.thread_tags import AUTOMATIC_THREAD_TAG_EXCLUSIONS, coerce_tag_name
from mindroom.timing import timed
from mindroom.tool_system.events import body_without_tool_markers

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_MAX_INITIAL_TAGS = 3
_MAX_MESSAGES_BEFORE_TRUNCATION = 50
_TRUNCATION_SAMPLE_SIZE = 3


class _ThreadSummary(BaseModel):
    """Structured thread summary response."""

    summary: str = Field(
        max_length=THREAD_SUMMARY_MAX_LENGTH,
        description="One-line summary of the thread conversation",
    )


class _ThreadEnrichment(_ThreadSummary):
    """Structured second-summary response with one-shot topic tags."""

    tags: list[str] = Field(
        min_length=1,
        max_length=_MAX_INITIAL_TAGS,
        description="1-3 durable topic tags, most relevant first",
    )


@dataclass(frozen=True)
class GeneratedSummary:
    """A generated title, and the initial tags to apply when the call asked for them and any were usable."""

    summary: str
    tags: tuple[str, ...] = ()


def _summary_provider(config: Config, runtime_paths: RuntimePaths, model_name: str) -> ModelProvider:
    """Return the summary model's provider without the temperature its config authors.

    Summaries send ``defaults.thread_summary_temperature`` instead, and no temperature at all when that is None or
    the route keeps its provider's sampling default. Ollama authors sampling in ``options`` instead: it sends the
    value as ``options.temperature``, and keeps an authored ``options.temperature`` when the value is None.
    """
    model_config = config.models.get(model_name)
    if model_config is not None and model_config.extra_kwargs and "temperature" in model_config.extra_kwargs:
        extra_kwargs = {key: value for key, value in model_config.extra_kwargs.items() if key != "temperature"}
        models = {**config.models, model_name: model_config.model_copy(update={"extra_kwargs": extra_kwargs})}
        config = config.model_copy(update={"models": models})
    return provider_registry.build_model_provider(config, runtime_paths, model_name)


def resolve_thread_summary_model_name(
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str | None,
    *,
    entity_name: str | None = None,
) -> str:
    """Return the model name for automatic thread summaries in one room.

    Precedence: room-scoped override (room ID, room key, or full alias) > responding
    entity's name as a ``room_thread_summary_models`` key (covers ad-hoc
    rooms with no managed alias) > ``defaults.thread_summary_model``.
    """
    if override := resolve_room_scoped_model_override(config.room_thread_summary_models, room_id, runtime_paths):
        return override
    if entity_name and entity_name in config.room_thread_summary_models:
        return config.room_thread_summary_models[entity_name]
    return config.defaults.thread_summary_model or "default"


def _conversation_text(messages: Sequence[ResolvedVisibleMessage]) -> str:
    """Render a thread's ordinary messages as the summarizer reads them.

    For threads exceeding ``_MAX_MESSAGES_BEFORE_TRUNCATION`` messages, samples
    the first and last few messages with an omission note in between.
    """
    lines: list[str] = []
    for message in messages:
        sender = message.sender or "unknown"
        body = body_without_tool_markers(message.body, message.content)
        if body:
            lines.append(f"{sender}: {body}")

    if len(lines) > _MAX_MESSAGES_BEFORE_TRUNCATION:
        n = _TRUNCATION_SAMPLE_SIZE
        omitted = len(lines) - 2 * n
        lines = [*lines[:n], f"[... {omitted} messages omitted ...]", *lines[-n:]]

    return "\n".join(lines)


@timed("thread_summary_generation")
async def generate_summary(
    messages: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    model_name: str,
    tag_vocabulary: str | None,
) -> GeneratedSummary | None:
    """Summarize a thread's ordinary messages with ``model_name``, plus one-shot tags from ``tag_vocabulary`` when given."""
    provider = _summary_provider(config, runtime_paths, model_name)
    prompt = config.render_prompt(
        "THREAD_SUMMARY_USER_PROMPT_TEMPLATE",
        conversation=escape(_conversation_text(messages)),
        tag_vocabulary=(
            escape(tag_vocabulary)
            if tag_vocabulary is not None
            else "(tags are not requested for this summary refresh)"
        ),
    )
    response = await complete_prompt(
        provider,
        prompt,
        instructions=[config.get_prompt("THREAD_SUMMARY_INSTRUCTIONS")],
        schema=_ThreadEnrichment if tag_vocabulary is not None else _ThreadSummary,
        overrides=RequestOverrides(temperature=config.defaults.thread_summary_temperature),
    )
    await record_system_usage(response.usage, runtime_paths=runtime_paths, kind="thread_summary")
    content = response.content
    if tag_vocabulary is None:
        return GeneratedSummary(summary=content.summary) if isinstance(content, _ThreadSummary) else None
    if not isinstance(content, _ThreadEnrichment):
        return None
    tags: list[str] = []
    for raw_tag in content.tags:
        tag = coerce_tag_name(raw_tag)
        if tag is not None and tag not in AUTOMATIC_THREAD_TAG_EXCLUSIONS and tag not in tags:
            tags.append(tag)
    return GeneratedSummary(summary=content.summary, tags=tuple(tags))
