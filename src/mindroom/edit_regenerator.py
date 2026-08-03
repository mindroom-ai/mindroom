"""Regenerate edited turns through a per-response newest-wins mailbox."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.coalescing_batch import coalesced_prompt, tagged_coalesced_prompt
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_source import EDIT_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.handled_turns import with_user_stop
from mindroom.hooks import hook_ingress_policy
from mindroom.matrix.client_visible_messages import extract_visible_edit_body
from mindroom.response_runner import ResponseRequest
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.timestamp_formatting import normalize_timestamp_ms
from mindroom.turn_record import canonicalize_turn_record

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.handled_turns import SourceEventRevision, TurnRecord
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.event_info import EventInfo
    from mindroom.message_target import MessageTarget
    from mindroom.sync_restart_retry import InterruptedTurnRooms
    from mindroom.turn_policy import IngressHookRunner
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class EditRegeneratorDeps:
    """Collaborators needed for edit-triggered regeneration."""

    runtime: SupportsClientConfig
    runtime_paths: RuntimePaths
    agent_name: str
    resolver: ConversationResolver
    turn_store: TurnStore
    ingress_hook_runner: IngressHookRunner
    generate_response: Callable[[ResponseRequest], Awaitable[str | None]]
    wait_for_turn_settled: Callable[[tuple[str, ...]], Awaitable[None]]
    receipt_order: Callable[[], Awaitable[int]]
    interrupted_turn_rooms: InterruptedTurnRooms
    timestamp_formatter: Callable[[float | None], str | None]


@dataclass(frozen=True)
class _Edit:
    original_event_id: str
    body: str
    context: MessageContext
    envelope: MessageEnvelope
    revision: SourceEventRevision
    receipt_order: int
    suppressed: bool


def _edit_remains_active(
    record: TurnRecord,
    edit: _Edit,
    source_event_id: str,
    suppressed_revisions: dict[str, SourceEventRevision],
) -> bool:
    """Update suppression state and reject revisions covered by a durable STOP."""
    if edit.suppressed:
        suppressed_revisions[source_event_id] = edit.revision
        return False
    suppressed_revisions.pop(source_event_id, None)
    cutoff = record.user_stop_receipt_order
    return cutoff is None or edit.receipt_order > cutoff


@dataclass
class _Mailbox:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, _Edit] = field(default_factory=dict)
    reserved_revisions: dict[str, SourceEventRevision] = field(default_factory=dict)
    participants: int = 0


@dataclass
class EditRegenerator:
    """Re-run the owned response for one edited user turn."""

    deps: EditRegeneratorDeps
    _mailboxes: dict[tuple[str, str, str], _Mailbox] = field(default_factory=dict, init=False, repr=False)

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
            caller_label="edit_regeneration_context",
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
        event: nio.RoomMessageText,
        event_info: EventInfo,
        requester_user_id: str,
    ) -> None:
        """Handle an edited message by regenerating the owned response."""
        if not event_info.original_event_id:
            return
        original_event_id = event_info.original_event_id
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        if registry.current_entity_name_for_user_id(event.sender):
            return

        context = await self.deps.resolver.extract_message_context(
            room,
            event,
            caller_label="edit_regeneration_context",
        )
        turn_record = self.deps.turn_store.load_turn(
            room=room,
            thread_id=context.thread_id or event_info.thread_id or event_info.thread_id_from_edit,
            original_event_id=original_event_id,
            requester_user_id=requester_user_id,
        )
        if turn_record is None:
            await self.deps.wait_for_turn_settled((original_event_id,))
            turn_record = self.deps.turn_store.load_turn(
                room=room,
                thread_id=context.thread_id or event_info.thread_id or event_info.thread_id_from_edit,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            )
        if turn_record is None:
            return
        if (
            turn_record.conversation_target is None
            or turn_record.history_scope is None
            or turn_record.response_owner is None
        ):
            return
        if turn_record.requester_id_for_source(original_event_id) != requester_user_id:
            return
        context = await self._edit_regeneration_context(
            context,
            room,
            conversation_target=turn_record.conversation_target,
        )
        if turn_record.response_owner != self.deps.agent_name:
            return
        if original_event_id in turn_record.redacted_source_event_ids:
            return
        receipt_order = await self.deps.receipt_order()
        revision = (event.server_timestamp, event.event_id)
        committed = (turn_record.source_event_revisions or {}).get(original_event_id)
        if committed is not None and revision < committed:
            return

        edited_content, _ = await extract_visible_edit_body(
            event.source,
            self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        if edited_content is None:
            return
        envelope = self.deps.resolver.build_message_envelope(
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=turn_record.conversation_target,
            body=edited_content,
            source_kind=EDIT_SOURCE_KIND,
        )
        assert turn_record.anchor_event_id is not None
        key = (turn_record.conversation_target.room_id, turn_record.anchor_event_id, envelope.requester_id)
        mailbox = self._mailboxes.setdefault(key, _Mailbox())
        reserved_revision = mailbox.reserved_revisions.get(original_event_id)
        if reserved_revision is not None and revision <= reserved_revision:
            return
        mailbox.reserved_revisions[original_event_id] = revision
        mailbox.participants += 1
        try:
            suppressed = revision == (turn_record.suppressed_source_event_revisions or {}).get(
                original_event_id,
            ) or (
                revision != committed
                and await self.deps.ingress_hook_runner.emit_message_received_hooks(
                    envelope=envelope,
                    correlation_id=event.event_id,
                    policy=hook_ingress_policy(envelope),
                )
            )
            if mailbox.reserved_revisions.get(original_event_id) != revision:
                return
            mailbox.pending[original_event_id] = _Edit(
                original_event_id=original_event_id,
                body=edited_content,
                context=context,
                envelope=envelope,
                revision=revision,
                receipt_order=receipt_order,
                suppressed=suppressed,
            )
            async with mailbox.lock:
                await self._drain(room, turn_record, mailbox)
        finally:
            mailbox.participants -= 1
            if mailbox.participants == 0 and self._mailboxes.get(key) is mailbox:
                self._mailboxes.pop(key)

    def _build_request(  # noqa: C901
        self,
        room: nio.MatrixRoom,
        mailbox: _Mailbox,
    ) -> tuple[ResponseRequest | None, TurnRecord | None, dict[str, SourceEventRevision]]:
        latest = max(mailbox.pending.values(), key=lambda edit: edit.revision)
        record = self.deps.turn_store.load_turn(
            room=room,
            thread_id=latest.context.thread_id,
            original_event_id=latest.original_event_id,
            requester_user_id=latest.envelope.requester_id,
        )
        if (
            record is None
            or record.conversation_target is None
            or record.history_scope is None
            or record.response_owner != self.deps.agent_name
            or record.response_event_id is None
        ):
            return None, None, {}
        revisions = dict(record.source_event_revisions or {})
        suppressed_revisions = dict(record.suppressed_source_event_revisions or {})
        applied: dict[str, SourceEventRevision] = {}
        active: dict[str, _Edit] = {}
        prompt_map = dict(record.source_event_prompts or {})
        retrying = True
        for source_event_id, edit in mailbox.pending.items():
            committed = revisions.get(source_event_id)
            if source_event_id in record.redacted_source_event_ids or (
                committed is not None and edit.revision < committed
            ):
                applied[source_event_id] = edit.revision
                continue
            revisions[source_event_id] = edit.revision
            applied[source_event_id] = edit.revision
            prompt_map[record.prompt_source_event_id(source_event_id)] = edit.body
            if _edit_remains_active(record, edit, source_event_id, suppressed_revisions):
                active[source_event_id] = edit
                retrying &= edit.revision == committed
        if not active:
            if revisions != dict(record.source_event_revisions or {}) or suppressed_revisions != dict(
                record.suppressed_source_event_revisions or {},
            ):
                record = canonicalize_turn_record(
                    record,
                    source_event_prompts=prompt_map,
                    source_event_revisions=revisions,
                    suppressed_source_event_revisions=suppressed_revisions,
                )
                self.deps.turn_store.record_turn(record)
            return None, None, applied

        driving_edit = max(active.values(), key=lambda edit: edit.revision)
        active_receipt_order = max(edit.receipt_order for edit in active.values())
        retry_source_event_id = record.prompt_source_event_id(driving_edit.original_event_id) if retrying else None
        if record.is_coalesced:
            prompt_parts = [prompt_map.get(source_event_id) for source_event_id in record.replay_source_event_ids]
            if any(part is None for part in prompt_parts):
                return None, None, applied
            prompt = coalesced_prompt([part for part in prompt_parts if part is not None])
            structured = False
            if record.source_event_metadata is not None:
                tagged_prompt = tagged_coalesced_prompt(
                    list(record.replay_source_event_ids),
                    prompt_map,
                    dict(record.source_event_metadata),
                    timestamp_formatter=self.deps.timestamp_formatter,
                )
                if tagged_prompt is not None:
                    prompt, structured = tagged_prompt, True
        else:
            prompt, structured = driving_edit.body, False
        record = canonicalize_turn_record(
            record,
            source_event_prompts=prompt_map,
            source_event_revisions=revisions,
            suppressed_source_event_revisions=suppressed_revisions,
        )
        target = record.conversation_target
        assert target is not None
        requester_id = driving_edit.envelope.requester_id
        metadata = self.deps.turn_store.build_run_metadata(
            record,
            additional_discovery_event_ids=(
                (driving_edit.original_event_id,)
                if not record.is_coalesced and driving_edit.original_event_id != record.anchor_event_id
                else ()
            ),
        )

        def record_interrupted_turn() -> None:
            # The interrupted revision must stay uncommitted so the replacement
            # runtime's recovery re-drives it instead of treating it as applied.
            if self.deps.interrupted_turn_rooms.register(driving_edit.revision[1], room_id=room.room_id):
                applied.clear()

        return (
            ResponseRequest(
                thread_history=driving_edit.context.thread_history,
                prompt=prompt,
                response_envelope=driving_edit.envelope,
                existing_event_id=record.response_event_id,
                user_id=requester_id,
                correlation_id=driving_edit.revision[1],
                matrix_run_metadata=metadata,
                current_timestamp_ms=normalize_timestamp_ms(driving_edit.revision[0]),
                current_prompt_is_structured=structured,
                on_lifecycle_lock_acquired=lambda: self.deps.turn_store.remove_stale_runs_for_edit(
                    turn_record=record,
                    requester_user_id=requester_id,
                ),
                prepare_source_turn=lambda: self.deps.turn_store.prepare_edit_response_source(
                    target=target,
                    source_event_ids=tuple(
                        dict.fromkeys((*record.replay_source_event_ids, driving_edit.original_event_id)),
                    ),
                    response_event_id=record.response_event_id,
                    edit_receipt_order=active_receipt_order,
                ),
                on_interrupted_response_recoverable=record_interrupted_turn,
                sync_restart_retry_source_event_id=retry_source_event_id,
                on_deferred_outcome_handled=lambda response_event_id: (
                    self.deps.turn_store.record_responded_turn(
                        canonicalize_turn_record(record, response_event_id=response_event_id),
                    )
                    if applied
                    else None
                ),
                on_user_stop_handled=lambda response_event_id, stop_receipt_order: (
                    self.deps.turn_store.record_turn_durably(
                        with_user_stop(
                            record,
                            response_event_id,
                            stop_receipt_order,
                            delivery_settled=True,
                        ),
                    )
                    if applied
                    else None
                ),
            ),
            record,
            applied,
        )

    @staticmethod
    def _discard(mailbox: _Mailbox, revisions: dict[str, SourceEventRevision]) -> None:
        for source_event_id, revision in revisions.items():
            pending = mailbox.pending.get(source_event_id)
            if pending is not None and pending.revision <= revision:
                mailbox.pending.pop(source_event_id)

    async def _drain(self, room: nio.MatrixRoom, initial_record: TurnRecord, mailbox: _Mailbox) -> None:
        claimed_record = initial_record
        while True:
            if self.deps.turn_store.try_claim_turn(claimed_record):
                break
            await self.deps.wait_for_turn_settled(claimed_record.indexed_event_ids)
            latest = max(mailbox.pending.values(), key=lambda edit: edit.revision)
            refreshed_record = self.deps.turn_store.get_turn_record(latest.original_event_id)
            if refreshed_record is None:
                return
            same_identity = (
                refreshed_record.source_event_ids == claimed_record.source_event_ids
                and refreshed_record.anchor_event_id == claimed_record.anchor_event_id
            )
            claimed_record = refreshed_record
            if same_identity:
                if not self.deps.turn_store.try_claim_turn(claimed_record):
                    return
                break
        try:
            await self._drain_claimed(room, mailbox)
        finally:
            self.deps.turn_store.release_pending_turn_claim(claimed_record)

    async def _drain_claimed(self, room: nio.MatrixRoom, mailbox: _Mailbox) -> None:
        while mailbox.pending:
            latest = max(mailbox.pending.values(), key=lambda edit: edit.revision)
            request, record, applied = self._build_request(room, mailbox)
            if request is None or record is None:
                self._discard(mailbox, applied)
                if not applied:
                    return
                continue
            regenerated_event_id = await self.deps.generate_response(request)
            if regenerated_event_id is not None:
                if not applied:
                    return
                self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(record, response_event_id=regenerated_event_id),
                )
                self._discard(mailbox, applied)
                continue
            fresh_record = self.deps.turn_store.get_turn_record(latest.original_event_id)
            if fresh_record is not None and fresh_record.redacted_source_event_ids != record.redacted_source_event_ids:
                self._discard(
                    mailbox,
                    {
                        source_event_id: revision
                        for source_event_id, revision in applied.items()
                        if source_event_id in fresh_record.redacted_source_event_ids
                    },
                )
                continue
            self._discard(mailbox, applied)
