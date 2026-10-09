"""Move a Matrix thread into another room by copying its conversation."""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import nio
from agno.tools import Toolkit

from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    SKIP_MENTIONS_KEY,
    STREAM_STATUS_APPROVAL_PENDING,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
    TOOL_TRACE_CONTENT_KEY,
)
from mindroom.custom_tools.attachment_helpers import (
    resolve_canonical_tool_thread_target,
    resolve_requested_room_id,
    room_access_allowed,
)
from mindroom.custom_tools.tool_payloads import custom_tool_payload
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix.client_delivery import (
    MatrixDeliveryFailure,
    cached_room,
    resolve_room_encryption_outcome,
    send_message_result,
)
from mindroom.matrix.client_room_admin import get_room_members
from mindroom.matrix.conversation_reads import complete_thread_history
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.member_display_names import room_member_display_names
from mindroom.matrix.message_builder import build_thread_relation
from mindroom.matrix.thread_room_scan import resolve_thread_root_event_id_for_client
from mindroom.thread_tags import RESOLVED_THREAD_TAG, ThreadTagsError, get_thread_tags, set_thread_tag
from mindroom.tool_system.runtime_context import get_tool_runtime_context

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from mindroom.entity_resolution import EntityIdentityRegistry
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

# Everything else on a message is run, stream, delivery, relation, or relay
# state that belongs to the original event and would mislead the runtime or
# clients if it reappeared on a copy.
_COPIED_CONTENT_KEYS = (
    "msgtype",
    "body",
    "format",
    "formatted_body",
    "url",
    "file",
    "info",
    "filename",
    TOOL_TRACE_CONTENT_KEY,
)
_IN_PROGRESS_STREAM_STATUSES = frozenset(
    {STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING, STREAM_STATUS_APPROVAL_PENDING},
)
_MEDIA_MSGTYPES = frozenset({"m.image", "m.file", "m.audio", "m.video"})


@dataclass(frozen=True, slots=True)
class _PlannedCopy:
    """One message to re-post in the target thread, without its thread relation."""

    poster: str
    content: dict[str, Any]


def _plan_thread_copy(
    messages: Sequence[ResolvedVisibleMessage],
    *,
    entity_name_for_sender: Callable[[str], str | None],
    target_posters: frozenset[str],
    display_names: Mapping[str, str],
) -> list[_PlannedCopy]:
    """Return the posts that recreate one thread's conversation in another room.

    An entity that can post in the target room re-posts its own messages, so
    each agent still sees its earlier replies as its own turns. Everyone else
    is relayed by the router with visible attribution.
    """
    plan: list[_PlannedCopy] = []
    for message in messages:
        if message.content.get("msgtype") == "m.notice" or message.stream_status in _IN_PROGRESS_STREAM_STATUSES:
            continue
        content = {key: message.content[key] for key in _COPIED_CONTENT_KEYS if key in message.content}
        # Copies are history, never requests: no mention in them may wake an agent.
        content[SKIP_MENTIONS_KEY] = True
        content["m.mentions"] = {}
        poster = entity_name_for_sender(message.sender)
        author = message.sender
        relayed_author = message.content.get(ORIGINAL_SENDER_KEY)
        if poster is not None and isinstance(relayed_author, str) and relayed_author:
            # A managed sender's own relay, such as the router's voice transcript, names who spoke.
            author = relayed_author
            content[ORIGINAL_SENDER_KEY] = author
        if poster is None or poster not in target_posters:
            poster = ROUTER_AGENT_NAME
            _attribute_relay(content, author, display_names.get(author, author))
        plan.append(_PlannedCopy(poster=poster, content=content))
    return plan


def _attribute_relay(content: dict[str, Any], sender: str, name: str) -> None:
    """Name the original author on a router-posted copy."""
    # No source kind: the relay is attributed in prompts but never becomes a human turn.
    content[ORIGINAL_SENDER_KEY] = sender
    body = str(content.get("body", ""))
    if content.get("msgtype") in _MEDIA_MSGTYPES:
        # With a filename present, clients show the body as the caption.
        content["filename"] = content.get("filename") or body
    content["body"] = f"{name}: {body}"
    if "formatted_body" in content:
        content["formatted_body"] = f"<strong>{html.escape(name)}</strong>: {content['formatted_body']}"


@dataclass(frozen=True, slots=True)
class _PreparedMove:
    """A move whose preconditions all hold, ready to post."""

    target_room_id: str
    root_id: str
    last_event_id: str
    plan: list[_PlannedCopy]
    skipped: int
    poster_clients: dict[str, nio.AsyncClient]


