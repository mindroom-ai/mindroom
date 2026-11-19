"""The one model call that titles a thread, and tags it on its first refresh."""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import TYPE_CHECKING

import nio
from pydantic import BaseModel, Field

from mindroom.agent_loop.helper import complete_prompt
from mindroom.config.models import ModelTable, split_model_reference
from mindroom.entity_resolution import resolve_room_scoped_value
from mindroom.helper_usage import record_system_usage
from mindroom.llm.provider import RequestOverrides
from mindroom.logging_config import get_logger
from mindroom.matrix.event_types import ROOM_SETTINGS_EVENT_TYPE
from mindroom.providers import registry as provider_registry
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
_ROOM_INSTRUCTIONS_MAX_LENGTH = 2000
# Generated titles only; a person's pin may still use the whole ``THREAD_SUMMARY_MAX_LENGTH``.
GENERATED_TITLE_MAX_LENGTH = 80

logger = get_logger(__name__)


class _ThreadSummary(BaseModel):
    """Structured thread summary response."""

    summary: str = Field(
        max_length=GENERATED_TITLE_MAX_LENGTH,
        description="Short one-line title of the thread conversation",
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
    # ``name@level`` runs ``name``, so the temperature comes off ``name`` and the table keeps reading the reference.
    base_name, _level = split_model_reference(model_name)
    model_config = config.models.get(base_name)
    if model_config is not None and model_config.extra_kwargs and "temperature" in model_config.extra_kwargs:
        extra_kwargs = {key: value for key, value in model_config.extra_kwargs.items() if key != "temperature"}
        models = ModelTable(
            {**config.models, base_name: model_config.model_copy(update={"extra_kwargs": extra_kwargs})},
        )
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
    if override := resolve_room_scoped_value(config.room_thread_summary_models, room_id, runtime_paths):
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


async def room_summary_instructions(client: nio.AsyncClient, room_id: str) -> str | None:
    """Return the room admins' extra title rules, or None when unset; a failed read logs and counts as unset."""
    try:
        response = await client.room_get_state_event(room_id, ROOM_SETTINGS_EVENT_TYPE, "thread_summary_instructions")
    except Exception:
        logger.warning("Room thread summary instructions read failed", room_id=room_id, exc_info=True)
        return None
    if isinstance(response, nio.RoomGetStateEventResponse):
        value = response.content.get("value")
        return (value.strip()[:_ROOM_INSTRUCTIONS_MAX_LENGTH] or None) if isinstance(value, str) else None
    if not isinstance(response, nio.RoomGetStateEventError) or response.status_code != "M_NOT_FOUND":
        logger.warning("Room thread summary instructions read failed", room_id=room_id, error=str(response))
    return None


@timed("thread_summary_generation")
async def generate_summary(
    messages: Sequence[ResolvedVisibleMessage],
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    model_name: str,
    tag_vocabulary: str | None,
    room_instructions: str | None = None,
    current_title: str | None = None,
) -> GeneratedSummary | None:
    """Summarize a thread's ordinary messages with ``model_name``, plus one-shot tags from ``tag_vocabulary`` when given.

    ``room_instructions`` are the room admins' extra title rules, appended to the summary instructions.
    ``current_title`` is shown after the thread with the rule to repeat it unless the topic changed.
    """
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
    if current_title is not None:
        # Appended in code, so a user prompt override without the placeholder keeps working.
        current = config.render_prompt("THREAD_SUMMARY_CURRENT_TITLE_TEMPLATE", current_title=escape(current_title))
        prompt = f"{prompt}\n\n{current}"
    response = await complete_prompt(
        provider,
        prompt,
        instructions=[
            config.get_prompt("THREAD_SUMMARY_INSTRUCTIONS")
            if room_instructions is None
            else config.render_prompt(
                "THREAD_SUMMARY_ROOM_INSTRUCTIONS_TEMPLATE",
                instructions=config.get_prompt("THREAD_SUMMARY_INSTRUCTIONS"),
                room_instructions=room_instructions,
            ),
        ],
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
