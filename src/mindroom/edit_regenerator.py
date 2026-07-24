"""Own the edited-message regeneration workflow for previously handled turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from mindroom.coalescing_batch import coalesced_prompt, tagged_coalesced_prompt
from mindroom.conversation_resolver import MessageContext
from mindroom.dispatch_source import EDIT_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import hook_ingress_policy
from mindroom.matrix.client_visible_messages import extract_visible_edit_body
from mindroom.response_runner import ResponseRequest
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.timestamp_formatting import normalize_timestamp_ms

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio
    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.handled_turns import SourceEventRevision, TurnRecord
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.event_info import EventInfo
    from mindroom.message_target import MessageTarget
    from mindroom.turn_policy import IngressHookRunner
    from mindroom.turn_store import TurnStore


class _GenerateResponse(Protocol):
    """Minimal response-generation surface needed for edit regeneration."""

    async def __call__(self, request: ResponseRequest) -> str | None:
        """Generate or regenerate a response for one handled turn."""


@dataclass(frozen=True)
class EditRegeneratorDeps:
    """Collaborators needed for edit-triggered regeneration."""

    runtime: SupportsClientConfig
    get_logger: Callable[[], structlog.stdlib.BoundLogger]
    runtime_paths: RuntimePaths
    agent_name: str
    resolver: ConversationResolver
    turn_store: TurnStore
    ingress_hook_runner: IngressHookRunner
    generate_response: _GenerateResponse
    wait_for_turn_settled: Callable[[tuple[str, ...]], Awaitable[None]]
    timestamp_formatter: Callable[[float | None], str | None]


@dataclass(frozen=True)
class _Edit:
    original_event_id: str
    body: str
    context: MessageContext
    envelope: MessageEnvelope
    requester_user_id: str
    correlation_id: str
    timestamp_ms: int
    revision: SourceEventRevision
    suppressed: bool


@dataclass
class _Mailbox:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, _Edit] = field(default_factory=dict)
    participants: int = 0


@dataclass
class EditRegenerator:
    """Re-run the owned response for one edited user turn."""

    deps: EditRegeneratorDeps
    _mailboxes: dict[tuple[str, str, str], _Mailbox] = field(default_factory=dict, init=False, repr=False)

    def _logger(self) -> structlog.stdlib.BoundLogger:
        return self.deps.get_logger()

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for edit regeneration"
            raise RuntimeError(msg)
        return client

    async def edit_regeneration_context(
        self,
        context: MessageContext,
        room: nio.MatrixRoom,
        *,
        conversation_target: MessageTarget,
    ) -> MessageContext:
        """Return edit context aligned with the recorded thread root."""
        if conversation_target.resolved_thread_id is None:
            return context
        if context.thread_id == conversation_target.resolved_thread_id:
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

    async def handle_message_edit(  # noqa: C901, PLR0911
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        event_info: EventInfo,
        requester_user_id: str,
    ) -> None:
        """Handle an edited message by regenerating the owned response."""
        if not event_info.original_event_id:
            self._logger().debug("Edit event has no original event ID")
            return
        original_event_id = event_info.original_event_id
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        sender_agent_name = registry.current_entity_name_for_user_id(event.sender)
        if sender_agent_name:
            self._logger().debug("ignoring_edit_from_other_agent", agent=sender_agent_name)
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
            self._logger().debug(
                "No handled turn record found for edited message",
                original_event_id=original_event_id,
            )
            return
        if (
            turn_record.conversation_target is None
            or turn_record.history_scope is None
            or turn_record.response_owner is None
        ):
            self._logger().warning(
                "Skipping edited turn regeneration without persisted response context",
                original_event_id=original_event_id,
                has_conversation_target=turn_record.conversation_target is not None,
                has_history_scope=turn_record.history_scope is not None,
                has_response_owner=turn_record.response_owner is not None,
            )
            return
        context = await self.edit_regeneration_context(
            context,
            room,
            conversation_target=turn_record.conversation_target,
        )
        if turn_record.response_owner != self.deps.agent_name:
            self._logger().debug(
                "Ignoring edited message for turn owned by another entity",
                original_event_id=original_event_id,
                response_owner=turn_record.response_owner,
            )
            return
        if original_event_id in turn_record.redacted_source_event_ids:
            self._logger().debug("Ignoring edit for redacted source message", original_event_id=original_event_id)
            return
        revision = (event.server_timestamp, event.event_id)
        committed = (turn_record.source_event_revisions or {}).get(original_event_id)
        if committed is not None and revision <= committed:
            return

        edited_content, _ = await extract_visible_edit_body(
            event.source,
            self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        if edited_content is None:
            self._logger().debug("Edited message missing resolved body", event_id=event.event_id)
            return
        envelope = self.deps.resolver.build_message_envelope(
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=turn_record.conversation_target,
            body=edited_content,
            source_kind=EDIT_SOURCE_KIND,
        )
        edit = _Edit(
            original_event_id=original_event_id,
            body=edited_content,
            context=context,
            envelope=envelope,
            requester_user_id=requester_user_id,
            correlation_id=event.event_id,
            timestamp_ms=event.server_timestamp,
            revision=revision,
            suppressed=await self.deps.ingress_hook_runner.emit_message_received_hooks(
                envelope=envelope,
                correlation_id=event.event_id,
                policy=hook_ingress_policy(envelope),
            ),
        )
        assert turn_record.anchor_event_id is not None
        key = (turn_record.conversation_target.room_id, turn_record.anchor_event_id, requester_user_id)
        mailbox = self._mailboxes.setdefault(key, _Mailbox())
        queued = mailbox.pending.get(original_event_id)
        if queued is not None and revision <= queued.revision:
            return
        mailbox.pending[original_event_id] = edit
        mailbox.participants += 1
        try:
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
            requester_user_id=latest.requester_user_id,
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
        applied: dict[str, SourceEventRevision] = {}
        eligible: dict[str, _Edit] = {}
        active: dict[str, _Edit] = {}
        for source_event_id, edit in mailbox.pending.items():
            committed = revisions.get(source_event_id)
            if source_event_id in record.redacted_source_event_ids or (
                committed is not None and edit.revision <= committed
            ):
                applied[source_event_id] = edit.revision
                continue
            revisions[source_event_id] = edit.revision
            applied[source_event_id] = edit.revision
            eligible[source_event_id] = edit
            if not edit.suppressed:
                active[source_event_id] = edit
        prompt_map = dict(record.source_event_prompts or {})
        prompt_map.update({source_event_id: edit.body for source_event_id, edit in eligible.items()})
        if not active:
            if revisions != dict(record.source_event_revisions or {}):
                record = replace(record, source_event_revisions=revisions)
                if record.is_coalesced:
                    record = replace(record, source_event_prompts=prompt_map)
                self.deps.turn_store.record_turn(record)
            return None, None, applied

        driving_edit = max(active.values(), key=lambda edit: edit.revision)
        if record.is_coalesced:
            prompt_parts = [prompt_map.get(source_event_id) for source_event_id in record.replay_source_event_ids]
            if record.source_event_prompts is None or any(part is None for part in prompt_parts):
                self._logger().warning(
                    "Skipping edited coalesced turn regeneration with incomplete prompt map",
                    original_event_id=driving_edit.original_event_id,
                    anchor_event_id=record.anchor_event_id,
                )
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
            record = replace(record, source_event_prompts=prompt_map)
        else:
            prompt, structured = driving_edit.body, False
        record = replace(record, source_event_revisions=revisions)
        assert record.conversation_target is not None
        target = record.conversation_target
        metadata = self.deps.turn_store.build_run_metadata(
            record,
            additional_discovery_event_ids=(
                (driving_edit.original_event_id,)
                if not record.is_coalesced and driving_edit.original_event_id != record.anchor_event_id
                else ()
            ),
        )
        return (
            ResponseRequest(
                thread_history=driving_edit.context.thread_history,
                prompt=prompt,
                response_envelope=driving_edit.envelope,
                existing_event_id=record.response_event_id,
                user_id=driving_edit.requester_user_id,
                correlation_id=driving_edit.correlation_id,
                matrix_run_metadata=metadata,
                current_timestamp_ms=normalize_timestamp_ms(driving_edit.timestamp_ms),
                current_prompt_is_structured=structured,
                on_lifecycle_lock_acquired=lambda: self.deps.turn_store.remove_stale_runs_for_edit(
                    turn_record=record,
                    requester_user_id=driving_edit.requester_user_id,
                ),
                prepare_source_turn=lambda: self.deps.turn_store.prepare_response_for_redactions(
                    target=target,
                    source_event_ids=tuple(
                        dict.fromkeys((*record.replay_source_event_ids, driving_edit.original_event_id)),
                    ),
                ),
                on_deferred_outcome_handled=lambda response_event_id: self.deps.turn_store.record_turn(
                    replace(record, response_event_id=response_event_id),
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
        assert initial_record.conversation_target is not None
        if initial_record.response_event_id is None:
            await self.deps.wait_for_turn_settled(initial_record.indexed_event_ids)
        while mailbox.pending:
            latest = max(mailbox.pending.values(), key=lambda edit: edit.revision)
            request, record, applied = self._build_request(room, mailbox)
            if request is None or record is None:
                self._discard(mailbox, applied)
                if not applied:
                    self._logger().debug(
                        "Skipping edit regeneration after durable turn reload",
                        original_event_id=latest.original_event_id,
                    )
                    return
                continue
            regenerated_event_id = await self.deps.generate_response(request)
            if regenerated_event_id is not None:
                self.deps.turn_store.record_turn(
                    replace(record, response_event_id=regenerated_event_id),
                )
                self._discard(mailbox, applied)
                self._logger().info("Successfully regenerated response for edited message")
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
            self._logger().info(
                "Suppressed regeneration left existing response unchanged",
                original_event_id=latest.original_event_id,
                response_event_id=record.response_event_id,
            )
