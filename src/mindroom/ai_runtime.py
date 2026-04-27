"""Internal AI execution helpers kept off the public ``mindroom.ai`` seam."""

from __future__ import annotations

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from agno.db.base import SessionType
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.logging_config import get_logger
from mindroom.media_fallback import append_inline_media_fallback_prompt
from mindroom.media_inputs import MediaInputs

if TYPE_CHECKING:
    from agno.agent import Agent
    from agno.db.base import BaseDb
    from agno.models.base import Model

    from mindroom.history import ScopeSessionContext

__all__ = [
    "ModelRunInput",
    "append_inline_media_fallback_to_run_input",
    "attach_media_to_run_input",
    "cached_agent_run",
    "cleanup_queued_notice_state",
    "copy_run_input",
    "install_queued_message_notice_hook",
    "queued_message_signal_context",
    "scrub_queued_notice_session_context",
]

logger = get_logger(__name__)

type ModelRunInput = str | Sequence[Message]

_QUEUED_MESSAGE_NOTICE_MARKER_KEY = "mindroom_queued_message_notice"
_QUEUED_MESSAGE_NOTICE_HOOK_ATTR = "_mindroom_queued_message_notice_hook_installed"
QUEUED_MESSAGE_NOTICE_TEXT = (
    "[SYSTEM NOTICE] A new message from the user has arrived in this thread while you were working. "
    "You should wrap up your current work and produce a final text response now. "
    "Avoid further tool calls unless strictly necessary. "
    "The new message will be handled in your next turn."
)


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


def append_inline_media_fallback_to_run_input(run_input: ModelRunInput) -> list[Message]:
    """Append the inline-media fallback note to the current user turn."""
    run_messages = copy_run_input(run_input)
    current_message = run_messages[-1]
    current_text = current_message.content if isinstance(current_message.content, str) else ""
    current_message.content = append_inline_media_fallback_prompt(current_text)
    current_message.audio = None
    current_message.images = None
    current_message.files = None
    current_message.videos = None
    return run_messages


class _SupportsQueuedMessageState(Protocol):
    def has_pending_human_messages(self) -> bool: ...


@dataclass
class _QueuedMessageNoticeContext:
    state: _SupportsQueuedMessageState | None


_queued_message_notice_context: ContextVar[_QueuedMessageNoticeContext | None] = ContextVar(
    "queued_message_notice_context",
    default=None,
)


@contextmanager
def queued_message_signal_context(
    signal: _SupportsQueuedMessageState | None,
) -> Generator[None, None, None]:
    """Bind one queued-message signal to the current async task."""
    token = _queued_message_notice_context.set(_QueuedMessageNoticeContext(state=signal))
    try:
        yield
    finally:
        _queued_message_notice_context.reset(token)


def _has_queued_notice_marker(message: Message) -> bool:
    provider_data = message.provider_data
    return isinstance(provider_data, dict) and provider_data.get(_QUEUED_MESSAGE_NOTICE_MARKER_KEY) is True


def _is_queued_notice_message(message: Message) -> bool:
    """Return whether one Agno message is the hidden queued-message notice."""
    return _has_queued_notice_marker(message)


def _strip_queued_notice_messages(messages: list[Message] | None) -> bool:
    """Remove queued-message notices from one mutable message list."""
    if not messages:
        return False
    filtered_messages = [message for message in messages if not _is_queued_notice_message(message)]
    if len(filtered_messages) == len(messages):
        return False
    messages[:] = filtered_messages
    return True


def _append_queued_notice_if_needed(
    *,
    messages: list[Message],
    function_call_results: Sequence[Message],
) -> None:
    _strip_queued_notice_messages(messages)
    if any(message.stop_after_tool_call for message in function_call_results):
        return
    notice_context = _queued_message_notice_context.get()
    if notice_context is None or notice_context.state is None or not notice_context.state.has_pending_human_messages():
        return
    messages.append(
        Message(
            role="user",
            content=QUEUED_MESSAGE_NOTICE_TEXT,
            provider_data={_QUEUED_MESSAGE_NOTICE_MARKER_KEY: True},
        ),
    )


