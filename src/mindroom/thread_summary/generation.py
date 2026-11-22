"""The one model call that titles a thread, and tags it on its first refresh."""

from __future__ import annotations

import asyncio
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
from mindroom.room_settings import state_event_content
from mindroom.thread_summary.notices import normalize_thread_summary_text
from mindroom.thread_tags import AUTOMATIC_THREAD_TAG_EXCLUSIONS, coerce_tag_name
from mindroom.timing import timed
from mindroom.tool_system.events import body_without_tool_markers

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage

_MAX_INITIAL_TAGS = 3
_MAX_MESSAGES_BEFORE_TRUNCATION = 50
_TRUNCATION_EDGE_COUNT = 5
_TRUNCATION_MIDDLE_COUNT = 12
_MAX_MESSAGE_CHARS = 600
_CLIP_TAIL_CHARS = 250
_CLIP_SEPARATOR = " … "
_ROOM_INSTRUCTIONS_MAX_LENGTH = 2000
# Each room's `thread_summary_model` setting that names no configured model, which is logged once.
_unknown_room_models_logged: set[tuple[Path, str, str]] = set()
# A new generated title over these after normalization goes back to the model; a person's pin may use
# ``THREAD_SUMMARY_MAX_LENGTH``.
GENERATED_TITLE_MAX_LENGTH = 80
GENERATED_TITLE_MAX_WORDS = 6
# One answer plus two retries that each see the rejected title and why.
_TITLE_ATTEMPTS = 3

logger = get_logger(__name__)


