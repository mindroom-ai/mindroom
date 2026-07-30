"""Internal AI execution helpers kept off the public ``mindroom.ai`` seam."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast
from uuid import uuid4

from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.history_run_visibility import is_model_history_visible_run
from mindroom.logging_config import get_logger
from mindroom.media_fallback import append_inline_media_fallback_prompt
from mindroom.media_inputs import MediaInputs, MediaKind

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.media import Audio, File, Image, Video
    from agno.models.base import Model

    from mindroom.history.runtime import ScopeSessionContext

__all__ = [
    "EMPTY_RESPONSE_NOTICE",
    "ModelRunInput",
    "append_inline_media_fallback_to_run_input",
    "attach_media_to_run_input",
    "cached_agent_run",
    "copy_run_input",
    "discard_empty_completed_run",
    "finalize_queued_notice_response_turn_async",
    "install_queued_message_notice_hook",
    "is_empty_completed_run",
    "media_inputs_from_run_input",
    "next_retry_run_id",
    "note_attempt_run_id",
    "queued_message_signal_context",
    "register_queued_notice_storage",
    "scrub_queued_notice_session_context",
]

logger = get_logger(__name__)

type ModelRunInput = str | Sequence[Message]

_QUEUED_MESSAGE_NOTICE_MARKER_KEY = "mindroom_queued_message_notice"
_QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER = "persisted"
_QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY = "mindroom_queued_message_notice_response_turn_id"
_QUEUED_MESSAGE_NOTICE_HOOK_ATTR = "_mindroom_queued_message_notice_hook_installed"

EMPTY_RESPONSE_NOTICE = "The model returned an empty response — please try again."


def _normalize_run_input(run_input: ModelRunInput) -> list[Message]:
    """Coerce legacy string input into canonical provider messages."""
    if isinstance(run_input, str):
        return [Message(role="user", content=run_input)]
    return [message.model_copy(deep=True) for message in run_input]


def copy_run_input(run_input: ModelRunInput) -> list[Message]:
    """Deep-copy canonical run input so retries can mutate safely."""
    return _normalize_run_input(run_input)


def attach_media_to_run_input(
    run_input: ModelRunInput,
    media_inputs: MediaInputs,
) -> list[Message]:
    """Attach media to the current user message."""
    run_messages = copy_run_input(run_input)
    current_message = run_messages[-1]
    current_message.audio = media_inputs.audio
    current_message.images = media_inputs.images
    current_message.files = media_inputs.files
    current_message.videos = media_inputs.videos
    return run_messages


def media_inputs_from_run_input(run_input: ModelRunInput) -> MediaInputs:
    """Collect media attached to canonical run-input messages.

    Agent and team paths inspect the collected kinds for media-capability
    routing while preserving media on its canonical message.
    """
    if isinstance(run_input, str):
        return MediaInputs()
    audio: list[Audio] = []
    images: list[Image] = []
    files: list[File] = []
    videos: list[Video] = []
    for message in run_input:
        audio.extend(message.audio or ())
        images.extend(message.images or ())
        files.extend(message.files or ())
        videos.extend(message.videos or ())
    return MediaInputs.from_optional(audio=audio, images=images, files=files, videos=videos)


def append_inline_media_fallback_to_run_input(
    run_input: ModelRunInput,
    *,
    fallback_prompt: str,
    removed_kinds: frozenset[MediaKind],
) -> list[Message]:
    """Strip rejected media kinds from all run-input messages and append the fallback note."""
    run_messages = copy_run_input(run_input)
    for message in run_messages:
        if "audio" in removed_kinds:
            message.audio = None
        if "image" in removed_kinds:
            message.images = None
        if "file" in removed_kinds:
            message.files = None
        if "video" in removed_kinds:
            message.videos = None
    current_message = run_messages[-1]
    current_text = current_message.content if isinstance(current_message.content, str) else ""
    current_message.content = append_inline_media_fallback_prompt(current_text, fallback_prompt=fallback_prompt)
    return run_messages


class _SupportsQueuedMessageState(Protocol):
    def has_pending_human_messages(self) -> bool: ...


@dataclass
class _QueuedMessageNoticeContext:
    state: _SupportsQueuedMessageState | None
    response_turn_id: str = field(default_factory=lambda: str(uuid4()))
    notice_fired: bool = False
    storage_targets: dict[tuple[str, str, SessionType], _QueuedNoticeStorageTarget] = field(default_factory=dict)


@dataclass
class _QueuedNoticeStorageTarget:
    storage_factory: Callable[[], BaseDb]
    session_id: str
    session_type: SessionType
    entity_name: str


_queued_message_notice_context: ContextVar[_QueuedMessageNoticeContext | None] = ContextVar(
    "queued_message_notice_context",
    default=None,
)


@contextmanager
def queued_message_signal_context(
    signal: _SupportsQueuedMessageState | None,
) -> Generator[_QueuedMessageNoticeContext, None, None]:
    """Bind one queued-message signal to the current async task."""
    notice_context = _QueuedMessageNoticeContext(state=signal)
    token = _queued_message_notice_context.set(notice_context)
    try:
        yield notice_context
    finally:
        _queued_message_notice_context.reset(token)


def _has_queued_notice_marker(message: Message) -> bool:
    provider_data = message.provider_data
    return isinstance(provider_data, dict) and provider_data.get(_QUEUED_MESSAGE_NOTICE_MARKER_KEY) in (
        True,
        _QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER,
    )


def _queued_notice_marker(message: Message) -> bool | str | None:
    provider_data = message.provider_data
    if not isinstance(provider_data, dict):
        return None
    marker = provider_data.get(_QUEUED_MESSAGE_NOTICE_MARKER_KEY)
    return marker if marker in (True, _QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER) else None


def _queued_notice_response_turn_id(message: Message) -> str | None:
    provider_data = message.provider_data
    if not isinstance(provider_data, dict):
        return None
    response_turn_id = provider_data.get(_QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY)
    return response_turn_id if isinstance(response_turn_id, str) and response_turn_id else None


def _is_queued_notice_message(
    message: Message,
    *,
    response_turn_id: str | None = None,
) -> bool:
    """Return whether one Agno message is the hidden queued-message notice."""
    if not _has_queued_notice_marker(message):
        return False
    if response_turn_id is None:
        return True
    return _queued_notice_response_turn_id(message) == response_turn_id


def _strip_queued_notice_messages(
    messages: list[Message] | None,
    *,
    response_turn_id: str | None = None,
) -> bool:
    """Remove queued-message notices from one mutable message list."""
    if not messages:
        return False
    filtered_messages = [
        message
        for message in messages
        if not _is_queued_notice_message(
            message,
            response_turn_id=response_turn_id,
        )
    ]
    if len(filtered_messages) == len(messages):
        return False
    messages[:] = filtered_messages
    return True


def _append_queued_notice_if_needed(
    *,
    messages: list[Message],
    function_call_results: Sequence[Message],
    notice_text: str,
) -> None:
    notice_context = _queued_message_notice_context.get()
    if any(message.stop_after_tool_call for message in function_call_results):
        return
    if notice_context is not None:
        _strip_queued_notice_messages(
            messages,
            response_turn_id=notice_context.response_turn_id,
        )
    if notice_context is None or notice_context.state is None or not notice_context.state.has_pending_human_messages():
        return
    messages.append(
        Message(
            role="user",
            content=notice_text,
            provider_data={
                _QUEUED_MESSAGE_NOTICE_MARKER_KEY: True,
                _QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY: notice_context.response_turn_id,
            },
        ),
    )
    if not notice_context.notice_fired:
        notice_context.notice_fired = True
        logger.info(
            "queued_message_notice_injected",
            response_turn_id=notice_context.response_turn_id,
        )


def _strip_response_turn_notice_from_run_output(
    run_output: RunOutput | TeamRunOutput,
    *,
    response_turn_id: str,
) -> bool:
    """Remove one response's notice from a top-level or nested run output."""
    changed = _strip_queued_notice_messages(
        run_output.messages,
        response_turn_id=response_turn_id,
    )
    if isinstance(run_output, TeamRunOutput) and run_output.member_responses:
        for member_response in run_output.member_responses:
            if isinstance(member_response, RunOutput | TeamRunOutput):
                changed = (
                    _strip_response_turn_notice_from_run_output(
                        member_response,
                        response_turn_id=response_turn_id,
                    )
                    or changed
                )
    return changed


