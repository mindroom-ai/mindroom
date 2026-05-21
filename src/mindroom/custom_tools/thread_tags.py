"""Thread tagging tools for AI agents."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit

from mindroom.custom_tools.attachment_helpers import (
    resolve_canonical_tool_thread_target,
    resolve_context_thread_id,
    resolve_requested_room_id,
    room_access_allowed,
)
from mindroom.custom_tools.tool_payloads import ordered_custom_tool_payload
from mindroom.matrix.client_thread_history import RoomThreadsPageError
from mindroom.matrix.conversation_cache import resolve_thread_root_event_id_for_client
from mindroom.thread_tags import (
    ThreadTagRecord,
    ThreadTagsError,
    ThreadTagsListing,
    list_tagged_threads,
    normalize_tag_name,
    remove_thread_tag,
    set_thread_tag,
)
from mindroom.tool_system.runtime_context import get_tool_runtime_context

# ruff: noqa: D406, D407, D413

if TYPE_CHECKING:
    from collections.abc import Mapping


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


class ThreadTagsTools(Toolkit):
    """Tools for tagging Matrix threads via shared room state."""

    def __init__(self) -> None:
        super().__init__(
            name="thread_tags",
            tools=[self.tag_thread, self.untag_thread, self.list_thread_tags],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return ordered_custom_tool_payload("thread_tags", status, **kwargs)

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

        resolved_room_id, room_error = resolve_requested_room_id(context, room_id)
        if room_error is not None:
            return self._payload(
                "error",
                action="tag",
                room_id=room_id,
                message=room_error,
            )
        assert resolved_room_id is not None
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

        thread_target = await resolve_canonical_tool_thread_target(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            normalize_thread_id=lambda normalize_room_id, normalize_event_id: resolve_thread_root_event_id_for_client(
                context.client,
                normalize_room_id,
                normalize_event_id,
                conversation_cache=context.conversation_cache,
            ),
        )
        if thread_target.error is not None:
            return self._payload(
                "error",
                action="tag",
                room_id=resolved_room_id,
                tag=normalized_tag,
                thread_id=thread_target.requested_thread_id,
                message=thread_target.error,
            )
        assert thread_target.canonical_thread_id is not None
        normalized_thread_id = thread_target.canonical_thread_id

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

        resolved_room_id, room_error = resolve_requested_room_id(context, room_id)
        if room_error is not None:
            return self._payload(
                "error",
                action="untag",
                room_id=room_id,
                message=room_error,
            )
        assert resolved_room_id is not None
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

        effective_thread_id = resolve_context_thread_id(
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
            thread_target = await resolve_canonical_tool_thread_target(
                context,
                room_id=resolved_room_id,
                thread_id=effective_thread_id,
                normalize_thread_id=lambda normalize_room_id, normalize_event_id: (
                    resolve_thread_root_event_id_for_client(
                        context.client,
                        normalize_room_id,
                        normalize_event_id,
                        conversation_cache=context.conversation_cache,
                    )
                ),
                allow_context_fallback=False,
            )
            if thread_target.error is not None:
                return self._payload(
                    "error",
                    action="untag",
                    room_id=resolved_room_id,
                    thread_id=thread_target.requested_thread_id,
                    tag=normalized_tag,
                    message=thread_target.error,
                )
            assert thread_target.canonical_thread_id is not None
            target_thread_id = thread_target.canonical_thread_id

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

    async def list_thread_tags(  # noqa: C901, D417, PLR0911, PLR0912
        self,
        thread_id: str | None = None,
        room_id: str | None = None,
        tag: str | None = None,
        include_tag: str | None = None,
        exclude_tag: str | None = None,
        include_untagged: bool = False,
    ) -> str:
        """List Matrix thread tags in a room.

        Inspect which threads in a room have which tags. Default mode returns only
        threads that have at least one tag stored as room state. Pass
        `include_untagged=True` to also surface every other thread root in the room
        (threads with no tags appear with an empty `tags` dict). This enables the
        headline query for "what threads are still unresolved?":

            list_thread_tags(exclude_tag="resolved", include_untagged=True)

        Parameters:
          room_id: Room to query. Defaults to the current Matrix tool runtime
            room when invoked from a runtime context.
          thread_id: When set, restrict the query to a single thread root. Incompatible
            with `include_untagged=True` (raises a validation error).
          tag: Return only matching threads that carry this exact tag. In room-wide
            mode, untagged threads are filtered out.
          include_tag: Filter to threads that have this tag. Untagged threads are
            filtered out.
          exclude_tag: Filter to threads that do NOT have this tag. Untagged threads
            pass (they have no tags, so no excluded tag is present).
          include_untagged: When True, also enumerate every thread root in the room
            via Matrix `/threads` and synthesize empty-tag entries for ones with no
            tag state. The payload then also includes `include_untagged: true` and
            `truncated: bool`. Untagged threads are filtered out by `tag=` or
            `include_tag=`; use `exclude_tag=` alone for the unresolved-threads query.
            Defaults to False.

        Returns:
          Thread-specific queries return a JSON object with `room_id`,
          `thread_id`, and `tags`.

          Room-wide queries return a JSON object with `room_id`, `room_wide`,
          and `threads` (mapping of thread_id -> tag dict). On success with
          `include_untagged=True`, the payload also includes
          `include_untagged: bool` and `truncated: bool`.
        """
        context = get_tool_runtime_context()
        if context is None:
            return self._context_error()

        resolved_room_id, room_error = resolve_requested_room_id(context, room_id)
        if room_error is not None:
            return self._payload(
                "error",
                action="list",
                room_id=room_id,
                message=room_error,
            )
        assert resolved_room_id is not None
        if not room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action="list",
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if include_untagged and thread_id is not None:
            return self._payload(
                "error",
                action="list",
                room_id=resolved_room_id,
                thread_id=thread_id,
                message="`include_untagged=True` is only valid for room-wide queries; do not pass `thread_id`.",
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

        effective_thread_id = resolve_context_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            allow_context_fallback=room_id is None and not include_untagged,
        )
        if effective_thread_id is None:
            try:
                listing: ThreadTagsListing = await list_tagged_threads(
                    context.client,
                    resolved_room_id,
                    tag=normalized_tag,
                    include_tag=normalized_include_tag,
                    exclude_tag=normalized_exclude_tag,
                    include_untagged=include_untagged,
                )
            except RoomThreadsPageError as exc:
                error_payload: dict[str, object] = {
                    "action": "list",
                    "response": exc.response,
                    "room_id": resolved_room_id,
                    "tag": normalized_tag,
                    "include_tag": normalized_include_tag,
                    "exclude_tag": normalized_exclude_tag,
                    "include_untagged": include_untagged,
                }
                if exc.errcode is not None:
                    error_payload["errcode"] = exc.errcode
                if exc.retry_after_ms is not None:
                    error_payload["retry_after_ms"] = exc.retry_after_ms
                return self._payload("error", **error_payload)
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

            serialized_threads = {
                thread_root_id: _serialized_tags_for_output(state.tags, tag=normalized_tag)
                for thread_root_id, state in listing.tag_state.items()
            }
            payload_fields: dict[str, object] = {
                "action": "list",
                "room_id": resolved_room_id,
                "room_wide": True,
                "tag": normalized_tag,
                "include_tag": normalized_include_tag,
                "exclude_tag": normalized_exclude_tag,
                "threads": serialized_threads,
            }
            if include_untagged:
                payload_fields["include_untagged"] = True
                payload_fields["truncated"] = listing.truncated

            return self._payload("ok", **payload_fields)

        thread_target = await resolve_canonical_tool_thread_target(
            context,
            room_id=resolved_room_id,
            thread_id=effective_thread_id,
            normalize_thread_id=lambda normalize_room_id, normalize_event_id: resolve_thread_root_event_id_for_client(
                context.client,
                normalize_room_id,
                normalize_event_id,
                conversation_cache=context.conversation_cache,
            ),
            allow_context_fallback=False,
        )
        if thread_target.error is not None:
            return self._payload(
                "error",
                action="list",
                room_id=resolved_room_id,
                thread_id=thread_target.requested_thread_id,
                tag=normalized_tag,
                include_tag=normalized_include_tag,
                exclude_tag=normalized_exclude_tag,
                message=thread_target.error,
            )
        assert thread_target.canonical_thread_id is not None
        normalized_thread_id = thread_target.canonical_thread_id

        try:
            listing = await list_tagged_threads(
                context.client,
                resolved_room_id,
                tag=normalized_tag,
                include_tag=normalized_include_tag,
                exclude_tag=normalized_exclude_tag,
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
        state = listing.tag_state.get(normalized_thread_id)
        if state is not None:
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
