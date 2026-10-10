"""Regenerate the reply to an edited message when it is the latest in its conversation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.coalescing_batch import coalesced_prompt, tagged_coalesced_prompt
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_source import EDIT_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import hook_ingress_policy
from mindroom.logging_config import get_logger
from mindroom.matrix.client_visible_messages import extract_visible_edit_body
from mindroom.matrix.member_display_names import room_member_display_names
from mindroom.reply_lifecycle import ReplyState
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_runner import ResponseRequest
from mindroom.response_sources import ResponseSources
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.timestamp_formatting import normalize_timestamp_ms
from mindroom.turn_record import canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Sequence

    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.handled_turns import SourceEventRevision, TurnRecord
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.event_info import EventInfo
    from mindroom.message_target import MessageTarget
    from mindroom.reply_lifecycle import Reply
    from mindroom.turn_policy import IngressHookRunner
    from mindroom.turn_store import TurnStore


logger = get_logger(__name__)


class _TrackInboxResponse(Protocol):
    """Own one response task off the room's event lane, as ``ResponseRunner.track_inbox_response`` does."""

    def __call__(
        self,
        response: Coroutine[Any, Any, None],
        *,
        name: str,
        room_id: str,
        recovery_proof_ready: Callable[[], bool | Awaitable[bool]],
        source_event_ids: tuple[str, ...] = (),
    ) -> asyncio.Task[None]: ...


@dataclass(frozen=True)
class EditRegeneratorDeps:
    """Collaborators needed for edit-triggered regeneration."""

    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    agent_name: str
    resolver: ConversationResolver
    turn_store: TurnStore
    ingress_hook_runner: IngressHookRunner
    # Runs one regeneration through the bot's response path.
    generate_response: Callable[[ResponseRequest], Awaitable[object]]
    track_inbox_response: _TrackInboxResponse
    # Settles sources nothing will answer, as the room's lane does for an event it ignores.
    settle_sources: Callable[[tuple[str, ...]], Awaitable[None]]
    # Records a Stop on a reply that still runs, as a Stop reaction would.
    stop_reply: Callable[[Reply, int], Awaitable[object]]
    receipt_order: Callable[[], Awaitable[int]]
    timestamp_formatter: Callable[[float | None], str | None]
    # The newest reply answering any of these sources, from the reply records.
    reply_for_sources: Callable[[tuple[str, ...]], Awaitable[Reply | None]]
    # Whether a human wrote in the turn's conversation after its sources.
    later_human_message: Callable[[TurnRecord], Awaitable[bool]]


@dataclass(frozen=True)
class _EditedSource:
    """The message an edit changed, the edit's revision, and the answer it regenerates."""

    source_event_id: str
    revision: SourceEventRevision
    reply_event_id: str | None


def _regenerable(reply: Reply | None) -> bool:
    """Return whether an edit regenerates this reply: one that showed something and is not gone."""
    return reply is not None and reply.event_id is not None and reply.state is not ReplyState.GONE