def _load_queued_notice_session(
    raw_session: AgentSession | TeamSession | dict[str, object],
    *,
    session_type: SessionType,
) -> AgentSession | TeamSession | None:
    """Deserialize one stored Agno session for queued-notice finalization."""
    if isinstance(raw_session, dict):
        session_mapping = cast("dict[str, Any]", raw_session)
        return (
            TeamSession.from_dict(session_mapping)
            if session_type is SessionType.TEAM
            else AgentSession.from_dict(session_mapping)
        )
    return raw_session


def _session_run_outputs(session: AgentSession | TeamSession) -> list[RunOutput | TeamRunOutput]:
    return [run for run in session.runs or [] if isinstance(run, RunOutput | TeamRunOutput)]


def _run_output_notice_messages(
    run_output: RunOutput | TeamRunOutput,
    *,
    response_turn_id: str,
) -> list[Message]:
    matches = _top_level_queued_notice_messages(
        run_output,
        response_turn_id=response_turn_id,
    )
    if isinstance(run_output, TeamRunOutput) and run_output.member_responses:
        for member_response in run_output.member_responses:
            if isinstance(member_response, RunOutput | TeamRunOutput):
                matches.extend(
                    _run_output_notice_messages(
                        member_response,
                        response_turn_id=response_turn_id,
                    ),
                )
    return matches


