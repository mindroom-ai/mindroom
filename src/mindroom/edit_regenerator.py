"""Own the edited-message regeneration workflow for previously handled turns."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.coalescing import coalesced_prompt
from mindroom.conversation_resolver import MessageContext
from mindroom.handled_turns import HandledTurnRecord, HandledTurnState
from mindroom.hooks.ingress import hook_ingress_policy
from mindroom.matrix.client_visible_messages import extract_visible_edit_body
from mindroom.matrix.identity import extract_agent_name
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import nio
    import structlog

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.final_delivery import TurnDeliveryResolution
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.matrix.event_info import EventInfo
    from mindroom.message_target import MessageTarget
    from mindroom.turn_policy import IngressHookRunner
    from mindroom.turn_store import TurnStore


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
    ) -> TurnDeliveryResolution:
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


@dataclass
class EditRegenerator:
    """Re-run the owned response for one edited user turn."""

    deps: EditRegeneratorDeps

    def _logger(self) -> structlog.stdlib.BoundLogger:
        return self.deps.get_logger()

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for edit regeneration"
            raise RuntimeError(msg)
        return client

    def _record_turn_record(self, turn_record: HandledTurnRecord) -> None:
        """Persist one exact handled-turn record without losing its anchor event."""
        self.deps.turn_store.record_turn_record(turn_record)

    async def edit_regeneration_context(
        self,
        context: MessageContext,
        room: nio.MatrixRoom,
        *,
        conversation_target: MessageTarget | None,
    ) -> MessageContext:
        """Return edit context, reusing the recorded thread root when available."""
        if conversation_target is None or conversation_target.resolved_thread_id is None:
            return context
        if context.thread_id == conversation_target.resolved_thread_id:
            return context
        thread_history = await self.deps.resolver.fetch_thread_history(
            self._client(),
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
            requires_full_thread_history=context.requires_full_thread_history,
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

        context = await self.deps.resolver.extract_message_context(room, event)
        loaded_turn = self.deps.turn_store.load_turn(
            room=room,
            thread_id=context.thread_id or event_info.thread_id or event_info.thread_id_from_edit,
            original_event_id=original_event_id,
            requester_user_id=requester_user_id,
        )
        if loaded_turn is None:
            self._logger().debug(
                "No handled turn record found for edited message",
                original_event_id=original_event_id,
            )
            return
        turn_record = loaded_turn.record
        context = await self.edit_regeneration_context(
            context,
            room,
            conversation_target=turn_record.conversation_target,
        )
        if loaded_turn.response_owner_missing:
            turn_record = replace(turn_record, response_owner=self.deps.agent_name)
        response_event_id = turn_record.response_event_id
        if response_event_id is None:
            self._logger().debug("missing_previous_response_for_edit", event_id=original_event_id)
            return
        regeneration_target = turn_record.conversation_target or self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=context.thread_id,
            reply_to_event_id=turn_record.anchor_event_id,
        )
        regeneration_history_scope = turn_record.history_scope or self.deps.turn_store.response_history_scope()
        regeneration_response_owner = turn_record.response_owner or self.deps.agent_name
        if regeneration_response_owner != self.deps.agent_name:
            self._logger().debug(
                "Ignoring edited message for turn owned by another entity",
                original_event_id=original_event_id,
                response_owner=regeneration_response_owner,
            )
            return
        needs_turn_record_backfill = (
            loaded_turn.requires_backfill
            or loaded_turn.response_owner_missing
            or turn_record.history_scope is None
            or turn_record.conversation_target is None
        )
        coalesced_source_event_prompts = turn_record.source_event_prompts

        self._logger().info(
            "Regenerating response for edited message",
            original_event_id=original_event_id,
            response_event_id=response_event_id,
        )

        edited_content, _ = await extract_visible_edit_body(
            event.source,
            self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
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
        else:
            regeneration_prompt = edited_content
        regeneration_metadata_turn = (
            regeneration_handled_turn
            if regeneration_turn_record.is_coalesced
            else HandledTurnState.from_source_event_id(regeneration_turn_record.anchor_event_id)
        )
        regeneration_matrix_run_metadata = self.deps.turn_store.build_run_metadata(
            regeneration_metadata_turn,
            additional_source_event_ids=(
                (original_event_id,)
                if not regeneration_turn_record.is_coalesced
                and original_event_id != regeneration_turn_record.anchor_event_id
                else ()
            ),
        )
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
        if await self.deps.ingress_hook_runner.emit_message_received_hooks(
            envelope=envelope,
            correlation_id=event.event_id,
            policy=ingress_policy,
        ):
            self._record_turn_record(regeneration_turn_record)
            return

        regenerated_resolution = await self.deps.generate_response(
            room_id=room.room_id,
            prompt=regeneration_prompt,
            reply_to_event_id=regeneration_turn_record.anchor_event_id,
            thread_id=regeneration_target.resolved_thread_id,
            target=regeneration_target,
            thread_history=context.thread_history,
            existing_event_id=response_event_id,
            existing_event_is_placeholder=False,
            user_id=requester_user_id,
            response_envelope=envelope,
            correlation_id=event.event_id,
            matrix_run_metadata=regeneration_matrix_run_metadata,
            on_lifecycle_lock_acquired=lambda: self.deps.turn_store.remove_stale_runs_for_edit(
                loaded_turn=replace(
                    loaded_turn,
                    record=regeneration_turn_record,
                ),
                room=room,
                thread_id=context.thread_id,
                original_event_id=original_event_id,
                requester_user_id=requester_user_id,
            ),
        )

        if regenerated_resolution.should_mark_handled:
            self._record_turn_record(
                replace(
                    regeneration_turn_record,
                    response_event_id=regenerated_resolution.response_identity_event_id,
                    visible_echo_event_id=regenerated_resolution.visible_response_event_id,
                ),
            )
            self._logger().info("Successfully regenerated response for edited message")
        else:
            if needs_turn_record_backfill:
                self._record_turn_record(regeneration_turn_record)
            if regenerated_resolution.state == "kept_prior_visible_response_after_error":
                self._logger().warning(
                    "Regeneration failed; keeping existing visible response",
                    original_event_id=original_event_id,
                    response_event_id=response_event_id,
                )
            else:
                self._logger().info(
                    "Suppressed regeneration left existing response unchanged",
                    original_event_id=original_event_id,
                    response_event_id=response_event_id,
                )