@dataclass
class EditRegenerator:
    """Re-run the reply to the latest message of a conversation when its author edits it."""

    deps: EditRegeneratorDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for edit regeneration"
            raise RuntimeError(msg)
        return client

    async def _edit_regeneration_context(
        self,
        context: MessageContext,
        room: nio.MatrixRoom,
        *,
        conversation_target: MessageTarget,
    ) -> MessageContext:
        """Return edit context aligned with the recorded thread root."""
        if (
            conversation_target.resolved_thread_id is None
            or context.thread_id == conversation_target.resolved_thread_id
        ):
            return context
        thread_history = await self.deps.resolver.fetch_thread_history(
            room.room_id,
            conversation_target.resolved_thread_id,
        )
        return MessageContext(
            am_i_mentioned=context.am_i_mentioned,
            is_thread=True,
            thread_id=conversation_target.resolved_thread_id,
            thread_history=thread_history,
            mentioned_agents=context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            replay_guard_history=thread_history,
            requires_model_history_refresh=context.requires_model_history_refresh,
        )

    async def handle_message_edit(  # noqa: C901, PLR0911, PLR0912
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        event_info: EventInfo,
        requester_user_id: str,
    ) -> bool | None:
        """Regenerate the reply an edit's message got; True when its regeneration owns the edit.

        Only the latest message of its conversation regenerates; a reply that
        still runs, or one an approval holds, is stopped first, which cancels
        that approval. Any other edit changes nothing the agent did.
        """
        if not event_info.original_event_id:
            return None
        original_event_id = event_info.original_event_id
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        if registry.current_entity_name_for_user_id(event.sender):
            return None

        # Every decision below is taken from the record's own conversation target, never from the thread the
        # edit names.
        turn_record = await self.deps.turn_store.load_turn(original_event_id)
        if (
            turn_record is None
            or turn_record.conversation_target is None
            or turn_record.history_scope is None
            or turn_record.response_owner != self.deps.agent_name
        ):
            return None
        if turn_record.requester_id_for_source(original_event_id) != requester_user_id:
            return None
        # Regeneration replays every source under the editor's identity, so a
        # record mixing senders would run their messages as the editor.
        if not turn_record.replay_sources_all_from_requester(requester_user_id):
            return None
        # A requester owns replies an entity wrote for them without having written them.
        if any(metadata.speaker is not None for metadata in (turn_record.source_event_metadata or {}).values()):
            return None
        if original_event_id in turn_record.redacted_source_event_ids:
            return None
        reply = await self.deps.reply_for_sources(turn_record.source_event_ids)
        if not _regenerable(reply) or await self.deps.later_human_message(turn_record):
            logger.info("edit_not_regenerated", room_id=room.room_id, source_event_id=original_event_id)
            return None
        assert reply is not None

        revision = (event.server_timestamp, event.event_id)
        committed = (turn_record.source_event_revisions or {}).get(original_event_id)
        watermark = turn_record.revision_watermark(original_event_id)
        if watermark is not None and revision < watermark:
            return None
        registered = await self.deps.turn_store.register_edit_revision(original_event_id, revision)
        if registered is None:
            return None
        replay = (registered.revision_replay or {}).get(revision[1])
        if replay is not None and replay.redacted:
            return None
        edited_content, _ = await extract_visible_edit_body(
            event.source,
            self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        if edited_content is None:
            return None
        context = await self._edit_regeneration_context(
            await self.deps.resolver.extract_message_context(room, event),
            room,
            conversation_target=turn_record.conversation_target,
        )
        envelope = self.deps.resolver.build_message_envelope(
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=turn_record.conversation_target,
            body=edited_content,
            source_kind=EDIT_SOURCE_KIND,
        )
        if revision != committed and await self.deps.ingress_hook_runner.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=event.event_id,
            policy=hook_ingress_policy(envelope),
        ):
            return None

        record = canonicalize_turn_record(
            registered,
            source_event_prompts={
                **(registered.source_event_prompts or {}),
                registered.prompt_source_event_id(original_event_id): edited_content,
            },
            source_event_revisions={**(registered.source_event_revisions or {}), original_event_id: revision},
        )
        prompt, structured = self._prompt(room, record, edited_content)
        if prompt is None:
            # A sibling's text is no longer known: nothing regenerates from a partial turn.
            return None
        if reply.current_span_id is not None or reply.approval_id is not None:
            # The answer still runs or waits for an approval: the edit stops it, which cancels that approval,
            # and the regeneration takes its place.
            await self.deps.stop_reply(reply, await self.deps.receipt_order())
        request = self._request(
            room,
            record,
            context,
            envelope,
            _EditedSource(original_event_id, revision, reply.event_id),
            prompt,
            structured=structured,
        )
        return self._regenerate(request)

    def _prompt(self, room: nio.MatrixRoom, record: TurnRecord, edited_content: str) -> tuple[str | None, bool]:
        """Return the prompt the edited turn runs with, and whether it is structured."""
        if not record.is_coalesced:
            return edited_content, False
        prompt_map = dict(record.source_event_prompts or {})
        parts = [prompt_map.get(source_event_id) for source_event_id in record.replay_source_event_ids]
        if any(part is None for part in parts):
            return None, False
        if record.source_event_metadata is not None:
            tagged = tagged_coalesced_prompt(
                list(record.replay_source_event_ids),
                prompt_map,
                dict(record.source_event_metadata),
                timestamp_formatter=self.deps.timestamp_formatter,
                member_display_names=room_member_display_names(room),
            )
            if tagged is not None:
                return tagged, True
        return coalesced_prompt([part for part in parts if part is not None]), False

    def _request(
        self,
        room: nio.MatrixRoom,
        record: TurnRecord,
        context: MessageContext,
        envelope: MessageEnvelope,
        edited: _EditedSource,
        prompt: str,
        *,
        structured: bool,
    ) -> ResponseRequest:
        requester_id = envelope.requester_id
        revision = edited.revision

        async def prepare_snapshot(history: Sequence[ResolvedVisibleMessage]) -> bool:
            return await self.deps.turn_store.prepare_edit_snapshot(
                record=record,
                driving_revision_id=revision[1],
                consumed_revision_ids=tuple(message.latest_event_id for message in history),
                thread_history=history,
            )

        async def commit_edit() -> None:
            # The claimed reply answers the edited text from now on, so the turn holds it and the history run
            # this edit replaces may go.
            await self.deps.turn_store.record_edit(record)
            await self.deps.turn_store.remove_stale_runs_for_edit(turn_record=record, requester_user_id=requester_id)

        return ResponseRequest(
            thread_history=context.thread_history,
            member_display_names=room_member_display_names(room),
            prompt=prompt,
            response_envelope=envelope,
            sources=ResponseSources(
                pending_event_ids=(revision[1],),
                logical_source_event_ids=record.source_event_ids,
                discovery_event_ids=record.discovery_event_ids,
            ),
            existing_event_id=edited.reply_event_id,
            user_id=requester_id,
            correlation_id=revision[1],
            matrix_run_metadata=self.deps.turn_store.build_run_metadata(
                record,
                additional_discovery_event_ids=(
                    (edited.source_event_id,)
                    if not record.is_coalesced and edited.source_event_id != record.anchor_event_id
                    else ()
                ),
            ),
            current_timestamp_ms=normalize_timestamp_ms(revision[0]),
            current_prompt_is_structured=structured,
            prepare_source_turn=prepare_snapshot,
            on_reply_claimed=commit_edit,
            edit_regeneration=True,
            source_handoff=asyncio.Event(),
        )

    def _regenerate(self, request: ResponseRequest) -> bool:
        """Start the regeneration off the room's lane, which hands it the edit.

        Its claim waits for the conversation, which another reply of this agent
        holds for as long as that reply runs, so the lane does not wait for it.
        A regeneration that ends with no span owning its edit settles the edit
        itself, unless its claim was deferred to the wake that retries it.
        """
        claimed = False
        commit_edit = request.on_reply_claimed
        handoff = request.source_handoff
        assert commit_edit is not None
        assert handoff is not None

        async def on_claimed() -> None:
            nonlocal claimed
            claimed = True
            await commit_edit()

        async def settle_unless_owned() -> None:
            if not claimed and not handoff.is_set():
                await self.deps.settle_sources(request.sources.pending_event_ids)

        async def regenerate() -> None:
            try:
                await self.deps.generate_response(replace(request, on_reply_claimed=on_claimed))
            except (asyncio.CancelledError, ResponseAdmissionRefusedError):
                # Cancelled, as at shutdown, or refused by a runtime being replaced, the edit stays pending, so a
                # restart or the replacement regenerates again.
                raise
            except Exception:
                await settle_unless_owned()
                raise
            await settle_unless_owned()

        self.deps.track_inbox_response(
            regenerate(),
            name=f"edit_regeneration:{request.correlation_id}",
            room_id=request.response_envelope.target.room_id,
            # The edit stays pending until a span settles it, so a restart regenerates again.
            recovery_proof_ready=lambda: True,
            source_event_ids=request.sources.pending_event_ids,
        )
        return True