def _top_level_queued_notice_messages(
    run_output: RunOutput | TeamRunOutput,
    *,
    response_turn_id: str,
) -> list[Message]:
    return [
        message
        for message in run_output.messages or []
        if _is_queued_notice_message(
            message,
            response_turn_id=response_turn_id,
        )
    ]


def _new_persisted_queued_notice(response_turn_id: str, notice_text: str) -> Message:
    return Message(
        role="user",
        content=notice_text,
        provider_data={
            _QUEUED_MESSAGE_NOTICE_MARKER_KEY: _QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER,
            _QUEUED_MESSAGE_NOTICE_RESPONSE_TURN_ID_KEY: response_turn_id,
        },
    )


def _queued_notice_text_to_persist(
    *,
    destination_matches: Sequence[Message],
) -> str | None:
    destination_live_notice = next(
        (
            message
            for message in destination_matches
            if _queued_notice_marker(message) is True and isinstance(message.content, str)
        ),
        None,
    )
    if destination_live_notice is not None:
        return cast("str", destination_live_notice.content)
    persisted_source = next(
        (
            message
            for message in destination_matches
            if _queued_notice_marker(message) == _QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER
            and isinstance(message.content, str)
        ),
        None,
    )
    return cast("str", persisted_source.content) if persisted_source is not None else None