class _ThreadSummary(BaseModel):
    """Structured thread summary response."""

    title: str = Field(description="Short one-line title of the thread conversation")
    # ``default_factory``: strict OpenAI schemas refuse a ``default`` keyword.
    keep_current: bool = Field(
        default_factory=bool,
        description="True to keep the thread's current title, when one is shown",
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
    keep_current: bool = False


def title_error(title: str) -> str | None:
    """Return why a normalized title breaks the short-title limits, or None; emoji and dashes are not words."""
    words = sum(any(char.isalnum() for char in token) for token in title.split())
    if not title:
        return "The title is empty."
    if words > GENERATED_TITLE_MAX_WORDS or len(title) > GENERATED_TITLE_MAX_LENGTH:
        return (
            f"The title has {words} words and {len(title)} characters; the limit is "
            f"{GENERATED_TITLE_MAX_WORDS} words and {GENERATED_TITLE_MAX_LENGTH} characters."
        )
    return None


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
    """Return the configured model name for automatic thread summaries in one room.

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

    Threads at or under ``_MAX_MESSAGES_BEFORE_TRUNCATION`` messages are rendered
    verbatim. Longer threads sample the first and last ``_TRUNCATION_EDGE_COUNT``
    messages plus ``_TRUNCATION_MIDDLE_COUNT`` evenly spaced middle messages
    with omission notes for each gap, clipping overlong bodies to
    ``_MAX_MESSAGE_CHARS`` characters while keeping both head and tail.
    """
    entries: list[tuple[str, str]] = []
    for message in messages:
        sender = message.sender or "unknown"
        body = body_without_tool_markers(message.body, message.content)
        if body:
            entries.append((sender, body))

    total = len(entries)
    if total <= _MAX_MESSAGES_BEFORE_TRUNCATION:
        return "\n".join(f"{sender}: {body}" for sender, body in entries)

    edge = _TRUNCATION_EDGE_COUNT
    span = total - 2 * edge + 1
    middle_indices = [
        edge - 1 + round(step * span / (_TRUNCATION_MIDDLE_COUNT + 1))
        for step in range(1, _TRUNCATION_MIDDLE_COUNT + 1)
    ]
    selected = sorted({*range(edge), *middle_indices, *range(total - edge, total)})
    head_chars = _MAX_MESSAGE_CHARS - _CLIP_TAIL_CHARS - len(_CLIP_SEPARATOR)
    sampled: list[str] = []
    prev = -1
    for index in selected:
        omitted = index - prev - 1
        if omitted > 0:
            unit = "message" if omitted == 1 else "messages"
            sampled.append(f"[... {omitted} {unit} omitted ...]")
        sender, body = entries[index]
        stripped = body.strip()
        if len(stripped) > _MAX_MESSAGE_CHARS:
            head = stripped[:head_chars].rstrip()
            tail = stripped[-_CLIP_TAIL_CHARS:].lstrip()
            stripped = f"{head}{_CLIP_SEPARATOR}{tail}"
        sampled.append(f"{sender}: {stripped}")
        prev = index
    return "\n".join(sampled)


async def _room_title_setting(client: nio.AsyncClient, room_id: str, key: str) -> str | None:
    """Return one of the room's title settings as the room holds it now; unset or unreadable is None, logged if unread."""
    try:
        response = await client.room_get_state_event(room_id, ROOM_SETTINGS_EVENT_TYPE, key)
    except Exception:
        logger.warning("Room thread summary setting read failed", room_id=room_id, setting=key, exc_info=True)
        return None
    if (content := await state_event_content(response)) is not None:
        value = content.get("value")
        return (value.strip() or None) if isinstance(value, str) else None
    if not isinstance(response, nio.RoomGetStateEventError) or response.status_code != "M_NOT_FOUND":
        logger.warning("Room thread summary setting read failed", room_id=room_id, setting=key, error=str(response))
    return None


async def room_title_settings(
    client: nio.AsyncClient,
    config: Config,
    runtime_paths: RuntimePaths,
    room_id: str,
) -> tuple[str | None, str | None]:
    """Return the room's title model, `name` or `name@level`, and its admins' extra title rules, read at once.

    Each is None when unset or unreadable; a failed read logs. A `thread_summary_model` that names no configured model
    is None too, logged once, so the configured summary model applies.
    """
    model_name, instructions = await asyncio.gather(
        _room_title_setting(client, room_id, "thread_summary_model"),
        _room_title_setting(client, room_id, "thread_summary_instructions"),
    )
    if model_name is not None and model_name not in config.models:
        if (seen := (runtime_paths.storage_root, room_id, model_name)) not in _unknown_room_models_logged:
            _unknown_room_models_logged.add(seen)
            logger.warning("Room thread summary model ignored", room_id=room_id, model=model_name)
        model_name = None
    return model_name, instructions and instructions[:_ROOM_INSTRUCTIONS_MAX_LENGTH]


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
    ``current_title`` is shown after the thread, and the system rules let the model keep it unless the topic changed.
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
    instructions = [
        config.get_prompt("THREAD_SUMMARY_INSTRUCTIONS")
        if room_instructions is None
        else config.render_prompt(
            "THREAD_SUMMARY_ROOM_INSTRUCTIONS_TEMPLATE",
            instructions=config.get_prompt("THREAD_SUMMARY_INSTRUCTIONS"),
            room_instructions=room_instructions,
        ),
    ]
    if current_title is not None:
        # Appended in code, so a user prompt override without the placeholder keeps working.
        current = config.render_prompt("THREAD_SUMMARY_CURRENT_TITLE_TEMPLATE", current_title=escape(current_title))
        prompt = f"{prompt}\n\n{current}"
        instructions.append(config.get_prompt("THREAD_SUMMARY_KEEP_INSTRUCTIONS"))
    schema = _ThreadEnrichment if tag_vocabulary is not None else _ThreadSummary
    request = prompt
    error = None
    for _attempt in range(_TITLE_ATTEMPTS):
        response = await complete_prompt(
            provider,
            request,
            instructions=instructions,
            schema=schema,
            overrides=RequestOverrides(temperature=config.defaults.thread_summary_temperature),
        )
        await record_system_usage(response.usage, runtime_paths=runtime_paths, kind="thread_summary")
        content = response.content
        if not isinstance(content, schema):
            return None
        # A kept title is the current one, so only a new title is checked.
        if current_title is not None and content.keep_current:
            break
        error = title_error(normalize_thread_summary_text(content.title))
        if error is None:
            break
        # The rejected answer and the reason go back with the same request, so any route can retry.
        request = f"{prompt}\n\n" + config.render_prompt(
            "THREAD_SUMMARY_REJECTED_TITLE_TEMPLATE",
            rejected_title=escape(content.title),
            error=error,
        )
    else:
        logger.warning("Thread title rejected on every attempt", attempts=_TITLE_ATTEMPTS, error=error)
        return None
    if not isinstance(content, _ThreadEnrichment):
        return GeneratedSummary(summary=content.title, keep_current=content.keep_current)
    tags: list[str] = []
    for raw_tag in content.tags:
        tag = coerce_tag_name(raw_tag)
        if tag is not None and tag not in AUTOMATIC_THREAD_TAG_EXCLUSIONS and tag not in tags:
            tags.append(tag)
    return GeneratedSummary(summary=content.title, tags=tuple(tags), keep_current=content.keep_current)
