"""Durable terminal delivery at the DeliveryGateway seam.

These cover the end-to-end contract: once a terminal outcome is committed,
transport failure persists a durable intent, a later attempt makes it visible,
and startup stale-stream cleanup leaves that pending repair alone.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest
from nio.exceptions import SendRetryError

from mindroom.config.main import AgentConfig, Config
from mindroom.constants import STREAM_STATUS_COMPLETED, STREAM_STATUS_KEY
from mindroom.delivery_gateway import (
    _DURABLE_TERMINAL_RETRY_FAILURE_REASON,
    DeliveryGateway,
    DeliveryGatewayDeps,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    ResponseIdentity,
)
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.matrix.stale_stream_cleanup import StaleStreamCleanupActor, recover_stale_streaming_messages
from mindroom.message_target import MessageTarget
from mindroom.redacted_turn_cleanup import RedactedTurnCleanup, RedactedTurnCleanupDeps
from mindroom.terminal_delivery import TerminalDeliveryStore, _reset_terminal_delivery_store_runtime
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, message_origin, test_runtime_paths
from tests.event_cache_test_support import raw_nio_redaction

if TYPE_CHECKING:
    from pathlib import Path

    import structlog

    from mindroom.constants import RuntimePaths

ROOM_ID = "!test:localhost"
SOURCE_EVENT_ID = "$source"
PLACEHOLDER_EVENT_ID = "$placeholder"
FINAL_BODY = "the committed final answer"


@pytest.fixture(autouse=True)
def _clean_store_runtime() -> None:
    """Keep process-wide durable store state from leaking between tests."""
    _reset_terminal_delivery_store_runtime()
    yield
    _reset_terminal_delivery_store_runtime()


def _config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(agents={"helper": AgentConfig(display_name="HelperAgent", rooms=[ROOM_ID])}),
        runtime_paths,
    )
    return config, runtime_paths


def _envelope(target: MessageTarget) -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id=SOURCE_EVENT_ID,
        target=target,
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="helper",
        origin=message_origin(
            sender_id="@user:localhost",
            requester_id="@user:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
    )


def _identity(target: MessageTarget, *, correlation_id: str = "corr-durable") -> ResponseIdentity:
    return ResponseIdentity(
        response_kind="ai",
        response_envelope=_envelope(target),
        correlation_id=correlation_id,
    )


def _response_hooks(target: MessageTarget, *, response_text: str = FINAL_BODY) -> SimpleNamespace:
    return SimpleNamespace(
        apply_before_response=AsyncMock(
            return_value=SimpleNamespace(
                response_text=response_text,
                response_kind="ai",
                tool_trace=None,
                extra_content=None,
                envelope=_envelope(target),
                suppress=False,
            ),
        ),
        apply_final_response_transform=AsyncMock(
            return_value=SimpleNamespace(response_text=response_text, response_kind="ai", envelope=_envelope(target)),
        ),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )


def _resolver() -> MagicMock:
    resolver = MagicMock()
    resolver.deps.conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value=None)
    resolver.deps.conversation_cache.notify_outbound_message = MagicMock()
    return resolver


def _gateway(
    *,
    tmp_path: Path,
    client: nio.AsyncClient,
    target: MessageTarget,
    store: TerminalDeliveryStore | None = None,
    logger: structlog.stdlib.BoundLogger | None = None,
) -> tuple[DeliveryGateway, TerminalDeliveryStore]:
    config, runtime_paths = _config(tmp_path)
    terminal_delivery_store = store or TerminalDeliveryStore(
        agent_name="helper",
        base_path=tmp_path / "tracking",
    )
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=client, orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths,
            agent_name="helper",
            logger=logger or MagicMock(),
            redact_message_event=AsyncMock(return_value=True),
            resolver=_resolver(),
            response_hooks=_response_hooks(target),
            terminal_delivery_store=terminal_delivery_store,
        ),
    )
    return gateway, terminal_delivery_store


def _stream_outcome(*, failure_reason: str = "terminal_update_failed") -> StreamTransportOutcome:
    return StreamTransportOutcome(
        last_physical_stream_event_id=PLACEHOLDER_EVENT_ID,
        terminal_status="completed",
        rendered_body="partial strea",
        visible_body_state="visible_body",
        canonical_final_body_candidate=FINAL_BODY,
        failure_reason=failure_reason,
    )


class TestRecordingCommittedOutcomes:
    """A committed terminal outcome survives exhausted transport retries."""

    @pytest.mark.asyncio
    async def test_stream_terminal_edit_failure_persists_the_final_body(self, tmp_path: Path) -> None:
        """Recovery blocking past the immediate budget leaves a durable pending final."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)

        outcome = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert outcome.failure_reason == _DURABLE_TERMINAL_RETRY_FAILURE_REASON
        pending = store.unsettled_items()
        assert len(pending) == 1
        assert pending[0].target_event_id == PLACEHOLDER_EVENT_ID
        assert pending[0].outcome_kind == "completed"
        assert FINAL_BODY in pending[0].body
        assert (pending[0].extra_content or {})[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED

    @pytest.mark.asyncio
    async def test_model_error_without_delivery_failure_is_not_persisted(self, tmp_path: Path) -> None:
        """Only Matrix transport failures become durable retries."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)

        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(failure_reason="model_error"),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )

        assert store.unsettled_items() == ()

    @pytest.mark.asyncio
    async def test_final_edit_failure_persists_instead_of_overwriting_the_placeholder(self, tmp_path: Path) -> None:
        """A failed placeholder finalize keeps the answer instead of a failure note."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(side_effect=SendRetryError("Room timeline recovery is still pending."))
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=target,
                existing_event_id=PLACEHOLDER_EVENT_ID,
                response_text=FINAL_BODY,
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
                existing_event_is_placeholder=True,
            ),
        )

        assert outcome.failure_reason == _DURABLE_TERMINAL_RETRY_FAILURE_REASON
        assert outcome.final_visible_body is None
        pending = store.unsettled_items()
        assert len(pending) == 1
        assert pending[0].body == FINAL_BODY

    @pytest.mark.asyncio
    async def test_no_durable_store_keeps_the_previous_failure_behaviour(self, tmp_path: Path) -> None:
        """Without a store the gateway still finalizes a failed placeholder visibly."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(side_effect=SendRetryError("Room timeline recovery is still pending."))
        config, runtime_paths = _config(tmp_path)
        gateway = DeliveryGateway(
            DeliveryGatewayDeps(
                runtime=SimpleNamespace(client=client, orchestrator=None, config=config, runtime_started_at=0.0),
                runtime_paths=runtime_paths,
                agent_name="helper",
                logger=MagicMock(),
                redact_message_event=AsyncMock(return_value=True),
                resolver=_resolver(),
                response_hooks=_response_hooks(target),
            ),
        )

        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=target,
                existing_event_id=PLACEHOLDER_EVENT_ID,
                response_text=FINAL_BODY,
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
                existing_event_is_placeholder=True,
            ),
        )

        assert outcome.failure_reason == "delivery_failed"


class TestDurableAttempts:
    """One durable attempt against a live Matrix client."""

    @pytest.mark.asyncio
    async def test_pending_final_lands_after_recovery_and_survives_restart(self, tmp_path: Path) -> None:
        """A pending final reloads after restart and the active bot makes it visible."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        blocked_client = make_matrix_client_mock()
        blocked_client.room_send = AsyncMock(side_effect=SendRetryError("Room timeline recovery is still pending."))
        first_gateway, first_store = _gateway(tmp_path=tmp_path, client=blocked_client, target=target)
        await first_gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )
        assert len(first_store.unsettled_items()) == 1

        _reset_terminal_delivery_store_runtime()
        recovered_client = make_matrix_client_mock()
        recovered_client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$repaired"}, ROOM_ID),
        )
        restarted_gateway, restarted_store = _gateway(tmp_path=tmp_path, client=recovered_client, target=target)
        reloaded = restarted_store.warm()
        assert len(reloaded) == 0
        item = restarted_store.unsettled_items()[0]

        attempt = await restarted_gateway.attempt_pending_terminal_delivery(item)

        assert attempt.result == "delivered"
        sent_content = recovered_client.room_send.await_args.kwargs["content"]
        assert sent_content["m.relates_to"]["event_id"] == PLACEHOLDER_EVENT_ID
        assert sent_content["m.relates_to"]["rel_type"] == "m.replace"
        assert FINAL_BODY in sent_content["m.new_content"]["body"]
        assert recovered_client.room_send.await_args.kwargs["tx_id"] == item.transaction_id

    @pytest.mark.asyncio
    async def test_repeating_the_same_attempt_reuses_one_transaction(self, tmp_path: Path) -> None:
        """A retry after an unacknowledged success repeats one idempotent edit."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(return_value=nio.RoomSendResponse.from_dict({"event_id": "$repaired"}, ROOM_ID))
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )
        item = store.unsettled_items()[0]

        first = await gateway.attempt_pending_terminal_delivery(item)
        second = await gateway.attempt_pending_terminal_delivery(item)

        assert first.result == "delivered"
        assert second.result == "delivered"
        transaction_ids = {call.kwargs["tx_id"] for call in client.room_send.await_args_list}
        assert transaction_ids == {item.transaction_id}
        sent_bodies = {call.kwargs["content"]["m.new_content"]["body"] for call in client.room_send.await_args_list}
        assert len(sent_bodies) == 1

    @pytest.mark.asyncio
    async def test_redacted_target_is_superseded_not_recreated(self, tmp_path: Path) -> None:
        """A redacted response event ends the retry instead of resurrecting content."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventResponse.from_dict(
                {
                    "event_id": PLACEHOLDER_EVENT_ID,
                    "sender": "@helper:localhost",
                    "origin_server_ts": 1,
                    "type": "m.room.message",
                    "room_id": ROOM_ID,
                    "content": {},
                    "unsigned": {"redacted_because": {"type": "m.room.redaction"}},
                },
            ),
        )
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )
        item = store.unsettled_items()[0]

        attempt = await gateway.attempt_pending_terminal_delivery(item)

        assert attempt.result == "superseded"
        assert attempt.reason == "target_event_redacted"
        client.room_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_target_is_superseded(self, tmp_path: Path) -> None:
        """A response event the homeserver no longer knows ends the retry."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_get_event = AsyncMock(
            return_value=nio.RoomGetEventError.from_dict({"errcode": "M_NOT_FOUND", "error": "not found"}),
        )
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )
        item = store.unsettled_items()[0]

        attempt = await gateway.attempt_pending_terminal_delivery(item)

        assert attempt.result == "superseded"
        assert attempt.reason == "target_event_missing"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("errcode", "expected_result"),
        [
            ("M_FORBIDDEN", "permanent"),
            ("M_TOO_LARGE", "permanent"),
            ("M_LIMIT_EXCEEDED", "transient"),
            ("M_UNKNOWN", "transient"),
        ],
    )
    async def test_matrix_error_codes_are_classified(
        self,
        tmp_path: Path,
        errcode: str,
        expected_result: str,
    ) -> None:
        """Permanent room failures dead-letter while transport failures keep retrying."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        client = make_matrix_client_mock()
        client.room_send = AsyncMock(
            return_value=nio.RoomSendError.from_dict({"errcode": errcode, "error": "rejected"}, ROOM_ID),
        )
        gateway, store = _gateway(tmp_path=tmp_path, client=client, target=target)
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )
        item = store.unsettled_items()[0]

        attempt = await gateway.attempt_pending_terminal_delivery(item)

        assert attempt.result == expected_result

    @pytest.mark.asyncio
    async def test_replaced_client_is_resolved_per_attempt(self, tmp_path: Path) -> None:
        """A config reload swaps the Matrix client without stranding pending work."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        blocked_client = make_matrix_client_mock()
        blocked_client.room_send = AsyncMock(side_effect=SendRetryError("Room timeline recovery is still pending."))
        gateway, store = _gateway(tmp_path=tmp_path, client=blocked_client, target=target)
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )
        item = store.unsettled_items()[0]
        assert (await gateway.attempt_pending_terminal_delivery(item)).result == "transient"

        replacement_client = make_matrix_client_mock()
        replacement_client.room_send = AsyncMock(
            return_value=nio.RoomSendResponse.from_dict({"event_id": "$repaired"}, ROOM_ID),
        )
        gateway.deps.runtime.client = replacement_client

        attempt = await gateway.attempt_pending_terminal_delivery(item)

        assert attempt.result == "delivered"
        replacement_client.room_send.assert_awaited()

    @pytest.mark.asyncio
    async def test_attempt_without_a_client_is_transient(self, tmp_path: Path) -> None:
        """A torn-down client defers the attempt instead of failing it permanently."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )
        item = store.unsettled_items()[0]
        gateway.deps.runtime.client = None

        attempt = await gateway.attempt_pending_terminal_delivery(item)

        assert attempt.result == "transient"
        assert attempt.reason == "matrix_client_unavailable"


