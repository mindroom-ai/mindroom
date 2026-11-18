"""Native Matrix room introspection, agent discovery, and history actions."""

from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict, deque
from dataclasses import asdict, replace
from threading import Lock
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

import nio
from aiohttp import ClientError

from mindroom.approval.manager import get_approval_store
from mindroom.custom_tools.attachment_helpers import resolve_requested_room_id, room_access_allowed
from mindroom.custom_tools.matrix_agent_discovery import available_room_agents
from mindroom.custom_tools.matrix_helpers import check_rate_limit
from mindroom.custom_tools.tool_payloads import custom_tool_payload, tool_context_or_error
from mindroom.entity_resolution import persisted_bot_user_ids
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import (
    message_preview,
    thread_root_body_preview,
    trusted_visible_sender_ids,
)
from mindroom.matrix.joined_members import read_joined_members
from mindroom.matrix.room_history_reads import RoomThreadsPageError, get_room_threads_page
from mindroom.requester_identity import equivalent_requester_ids
from mindroom.thread_summary.state import current_thread_summary
from mindroom.thread_workflow import latest_reply_question_ts, room_thread_facts, thread_workflow
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)


class MatrixRoomTools(Toolkit):
    """Native Matrix room introspection actions."""

    _rate_limit_lock: ClassVar[Lock] = Lock()
    _recent_actions: ClassVar[dict[tuple[str, str, str], deque[float]]] = defaultdict(deque)
    _RATE_LIMIT_WINDOW_SECONDS: ClassVar[float] = 30.0
    _RATE_LIMIT_MAX_ACTIONS: ClassVar[int] = 20
    _DEFAULT_THREAD_LIMIT: ClassVar[int] = 20
    _MAX_THREAD_LIMIT: ClassVar[int] = 50
    # Each summary reads a whole thread, which is a homeserver walk the first time; a few at once keep a full page fast.
    _SUMMARY_READ_CONCURRENCY: ClassVar[int] = 6
    _MAX_STATE_EVENTS: ClassVar[int] = 100

    def __init__(self) -> None:
        super().__init__(
            name="matrix_room",
            tools=[self.matrix_room],
        )

    @staticmethod
    def _payload(status: str, **kwargs: object) -> str:
        return custom_tool_payload("matrix_room", status, **kwargs)

    @classmethod
    def _thread_limit(cls, limit: int | None) -> int:
        """Clamp thread limit to [1, 50], defaulting to 20."""
        if limit is None:
            return cls._DEFAULT_THREAD_LIMIT
        return max(1, min(limit, cls._MAX_THREAD_LIMIT))

    @staticmethod
    def _thread_metadata(source: dict[str, Any]) -> dict[str, Any]:
        """Return the event's bundled `m.thread` aggregation, empty when any level of it is malformed."""
        unsigned = source.get("unsigned")
        relations = unsigned.get("m.relations") if isinstance(unsigned, dict) else None
        thread_metadata = relations.get("m.thread") if isinstance(relations, dict) else None
        return thread_metadata if isinstance(thread_metadata, dict) else {}

    def _thread_row(self, room_id: str, event: nio.Event) -> dict[str, object] | None:
        """Return a well-formed root's row from its bundled metadata; its `body_preview` is read later."""
        event_id = event.event_id
        sender = event.sender
        timestamp = event.server_timestamp
        source = event.source
        if (
            not isinstance(event_id, str)
            or not isinstance(sender, str)
            or not isinstance(timestamp, int)
            or isinstance(timestamp, bool)
            or not isinstance(source, dict)
        ):
            logger.warning(
                "Skipping malformed room thread root",
                room_id=room_id,
                event_type=type(event).__name__,
            )
            return None
        thread_metadata = self._thread_metadata(source)
        reply_count = thread_metadata.get("count")
        latest_event = thread_metadata.get("latest_event")
        latest_activity_ts = latest_event.get("origin_server_ts") if isinstance(latest_event, dict) else None
        payload: dict[str, object] = {
            "thread_id": event_id,
            "sender": sender,
            "timestamp": timestamp,
            "body_preview": "",
            "reply_count": reply_count if isinstance(reply_count, int) and not isinstance(reply_count, bool) else 0,
        }
        if isinstance(latest_activity_ts, int) and not isinstance(latest_activity_ts, bool):
            payload["latest_activity_ts"] = latest_activity_ts
        return payload

    async def _add_current_summaries(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        threads: list[dict[str, object]],
        trusted_sender_ids: frozenset[str],
    ) -> None:
        membership_index = context.require_agent_reply_memberships()
        reads = asyncio.Semaphore(self._SUMMARY_READ_CONCURRENCY)

        async def add_current_summary(thread: dict[str, object]) -> None:
            async with reads:
                try:
                    current_summary = await current_thread_summary(
                        context.client,
                        cast("str", thread.get("room_id", room_id)),
                        cast("str", thread["thread_id"]),
                        config=context.config,
                        runtime_paths=context.runtime_paths,
                        conversation_reader=context.conversation_reader,
                        entity_name=context.agent_name,
                        membership_index=membership_index,
                        trusted_sender_ids=trusted_sender_ids,
                    )
                except Exception:
                    if "room_id" not in thread:  # A named listing fails as it always has.
                        raise
                    logger.warning("matrix_room_threads_summary_unreadable", room_id=thread["room_id"], exc_info=True)
                    return
            if current_summary is not None:
                thread["summary"] = current_summary.summary
                thread["summary_pinned"] = current_summary.pinned

        await asyncio.gather(*(add_current_summary(thread) for thread in threads))

    @classmethod
    def _check_rate_limit(
        cls,
        context: ToolRuntimeContext,
        room_id: str,
    ) -> str | None:
        return check_rate_limit(
            lock=cls._rate_limit_lock,
            recent_actions=cls._recent_actions,
            window_seconds=cls._RATE_LIMIT_WINDOW_SECONDS,
            max_actions=cls._RATE_LIMIT_MAX_ACTIONS,
            tool_name="matrix_room",
            context=context,
            room_id=room_id,
        )

    @staticmethod
    def _transport_error_message(exc: TimeoutError | ClientError) -> str:
        detail = str(exc).strip()
        suffix = f": {detail}" if detail else ""
        return f"Matrix request failed ({type(exc).__name__}){suffix}."

    async def _room_info(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
    ) -> str:
        cached_room: nio.MatrixRoom | None = context.client.rooms.get(room_id)
        if cached_room is None:
            return self._payload(
                "error",
                action="room-info",
                room_id=room_id,
                message="Room not found in client state.",
            )

        power_levels = cached_room.power_levels
        power_summary: dict[str, Any] = {}
        if power_levels is not None:
            power_summary = {
                "ban": power_levels.defaults.ban,
                "invite": power_levels.defaults.invite,
                "kick": power_levels.defaults.kick,
                "redact": power_levels.defaults.redact,
                "state_default": power_levels.defaults.state_default,
                "events_default": power_levels.defaults.events_default,
                "users_default": power_levels.defaults.users_default,
            }

        creator: str | None = None
        try:
            create_response = await context.client.room_get_state_event(room_id, "m.room.create")
        except (ClientError, TimeoutError):
            create_response = None
        if isinstance(create_response, nio.RoomGetStateEventResponse):
            content = create_response.content
            creator = content.get("creator") if isinstance(content, dict) else None

        return self._payload(
            "ok",
            action="room-info",
            room_id=room_id,
            thread_id=context.resolved_thread_id if room_id == context.room_id else None,
            reply_to_event_id=context.reply_to_event_id if room_id == context.room_id else None,
            requester_id=context.requester_id,
            agent_name=context.agent_name,
            name=cached_room.name,
            topic=cached_room.topic,
            member_count=cached_room.member_count,
            encrypted=cached_room.encrypted,
            join_rule=cached_room.join_rule,
            canonical_alias=cached_room.canonical_alias,
            room_version=cached_room.room_version,
            guest_access=cached_room.guest_access,
            power_levels_summary=power_summary,
            creator=creator,
        )

    async def _members(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
    ) -> str:
        try:
            response = await read_joined_members(context.client, room_id)
        except (ClientError, TimeoutError) as exc:
            return self._payload(
                "error",
                action="members",
                room_id=room_id,
                message=self._transport_error_message(exc),
            )
        if not isinstance(response, nio.JoinedMembersResponse):
            return self._payload(
                "error",
                action="members",
                room_id=room_id,
                message=f"Failed to fetch members: {response}",
            )

        cached_room: nio.MatrixRoom | None = context.client.rooms.get(room_id)
        power_levels = cached_room.power_levels if cached_room is not None else None

        members_list: list[dict[str, Any]] = []
        for member in response.members:
            member_info: dict[str, Any] = {
                "user_id": member.user_id,
                "display_name": member.display_name,
                "avatar_url": member.avatar_url,
            }
            if power_levels is not None:
                member_info["power_level"] = power_levels.get_user_level(member.user_id)
            members_list.append(member_info)

        return self._payload(
            "ok",
            action="members",
            room_id=room_id,
            count=len(members_list),
            members=members_list,
        )

    @staticmethod
    def _labelling_identities(context: ToolRuntimeContext) -> tuple[int, frozenset[str], set[str]]:
        """Return the clock, this runtime's configured bot accounts and the requester's own IDs, read once per call."""
        config, paths = context.config, context.runtime_paths
        bot_ids = persisted_bot_user_ids(paths) & trusted_visible_sender_ids(config, paths)
        return int(time.time() * 1000), bot_ids, equivalent_requester_ids(context.requester_id, config, paths)

    async def _thread_labels(
        self,
        context: ToolRuntimeContext,
        room_id: str,
        thread_roots: list[nio.Event],
        identities: tuple[int, frozenset[str], set[str]] | None,
    ) -> dict[str, dict[str, object]] | None:
        """Label each root from one room-state read, the bots' running replies and the approval journal."""
        if identities is None:
            return None
        now, bot_ids, requester_ids = identities
        try:
            state = await context.client.room_get_state(room_id)
        except (ClientError, TimeoutError) as exc:
            raise RoomThreadsPageError(response=f"{type(exc).__name__}: {exc}") from exc
        if not isinstance(state, nio.RoomGetStateResponse):
            raise RoomThreadsPageError(
                response=f"Failed to fetch room state: {state}",
                errcode=getattr(state, "status_code", None),
                retry_after_ms=getattr(state, "retry_after_ms", None),
            )
        facts = room_thread_facts(room_id, state, context.runtime_paths, bot_ids, now)
        bots = context.orchestrator.agent_bots.values() if context.orchestrator is not None else ()
        replying = set[str | None]().union(*(bot.active_thread_ids_for_room(room_id) for bot in bots))
        manager = get_approval_store()
        try:
            approvals = (
                {}
                if manager is None
                else await manager.pending_approval_threads(room_id=room_id, approver_user_ids=requester_ids)
            )
        except Exception as exc:
            raise RoomThreadsPageError(response=f"Failed to read pending approvals: {exc}") from exc
        return {
            event.event_id: thread_workflow(
                facts.get(event.event_id, {}),
                now=now,
                replying=event.event_id in replying,
                approval_ts=approvals.get(event.event_id),
                # A question counts only in a thread the requester started (design decision 8).
                question_ts=latest_reply_question_ts(self._thread_metadata(event.source), bot_ids)
                if event.sender in requester_ids and isinstance(event.source, dict)
                else None,
            )
            for event in thread_roots
        }

    def _thread_rows(
        self,
        room_id: str,
        thread_roots: list[nio.Event],
        labels: dict[str, dict[str, object]] | None,
        workflow: str | None,
    ) -> list[tuple[dict[str, object], nio.Event]]:
        """Return each well-formed root of the asked group as its labelled row, beside the event to preview."""
        rows: list[tuple[dict[str, object], nio.Event]] = []
        for event in thread_roots:
            label = {} if labels is None else labels.get(event.event_id, {})
            if workflow in (None, "all", label.get("workflow")) and (row := self._thread_row(room_id, event)):
                rows.append((row | label, event))
        return rows

    async def _threads_payload(
        self,
        context: ToolRuntimeContext,
        rows: list[tuple[dict[str, object], nio.Event]],
        *,
        room_id: str,
        include_summaries: bool,
        **listing: object,
    ) -> str:
        """Read the previews, and with `include_summaries` the summaries, of the rows to return, and list them."""
        trusted_sender_ids = trusted_visible_sender_ids(context.config, context.runtime_paths)
        for row, event in rows:
            try:
                row["body_preview"] = await thread_root_body_preview(
                    event,
                    client=context.client,
                    config=context.config,
                    runtime_paths=context.runtime_paths,
                    trusted_sender_ids=trusted_sender_ids,
                )
            except Exception:
                if "room_id" not in row:  # A named listing fails as it always has.
                    raise
                logger.warning("matrix_room_threads_preview_unreadable", room_id=row["room_id"], exc_info=True)
        threads_list = [row for row, _event in rows]
        if include_summaries:
            await self._add_current_summaries(
                context,
                room_id=room_id,
                threads=threads_list,
                trusted_sender_ids=trusted_sender_ids,
            )
        return self._payload(
            "ok",
            action="threads",
            room_id=room_id,
            count=len(threads_list),
            threads=threads_list,
            **listing,
        )

    async def _threads(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        limit: int,
        page_token: str | None,
        include_summaries: bool,
        workflow: str | None,
    ) -> str:
        try:
            thread_roots, next_token = await get_room_threads_page(
                context.client,
                room_id,
                limit=limit,
                page_token=page_token,
            )
            identities = None if workflow is None else self._labelling_identities(context)
            labels = await self._thread_labels(context, room_id, thread_roots, identities)
        except RoomThreadsPageError as exc:
            error_payload: dict[str, object] = {
                "action": "threads",
                "response": exc.response,
                "room_id": room_id,
            }
            if exc.errcode is not None:
                error_payload["errcode"] = exc.errcode
            if exc.retry_after_ms is not None:
                error_payload["retry_after_ms"] = exc.retry_after_ms
            return self._payload("error", **error_payload)

        return await self._threads_payload(
            context,
            self._thread_rows(room_id, thread_roots, labels, workflow),
            room_id=room_id,
            include_summaries=include_summaries,
            next_token=next_token,
            has_more=next_token is not None,
        )

    async def _all_rooms_threads(
        self,
        context: ToolRuntimeContext,
        *,
        limit: int,
        page_token: str | None,
        include_summaries: bool,
        workflow: str | None,
    ) -> str:
        """List one page of threads of every room the requester is joined to and shares with you, waiting rows first."""
        message = self._check_rate_limit(context, context.room_id)
        if message is None and (page_token or "").strip():
            message = "page_token cannot be used with room_id='all'; name one room to page through it."
        if message is not None:
            return self._payload("error", action="threads", room_id="all", message=message)
        identities = self._labelling_identities(context) if workflow is not None else None
        reads = asyncio.Semaphore(self._SUMMARY_READ_CONCURRENCY)

        async def read_room(room_id: str) -> tuple[list[tuple[dict[str, object], nio.Event]], bool, Exception | None]:
            """Return a shared room's rows, whether it has older threads, and why reading it failed."""
            async with reads:
                # Privacy boundary: a room is read only once the requester is proven joined to it now.
                try:
                    shared = await room_access_allowed(context, room_id, contact_room_ok=True, require_membership=True)
                except (ClientError, TimeoutError):
                    shared = False
                if not shared:
                    return [], False, None
                try:
                    roots, next_token = await get_room_threads_page(context.client, room_id, limit=limit)
                    labels = await self._thread_labels(context, room_id, roots, identities)
                except Exception as exc:  # One unreadable room must not hide the others.
                    logger.warning("matrix_room_threads_room_unreadable", room_id=room_id, exc_info=True)
                    return [], False, exc
            rows = self._thread_rows(room_id, roots, labels, workflow)
            return [({"room_id": room_id, **row}, event) for row, event in rows], next_token is not None, None

        room_ids = list(context.client.rooms)
        results = await asyncio.gather(*(read_room(room_id) for room_id in room_ids))
        rows = sorted(
            (row for room_rows, _more, _failed in results for row in room_rows),
            key=lambda row: (
                row[0].get("why") not in ("approval", "question"),
                -cast("int", row[0].get("latest_activity_ts", row[0]["timestamp"])),
            ),
        )
        failed = [(room_id, exc) for room_id, (_rows, _more, exc) in zip(room_ids, results, strict=True) if exc]
        hints = [e.retry_after_ms for _id, e in failed if isinstance(e, RoomThreadsPageError) and e.retry_after_ms]
        return await self._threads_payload(
            context,
            rows[:limit],
            room_id="all",
            include_summaries=include_summaries,
            next_token=None,
            has_more=len(rows) > limit or any(more for _rows, more, _failed in results),
            **({"failed_room_ids": sorted(room_id for room_id, _exc in failed)} if failed else {}),
            **({"retry_after_ms": max(hints)} if hints else {}),
        )

    async def _state(
        self,
        context: ToolRuntimeContext,
        *,
        room_id: str,
        event_type: str | None,
        state_key: str,
    ) -> str:
        if event_type is not None:
            try:
                response = await context.client.room_get_state_event(
                    room_id,
                    event_type,
                    state_key,
                )
            except (ClientError, TimeoutError) as exc:
                return self._payload(
                    "error",
                    action="state",
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key,
                    message=self._transport_error_message(exc),
                )
            if isinstance(response, nio.RoomGetStateEventResponse):
                return self._payload(
                    "ok",
                    action="state",
                    room_id=room_id,
                    event_type=event_type,
                    state_key=state_key,
                    content=response.content,
                )
            return self._payload(
                "error",
                action="state",
                room_id=room_id,
                event_type=event_type,
                state_key=state_key,
                message=f"Failed to fetch state event: {response}",
            )

        try:
            response = await context.client.room_get_state(room_id)
        except (ClientError, TimeoutError) as exc:
            return self._payload(
                "error",
                action="state",
                room_id=room_id,
                message=self._transport_error_message(exc),
            )
        if not isinstance(response, nio.RoomGetStateResponse):
            return self._payload(
                "error",
                action="state",
                room_id=room_id,
                message=f"Failed to fetch room state: {response}",
            )

        state_events: list[dict[str, Any]] = []
        type_counts: dict[str, int] = defaultdict(int)
        for event_dict in response.events:
            etype = event_dict.get("type", "")
            type_counts[etype] += 1
            if etype == "m.room.member":
                continue
            if len(state_events) >= self._MAX_STATE_EVENTS:
                continue
            content = event_dict.get("content", {})
            state_events.append(
                {
                    "type": etype,
                    "state_key": event_dict.get("state_key", ""),
                    "content_preview": message_preview(json.dumps(content)),
                },
            )

        state_summary = dict(sorted(type_counts.items()))

        return self._payload(
            "ok",
            action="state",
            room_id=room_id,
            count=len(state_events),
            state_summary=state_summary,
            events=state_events,
        )

    async def matrix_room(  # noqa: PLR0911
        self,
        action: Literal["room-info", "members", "agents", "threads", "state"] = "room-info",
        room_id: str | None = None,
        limit: int | None = None,
        event_type: str | None = None,
        state_key: str | None = None,
        page_token: str | None = None,
        include_summaries: bool = False,
        workflow: Literal["needs_you", "working", "later", "all"] | None = None,
    ) -> str:
        """Inspect Matrix room metadata, available agents, members, threads, and state.

        Actions:
        - room-info: Room metadata and current thread_id, reply_to_event_id, requester_id, and agent_name.
        - members: List joined members with display names and power levels.
        - agents: List agents and teams available to answer this requester in the room, including yourself.
          Each row has name, matrix_user_id, description, and thread_mode (thread or room).
          To start a conversation, use matrix_message(recipient=name, message="...").
          This lists conversation targets; run_subagent separately lists your allowed subagents.
        - threads: List thread roots with preview, sender, timestamp, reply count, and latest activity when available.
          With include_summaries=True, each row also has summary (the current thread title, or null) and
          summary_pinned (true when the thread's title is pinned, so automatic summaries leave it alone); both are omitted
          when the thread's full history is unavailable. This reads every listed thread, so pass it only when needed.
          Use page_token from a previous response's next_token to paginate.
          workflow labels rows (workflow: needs_you, working, later, done) and keeps one group, or every row with all.
          needs_you rows have why: approval or question (waits on the requester; lead with these) or your_turn (idle).
          later rows have back_at (ms) when known. A filtered page can hold fewer rows than limit.
          Approximate: questions show only in unencrypted rooms; other runtimes' approvals and replies are unseen.
          room_id="all" (threads only) covers every room the requester shares with you; rows carry room_id.
          It reads one page per room, so name a room and use page_token to go further.
        - state: Read room state. If event_type is given, return that specific state event.
          If omitted, return a summary of all state events (m.room.member events are elided).

        room_id defaults to the current room. limit (default 20, max 50) and include_summaries apply to threads.
        """
        if isinstance(context := tool_context_or_error(self._payload, "Matrix room"), str):
            return context

        context = replace(context, config=context.current_config, config_provider=None)
        if action == "threads" and (room_id or "").strip() == "all":
            return await self._all_rooms_threads(
                context,
                limit=self._thread_limit(limit),
                page_token=page_token,
                include_summaries=include_summaries,
                workflow=workflow,
            )
        resolved_room_id, room_error = await resolve_requested_room_id(context, room_id)
        if room_error is not None or resolved_room_id is None:
            return self._payload("error", action=action, message=room_error)
        if not await room_access_allowed(context, resolved_room_id, contact_room_ok=True):
            return self._payload(
                "error",
                action=action,
                room_id=resolved_room_id,
                message="Not authorized to access the target room.",
            )

        if (limit_error := self._check_rate_limit(context, resolved_room_id)) is not None:
            return self._payload(
                "error",
                action=action,
                room_id=resolved_room_id,
                message=limit_error,
            )

        if action == "room-info":
            return await self._room_info(context, room_id=resolved_room_id)
        if action == "members":
            return await self._members(context, room_id=resolved_room_id)
        if action == "agents":
            agents = await available_room_agents(context, resolved_room_id)
            return self._payload(
                "ok",
                action="agents",
                room_id=resolved_room_id,
                agents=[asdict(agent) for agent in agents],
            )
        if action == "threads":
            return await self._threads(
                context,
                room_id=resolved_room_id,
                limit=self._thread_limit(limit),
                page_token=(page_token or "").strip() or None,
                include_summaries=include_summaries,
                workflow=workflow,
            )
        return await self._state(
            context,
            room_id=resolved_room_id,
            event_type=(event_type or "").strip() or None,
            state_key=(state_key or "").strip(),
        )