def _finalize_queued_notice_in_runs(
    runs: Sequence[RunOutput | TeamRunOutput],
    *,
    response_turn_id: str,
) -> bool:
    """Leave one exact persisted notice where the newest replayable run saw it."""
    destination = next(
        (
            run
            for run in reversed(runs)
            if is_model_history_visible_run(run)
            and _top_level_queued_notice_messages(
                run,
                response_turn_id=response_turn_id,
            )
        ),
        None,
    )
    all_matches = [
        message
        for run in runs
        for message in _run_output_notice_messages(
            run,
            response_turn_id=response_turn_id,
        )
    ]
    if not all_matches and destination is None:
        return False

    destination_matches = (
        _top_level_queued_notice_messages(
            destination,
            response_turn_id=response_turn_id,
        )
        if destination is not None
        else []
    )
    notice_text = _queued_notice_text_to_persist(
        destination_matches=destination_matches,
    )
    if (
        notice_text is not None
        and len(all_matches) == 1
        and len(destination_matches) == 1
        and _queued_notice_marker(destination_matches[0]) == _QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER
        and destination_matches[0].content == notice_text
    ):
        return False

    insertion_index: int | None = None
    if destination is not None and destination.messages:
        insertion_index = next(
            (
                index
                for index, message in enumerate(destination.messages)
                if _is_queued_notice_message(
                    message,
                    response_turn_id=response_turn_id,
                )
            ),
            None,
        )

    for run in runs:
        _strip_response_turn_notice_from_run_output(
            run,
            response_turn_id=response_turn_id,
        )

    if destination is None or notice_text is None:
        return True
    if destination.messages is None:
        destination.messages = []
    persisted_notice = _new_persisted_queued_notice(response_turn_id, notice_text)
    if insertion_index is None:
        destination.messages.append(persisted_notice)
    else:
        destination.messages.insert(min(insertion_index, len(destination.messages)), persisted_notice)
    return True


def _finalize_queued_notice_in_new_session_storage(
    target: _QueuedNoticeStorageTarget,
    response_turn_id: str,
) -> None:
    """Finalize one response in a worker-owned session storage handle."""
    storage = target.storage_factory()
    try:
        raw_session = storage.get_session(target.session_id, target.session_type)
        if raw_session is None:
            return
        session = _load_queued_notice_session(
            cast("AgentSession | TeamSession | dict[str, object]", raw_session),
            session_type=target.session_type,
        )
        if session is None:
            return
        if _finalize_queued_notice_in_runs(
            _session_run_outputs(session),
            response_turn_id=response_turn_id,
        ):
            storage.upsert_session(session)
    finally:
        storage.close()


def register_queued_notice_storage(
    *,
    storage_factory: Callable[[], BaseDb] | None,
    session_id: str | None,
    session_type: SessionType,
    entity_name: str,
) -> None:
    """Register storage touched by one response for queued-notice finalization."""
    notice_context = _queued_message_notice_context.get()
    if notice_context is None:
        return
    if storage_factory is None or not session_id:
        return
    target_key = (entity_name, session_id, session_type)
    target = notice_context.storage_targets.get(target_key)
    if target is None:
        target = _QueuedNoticeStorageTarget(
            storage_factory=storage_factory,
            session_id=session_id,
            session_type=session_type,
            entity_name=entity_name,
        )
        notice_context.storage_targets[target_key] = target


def _finalize_queued_notice_storage_targets(
    targets: Sequence[_QueuedNoticeStorageTarget],
    response_turn_id: str,
) -> None:
    """Finalize all durable targets for one response from a worker thread."""
    for target in targets:
        try:
            _finalize_queued_notice_in_new_session_storage(
                target,
                response_turn_id,
            )
        except Exception:
            logger.exception(
                "Failed to finalize queued-message notice in session history",
                entity=target.entity_name,
                session_id=target.session_id,
                session_type=target.session_type.value,
                response_turn_id=response_turn_id,
            )


async def finalize_queued_notice_response_turn_async(
    notice_context: _QueuedMessageNoticeContext,
) -> None:
    """Finalize one delivered notice at the user-visible response boundary."""
    if not notice_context.notice_fired:
        return
    if not notice_context.storage_targets:
        return
    storage_task = asyncio.create_task(
        asyncio.to_thread(
            _finalize_queued_notice_storage_targets,
            tuple(notice_context.storage_targets.values()),
            notice_context.response_turn_id,
        ),
    )
    try:
        await asyncio.shield(storage_task)
    except asyncio.CancelledError:
        while not storage_task.done():
            try:
                await asyncio.shield(storage_task)
            except asyncio.CancelledError:
                continue
        storage_task.result()
        raise


