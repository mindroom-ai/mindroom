"""Edit-triggered response regeneration for previously handled turns."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.agents import remove_run_by_event_id
from mindroom.coalescing import coalesced_prompt
from mindroom.conversation_resolver import MessageContext
from mindroom.conversation_state_writer import (
    LoadPersistedTurnMetadataRequest,
    PersistedTurnMetadata,
    RemoveStaleRunsRequest,
)
from mindroom.handled_turns import HandledTurnLedger, HandledTurnRecord, HandledTurnState
from mindroom.hooks.ingress import hook_ingress_policy
from mindroom.matrix.identity import extract_agent_name
from mindroom.matrix.message_content import extract_edit_body
from mindroom.post_response_effects import matrix_run_metadata_for_handled_turn, record_handled_turn
from mindroom.thread_utils import should_agent_respond

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.conversation_state_writer import ConversationStateWriter
    from mindroom.dispatch_planner import DispatchHookService
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.event_info import EventInfo
    from mindroom.message_target import MessageTarget
    from mindroom.tool_system.runtime_context import ToolRuntimeSupport


class _GenerateResponse(Protocol):
    """Minimal response-generation surface needed for edit regeneration."""

    async def __call__(
        self,
        *,
        room_id: str,
        prompt: str,
        reply_to_event_id: str,
        thread_id: str | None,
        thread_history: Sequence[ResolvedVisibleMessage],
        existing_event_id: str | None = None,
        existing_event_is_placeholder: bool = False,
        user_id: str | None = None,
        response_envelope: MessageEnvelope | None = None,
        correlation_id: str | None = None,
        target: MessageTarget | None = None,
        matrix_run_metadata: dict[str, Any] | None = None,
        on_lifecycle_lock_acquired: Callable[[], None] | None = None,
    ) -> str | None:
        """Generate or regenerate a response for one handled turn."""


@dataclass(frozen=True)
class EditRegeneratorDeps:
    """Collaborators needed for edit-triggered regeneration."""

    runtime: BotRuntimeView
    get_logger: Callable[[], structlog.stdlib.BoundLogger]
    runtime_paths: RuntimePaths
    agent_name: str
    get_handled_turn_ledger: Callable[[], HandledTurnLedger]
    resolver: ConversationResolver
    state_writer: ConversationStateWriter
    tool_runtime: ToolRuntimeSupport
    dispatch_hook_service: DispatchHookService
    generate_response: _GenerateResponse


@dataclass
class EditRegenerator:
    """Own edit-triggered response regeneration for previously handled turns."""

    deps: EditRegeneratorDeps

    def _logger(self) -> structlog.stdlib.BoundLogger:
        return self.deps.get_logger()

    def _handled_turn_ledger(self) -> HandledTurnLedger:
        return self.deps.get_handled_turn_ledger()

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for edit regeneration"
            raise RuntimeError(msg)
        return client

    def _mark_source_events_responded(self, handled_turn: HandledTurnState) -> None:
        """Mark one or more source events as handled by the same response."""
        record_handled_turn(self._handled_turn_ledger(), handled_turn)

    def load_persisted_turn_metadata(
        self,
        *,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> PersistedTurnMetadata | None:
        """Load persisted run metadata for one edited turn when available."""
        return self.deps.state_writer.load_persisted_turn_metadata(
            LoadPersistedTurnMetadataRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
            build_message_target=self.deps.resolver.build_message_target,
            build_tool_execution_identity=self.deps.tool_runtime.build_execution_identity,
        )

    async def edit_regeneration_context(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageText,
        *,
        conversation_target: MessageTarget | None,
    ) -> MessageContext:
        """Return edit context, reusing the recorded thread root when available."""
        context = await self.deps.resolver.extract_message_context(room, event)
        if conversation_target is None or conversation_target.resolved_thread_id is None:
            return context
        if context.thread_id == conversation_target.resolved_thread_id:
            return context
        return MessageContext(
            am_i_mentioned=context.am_i_mentioned,
            is_thread=True,
            thread_id=conversation_target.resolved_thread_id,
            thread_history=await self.deps.resolver.fetch_thread_history(
                self._client(),
                room.room_id,
                conversation_target.resolved_thread_id,
            ),
            mentioned_agents=context.mentioned_agents,
            has_non_agent_mentions=context.has_non_agent_mentions,
            requires_full_thread_history=context.requires_full_thread_history,
        )

    def remove_stale_runs_for_turn_record(
        self,
        *,
        turn_record: HandledTurnRecord,
        recorded_turn_context_available: bool,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> None:
        """Remove stale persisted runs using the recorded turn context when possible."""
        if (
            recorded_turn_context_available
            and turn_record.conversation_target is not None
            and turn_record.history_scope is not None
        ):
            self.deps.state_writer.remove_stale_runs_for_turn_record(
                turn_record=turn_record,
                requester_user_id=requester_user_id,
                build_tool_execution_identity=self.deps.tool_runtime.build_execution_identity,
                remove_run_by_event_id_fn=remove_run_by_event_id,
            )
            return
        self.remove_stale_runs_for_edited_message(
            room=room,
            thread_id=thread_id,
            original_event_id=original_event_id,
            requester_user_id=requester_user_id,
        )

    def remove_stale_runs_for_edited_message(
        self,
        *,
        room: nio.MatrixRoom,
        thread_id: str | None,
        original_event_id: str,
        requester_user_id: str,
    ) -> None:
        """Remove persisted runs tied to the pre-edit message before regenerating."""
        self.deps.state_writer.remove_stale_runs_for_edited_message(
            RemoveStaleRunsRequest(
                room=room,
                thread_id=thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
            build_message_target=self.deps.resolver.build_message_target,
            build_tool_execution_identity=self.deps.tool_runtime.build_execution_identity,
            remove_run_by_event_id_fn=remove_run_by_event_id,
        )

    async def handle_message_edit(  # noqa: C901, PLR0911, PLR0912, PLR0915
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

        sender_agent_name = extract_agent_name(event.sender, self.deps.runtime.config, self.deps.runtime_paths)
        if sender_agent_name:
            self._logger().debug("ignoring_edit_from_other_agent", agent=sender_agent_name)
            return

        turn_record = self._handled_turn_ledger().get_turn_record(original_event_id)
        context = await self.edit_regeneration_context(
            room,
            event,
            conversation_target=turn_record.conversation_target if turn_record is not None else None,
        )
        persisted_turn_metadata = self.load_persisted_turn_metadata(
            room=room,
            thread_id=context.thread_id,
            original_event_id=original_event_id,
            requester_user_id=requester_user_id,
        )
        if turn_record is None:
            if persisted_turn_metadata is None:
                self._logger().debug(
                    "No handled turn record found for edited message",
                    original_event_id=original_event_id,
                )
                return
            turn_record = HandledTurnRecord(
                anchor_event_id=persisted_turn_metadata.anchor_event_id,
                source_event_ids=persisted_turn_metadata.source_event_ids,
                response_event_id=persisted_turn_metadata.response_event_id,
                source_event_prompts=persisted_turn_metadata.source_event_prompts,
            )
        recorded_turn_context_available = bool(
            turn_record.conversation_target is not None and turn_record.history_scope is not None,
        )
        response_owner_missing = turn_record.response_owner is None
        if response_owner_missing and persisted_turn_metadata is not None:
            turn_record = replace(turn_record, response_owner=self.deps.agent_name)
        response_event_id = (
            persisted_turn_metadata.response_event_id
            if persisted_turn_metadata is not None and persisted_turn_metadata.response_event_id is not None
            else turn_record.response_event_id
        )
        if response_event_id is None:
            self._logger().debug("missing_previous_response_for_edit", event_id=original_event_id)
            return
        regeneration_target = turn_record.conversation_target or self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=context.thread_id,
            reply_to_event_id=turn_record.anchor_event_id,
        )
        regeneration_history_scope = turn_record.history_scope or self.deps.state_writer.history_scope()
        regeneration_response_owner = turn_record.response_owner or self.deps.agent_name
        if regeneration_response_owner != self.deps.agent_name:
            self._logger().debug(
                "Ignoring edited message for turn owned by another entity",
                original_event_id=original_event_id,
                response_owner=regeneration_response_owner,
            )
            return
        needs_turn_record_backfill = (
            turn_record.response_event_id != response_event_id
            or response_owner_missing
            or turn_record.history_scope is None
            or turn_record.conversation_target is None
            or (
                turn_record.is_coalesced
                and turn_record.source_event_prompts is None
                and persisted_turn_metadata is not None
                and persisted_turn_metadata.source_event_prompts is not None
            )
        )
        coalesced_source_event_prompts = turn_record.source_event_prompts
        if (
            coalesced_source_event_prompts is None
            and persisted_turn_metadata is not None
            and persisted_turn_metadata.is_coalesced
        ):
            coalesced_source_event_prompts = persisted_turn_metadata.source_event_prompts

        self._logger().info(
            "Regenerating response for edited message",
            original_event_id=original_event_id,
            response_event_id=response_event_id,
        )

        edited_content, _ = await extract_edit_body(event.source, self._client())
        if edited_content is None:
            self._logger().debug("Edited message missing resolved body", event_id=event.event_id)
            return
        regeneration_handled_turn = HandledTurnState.create(
            turn_record.source_event_ids,
            response_event_id=response_event_id,
            response_owner=regeneration_response_owner,
            history_scope=regeneration_history_scope,
            conversation_target=regeneration_target,
        )
        regeneration_turn_record = replace(
            turn_record,
            response_event_id=response_event_id,
            response_owner=regeneration_response_owner,
            history_scope=regeneration_history_scope,
            conversation_target=regeneration_target,
        )
        if regeneration_turn_record.is_coalesced:
            if coalesced_source_event_prompts is None:
                self._logger().warning(
                    "Skipping edited coalesced turn regeneration without persisted source prompts",
                    original_event_id=original_event_id,
                    anchor_event_id=regeneration_turn_record.anchor_event_id,
                )
                return
            updated_prompt_map = dict(coalesced_source_event_prompts)
            updated_prompt_map[original_event_id] = edited_content
            rebuilt_prompt_parts: list[str] = []
            for source_event_id in regeneration_turn_record.source_event_ids:
                prompt_part = updated_prompt_map.get(source_event_id)
                if prompt_part is None:
                    self._logger().warning(
                        "Skipping edited coalesced turn regeneration with incomplete prompt map",
                        original_event_id=original_event_id,
                        missing_source_event_id=source_event_id,
                        anchor_event_id=regeneration_turn_record.anchor_event_id,
                    )
                    return
                rebuilt_prompt_parts.append(prompt_part)
            regeneration_prompt = coalesced_prompt(rebuilt_prompt_parts)
            regeneration_handled_turn = HandledTurnState.create(
                regeneration_turn_record.source_event_ids,
                response_event_id=response_event_id,
                source_event_prompts=updated_prompt_map,
                response_owner=regeneration_response_owner,
                history_scope=regeneration_history_scope,
                conversation_target=regeneration_target,
            )
            regeneration_turn_record = replace(regeneration_turn_record, source_event_prompts=updated_prompt_map)
            regeneration_matrix_run_metadata = matrix_run_metadata_for_handled_turn(regeneration_handled_turn)
        else:
            regeneration_prompt = edited_content
            regeneration_matrix_run_metadata = None
        envelope = self.deps.resolver.build_message_envelope(
            room_id=room.room_id,
            event=event,
            requester_user_id=requester_user_id,
            context=context,
            target=regeneration_target,
            body=edited_content,
            source_kind="edit",
        )
        ingress_policy = hook_ingress_policy(envelope)
        if await self.deps.dispatch_hook_service.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=event.event_id,
            policy=ingress_policy,
        ):
            self._mark_source_events_responded(regeneration_handled_turn)
            return

        if turn_record.response_owner is None:
            should_respond = should_agent_respond(
                agent_name=self.deps.agent_name,
                am_i_mentioned=context.am_i_mentioned,
                is_thread=context.is_thread,
                room=room,
                thread_history=context.thread_history,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                mentioned_agents=context.mentioned_agents,
                has_non_agent_mentions=context.has_non_agent_mentions,
                sender_id=requester_user_id,
            )
            if not should_respond and not regeneration_turn_record.is_coalesced:
                self._logger().debug("Agent should not respond to edited message")
                if needs_turn_record_backfill:
                    self._mark_source_events_responded(regeneration_handled_turn)
                return

        regenerated_event_id = await self.deps.generate_response(
            room_id=room.room_id,
            prompt=regeneration_prompt,
            reply_to_event_id=regeneration_turn_record.anchor_event_id,
            thread_id=regeneration_target.thread_id,
            target=regeneration_target,
            thread_history=context.thread_history,
            existing_event_id=response_event_id,
            existing_event_is_placeholder=False,
            user_id=requester_user_id,
            response_envelope=envelope,
            correlation_id=event.event_id,
            matrix_run_metadata=regeneration_matrix_run_metadata,
            on_lifecycle_lock_acquired=lambda: self.remove_stale_runs_for_turn_record(
                turn_record=regeneration_turn_record,
                recorded_turn_context_available=recorded_turn_context_available,
                room=room,
                thread_id=context.thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
        )

        if regenerated_event_id is not None:
            self._mark_source_events_responded(
                regeneration_handled_turn.with_response_event_id(regenerated_event_id),
            )
            self._logger().info("Successfully regenerated response for edited message")
        else:
            if needs_turn_record_backfill:
                self._mark_source_events_responded(regeneration_handled_turn)
            self._logger().info(
                "Suppressed regeneration left existing response unchanged",
                original_event_id=original_event_id,
                response_event_id=response_event_id,
            )