def _cleanup_queued_notice_from_run_output(run_output: RunOutput | TeamRunOutput | None) -> bool:
    """Remove queued-message notices from one returned run output."""
    if run_output is None:
        return False
    changed = _strip_queued_notice_messages(run_output.messages)
    if isinstance(run_output, TeamRunOutput) and run_output.member_responses:
        for member_response in run_output.member_responses:
            if isinstance(member_response, RunOutput | TeamRunOutput):
                changed = _cleanup_queued_notice_from_run_output(member_response) or changed
    return changed


def _load_session_for_cleanup(
    raw_session: AgentSession | TeamSession | dict[str, object],
    *,
    session_type: SessionType,
) -> AgentSession | TeamSession | None:
    """Deserialize one stored Agno session for queued-notice cleanup."""
    if isinstance(raw_session, dict):
        session_mapping = cast("dict[str, Any]", raw_session)
        return (
            TeamSession.from_dict(session_mapping)
            if session_type is SessionType.TEAM
            else AgentSession.from_dict(session_mapping)
        )
    return raw_session


def _strip_queued_notice_from_session(session: AgentSession | TeamSession) -> bool:
    changed = False
    for run in session.runs or []:
        if isinstance(run, (RunOutput, TeamRunOutput)):
            changed = _cleanup_queued_notice_from_run_output(run) or changed
    return changed


def _strip_queued_notice_from_session_storage(
    storage: BaseDb,
    session_id: str,
    *,
    session_type: SessionType = SessionType.AGENT,
) -> bool:
    """Remove queued-message notices from one persisted Agno session."""
    raw_session = storage.get_session(session_id, session_type)
    if raw_session is None:
        return False
    session = _load_session_for_cleanup(
        cast("AgentSession | TeamSession | dict[str, object]", raw_session),
        session_type=session_type,
    )
    if session is None:
        return False
    changed = _strip_queued_notice_from_session(session)
    if changed:
        storage.upsert_session(session)
    return changed


def cleanup_queued_notice_state(
    *,
    run_output: RunOutput | TeamRunOutput | None,
    storage: BaseDb | None,
    session_id: str | None,
    session_type: SessionType,
    entity_name: str,
) -> None:
    """Strip queued-message notices from returned and persisted run state."""
    _cleanup_queued_notice_from_run_output(run_output)
    if storage is None or not session_id:
        return
    try:
        _strip_queued_notice_from_session_storage(
            storage,
            session_id,
            session_type=session_type,
        )
    except Exception:
        logger.exception(
            "Failed to strip queued-message notice from session history",
            entity=entity_name,
            session_id=session_id,
            session_type=session_type.value,
        )


def scrub_queued_notice_session_context(
    *,
    scope_context: ScopeSessionContext | None,
    entity_name: str,
) -> None:
    """Strip stale queued-message notices from the loaded session before replay."""
    if scope_context is None or scope_context.session is None:
        return
    try:
        if _strip_queued_notice_from_session(scope_context.session):
            scope_context.storage.upsert_session(scope_context.session)
    except Exception:
        logger.exception(
            "Failed to strip queued-message notice from loaded session history",
            entity=entity_name,
            session_id=scope_context.session.session_id,
            session_type="team" if isinstance(scope_context.session, TeamSession) else "agent",
        )


def install_queued_message_notice_hook(model: Model) -> None:
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
        )

    model_dict["format_function_call_results"] = _format_function_call_results_with_notice
    try:
        original_handle_function_call_media = model._handle_function_call_media
    except AttributeError:
        return

    model_dict["_handle_function_call_media"] = _handle_function_call_media_with_notice


def _note_attempt_run_id(run_id_callback: Callable[[str], None] | None, run_id: str | None) -> None:
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
    _note_attempt_run_id(run_id_callback, run_id)
    prepared_input = attach_media_to_run_input(run_input, media_inputs)
    return await agent.arun(
        prepared_input,
        session_id=session_id,
        user_id=user_id,
        run_id=run_id,
        metadata=metadata,
    )