def _queued_notice_response_turn_ids(
    runs: Sequence[RunOutput | TeamRunOutput],
) -> set[str]:
    return {
        response_turn_id
        for run in runs
        for message in _run_output_notice_messages_for_any_response(run)
        if (response_turn_id := _queued_notice_response_turn_id(message)) is not None
    }


def _run_output_notice_messages_for_any_response(
    run_output: RunOutput | TeamRunOutput,
) -> list[Message]:
    matches = [message for message in run_output.messages or [] if _has_queued_notice_marker(message)]
    if isinstance(run_output, TeamRunOutput) and run_output.member_responses:
        for member_response in run_output.member_responses:
            if isinstance(member_response, RunOutput | TeamRunOutput):
                matches.extend(_run_output_notice_messages_for_any_response(member_response))
    return matches


def _has_notice_marker_for_response(
    runs: Sequence[RunOutput | TeamRunOutput],
    *,
    response_turn_id: str,
    marker: bool | str,
) -> bool:
    return any(
        _queued_notice_marker(message) == marker
        for run in runs
        for message in _run_output_notice_messages(
            run,
            response_turn_id=response_turn_id,
        )
    )


def _recover_prior_queued_notices(
    session: AgentSession | TeamSession,
    *,
    active_response_turn_id: str | None,
) -> bool:
    runs = _session_run_outputs(session)
    changed = False
    for response_turn_id in _queued_notice_response_turn_ids(runs):
        if response_turn_id == active_response_turn_id:
            continue
        if not _has_notice_marker_for_response(
            runs,
            response_turn_id=response_turn_id,
            marker=True,
        ):
            continue
        if _has_notice_marker_for_response(
            runs,
            response_turn_id=response_turn_id,
            marker=_QUEUED_MESSAGE_NOTICE_PERSISTED_MARKER,
        ):
            continue
        changed = (
            _finalize_queued_notice_in_runs(
                runs,
                response_turn_id=response_turn_id,
            )
            or changed
        )
    return changed


def scrub_queued_notice_session_context(
    *,
    scope_context: ScopeSessionContext | None,
    entity_name: str,
) -> None:
    """Recover prior crash-left notices without touching the active response."""
    if scope_context is None or scope_context.session is None:
        return
    notice_context = _queued_message_notice_context.get()
    try:
        if _recover_prior_queued_notices(
            scope_context.session,
            active_response_turn_id=notice_context.response_turn_id if notice_context is not None else None,
        ):
            scope_context.storage.upsert_session(scope_context.session)
    except Exception:
        logger.exception(
            "Failed to recover queued-message notice in loaded session history",
            entity=entity_name,
            session_id=scope_context.session.session_id,
            session_type="team" if isinstance(scope_context.session, TeamSession) else "agent",
        )


def is_empty_completed_run(response: RunOutput | TeamRunOutput) -> bool:
    """Return whether one run completed with no tool calls and no visible content."""
    if response.status is not RunStatus.completed or response.tools:
        return False
    content = response.content
    if content is None:
        return True
    return isinstance(content, str) and not content.strip()


def _remove_run_from_session(session: AgentSession | TeamSession, *, run_id: str) -> bool:
    """Remove one run from a mutable session run list by run id."""
    runs = session.runs or []
    kept = [run for run in runs if not (isinstance(run, (RunOutput, TeamRunOutput)) and run.run_id == run_id)]
    if len(kept) == len(runs):
        return False
    session.runs = kept
    return True


def _remove_run_from_session_storage(
    storage: BaseDb,
    session_id: str,
    *,
    run_id: str,
    session_type: SessionType,
) -> bool:
    """Remove one run from a persisted Agno session."""
    raw_session = storage.get_session(session_id, session_type)
    if raw_session is None:
        return False
    session = _load_queued_notice_session(
        cast("AgentSession | TeamSession | dict[str, object]", raw_session),
        session_type=session_type,
    )
    if session is None or not _remove_run_from_session(session, run_id=run_id):
        return False
    storage.upsert_session(session)
    return True


