"""Vendored Agno Team roleful-input patch.

Agno Agent preserves ``list[Message]`` input as roleful provider messages, while
Agno Team currently flattens that same shape through ``get_text_from_message``.
This throwaway monkey-patch mirrors the Agent message-builder path until Agno
Team has the same upstream behavior.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar, cast

from agno.agent import _messages as agent_messages
from agno.models.message import Message
from agno.run.messages import RunMessages
from agno.team import _messages as team_messages
from agno.utils.log import log_warning

from mindroom import attachment_media

if TYPE_CHECKING:
    from agno.media import Audio, File, Image, Video

_PATCHED = False
_PATCH_LOCK = threading.Lock()
type _RolefulInput = list[Message]
type _RunMessagesBuilder = Callable[..., RunMessages]
type _AsyncRunMessagesBuilder = Callable[..., Awaitable[RunMessages]]
_MediaT = TypeVar("_MediaT")


def _is_roleful_message_list(input_message: object) -> bool:
    return isinstance(input_message, list) and bool(input_message) and isinstance(input_message[0], Message)


def _append_input_messages(run_messages: RunMessages, input_messages: list[Any]) -> None:
    roleful_messages: list[Message] = []
    for input_message in input_messages:
        if isinstance(input_message, Message):
            message = input_message
        else:
            try:
                message = Message.model_validate(input_message)
            except Exception as exc:
                log_warning(f"Failed to validate message: {exc}")
                continue
        roleful_messages.append(message)
    if not roleful_messages:
        return

    additional_input = list(run_messages.extra_messages or [])
    run_messages.messages.extend(roleful_messages)
    if roleful_messages[-1].role == "user":
        run_messages.user_message = roleful_messages[-1]
        roleful_history = roleful_messages[:-1]
    else:
        roleful_history = roleful_messages
    run_messages.extra_messages = [*roleful_history, *additional_input]


def _media_content_key(kind: str, media: Image | Audio | Video | File) -> tuple[str, ...] | None:
    mime_type = media.mime_type or ""
    key: tuple[str, ...] | None = None
    if media.id and media.id.startswith("att_"):
        record = attachment_media._INLINE_MEDIA_RECORDS_BY_ID.get(media.id)
        if record is not None and record.kind == kind:
            key = attachment_media._inline_media_content_key(record)
    if key is None and media.filepath:
        path = str(Path(media.filepath).absolute())
        record = attachment_media._INLINE_MEDIA_RECORDS_BY_PATH.get(path)
        if record is not None and record.kind == kind:
            key = attachment_media._inline_media_content_key(record)
        else:
            key = (kind, mime_type, "filepath", path)
    if key is None and media.content is not None:
        if isinstance(media.content, bytes):
            content = media.content
        elif isinstance(media.content, str):
            content = media.content.encode("utf-8")
        else:
            content = None
        if content is not None:
            key = (kind, mime_type, hashlib.sha256(content).hexdigest())
    if key is None and media.url is not None:
        key = (kind, mime_type, "url", media.url)
    return key


def _image_content_key(image: Image) -> tuple[str, ...] | None:
    return _media_content_key("image", image)


def _audio_content_key(audio: Audio) -> tuple[str, ...] | None:
    return _media_content_key("audio", audio)


def _video_content_key(video: Video) -> tuple[str, ...] | None:
    return _media_content_key("video", video)


def _file_content_key(file: File) -> tuple[str, ...] | None:
    return _media_content_key("file", file)


def _keep_first_inline_media(
    media: list[_MediaT],
    seen: set[tuple[str, ...]],
    key_for: Callable[[_MediaT], tuple[str, ...] | None],
) -> list[_MediaT]:
    kept: list[_MediaT] = []
    for item in media:
        key = key_for(item)
        if key is None:
            kept.append(item)
        elif key not in seen:
            seen.add(key)
            kept.append(item)
    return kept


def _messages_in_inline_media_dedupe_order(run_messages: RunMessages) -> list[Message]:
    # Walk in provider payload order so the earliest occurrence of each media
    # content key wins. Keeping media at its first (history) position keeps the
    # request prefix byte-stable across turns for provider prompt caching.
    # Identity-based dedupe — Agno keeps the same Message instance in run_messages.messages and run_messages.user_message.
    seen_message_ids: set[int] = set()
    ordered_messages: list[Message] = []

    for message in run_messages.messages:
        if id(message) in seen_message_ids:
            continue
        seen_message_ids.add(id(message))
        ordered_messages.append(message)

    current_message = run_messages.user_message
    if current_message is not None and id(current_message) not in seen_message_ids:
        ordered_messages.append(current_message)

    return ordered_messages


def _dedupe_run_messages_inline_media(run_messages: RunMessages) -> RunMessages:
    """Remove duplicate inline media from one provider-bound run in place.

    Agno builds fresh per-run Message objects for persisted history before this
    function runs, so clearing older duplicate media here affects the current
    request payload rather than persisted session history. The first content key
    in provider payload order (earliest message) wins.

    Accepted trade-off: when a user re-sends byte-identical content, the
    current turn's copy is stripped and the bytes stay at the earlier history
    position. The model still sees the content exactly once, and exempting the
    current message instead would double-send all history media for team runs,
    which re-collect pinned history media onto their current turn.
    """
    seen_images: set[tuple[str, ...]] = set()
    seen_audio: set[tuple[str, ...]] = set()
    seen_files: set[tuple[str, ...]] = set()
    seen_videos: set[tuple[str, ...]] = set()
    for message in _messages_in_inline_media_dedupe_order(run_messages):
        if message.images:
            message.images = _keep_first_inline_media(list(message.images), seen_images, _image_content_key)
        if message.audio:
            message.audio = _keep_first_inline_media(list(message.audio), seen_audio, _audio_content_key)
        if message.files:
            message.files = _keep_first_inline_media(list(message.files), seen_files, _file_content_key)
        if message.videos:
            message.videos = _keep_first_inline_media(list(message.videos), seen_videos, _video_content_key)
    return run_messages


def apply_patch() -> None:
    """Patch Agno Team run-message builders once per interpreter."""
    global _PATCHED
    if _PATCHED:
        return
    with _PATCH_LOCK:
        if _PATCHED:
            return

        original_team_get_run_messages = cast("_RunMessagesBuilder", team_messages._get_run_messages)
        original_team_aget_run_messages = cast("_AsyncRunMessagesBuilder", team_messages._aget_run_messages)
        original_agent_get_run_messages = cast("_RunMessagesBuilder", agent_messages.get_run_messages)
        original_agent_aget_run_messages = cast("_AsyncRunMessagesBuilder", agent_messages.aget_run_messages)

        def _get_run_messages(*args: object, **kwargs: object) -> RunMessages:
            input_message = kwargs.get("input_message")
            if not _is_roleful_message_list(input_message):
                return _dedupe_run_messages_inline_media(original_team_get_run_messages(*args, **kwargs))

            passthrough_kwargs = {**kwargs, "input_message": None}
            run_messages = original_team_get_run_messages(*args, **passthrough_kwargs)
            _append_input_messages(run_messages, cast("_RolefulInput", input_message))
            return _dedupe_run_messages_inline_media(run_messages)

        async def _aget_run_messages(*args: object, **kwargs: object) -> RunMessages:
            input_message = kwargs.get("input_message")
            if not _is_roleful_message_list(input_message):
                return _dedupe_run_messages_inline_media(await original_team_aget_run_messages(*args, **kwargs))

            passthrough_kwargs = {**kwargs, "input_message": None}
            run_messages = await original_team_aget_run_messages(*args, **passthrough_kwargs)
            _append_input_messages(run_messages, cast("_RolefulInput", input_message))
            return _dedupe_run_messages_inline_media(run_messages)

        def _agent_get_run_messages(*args: object, **kwargs: object) -> RunMessages:
            return _dedupe_run_messages_inline_media(original_agent_get_run_messages(*args, **kwargs))

        async def _agent_aget_run_messages(*args: object, **kwargs: object) -> RunMessages:
            return _dedupe_run_messages_inline_media(await original_agent_aget_run_messages(*args, **kwargs))

        team_messages._get_run_messages = cast("Any", _get_run_messages)
        team_messages._aget_run_messages = cast("Any", _aget_run_messages)
        agent_messages.get_run_messages = cast("Any", _agent_get_run_messages)
        agent_messages.aget_run_messages = cast("Any", _agent_aget_run_messages)
        _PATCHED = True