class ThreadMoveTools(Toolkit):
    """Tools for moving a Matrix thread from the current room into another room."""

    def __init__(self) -> None:
        super().__init__(name="thread_move", tools=[self.move_thread])

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("thread_move", status, **kwargs)

    async def move_thread(self, room_id: str, thread_id: str | None = None) -> str:
        """Move the current or specified thread in the current room into another room.

        Copies the thread's messages into a new thread in the target room and copies its tags.
        The original thread gets a link to the copy and is marked resolved.

        Args:
            room_id: Target room ID, alias, or configured room name.
            thread_id: Thread root or reply event ID in the current room.
                Omit to move the active thread.

        """
        context = get_tool_runtime_context()
        if context is None:
            return self._payload("error", message="Thread move tool context is unavailable in this runtime path.")
        prepared = await _prepare_move(context, room_id, thread_id)
        if isinstance(prepared, str):
            return self._payload("error", message=prepared)

        via = MatrixID.parse(context.client.user_id).domain
        new_root_id: str | None = None
        previous_event_id: str | None = None
        for copied, planned in enumerate(prepared.plan):
            content = planned.content
            if new_root_id is not None:
                relation = build_thread_relation(new_root_id, latest_thread_event_id=previous_event_id)
                content = {**content, "m.relates_to": relation}
            delivered = await send_message_result(
                prepared.poster_clients[planned.poster],
                prepared.target_room_id,
                content,
                operation="thread_move",
            )
            if delivered is None:
                # The source thread stays untouched, so the user can retry or delete the partial copy.
                partial = {} if new_root_id is None else {"link": _permalink(prepared.target_room_id, new_root_id, via)}
                return self._payload(
                    "error",
                    message=f"Copied {copied} of {len(prepared.plan)} messages before a send failed.",
                    **partial,
                )
            new_root_id = new_root_id or delivered.event_id
            previous_event_id = delivered.event_id
        assert new_root_id is not None

        # The copy is complete, so later failures are reported rather than
        # failing the move: a retry would only duplicate the thread.
        link = _permalink(prepared.target_room_id, new_root_id, via)
        warnings: list[str] = []
        tags_copied = await _copy_tags(context, prepared, new_root_id, warnings)
        await _mark_source_moved(context, prepared, link, warnings)
        return self._payload(
            "ok",
            room_id=prepared.target_room_id,
            thread_id=new_root_id,
            link=link,
            copied=len(prepared.plan),
            skipped=prepared.skipped,
            tags_copied=tags_copied,
            warnings=warnings,
        )


async def _prepare_move(  # noqa: PLR0911
    context: ToolRuntimeContext,
    room_id: str,
    thread_id: str | None,
) -> _PreparedMove | str:
    """Check every precondition before anything is written, or return why the move is refused."""
    root_id, thread_error = await _source_thread_root(context, thread_id)
    if thread_error is not None:
        return thread_error
    assert root_id is not None
    target_room_id, room_error = await _target_room_id(context, room_id)
    if room_error is not None:
        return room_error
    assert target_room_id is not None
    if target_room_id == context.room_id:
        return "The thread is already in this room."
    # Membership comes first: an agent missing from the target room cannot
    # read its members, which the access check would report as a denial.
    registry = entity_identity_registry(context.current_config, context.runtime_paths)
    poster_clients = await _target_poster_clients(context, registry, target_room_id)
    if isinstance(poster_clients, str):
        return poster_clients
    if not await room_access_allowed(context, target_room_id):
        return "Not authorized to access the target room."
    history = await complete_thread_history(context.conversation_reader, context.room_id, root_id)
    if not history.is_full_history:
        return "This thread is too long to move."
    encryption_error = await _encryption_error(context.client, context.room_id, target_room_id)
    if encryption_error is not None:
        return encryption_error
    room = cached_room(context.client, context.room_id)
    plan = _plan_thread_copy(
        history,
        entity_name_for_sender=registry.current_entity_name_for_user_id,
        target_posters=frozenset(poster_clients),
        display_names=room_member_display_names(room) if room is not None else {},
    )
    if not plan:
        return "This thread has no messages to move."
    return _PreparedMove(
        target_room_id=target_room_id,
        root_id=root_id,
        last_event_id=history[-1].event_id,
        plan=plan,
        skipped=len(history) - len(plan),
        poster_clients=poster_clients,
    )


async def _source_thread_root(context: ToolRuntimeContext, thread_id: str | None) -> tuple[str | None, str | None]:
    """Return the root of the thread to move, or why none resolves; the active thread is already its root."""
    if thread_id is None and context.resolved_thread_id is not None:
        return context.resolved_thread_id, None
    target = await resolve_canonical_tool_thread_target(
        context,
        room_id=context.room_id,
        thread_id=thread_id,
        normalize_thread_id=lambda room, event_id: resolve_thread_root_event_id_for_client(
            context.client,
            room,
            event_id,
            relations=context.relations,
        ),
        fail_closed_on_normalization_error=True,
    )
    return target.canonical_thread_id, target.error