def discard_empty_completed_run(
    *,
    scope_context: ScopeSessionContext | None,
    session_id: str,
    run_id: str | None,
    session_type: SessionType,
    entity_name: str,
    output_tokens: int | None,
) -> None:
    """Log one empty completed model response and purge its run from session history.

    A persisted assistant turn with no content teaches the model that ending the
    turn immediately is the expected continuation, so the run is removed from both
    the loaded session and storage before the next prompt is built.
    """
    logger.warning(
        "model_returned_empty_response",
        entity=entity_name,
        session_id=session_id,
        run_id=run_id,
        output_tokens=output_tokens,
    )
    if scope_context is None or not run_id:
        return
    try:
        if scope_context.session is not None:
            _remove_run_from_session(scope_context.session, run_id=run_id)
        _remove_run_from_session_storage(
            scope_context.storage,
            session_id,
            run_id=run_id,
            session_type=session_type,
        )
    except Exception:
        logger.exception(
            "Failed to remove empty run from session history",
            entity=entity_name,
            session_id=session_id,
            run_id=run_id,
        )


def install_queued_message_notice_hook(
    model: Model,
    *,
    notice_text: str,
) -> None:
    """Append a hidden notice after tool results when a newer message is queued."""
    try:
        original_format_function_call_results = model.format_function_call_results
        model_dict = vars(model)
    except (AttributeError, TypeError):
        return
    if model_dict.get(_QUEUED_MESSAGE_NOTICE_HOOK_ATTR) is True:
        return
    setattr(model, _QUEUED_MESSAGE_NOTICE_HOOK_ATTR, True)

    def _format_function_call_results_with_notice(
        messages: list[Message],
        function_call_results: list[Message],
        compress_tool_results: bool = False,
        **kwargs: object,
    ) -> None:
        original_format_function_call_results(
            messages=messages,
            function_call_results=function_call_results,
            compress_tool_results=compress_tool_results,
            **kwargs,
        )
        _append_queued_notice_if_needed(
            messages=messages,
            function_call_results=function_call_results,
            notice_text=notice_text,
        )

    def _handle_function_call_media_with_notice(
        messages: list[Message],
        function_call_results: list[Message],
        send_media_to_model: bool = True,
    ) -> None:
        original_handle_function_call_media(
            messages=messages,
            function_call_results=function_call_results,
            send_media_to_model=send_media_to_model,
        )
        _append_queued_notice_if_needed(
            messages=messages,
            function_call_results=function_call_results,
            notice_text=notice_text,
        )

    model_dict["format_function_call_results"] = _format_function_call_results_with_notice
    try:
        original_handle_function_call_media = model._handle_function_call_media
    except AttributeError:
        return

    model_dict["_handle_function_call_media"] = _handle_function_call_media_with_notice


def next_retry_run_id(run_id: str | None) -> str | None:
    """Return a fresh Agno run identifier for a retry attempt."""
    if run_id is None:
        return None
    return str(uuid4())


def note_attempt_run_id(run_id_callback: Callable[[str], None] | None, run_id: str | None) -> None:
    """Publish the current run_id before starting a real Agno run attempt."""
    if run_id_callback is not None and run_id is not None:
        run_id_callback(run_id)


async def cached_agent_run(
    agent: Agent,
    run_input: ModelRunInput,
    session_id: str,
    *,
    user_id: str | None = None,
    run_id: str | None = None,
    run_id_callback: Callable[[str], None] | None = None,
    media: MediaInputs | None = None,
    metadata: dict[str, Any] | None = None,
) -> RunOutput:
    """Shared wrapper for one ``agent.arun()`` call."""
    media_inputs = media or MediaInputs()
    note_attempt_run_id(run_id_callback, run_id)
    prepared_input = attach_media_to_run_input(run_input, media_inputs)
    return await agent.arun(
        prepared_input,
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        metadata=metadata,
    )