class TestStaleStreamCleanupInteraction:
    """Startup repair must not fight durable delivery."""

    @pytest.mark.asyncio
    async def test_cleanup_skips_a_durably_owned_stream(self, tmp_path: Path) -> None:
        """No interruption note is written over a pending committed final response."""
        config, runtime_paths = _config(tmp_path)
        bot_user_id = "@helper:localhost"
        client = make_matrix_client_mock(user_id=bot_user_id)
        client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[ROOM_ID]))
        streaming_event = nio.RoomMessageText.from_dict(
            {
                "event_id": PLACEHOLDER_EVENT_ID,
                "sender": bot_user_id,
                "origin_server_ts": 1,
                "type": "m.room.message",
                "room_id": ROOM_ID,
                "content": {"msgtype": "m.text", "body": "partial strea", STREAM_STATUS_KEY: "streaming"},
            },
        )
        client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(room_id=ROOM_ID, chunk=[streaming_event], start="", end=None),
        )

        result = await recover_stale_streaming_messages(
            {
                bot_user_id: StaleStreamCleanupActor(
                    client=client,
                    conversation_cache=None,
                    pending_terminal_delivery_event_ids=lambda _room_id: frozenset({PLACEHOLDER_EVENT_ID}),
                ),
            },
            resume_client=None,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths,
            startup_cutoff_ms=None,
            scanned_room_ids=set(),
        )

        assert result.cleaned_count == 0
        assert result.resumed_count == 0
        client.room_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cleanup_does_not_auto_resume_a_durably_owned_stream(self, tmp_path: Path) -> None:
        """A pending committed final never triggers a duplicate regenerated turn."""
        config, runtime_paths = _config(tmp_path)
        config.defaults.auto_resume_after_restart = True
        bot_user_id = "@helper:localhost"
        client = make_matrix_client_mock(user_id=bot_user_id)
        client.joined_rooms = AsyncMock(return_value=nio.JoinedRoomsResponse(rooms=[ROOM_ID]))
        interrupted_event = nio.RoomMessageText.from_dict(
            {
                "event_id": PLACEHOLDER_EVENT_ID,
                "sender": bot_user_id,
                "origin_server_ts": 1,
                "type": "m.room.message",
                "room_id": ROOM_ID,
                "content": {"msgtype": "m.text", "body": "partial strea", STREAM_STATUS_KEY: "streaming"},
            },
        )
        client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(room_id=ROOM_ID, chunk=[interrupted_event], start="", end=None),
        )
        resume_client = make_matrix_client_mock(user_id=bot_user_id)

        result = await recover_stale_streaming_messages(
            {
                bot_user_id: StaleStreamCleanupActor(
                    client=client,
                    conversation_cache=None,
                    pending_terminal_delivery_event_ids=lambda _room_id: frozenset({PLACEHOLDER_EVENT_ID}),
                ),
            },
            resume_client=resume_client,
            resume_conversation_cache=None,
            config=config,
            runtime_paths=runtime_paths,
            startup_cutoff_ms=None,
            scanned_room_ids=set(),
        )

        assert result.resumed_count == 0
        resume_client.room_send.assert_not_awaited()


