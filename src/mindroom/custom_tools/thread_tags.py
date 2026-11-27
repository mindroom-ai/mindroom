"""Thread tagging tools for AI agents."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Literal

from mindroom.custom_tools.attachment_helpers import (
    resolve_canonical_tool_thread_target,
    resolve_context_thread_id,
    resolve_requested_room_id,
    room_access_allowed,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.matrix.room_history_reads import RoomThreadsPageError
from mindroom.thread_tag_vocabulary import (
    format_tag_vocabulary_for_description,
    load_tag_vocabulary_snapshot,
)
from mindroom.thread_tags import (
    RESOLVED_THREAD_TAG,
    SNOOZED_THREAD_TAG,
    ThreadTagRecord,
    ThreadTagsError,
    ThreadTagsListing,
    coerce_tag_name,
    list_tagged_threads,
    normalize_tag_name,
    remove_thread_tag,
    set_thread_tag,
)
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.runtime_context import ToolRuntimeContext
    from mindroom.tool_system.tool_function import ToolFunction


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


def _normalize_tool_tag(tag: str) -> str:
    """Preserve canonical IDs and otherwise coerce free-form model input."""
    try:
        return normalize_tag_name(tag)
    except ThreadTagsError:
        pass
    normalized_tag = coerce_tag_name(tag)
    if normalized_tag is None:
        msg = "tag must contain at least one letter, digit, or hyphen after normalization."
        raise ThreadTagsError(msg)
    return normalized_tag


_LIFECYCLE_TAG_TOOLS = {
    RESOLVED_THREAD_TAG: "thread_resolution",
    SNOOZED_THREAD_TAG: "thread_snooze",
}
_ALLOWED_PARAMS: dict[str, frozenset[str]] = {
    "tag": frozenset({"tag", "thread_id", "room_id", "note", "data"}),
    "untag": frozenset({"tag", "thread_id", "room_id", "canonical"}),
    "list": frozenset({"thread_id", "room_id", "tag", "include_tag", "exclude_tag", "include_untagged"}),
}
_BOOL_PARAMS = frozenset({"canonical", "include_untagged"})


def _validate_thread_tags_params(action: str, args: Mapping[str, object]) -> str | None:
    """Validate required and allowed parameters for one ``thread_tags`` action."""
    allowed = _ALLOWED_PARAMS.get(action)
    if allowed is None:
        return f"Unsupported thread_tags action {action!r}. Expected one of: tag, untag, list."
    unexpected = [
        name
        for name, value in args.items()
        if (bool(value) if name in _BOOL_PARAMS else value is not None) and name not in allowed
    ]
    if unexpected:
        names = ", ".join(f"`{name}`" for name in unexpected)
        return f"Parameter(s) {names} cannot be used with action={action!r}."
    tag = args.get("tag")
    if action in ("tag", "untag") and (not isinstance(tag, str) or not tag.strip()):
        return f"`tag` is required for action={action!r}."
    return None


def _normalize_topic_tool_tag(tag: str) -> str:
    """Normalize one topic tag and reject thread lifecycle state."""
    normalized_tag = _normalize_tool_tag(tag)
    owning_tool = _LIFECYCLE_TAG_TOOLS.get(normalized_tag)
    if owning_tool is not None:
        msg = (
            f"Tag {normalized_tag!r} is thread lifecycle state and cannot be changed with thread_tags. "
            f"Use {owning_tool} when that capability is explicitly configured."
        )
        raise ThreadTagsError(msg)
    return normalized_tag


class ThreadTagsTools(Toolkit):
    """Tool for tagging, untagging, and listing Matrix thread tags via shared room state."""

    def __init__(
        self,
        runtime_paths: RuntimePaths | None = None,
        current_room_id: str | None = None,
    ) -> None:
        super().__init__(name="thread_tags", tools=[self.thread_tags])
        self._vocabulary_line = (
            format_tag_vocabulary_for_description(load_tag_vocabulary_snapshot(runtime_paths, current_room_id))
            if runtime_paths is not None and current_room_id is not None
            else None
        )

    def offered_function(self, function: ToolFunction) -> ToolFunction:
        """Append the current room's ranked tag vocabulary to ``thread_tags`` when room context is available."""
        if function.name == "thread_tags" and self._vocabulary_line is not None:
            base = function.description or ""
            description = f"{base}\n{self._vocabulary_line}" if base else self._vocabulary_line
            return replace(function, description=description)
        return function

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_tags", status, False, **kwargs)

    async def thread_tags(  # noqa: PLR0911
        self,
        action: Literal["tag", "untag", "list"],
        tag: str | None = None,
        thread_id: str | None = None,
        room_id: str | None = None,
        note: str | None = None,
        data: dict[str, object] | None = None,
        canonical: bool = False,
        include_tag: str | None = None,
        exclude_tag: str | None = None,
        include_untagged: bool = False,
    ) -> str:
        """Add, remove, or list topic tags on Matrix threads (only pass parameters for the chosen action; `room_id` defaults to current room).

        - `tag`: add/update `tag` on `thread_id` (defaults to current thread); optional `room_id`, `note`, `data` (JSON object).
        - `untag`: remove `tag` from `thread_id` (defaults to current thread); optional `room_id`, `canonical=True` (skip root lookup).
        - `list`: inspect one thread or room-wide (`thread_id` omitted inspects current thread only when no `room_id`, tag filter, or `include_untagged=True` is passed; otherwise queries room-wide, e.g. unresolved: `action="list", exclude_tag="resolved", include_untagged=True`). Filters: `tag` (exact, excludes untagged), `include_tag` (has tag, excludes untagged), `exclude_tag` (lacks tag, untagged pass), `include_untagged=True` (enumerate every thread root via `/threads` with empty `tags`, adds `include_untagged` & `truncated`, incompatible with `thread_id`; defaults to False).

        Tags normalize to lowercase hyphenated slugs (canonical IDs <=50 chars preserved; new free-form tags <=25 chars, e.g. `"feature-request"`). Prefer existing tags (1-3 typical, <=5 max). Thread lifecycle state cannot be changed with this tool (use `thread_resolution` / `thread_snooze` when configured).
        """
        args = {k: v for k, v in locals().items() if k not in {"self", "action"}}
        if err := _validate_thread_tags_params(action, args):
            return self._payload("error", action=action, message=err)
        if isinstance(context := tool_context_or_error(self._payload, "Thread tags"), str):
            return context

        resolved_room_id, room_error = await resolve_requested_room_id(context, room_id)
        if room_error is not None:
            return self._payload("error", action=action, room_id=room_id, message=room_error)
        assert resolved_room_id is not None
        if not await room_access_allowed(context, resolved_room_id):
            return self._payload(
                "error",
                action=action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if action == "tag":
            assert tag is not None
            return await self._tag_thread(context, resolved_room_id, tag, thread_id=thread_id, note=note, data=data)
        if action == "untag":
            assert tag is not None
            return await self._untag_thread(context, resolved_room_id, tag, thread_id=thread_id, canonical=canonical)
        return await self._list_thread_tags(
            context,
            resolved_room_id,
            requested_room_id=room_id,
            thread_id=thread_id,
            tag=tag,
            include_tag=include_tag,
            exclude_tag=exclude_tag,
            include_untagged=include_untagged,
        )

    async def _tag_thread(
        self,
        context: ToolRuntimeContext,
        resolved_room_id: str,
        tag: str,
        *,
        thread_id: str | None,
        note: str | None,
        data: dict[str, object] | None,
    ) -> str:
        try:
            normalized_tag = _normalize_topic_tool_tag(tag)
        except ThreadTagsError as exc:
            return self._payload("error", action="tag", room_id=resolved_room_id, message=str(exc))

        thread_target = await resolve_canonical_tool_thread_target(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            allow_standalone_root=True,
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

    async def _untag_thread(
        self,
        context: ToolRuntimeContext,
        resolved_room_id: str,
        tag: str,
        *,
        thread_id: str | None,
        canonical: bool,
    ) -> str:
        try:
            normalized_tag = _normalize_topic_tool_tag(tag)
        except ThreadTagsError as exc:
            return self._payload("error", action="untag", room_id=resolved_room_id, message=str(exc))

        effective_thread_id = resolve_context_thread_id(context, room_id=resolved_room_id, thread_id=thread_id)
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
                allow_context_fallback=False,
                allow_standalone_root=True,
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

    async def _list_thread_tags(  # noqa: C901, PLR0911
        self,
        context: ToolRuntimeContext,
        resolved_room_id: str,
        *,
        requested_room_id: str | None,
        thread_id: str | None,
        tag: str | None,
        include_tag: str | None,
        exclude_tag: str | None,
        include_untagged: bool,
    ) -> str:
        if include_untagged and thread_id is not None:
            return self._payload(
                "error",
                action="list",
                room_id=resolved_room_id,
                thread_id=thread_id,
                message="`include_untagged=True` is only valid for room-wide queries; do not pass `thread_id`.",
            )

        try:
            normalized_tag = _normalize_tool_tag(tag) if tag is not None else None
            normalized_include_tag = _normalize_tool_tag(include_tag) if include_tag is not None else None
            normalized_exclude_tag = _normalize_tool_tag(exclude_tag) if exclude_tag is not None else None
        except ThreadTagsError as exc:
            return self._payload("error", action="list", room_id=resolved_room_id, message=str(exc))

        common: dict[str, object] = {
            "action": "list",
            "room_id": resolved_room_id,
            "tag": normalized_tag,
            "include_tag": normalized_include_tag,
            "exclude_tag": normalized_exclude_tag,
        }
        has_tag_filter = any(v is not None for v in (normalized_tag, normalized_include_tag, normalized_exclude_tag))
        effective_thread_id = resolve_context_thread_id(
            context,
            room_id=resolved_room_id,
            thread_id=thread_id,
            allow_context_fallback=requested_room_id is None and not include_untagged and not has_tag_filter,
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
                    **common,
                    "message": f"Failed to enumerate room thread roots: {exc.response}",
                    "response": exc.response,
                    "include_untagged": include_untagged,
                }
                if exc.errcode is not None:
                    error_payload["errcode"] = exc.errcode
                if exc.retry_after_ms is not None:
                    error_payload["retry_after_ms"] = exc.retry_after_ms
                return self._payload("error", **error_payload)
            except ThreadTagsError as exc:
                return self._payload("error", **common, include_untagged=include_untagged, message=str(exc))

            serialized_threads = {
                thread_root_id: _serialized_tags_for_output(state.tags, tag=normalized_tag)
                for thread_root_id, state in listing.tag_state.items()
            }
            payload_fields: dict[str, object] = {**common, "room_wide": True, "threads": serialized_threads}
            if include_untagged:
                payload_fields["include_untagged"] = True
                payload_fields["truncated"] = listing.truncated
            return self._payload("ok", **payload_fields)

        thread_target = await resolve_canonical_tool_thread_target(
            context,
            room_id=resolved_room_id,
            thread_id=effective_thread_id,
            allow_context_fallback=False,
            allow_standalone_root=True,
        )
        if thread_target.error is not None:
            return self._payload(
                "error",
                **common,
                thread_id=thread_target.requested_thread_id,
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
            return self._payload("error", **common, thread_id=normalized_thread_id, message=str(exc))

        state = listing.tag_state.get(normalized_thread_id)
        tags = _serialized_tags_for_output(state.tags, tag=normalized_tag) if state is not None else {}
        return self._payload("ok", **common, thread_id=normalized_thread_id, tags=tags)