async def _target_room_id(context: ToolRuntimeContext, room_id: str) -> tuple[str | None, str | None]:
    """Resolve the target room from a room ID, a configured room name, or any Matrix alias."""
    target_room_id, error = resolve_requested_room_id(context, room_id)
    if error is not None:
        return None, error
    assert target_room_id is not None
    # Only aliases of configured rooms resolve locally; the user's own rooms resolve through Matrix.
    if target_room_id.startswith("#"):
        response = await context.client.room_resolve_alias(target_room_id)
        if isinstance(response, nio.RoomResolveAliasResponse):
            target_room_id = response.room_id
    if not target_room_id.startswith("!"):
        return None, f"Unknown room {room_id!r}; pass a room ID, alias, or configured room name."
    return target_room_id, None


async def _target_poster_clients(
    context: ToolRuntimeContext,
    registry: EntityIdentityRegistry,
    target_room_id: str,
) -> dict[str, nio.AsyncClient] | str:
    """Return the clients of running entities joined to the target room, or why the move cannot post there."""
    router_error = "The router must be in the target room to move a thread."
    orchestrator = context.orchestrator
    router_client = orchestrator.running_entity_client(ROUTER_AGENT_NAME) if orchestrator is not None else None
    if orchestrator is None or router_client is None:
        return router_error
    members = await get_room_members(router_client, target_room_id)
    if members is None or router_client.user_id not in members:
        return router_error
    if context.client.user_id not in members:
        return f"Invite {context.agent_name} to the target room before moving a thread there."
    clients: dict[str, nio.AsyncClient] = {}
    for entity_name, matrix_id in registry.current_ids.items():
        client = orchestrator.running_entity_client(entity_name)
        if client is not None and matrix_id.full_id in members:
            clients[entity_name] = client
    return clients


async def _encryption_error(client: nio.AsyncClient, source_room_id: str, target_room_id: str) -> str | None:
    """Refuse to re-post end-to-end encrypted content in plaintext."""
    source_encrypted = await resolve_room_encryption_outcome(client, source_room_id, operation="thread_move")
    target_encrypted = await resolve_room_encryption_outcome(client, target_room_id, operation="thread_move")
    if isinstance(source_encrypted, MatrixDeliveryFailure) or isinstance(target_encrypted, MatrixDeliveryFailure):
        return "Could not determine whether the source and target rooms are encrypted."
    if source_encrypted and not target_encrypted:
        return "Cannot move a thread from an encrypted room into an unencrypted room."
    return None


async def _copy_tags(
    context: ToolRuntimeContext,
    prepared: _PreparedMove,
    new_root_id: str,
    warnings: list[str],
) -> list[str]:
    """Copy every tag of the source thread to the new thread and return the copied tag names."""
    try:
        state = await get_thread_tags(context.client, context.room_id, prepared.root_id)
    except ThreadTagsError as exc:
        warnings.append(f"Could not read the thread's tags: {exc}")
        return []
    records = state.tags if state is not None else {}
    copied: list[str] = []
    for tag in sorted(records):
        try:
            await set_thread_tag(
                context.client,
                prepared.target_room_id,
                new_root_id,
                tag,
                set_by=context.requester_id,
                note=records[tag].note,
                data=records[tag].data,
            )
        except ThreadTagsError as exc:
            warnings.append(f"Could not copy tag {tag}: {exc}")
            continue
        copied.append(tag)
    return copied


async def _mark_source_moved(
    context: ToolRuntimeContext,
    prepared: _PreparedMove,
    link: str,
    warnings: list[str],
) -> None:
    """Point the original thread to its copy and mark it resolved."""
    notice = {
        "msgtype": "m.notice",
        "body": f"Moved to {link}",
        SKIP_MENTIONS_KEY: True,
        "m.mentions": {},
        "m.relates_to": build_thread_relation(prepared.root_id, latest_thread_event_id=prepared.last_event_id),
    }
    if await send_message_result(context.client, context.room_id, notice, operation="thread_move") is None:
        warnings.append("Could not post the move notice in the original thread.")
    try:
        await set_thread_tag(
            context.client,
            context.room_id,
            prepared.root_id,
            RESOLVED_THREAD_TAG,
            set_by=context.requester_id,
        )
    except ThreadTagsError as exc:
        warnings.append(f"Could not mark the original thread resolved: {exc}")


def _permalink(room_id: str, event_id: str, via: str) -> str:
    """Return a matrix.to link to one event; room IDs in recent room versions name no server, so pass one."""
    return f"https://matrix.to/#/{quote(room_id, safe='')}/{quote(event_id, safe='')}?via={quote(via, safe='')}"