class TestRedactionCancellation:
    """Redacting a source or response stops the durable retry that owns it."""

    @pytest.mark.asyncio
    async def test_source_redaction_cancels_pending_delivery(self, tmp_path: Path) -> None:
        """A redacted question never resurrects its undelivered answer."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        gateway, store = _gateway(tmp_path=tmp_path, client=make_matrix_client_mock(), target=target)
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )
        assert len(store.unsettled_items()) == 1
        conversation_cache = MagicMock()
        conversation_cache.apply_redaction = AsyncMock()
        cleanup = RedactedTurnCleanup(
            RedactedTurnCleanupDeps(
                conversation_cache=conversation_cache,
                turn_store=MagicMock(),
                terminal_delivery_store=store,
            ),
        )
        room = MagicMock()
        room.room_id = ROOM_ID

        await cleanup.handle(
            room,
            raw_nio_redaction(
                {
                    "type": "m.room.redaction",
                    "event_id": "$redaction",
                    "sender": "@user:localhost",
                    "origin_server_ts": 1,
                },
                redacts=SOURCE_EVENT_ID,
            ),
        )

        assert store.unsettled_items() == ()
        settled = store.items()[0]
        assert settled.state == "superseded"
        assert settled.settled_reason == "source_event_redacted"


class TestObservability:
    """Structured logs stay free of response bodies."""

    @pytest.mark.asyncio
    async def test_recording_logs_no_response_body(self, tmp_path: Path) -> None:
        """Durable-retry logging reports shape and counts, never the answer text."""
        target = MessageTarget.resolve(ROOM_ID, None, SOURCE_EVENT_ID)
        logger = MagicMock()
        gateway, _store = _gateway(
            tmp_path=tmp_path,
            client=make_matrix_client_mock(),
            target=target,
            logger=logger,
        )

        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                stream_transport_outcome=_stream_outcome(),
                initial_delivery_kind="sent",
                identity=_identity(target),
                tool_trace=None,
                extra_content=None,
            ),
        )

        logged = repr(logger.mock_calls)
        assert "Persisted terminal delivery for durable retry" in logged
        assert FINAL_BODY not in logged
        assert "partial strea" not in logged
