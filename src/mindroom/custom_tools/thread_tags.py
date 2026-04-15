"""Thread tagging tools for AI agents."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import resolve_context_thread_id, room_access_allowed
from mindroom.thread_tags import (
    ThreadTagRecord,
    ThreadTagsError,
    get_thread_tags,
    list_tagged_threads,
    normalize_tag_name,
    normalize_thread_root_event_id,
    remove_thread_tag,
    set_thread_tag,
)
from mindroom.tool_system.runtime_context import ToolRuntimeContext, get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Mapping


def _resolve_target_thread_reference(
    context: ToolRuntimeContext,
    *,
    room_id: str,
    thread_id: str | None,
    allow_context_fallback: bool = True,
) -> str | None:
    """Resolve thread targeting in the order requested by the issue."""
    return resolve_context_thread_id(
        context,
        room_id=room_id,
        thread_id=thread_id,
        allow_context_fallback=allow_context_fallback,
    )


def _serialized_tags(tags: Mapping[str, ThreadTagRecord]) -> dict[str, dict[str, object]]:
    """Serialize thread-tag records for tool payloads."""
    return {tag: record.model_dump(mode="json", exclude_none=True) for tag, record in tags.items()}


def _serialized_tags_for_output(
    tags: Mapping[str, ThreadTagRecord],
    *,
    tag: str | None,
) -> dict[str, dict[str, object]]:
    """Serialize tags, optionally narrowing to one requested tag."""
    serialized = _serialized_tags(tags)
    if tag is None:
        return serialized
    return {tag: serialized[tag]} if tag in serialized else {}


def _thread_matches_tag_filters(
    tags: Mapping[str, ThreadTagRecord],
    *,
    tag: str | None,
    include_tag: str | None,
    exclude_tag: str | None,
) -> bool:
    """Return whether one tagged thread matches the requested list filters."""
    if tag is not None and tag not in tags:
        return False
    if include_tag is not None and include_tag not in tags:
        return False
    return exclude_tag is None or exclude_tag not in tags


class ThreadTagsTools(Toolkit):
    """Tools for tagging Matrix threads via shared room state."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_tags",
            tools=[self.tag_thread, self.untag_thread, self.list_thread_tags],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        payload: dict[str, object] = {"status": status, "tool": "thread_tags"}
        payload.update(kwargs)
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def _context_error(cls) -> str:
        return cls._payload(
            "error",
            message="Thread tags tool context is unavailable in this runtime path.",
        )

    async def tag_thread(  # noqa: PLR0911
        self,
        tag: str,
        thread_id: str | None = None,
        room_id: str | None = None,
        note: str | None = None,
        data: dict[str, object] | None = None,
    ) -> str:
        """Add or update one tag on the current or specified Matrix thread."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        resolved_room_id = room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action="tag",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        try:
            normalized_tag = normalize_tag_name(tag)
        except ThreadTagsError as exc:
            return self._payload(
                "error",
                action="tag",
                room_id=resolved_room_id,
                message=str(exc),
            )

        effective_thread_id = _resolve_target_thread_reference(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
        )
        if effective_thread_id is None:
            return self._payload(
                "error",
                action="tag",
                room_id=resolved_room_id,
                tag=normalized_tag,
                message="thread_id is required when no active thread context is available for the target room.",
            )

        normalized_thread_id = await normalize_thread_root_event_id(
            context.client,
            resolved_room_id,
            effective_thread_id,
            context.conversation_cache,
        )
        if normalized_thread_id is None:
            return self._payload(
                "error",
                action="tag",
                room_id=resolved_room_id,
                thread_id=effective_thread_id,
                tag=normalized_tag,
                message="Failed to resolve a canonical thread root for the target event.",
            )

        try:
            state = await set_thread_tag(
                context.client,
                resolved_room_id,
                normalized_thread_id,
                normalized_tag,
                set_by=context.requester_id,
                note=note,
                data=data,
            )
        except ThreadTagsError as exc:
            return self._payload(
                "error",
                action="tag",
                room_id=resolved_room_id,
                thread_id=normalized_thread_id,
                tag=normalized_tag,
                message=str(exc),
            )

        return self._payload(
            "ok",
            action="tag",
            room_id=resolved_room_id,
            thread_id=state.thread_root_id,
            tag=normalized_tag,
            tags=_serialized_tags(state.tags),
        )

    async def untag_thread(  # noqa: PLR0911
        self,
        tag: str,
        thread_id: str | None = None,
        room_id: str | None = None,
        canonical: bool = False,
    ) -> str:
        """Remove one tag from the current or specified Matrix thread."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        resolved_room_id = room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action="untag",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        try:
            normalized_tag = normalize_tag_name(tag)
        except ThreadTagsError as exc:
            return self._payload(
                "error",
                action="untag",
                room_id=resolved_room_id,
                message=str(exc),
            )

        effective_thread_id = _resolve_target_thread_reference(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
        )
        if effective_thread_id is None:
            return self._payload(
                "error",
                action="untag",
                room_id=resolved_room_id,
                tag=normalized_tag,
                message="thread_id is required when no active thread context is available for the target room.",
            )

        if canonical:
            target_thread_id = effective_thread_id
        else:
            target_thread_id = await normalize_thread_root_event_id(
                context.client,
                resolved_room_id,
                effective_thread_id,
                context.conversation_cache,
            )
            if target_thread_id is None:
                return self._payload(
                    "error",
                    action="untag",
                    room_id=resolved_room_id,
                    thread_id=effective_thread_id,
                    tag=normalized_tag,
                    message="Failed to resolve a canonical thread root for the target event.",
                )

        try:
            state = await remove_thread_tag(
                context.client,
                resolved_room_id,
                target_thread_id,
                normalized_tag,
                requester_user_id=context.requester_id,
            )
        except ThreadTagsError as exc:
            return self._payload(
                "error",
                action="untag",
                room_id=resolved_room_id,
                thread_id=target_thread_id,
                tag=normalized_tag,
                message=str(exc),
            )

        return self._payload(
            "ok",
            action="untag",
            room_id=resolved_room_id,
            thread_id=state.thread_root_id,
            tag=normalized_tag,
            tags=_serialized_tags(state.tags),
        )

    async def list_thread_tags(  # noqa: PLR0911
        self,
        thread_id: str | None = None,
        room_id: str | None = None,
        tag: str | None = None,
        include_tag: str | None = None,
        exclude_tag: str | None = None,
    ) -> str:
        """List tags for one thread or all tagged threads in one room, with optional tag filters."""
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        resolved_room_id = room_id or context.room_id
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action="list",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        try:
            normalized_tag = normalize_tag_name(tag) if tag is not None else None
            normalized_include_tag = normalize_tag_name(include_tag) if include_tag is not None else None
            normalized_exclude_tag = normalize_tag_name(exclude_tag) if exclude_tag is not None else None
        except ThreadTagsError as exc:
            return self._payload(
                "error",
                action="list",
                room_id=resolved_room_id,
                message=str(exc),
            )

        effective_thread_id = _resolve_target_thread_reference(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            allow_context_fallback=room_id is None,
        )
        if effective_thread_id is None:
            room_wide_tag = (
                normalized_tag if normalized_include_tag is None and normalized_exclude_tag is None else None
            )
            try:
                threads = await list_tagged_threads(
                    context.client,
                    resolved_room_id,
                    tag=room_wide_tag,
                )
            except ThreadTagsError as exc:
                return self._payload(
                    "error",
                    action="list",
                    room_id=resolved_room_id,
                    tag=normalized_tag,
                    include_tag=normalized_include_tag,
                    exclude_tag=normalized_exclude_tag,
                    message=str(exc),
                )

            return self._payload(
                "ok",
                action="list",
                room_id=resolved_room_id,
                room_wide=True,
                tag=normalized_tag,
                include_tag=normalized_include_tag,
                exclude_tag=normalized_exclude_tag,
                threads={
                    thread_root_id: _serialized_tags_for_output(state.tags, tag=normalized_tag)
                    for thread_root_id, state in threads.items()
                    if _thread_matches_tag_filters(
                        state.tags,
                        tag=None if room_wide_tag is not None else normalized_tag,
                        include_tag=normalized_include_tag,
                        exclude_tag=normalized_exclude_tag,
                    )
                },
            )

        normalized_thread_id = await normalize_thread_root_event_id(
            context.client,
            resolved_room_id,
            effective_thread_id,
            context.conversation_cache,
        )
        if normalized_thread_id is None:
            return self._payload(
                "error",
                action="list",
                room_id=resolved_room_id,
                thread_id=effective_thread_id,
                tag=normalized_tag,
                include_tag=normalized_include_tag,
                exclude_tag=normalized_exclude_tag,
                message="Failed to resolve a canonical thread root for the target event.",
            )

        try:
            state = await get_thread_tags(
                context.client,
                resolved_room_id,
                normalized_thread_id,
            )
        except ThreadTagsError as exc:
            return self._payload(
                "error",
                action="list",
                room_id=resolved_room_id,
                thread_id=normalized_thread_id,
                tag=normalized_tag,
                include_tag=normalized_include_tag,
                exclude_tag=normalized_exclude_tag,
                message=str(exc),
            )

        tags = {}
        if state is not None and _thread_matches_tag_filters(
            state.tags,
            tag=normalized_tag,
            include_tag=normalized_include_tag,
            exclude_tag=normalized_exclude_tag,
        ):
            tags = _serialized_tags_for_output(state.tags, tag=normalized_tag)

        return self._payload(
            "ok",
            action="list",
            room_id=resolved_room_id,
            thread_id=normalized_thread_id,
            tag=normalized_tag,
            include_tag=normalized_include_tag,
            exclude_tag=normalized_exclude_tag,
            tags=tags,
        )
