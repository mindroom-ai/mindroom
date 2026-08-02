"""Durable execution and recovery for explicit command turns."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.commands.handler import (
    COMMAND_TYPES_WITH_SIDE_EFFECTS,
    CommandHandlerContext,
    agent_owns_command,
    handle_command,
)
from mindroom.commands.parsing import CommandType
from mindroom.constants import ROUTER_AGENT_NAME, RuntimePaths
from mindroom.hooks import build_hook_matrix_admin
from mindroom.inbound_turn_normalizer import TextNormalizationRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio
    import structlog

    from mindroom.commands.parsing import Command
    from mindroom.dispatch_handoff import TextDispatchEvent
    from mindroom.handled_turns import TurnRecord
    from mindroom.hooks import HookMatrixAdmin
    from mindroom.inbound_turn_normalizer import InboundTurnNormalizer
    from mindroom.matrix.cache import ConversationEventCache
    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.message_target import MessageTarget
    from mindroom.runtime_protocols import SupportsClientConfigOrchestrator
    from mindroom.turn_policy import TurnPolicy
    from mindroom.turn_store import TurnStore
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler

_UNCERTAIN_COMMAND_RESULT = (
    "⚠️ This command was interrupted after execution began, so its outcome is uncertain. "
    "Inspect the current state before retrying it."
)


@dataclass(frozen=True)
class CommandTurnExecutorDeps:
    """Collaborators for command execution and durable recovery."""

    runtime: SupportsClientConfigOrchestrator
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    normalizer: InboundTurnNormalizer
    conversation_cache: MatrixConversationCache
    turn_policy: TurnPolicy
    turn_store: TurnStore
    visible_responses: VisibleResponseReconciler
    event_cache: Callable[[], ConversationEventCache]
    recover_config_confirmation_setup: Callable[[str, str], Awaitable[bool]]


@dataclass
class CommandTurnExecutor:
    """Own the durable command journal from admission through visible settlement."""

    deps: CommandTurnExecutorDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for command execution"
            raise RuntimeError(msg)
        return client

    async def execute(
        self,
        room: nio.MatrixRoom,
        event: TextDispatchEvent,
        requester_user_id: str,
        command: Command,
        *,
        target: MessageTarget,
        handled_turn: TurnRecord,
    ) -> None:
        """Run one explicit command under its durable turn journal."""
        event = await self.deps.normalizer.resolve_text_event(
            TextNormalizationRequest(event=event),
        )
        command_turn, recovered_response_event_id = await self.deps.visible_responses.prepare_visible_delivery_turn(
            handled_turn,
            requester_id=requester_user_id,
            correlation_id=event.event_id,
            target=target,
        )
        if command_turn is None:
            return
        if await self._recover_visible_response(
            room_id=room.room_id,
            command_type=command.type,
            command_turn=command_turn,
            response_event_id=recovered_response_event_id,
        ):
            return
        command_turn = await self._resume_or_start(
            command_turn,
            command_type=command.type,
            target=target,
            recovered_response_event_id=recovered_response_event_id,
        )
        if command_turn is None:
            return
        active_command_turn = command_turn

        async def send_response(
            response_text: str,
            *,
            skip_mentions: bool = False,
        ) -> str | None:
            return await self.deps.visible_responses.deliver_recoverable_text(
                active_command_turn,
                target=target,
                response_text=response_text,
                recovered_response_event_id=recovered_response_event_id,
                skip_mentions=skip_mentions,
            )

        async def record_command_result(response_text: str) -> None:
            nonlocal active_command_turn
            active_command_turn = await self._persist_checkpoint(
                active_command_turn,
                command_result_text=response_text,
            )

        def record_command_turn(outcome: TurnRecord) -> None:
            self.deps.turn_store.record_responded_turn(
                replace(active_command_turn, response_event_id=outcome.response_event_id),
            )

        orchestrator = self.deps.runtime.orchestrator
        reload_plugins = (
            (lambda: orchestrator.reload_plugins_now(source="command")) if orchestrator is not None else None
        )
        context = CommandHandlerContext(
            client=self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            logger=self.deps.logger,
            conversation_cache=self.deps.conversation_cache,
            event_cache=self.deps.event_cache(),
            matrix_admin=self._matrix_admin(),
            stable_target=target,
            record_handled_turn=record_command_turn,
            record_command_result=record_command_result,
            send_response=send_response,
            reload_plugins=reload_plugins,
            responder_candidates_for_room=self.deps.turn_policy.responder_candidates_for_room,
        )
        await handle_command(
            context=context,
            room=room,
            event=event,
            command=command,
            requester_user_id=requester_user_id,
        )

    def _matrix_admin(self) -> HookMatrixAdmin | None:
        orchestrator = self.deps.runtime.orchestrator
        if orchestrator is not None:
            return orchestrator.hook_matrix_admin()
        if self.deps.agent_name != ROUTER_AGENT_NAME:
            return None
        return build_hook_matrix_admin(
            self._client(),
            self.deps.runtime_paths,
            config=self.deps.runtime.config,
        )

    async def _recover_visible_response(
        self,
        *,
        room_id: str,
        command_type: CommandType,
        command_turn: TurnRecord,
        response_event_id: str | None,
    ) -> bool:
        if response_event_id is None:
            return False
        if command_type is CommandType.CONFIG and not await self.deps.recover_config_confirmation_setup(
            room_id,
            response_event_id,
        ):
            return False
        self.deps.turn_store.record_responded_turn(
            replace(command_turn, response_event_id=response_event_id),
        )
        return True

    async def _persist_checkpoint(
        self,
        command_turn: TurnRecord,
        *,
        command_execution_started: bool | None = None,
        command_result_text: str | None = None,
    ) -> TurnRecord:
        persisted_turn = await asyncio.to_thread(
            self.deps.turn_store.record_pending_turn,
            replace(
                command_turn,
                command_execution_started=(
                    command_turn.command_execution_started
                    if command_execution_started is None
                    else command_execution_started
                ),
                command_result_text=command_result_text,
            ),
        )
        if persisted_turn is None or persisted_turn.completed:
            msg = "Failed to persist pending command checkpoint"
            raise RuntimeError(msg)
        return persisted_turn

    async def _deliver_checkpointed_result(
        self,
        command_turn: TurnRecord,
        *,
        target: MessageTarget,
        response_text: str,
        recovered_response_event_id: str | None,
    ) -> None:
        response_event_id = await self.deps.visible_responses.deliver_recoverable_text(
            command_turn,
            target=target,
            response_text=response_text,
            recovered_response_event_id=recovered_response_event_id,
            skip_mentions=True,
        )
        self.deps.turn_store.record_responded_turn(
            replace(command_turn, response_event_id=response_event_id),
        )

    async def _resume_or_start(
        self,
        command_turn: TurnRecord,
        *,
        command_type: CommandType,
        target: MessageTarget,
        recovered_response_event_id: str | None,
    ) -> TurnRecord | None:
        if command_turn.command_execution_started and command_turn.command_result_text is None:
            command_turn = await self._persist_checkpoint(
                command_turn,
                command_result_text=_UNCERTAIN_COMMAND_RESULT,
            )
        if command_turn.command_result_text is not None:
            await self._deliver_checkpointed_result(
                command_turn,
                target=target,
                response_text=command_turn.command_result_text,
                recovered_response_event_id=recovered_response_event_id,
            )
            return None
        if command_type in COMMAND_TYPES_WITH_SIDE_EFFECTS:
            return await self._persist_checkpoint(
                command_turn,
                command_execution_started=True,
            )
        return command_turn

    async def execute_if_owned(
        self,
        room: nio.MatrixRoom,
        event: TextDispatchEvent,
        requester_user_id: str,
        command: Command,
        *,
        target: MessageTarget,
        handled_turn: TurnRecord,
    ) -> bool:
        """Execute only on the bot that owns this command response."""
        if not agent_owns_command(
            command,
            agent_name=self.deps.agent_name,
            config=self.deps.runtime.config,
            room=room,
            requester_user_id=requester_user_id,
        ):
            return False
        await self.execute(
            room=room,
            event=event,
            requester_user_id=requester_user_id,
            command=command,
            target=target,
            handled_turn=handled_turn,
        )
        return True
