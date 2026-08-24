"""The outbox is what makes one turn produce at most one visible answer.

A turn's final answer is durable before it is attempted and carries a
transaction ID derived from the turn, so a resend after a crash collapses onto
the event the homeserver already accepted rather than posting a second answer.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import (
    DURABLE_FINAL_OUTCOME_KEY,
    SILENT_SCHEDULE_NO_REPLY_TOKEN,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
)
from mindroom.delivery_gateway import (
    CancelledVisibleNoteRequest,
    DeliveryGateway,
    DeliveryGatewayDeps,
    DeliveryStage,
    FinalDeliveryRequest,
    ResponseIdentity,
    SendTextRequest,
    StreamingDeliveryRequest,
)
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
from mindroom.event_journal import DepartureSource
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.hooks.context import ResponseDraft
from mindroom.matrix.client_delivery import DeliveredMatrixEvent, MatrixDeliveryFailure, MatrixDeliveryFailureKind
from mindroom.matrix.large_messages import (
    _MATRIX_EVENT_HARD_LIMIT,
    _calculate_delivery_event_size,
    _calculate_event_size,
)
from mindroom.matrix_delivery import MatrixDeliveryWorker, PermanentDeliveryError, RecoveryOutcome
from mindroom.message_target import MessageTarget
from mindroom.streaming import PROGRESS_PLACEHOLDER
from mindroom.tool_system.events import ToolTraceEntry
from tests.conftest import (
    FakeOutbox,
    bind_runtime_paths,
    ignore_delivered_projection,
    ignore_final_delivery_handoff,
    make_outbox_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path

    from mindroom.event_journal import (
        DeliveryAcknowledgement,
        EventJournalStore,
        MatrixDelivery,
        MatrixDeliveryView,
        PrincipalStore,
        ProjectedEvent,
        TerminalTurnWrite,
    )


async def _empty_stream() -> AsyncIterator[str]:
    """Return a stream with nothing in it; these tests never run one."""
    return
    yield ""


pytestmark = pytest.mark.asyncio

_ROOM_ID = "!room:localhost"
_AGENT_USER_ID = "@agent:localhost"


def _failed_delivery() -> MatrixDeliveryFailure:
    """Return one typed Matrix failure for delivery-path tests."""
    return MatrixDeliveryFailure(MatrixDeliveryFailureKind.SEND_EXCEPTION, "test delivery failure")


def _identity(
    source_event_id: str = "$cause",
    *,
    source_kind: str = MESSAGE_SOURCE_KIND,
) -> ResponseIdentity:
    """Return the identity of one visible response, caused by one event."""
    return ResponseIdentity(
        response_kind="agent",
        response_envelope=SimpleNamespace(  # type: ignore[arg-type]
            source_event_id=source_event_id,
            source_kind=source_kind,
        ),
        correlation_id="c1",
    )


def _delivered_event_response(
    room_id: str,
    event_id: str,
    *,
    content: dict[str, object] | None = None,
    timestamp: int = 1_000,
) -> nio.RoomGetEventResponse:
    """Return the authoritative metadata for one test delivery."""
    event = MagicMock()
    event.event_id = event_id
    event.sender = _AGENT_USER_ID
    event.server_timestamp = timestamp
    event.source = {
        "event_id": event_id,
        "room_id": room_id,
        "type": "m.room.message",
        "content": content if content is not None else {"msgtype": "m.text", "body": event_id},
        "unsigned": {},
    }
    response = nio.RoomGetEventResponse()
    response.event = event
    return response


@pytest.fixture
def alice(journal_store: EventJournalStore) -> PrincipalStore:
    """Return one bound principal view."""
    return journal_store.principal("agent@alice")


def _gateway(
    tmp_path: Path,
    outbox: MatrixDeliveryView | None = None,
    *,
    sending_device_id: str | None = "CURRENT-DEVICE",
    terminal_turn_for: Callable[[str, str], TurnRecord | None] | None = None,
    terminal_turn_committed: Callable[[str, str], Awaitable[None]] | None = None,
) -> DeliveryGateway:
    """Return a delivery gateway whose only real collaborator is the outbox."""
    config = bind_runtime_paths(
        Config(agents={"agent": AgentConfig(display_name="Agent")}),
        test_runtime_paths(tmp_path),
    )
    client = AsyncMock()
    client.user_id = _AGENT_USER_ID
    room = MagicMock()
    room.encrypted = False
    client.rooms = {_ROOM_ID: room}
    client.olm = None
    client.room_get_event = AsyncMock(side_effect=_delivered_event_response)
    return DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(
                client=client,
                config=config,
                enable_streaming=True,
                orchestrator=None,
            ),
            runtime_paths=runtime_paths_for(config),
            agent_name="agent",
            logger=MagicMock(),
            redact_message_event=AsyncMock(return_value=True),
            resolver=SimpleNamespace(
                build_message_target=MagicMock(),
                deps=SimpleNamespace(
                    conversation_reader=SimpleNamespace(
                        latest_thread_event_id=AsyncMock(return_value="$root"),
                    ),
                ),
            ),
            response_hooks=MagicMock(_apply_before_response=AsyncMock(), emit_after_response=AsyncMock()),
            outbox=outbox if outbox is not None else make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
            sending_device_id=lambda: sending_device_id,
            terminal_turn_for=terminal_turn_for,
            terminal_turn_committed=terminal_turn_committed,
        ),
    )


class TestTurnDeliveryGoesThroughTheOutbox:
    """A send that belongs to a turn is durable before it is attempted."""

    @staticmethod
    def _hooks() -> MagicMock:
        """Return hooks that pass the draft through unchanged."""
        return MagicMock(
            _apply_before_response=AsyncMock(
                side_effect=lambda *, identity, response_text, tool_trace, extra_content: ResponseDraft(
                    response_text=response_text,
                    response_kind=identity.response_kind,
                    tool_trace=tool_trace,
                    extra_content=extra_content,
                    envelope=identity.response_envelope,
                ),
            ),
            _apply_final_response_transform=AsyncMock(side_effect=lambda *, draft, **_kwargs: draft),
            emit_after_response=AsyncMock(),
        )

    @staticmethod
    def _final_request(
        text: str,
        *,
        source_kind: str = MESSAGE_SOURCE_KIND,
    ) -> FinalDeliveryRequest:
        """Return one final delivery for the turn caused by `$cause`."""
        target = MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True)
        return FinalDeliveryRequest(
            target=target,
            existing_event_id=None,
            response_text=text,
            identity=_identity(source_kind=source_kind),
            tool_trace=None,
            extra_content=None,
        )

    @pytest.mark.parametrize(
        "text",
        ["", " \n\t", SILENT_SCHEDULE_NO_REPLY_TOKEN, f"  {SILENT_SCHEDULE_NO_REPLY_TOKEN.lower()}\n"],
    )
    async def test_silent_schedule_no_report_response_is_suppressed_after_hooks(
        self,
        tmp_path: Path,
        text: str,
    ) -> None:
        """Silent whitespace settles as suppressed without creating a Matrix event."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": text}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await gateway.deliver_final(
                self._final_request(text, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert outcome.event_id is None
        assert outcome.failure_reason == "silent_no_report"
        assert outbox.rows == {}
        send.assert_not_awaited()

    async def test_silent_schedule_tool_trace_no_reply_is_suppressed(self, tmp_path: Path) -> None:
        """Display-only tool markers must not turn a silent no-report result into a visible response."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": SILENT_SCHEDULE_NO_REPLY_TOKEN}))
        request = replace(
            self._final_request(
                f"🔧 `run_shell_command` [1]\n\n{SILENT_SCHEDULE_NO_REPLY_TOKEN}",
                source_kind=SILENT_SCHEDULE_SOURCE_KIND,
            ),
            tool_trace=[
                ToolTraceEntry(
                    type="tool_call_completed",
                    tool_name="run_shell_command",
                    args_preview="cmd=true",
                    result_preview="done",
                ),
            ],
        )

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await gateway.deliver_final(request)

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert outcome.event_id is None
        assert outcome.failure_reason == "silent_no_report"
        assert outbox.rows == {}
        send.assert_not_awaited()

    @pytest.mark.parametrize(
        ("text", "source_kind"),
        [
            ("Finding", SILENT_SCHEDULE_SOURCE_KIND),
            (f"Finding mentions {SILENT_SCHEDULE_NO_REPLY_TOKEN}", SILENT_SCHEDULE_SOURCE_KIND),
            (f"[{SILENT_SCHEDULE_NO_REPLY_TOKEN}]", SILENT_SCHEDULE_SOURCE_KIND),
            ("", MESSAGE_SOURCE_KIND),
            (SILENT_SCHEDULE_NO_REPLY_TOKEN, MESSAGE_SOURCE_KIND),
        ],
    )
    async def test_silent_findings_and_ordinary_empty_responses_deliver_normally(
        self,
        tmp_path: Path,
        text: str,
        source_kind: str,
    ) -> None:
        """Automatic suppression never swallows findings or ordinary responses."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": text}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await gateway.deliver_final(self._final_request(text, source_kind=source_kind))

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$sent"
        assert outcome.suppressed is False
        send.assert_awaited_once()

    @pytest.mark.parametrize("generated_text", ["", SILENT_SCHEDULE_NO_REPLY_TOKEN])
    async def test_silent_schedule_hook_finding_delivers_after_no_report_generation(
        self,
        tmp_path: Path,
        generated_text: str,
    ) -> None:
        """A before-response hook can turn a silent completion into a visible finding."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        hooks = self._hooks()

        async def add_finding(**kwargs: object) -> ResponseDraft:
            draft = await hooks._apply_before_response(**kwargs)
            draft.response_text = "Finding from hook"
            return draft

        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=add_finding)
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "Finding from hook"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await gateway.deliver_final(
                self._final_request(generated_text, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$sent"
        assert send.await_args.args[2]["body"] == "Finding from hook"

    async def test_silent_schedule_hook_can_replace_a_finding_with_no_reply(self, tmp_path: Path) -> None:
        """The no-report acknowledgment is interpreted after before-response hooks."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        hooks = self._hooks()

        async def replace_with_no_reply(**kwargs: object) -> ResponseDraft:
            draft = await hooks._apply_before_response(**kwargs)
            draft.response_text = SILENT_SCHEDULE_NO_REPLY_TOKEN
            return draft

        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=replace_with_no_reply)
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "Finding"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await gateway.deliver_final(
                self._final_request("Finding", source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert outcome.event_id is None
        assert outbox.rows == {}
        send.assert_not_awaited()

    async def test_explicit_hook_suppression_wins_for_silent_schedule_finding(self, tmp_path: Path) -> None:
        """Explicit suppression remains authoritative even when a hook adds visible text."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        hooks = self._hooks()

        async def add_suppressed_finding(**kwargs: object) -> ResponseDraft:
            draft = await hooks._apply_before_response(**kwargs)
            draft.response_text = "Finding from hook"
            draft.suppress = True
            return draft

        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=add_suppressed_finding)
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "Finding from hook"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await gateway.deliver_final(
                self._final_request("", source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert outcome.failure_reason == "suppressed_by_hook"
        send.assert_not_awaited()

    @pytest.mark.parametrize(
        ("with_placeholder", "edit_succeeds"),
        [(False, True), (True, True), (True, False)],
    )
    @pytest.mark.parametrize("response_text", ["", SILENT_SCHEDULE_NO_REPLY_TOKEN])
    async def test_silent_schedule_before_hook_failure_is_durably_visible(
        self,
        tmp_path: Path,
        with_placeholder: bool,
        edit_succeeds: bool,
        response_text: str,
    ) -> None:
        """A hook exception publishes one generic terminal error through the final outbox stage."""
        gateway = _gateway(tmp_path, FakeOutbox())
        gateway.deps.response_hooks._apply_before_response = AsyncMock(
            side_effect=RuntimeError("internal hook detail"),
        )
        send_text = AsyncMock(return_value="$failure")
        edit_text = AsyncMock(return_value=edit_succeeds)
        request = replace(
            self._final_request(response_text, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            existing_event_id="$placeholder" if with_placeholder else None,
            existing_event_is_placeholder=with_placeholder,
        )

        with (
            patch.object(DeliveryGateway, "send_text", new=send_text),
            patch.object(DeliveryGateway, "edit_text", new=edit_text),
        ):
            outcome = await gateway.deliver_final(request)

        assert outcome.terminal_status == "error"
        assert outcome.event_id == ("$placeholder" if with_placeholder else "$failure")
        assert outcome.is_visible_response is True
        if not with_placeholder or edit_succeeds:
            assert outcome.final_visible_body == "Response failed. Please retry."
            assert "internal hook detail" not in outcome.final_visible_body
        else:
            assert outcome.failure_reason == "delivery_failed"
        gateway.deps.redact_message_event.assert_not_awaited()
        durable_request = edit_text.await_args.args[-1] if with_placeholder else send_text.await_args.args[-1]
        assert durable_request.delivery_turn_id == "$cause"
        assert durable_request.retry_sync_recovery is True
        assert durable_request.extra_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
        if with_placeholder:
            send_text.assert_not_awaited()
        else:
            edit_text.assert_not_awaited()

    async def test_ordinary_before_hook_failure_keeps_existing_eventless_behavior(self, tmp_path: Path) -> None:
        """The silent-source repair must not change ordinary interactive delivery."""
        gateway = _gateway(tmp_path, FakeOutbox())
        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=RuntimeError("hook failed"))
        send_text = AsyncMock(return_value="$unexpected")

        with patch.object(DeliveryGateway, "send_text", new=send_text):
            outcome = await gateway.deliver_final(self._final_request("answer"))

        assert outcome.terminal_status == "error"
        assert outcome.event_id is None
        send_text.assert_not_awaited()

    async def test_a_final_answer_is_enqueued_before_it_is_sent(
        self,
        tmp_path: Path,
    ) -> None:
        """The row must exist before the network call, keyed on the causing event.

        That ordering is the whole point: a crash after Matrix accepted the
        message leaves a row recovery can find, and the turn that caused it is
        the only name for it that survives a restart.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await gateway.deliver_final(self._final_request("answer"))

        assert outcome.event_id == "$sent"
        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$sent"
        assert outbox.acknowledged_projections == [()]
        gateway.deps.runtime.client.room_get_event.assert_not_awaited()

    async def test_fake_outbox_stages_share_one_membership(self) -> None:
        """The delivery double must reject a FINAL owned by a later membership."""
        outbox = FakeOutbox()
        assert (
            await outbox.enqueue_matrix_delivery(
                delivery_id="$cause",
                stage=DeliveryStage.INITIAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "Thinking..."},
            )
            is not None
        )
        outbox.room_membership_epochs[_ROOM_ID] = 1

        final = await outbox.enqueue_matrix_delivery(
            delivery_id="$cause",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )

        assert final is None
        assert ("$cause", DeliveryStage.FINAL.value) not in outbox.rows

    async def test_interactive_prompt_is_frozen_in_the_terminal_matrix_payload(self, tmp_path: Path) -> None:
        """Projection ownership requires prompt metadata to cross Matrix with the answer."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "Choose"})
        response = """```interactive
{"question":"Pick","options":[{"emoji":"✅","label":"Yes","value":"yes"}]}
```"""

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await gateway.deliver_final(self._final_request(response))

        assert outcome.event_id == "$sent"
        assert outbox.rows["$cause", "final"].payload["io.mindroom.interactive"] == {
            "creator_agent": "agent",
            "option_labels": {"1": "Yes", "✅": "Yes"},
            "options": {"1": "yes", "✅": "yes"},
            "question_text": "Pick",
            "source_event_id": "$cause",
        }

    async def test_adopted_event_projection_uses_the_content_matrix_returned(self, tmp_path: Path) -> None:
        """Recovery must not project one frozen candidate onto a different adopted event."""
        outbox = FakeOutbox()
        frozen = {
            "msgtype": "m.text",
            "body": "Choose",
            "io.mindroom.interactive": {
                "creator_agent": "agent",
                "option_labels": {"1": "Yes"},
                "options": {"1": "yes"},
                "question_text": "Choose?",
                "source_event_id": "$cause",
            },
        }
        await outbox.enqueue_matrix_delivery(
            delivery_id="$cause",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload=frozen,
        )
        claimed = await outbox.load_matrix_delivery(delivery_id="$cause", stage=DeliveryStage.FINAL)
        assert claimed is not None
        gateway = _gateway(tmp_path, outbox)
        visible = {"msgtype": "m.text", "body": "A different reply already in the room"}
        gateway.deps.runtime.client.room_get_event.return_value = _delivered_event_response(
            _ROOM_ID,
            "$adopted",
            content=visible,
        )
        gateway.deps.runtime.client.room_get_event.side_effect = None

        projections = await gateway._observe_delivered(claimed, "$adopted")

        assert len(projections) == 1
        assert projections[0].content == visible

    async def test_an_edit_acknowledgement_projects_its_target_before_the_edit(self, tmp_path: Path) -> None:
        """A missed target echo cannot leave an acknowledged prompt edit unresolved."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        edit_content = {
            "msgtype": "m.text",
            "body": "Choose",
            "m.new_content": {"msgtype": "m.text", "body": "Choose"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        }

        async def observe(room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            if event_id == "$target":
                return _delivered_event_response(
                    room_id,
                    event_id,
                    content={"msgtype": "m.text", "body": "Thinking..."},
                    timestamp=1_000,
                )
            return _delivered_event_response(room_id, event_id, content=edit_content, timestamp=2_000)

        gateway.deps.runtime.client.room_get_event.side_effect = observe
        delivery = gateway._response_delivery(AsyncMock(return_value="$edit"), handoff=None)

        assert (
            await delivery.deliver(
                delivery_id="$cause",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload=edit_content,
                edits_event_id="$target",
            )
            == "$edit"
        )
        projections = outbox.acknowledged_projections[0]
        assert tuple(projection.event_id for projection in projections) == ("$target", "$edit")
        assert projections[1].replaces_event_id == "$target"

    async def test_departure_after_send_skips_observation_but_acknowledges_delivery(self, tmp_path: Path) -> None:
        """Old-membership content settles without a stale Matrix projection read."""
        outbox = FakeOutbox()
        payload = {
            "msgtype": "m.text",
            "body": "Choose",
            "io.mindroom.interactive": {
                "creator_agent": "agent",
                "option_labels": {"1": "Yes"},
                "options": {"1": "yes"},
                "question_text": "Choose?",
                "source_event_id": "$cause",
            },
        }

        async def send(_claimed: MatrixDelivery) -> str:
            outbox.room_membership_epochs[_ROOM_ID] = 1
            return "$sent"

        gateway = _gateway(tmp_path, outbox)
        delivery = gateway._response_delivery(send, handoff=None)

        assert (
            await delivery.deliver(
                delivery_id="$cause",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload=payload,
            )
            == "$sent"
        )
        gateway.deps.runtime.client.room_get_event.assert_not_awaited()
        assert outbox.acknowledged_projections == [()]
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$sent"

    async def test_an_undecryptable_edit_target_stays_unacknowledged(self, tmp_path: Path) -> None:
        """Ciphertext cannot supply the thread identity of an edit target."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        encrypted_target = nio.MegolmEvent.from_dict(
            {
                "event_id": "$target",
                "sender": _AGENT_USER_ID,
                "origin_server_ts": 1_000,
                "type": "m.room.encrypted",
                "room_id": _ROOM_ID,
                "content": {
                    "algorithm": "m.megolm.v1.aes-sha2",
                    "ciphertext": "ciphertext",
                    "device_id": "DEVICE",
                    "sender_key": "sender-key",
                    "session_id": "session",
                },
            },
        )
        assert isinstance(encrypted_target, nio.MegolmEvent)
        target_response = nio.RoomGetEventResponse()
        target_response.event = encrypted_target
        edit_content = {
            "msgtype": "m.text",
            "body": "Choose",
            "m.new_content": {"msgtype": "m.text", "body": "Choose"},
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$target"},
        }

        async def observe(room_id: str, event_id: str) -> nio.RoomGetEventResponse:
            if event_id == "$target":
                return target_response
            return _delivered_event_response(room_id, event_id, content=edit_content, timestamp=2_000)

        gateway.deps.runtime.client.room_get_event.side_effect = observe
        delivery = gateway._response_delivery(AsyncMock(return_value="$edit"), handoff=None)

        with pytest.raises(RuntimeError, match="could not decrypt delivered event"):
            await delivery.deliver(
                delivery_id="$cause",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload=edit_content,
                edits_event_id="$target",
            )

        assert outbox.rows["$cause", "final"].acknowledged_event_id is None

    async def test_an_unreadable_delivered_event_stays_unacknowledged_for_recovery(self, tmp_path: Path) -> None:
        """The outbox must retry rather than invent projection ordering metadata."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        gateway.deps.runtime.client.room_get_event.return_value = nio.RoomGetEventError("not found")
        gateway.deps.runtime.client.room_get_event.side_effect = None
        response = """```interactive
{"question":"Pick","options":[{"emoji":"✅","label":"Yes","value":"yes"}]}
```"""
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "Pick"})

        with (
            patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)),
            pytest.raises(RuntimeError, match="could not read delivered event"),
        ):
            await gateway.deliver_final(self._final_request(response))

        stored = outbox.rows["$cause", "final"]
        assert stored.attempted
        assert stored.acknowledged_event_id is None

    async def test_a_redacted_delivery_acknowledges_without_resurrecting_its_content(self, tmp_path: Path) -> None:
        """Server redaction wins over the frozen plaintext payload."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        response = _delivered_event_response(_ROOM_ID, "$sent")
        response.event.source["unsigned"] = {"redacted_because": {}}
        gateway.deps.runtime.client.room_get_event.return_value = response
        gateway.deps.runtime.client.room_get_event.side_effect = None
        interactive_text = """```interactive
{"question":"Pick","options":[{"emoji":"✅","label":"Yes","value":"yes"}]}
```"""
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "Pick"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await gateway.deliver_final(self._final_request(interactive_text))

        assert outcome.event_id == "$sent"
        assert outbox.acknowledged_projections == [()]

    async def test_the_same_turn_resends_under_the_same_transaction_id(
        self,
        tmp_path: Path,
    ) -> None:
        """A repeated turn must collapse onto the event the server already has.

        This is what stops a restart turning one answer into two. The ID is
        derived from the turn, so the second attempt presents the identical
        one and the homeserver discards it.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})
        send = AsyncMock(return_value=delivered)

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            await gateway.deliver_final(self._final_request("answer"))
            await gateway.deliver_final(self._final_request("answer"))

        transaction_ids = {call.kwargs["transaction_id"] for call in send.await_args_list}
        assert len(transaction_ids) == 1, "a repeated turn presented a different transaction ID"

    async def test_a_rerun_turn_does_not_send_again_and_keeps_the_first_answer(
        self,
        tmp_path: Path,
    ) -> None:
        """An acknowledged turn replays its event ID instead of sending.

        Regenerated content could never become visible anyway -- the
        homeserver would drop it as a duplicate transaction and the durable
        result and the room would disagree forever. Not sending at all is the
        same guarantee without the wasted round trip, so the second run must
        both skip the network and return the first answer's event.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "first"})
        send = AsyncMock(return_value=delivered)

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            first = await gateway.deliver_final(self._final_request("first"))
            second = await gateway.deliver_final(self._final_request("second answer entirely"))

        bodies = [call.args[2]["body"] for call in send.await_args_list]
        assert bodies == ["first"], f"a rerun turn sent again: {bodies}"
        assert first.event_id == "$sent"
        assert second.event_id == "$sent"

    async def test_a_send_with_no_turn_behind_it_stays_out_of_the_outbox(
        self,
        tmp_path: Path,
    ) -> None:
        """Voice echoes and command replies are not turns.

        Giving them a durable row would put entries in the outbox that no
        recovery pass can resolve, and two unrelated sends whose derived IDs
        collided would collapse into one visible message.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "a notice"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            event_id = await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text="a notice",
                ),
            )

        assert event_id == "$sent"
        assert outbox.rows == {}

    async def test_a_streaming_placeholder_is_durable_under_its_own_stage(
        self,
        tmp_path: Path,
    ) -> None:
        """A streamed answer creates its visible message once, as a placeholder.

        Everything after that is an edit of the same event, so the placeholder
        is the send a crash could turn into two answers in the room. It needs
        the same durability the blocking path has, under its own stage, so it
        does not collide with the final delivery of the same turn.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        delivered = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "..."})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            event_id = await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text="...",
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )

        assert event_id == "$placeholder"
        assert list(outbox.rows) == [("$cause", "initial")]

    async def test_the_placeholder_and_the_final_answer_do_not_collide(
        self,
        tmp_path: Path,
    ) -> None:
        """One turn has two durable delivery points and they are distinct.

        Sharing a stage would make the final answer look like a resend of the
        placeholder, so it would never be sent and the room would keep the
        placeholder for good.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        placeholder = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "..."})
        send = AsyncMock(return_value=placeholder)

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text="...",
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )
            gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
            await gateway.deliver_final(self._final_request("the answer"))

        assert sorted(outbox.rows) == [("$cause", "final"), ("$cause", "initial")]
        transaction_ids = {call.kwargs["transaction_id"] for call in send.await_args_list}
        assert len(transaction_ids) == 2, "the two delivery points shared a transaction ID"

    async def test_the_final_answer_is_durable_even_when_it_is_an_edit(
        self,
        tmp_path: Path,
    ) -> None:
        """Once a placeholder exists the answer arrives as an edit of it.

        That is the normal path, not a corner: every turn that shows
        "Thinking..." reaches its answer this way. An edit sent outside the
        outbox leaves nothing to recover, so a crash between generating the
        answer and editing it in leaves the user reading the placeholder for
        good.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=edited)) as edit:
            outcome = await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )

        assert outcome.event_id == "$placeholder"
        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].edits_event_id == "$placeholder"
        assert edit.await_args.kwargs["transaction_id"] == "tx-$cause-final"
        assert edit.await_args.kwargs["operation"] == "edit_message"
        # The stored payload is the finished replace event, because recovery
        # sends the row verbatim and cannot rebuild an envelope.
        stored = outbox.rows["$cause", "final"].payload
        assert stored["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$placeholder"}
        assert stored["m.new_content"]["body"] == "the answer"
        # Both layers are frozen, and the outer one is the only text a client
        # that does not understand m.replace ever renders. Recovery resends
        # this row byte for byte, so an outer body still reading "Thinking..."
        # would be permanent for those clients, not a one-attempt glitch.
        assert stored["body"] == "* the answer"

    async def test_deferred_final_edit_freezes_semantic_interactive_outcome(
        self,
        tmp_path: Path,
    ) -> None:
        """Approval recovery must restore plain text and interactive registration facts."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "Choose"})
        interactive_text = (
            'Choose one.\n```interactive\n{"question":"Pick",'
            '"options":[{"emoji":"✅","label":"Yes","value":"yes"}]}\n```'
        )

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=edited)):
            outcome = await gateway.deliver_final(
                replace(
                    self._final_request(interactive_text),
                    existing_event_id="$placeholder",
                    defer_source_handoff=True,
                ),
            )

        delivery = outbox.rows["$cause", "final"]
        frozen = delivery.payload
        new_content = frozen["m.new_content"]
        prompt = new_content["io.mindroom.interactive"]
        assert prompt["question_text"] == "Pick"
        assert prompt["options"] == {"1": "yes", "✅": "yes"}
        assert new_content[DURABLE_FINAL_OUTCOME_KEY] == {"version": 2}
        semantic = delivery.result
        assert semantic is not None
        assert semantic["body"] == outcome.final_visible_body
        assert semantic["interactive"]["question_text"] == "Pick"
        assert semantic["interactive"]["option_map"] == {"1": "yes", "✅": "yes"}

    async def test_large_deferred_final_edit_freezes_a_sendable_semantic_payload(
        self,
        tmp_path: Path,
    ) -> None:
        """A recoverable final edit must not leave an impossible outbox retry."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = _AGENT_USER_ID
        client.device_id = "DEVICE"
        client.room_get_event = AsyncMock(side_effect=_delivered_event_response)
        room = MagicMock()
        room.encrypted = False
        client.rooms = {_ROOM_ID: room}
        client.olm = None
        client.upload.return_value = (
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/final-edit"}),
            None,
        )
        client.room_send.return_value = nio.RoomSendResponse(event_id="$edit", room_id=_ROOM_ID)
        gateway.deps.runtime.client = client
        answer = "final answer " + ("x" * 100_000)

        outcome = await gateway.deliver_final(
            replace(
                self._final_request(answer),
                existing_event_id="$placeholder",
                defer_source_handoff=True,
            ),
        )

        assert outcome.terminal_status == "completed"
        delivery = outbox.rows["$cause", "final"]
        frozen = delivery.payload
        assert _calculate_event_size(frozen) <= _MATRIX_EVENT_HARD_LIMIT
        assert frozen["m.new_content"][DURABLE_FINAL_OUTCOME_KEY] == {"version": 2}
        assert delivery.result == {"body": answer, "interactive": None}
        assert client.room_send.await_args.kwargs["content"] == frozen

    async def test_delivery_identity_is_included_in_the_validated_event_size(
        self,
        tmp_path: Path,
    ) -> None:
        """The exact persisted and sent event must fit after identity is attached."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = _AGENT_USER_ID
        client.device_id = "DEVICE"
        client.room_get_event = AsyncMock(side_effect=_delivered_event_response)
        room = MagicMock()
        room.encrypted = False
        client.rooms = {_ROOM_ID: room}
        client.olm = None
        client.upload.return_value = (
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/identity-sized-edit"}),
            None,
        )
        client.room_send.return_value = nio.RoomSendResponse(event_id="$edit", room_id=_ROOM_ID)
        gateway.deps.runtime.client = client
        request = replace(
            self._final_request("x" * 20_500),
            existing_event_id="$placeholder",
            defer_source_handoff=True,
            extra_content={"io.mindroom.test_metadata": "m" * 10_500},
        )

        outcome = await gateway.deliver_final(request)

        assert outcome.terminal_status == "completed"
        frozen = outbox.rows["$cause", "final"].payload
        assert _calculate_event_size(frozen) <= _MATRIX_EVENT_HARD_LIMIT
        assert client.room_send.await_args.kwargs["content"] == frozen

    async def test_uncached_encrypted_room_is_fitted_before_durable_enqueue(
        self,
        tmp_path: Path,
    ) -> None:
        """Remote encryption state must shape the payload before the outbox freezes it."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        client = AsyncMock(spec=nio.AsyncClient)
        client.rooms = {}
        client.olm = MagicMock()
        client.olm.device_id = "DEVICE"
        client.room_get_state_event.return_value = MagicMock(spec=nio.RoomGetStateEventResponse)
        client.upload.return_value = (
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/encrypted-sidecar"}),
            None,
        )
        gateway.deps.runtime.client = client
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "preview"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await gateway.deliver_final(self._final_request("x" * 50_000))

        assert outcome.event_id == "$sent"
        frozen = outbox.rows["$cause", "final"].payload
        assert frozen["msgtype"] == "m.file"
        assert (
            _calculate_delivery_event_size(
                frozen,
                room_id=_ROOM_ID,
                room_encrypted=True,
                device_id="DEVICE",
            )
            <= _MATRIX_EVENT_HARD_LIMIT
        )
        client.room_get_state_event.assert_awaited_once_with(_ROOM_ID, "m.room.encryption")

    async def test_plaintext_durable_payload_is_fitted_for_a_later_encrypted_send(
        self,
        tmp_path: Path,
    ) -> None:
        """Persistence must freeze bytes that remain valid if encryption is enabled."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = _AGENT_USER_ID
        client.device_id = "DEVICE"
        client.room_get_event = AsyncMock(side_effect=_delivered_event_response)
        room = MagicMock()
        room.encrypted = False
        client.rooms = {_ROOM_ID: room}
        client.olm = None
        client.upload.return_value = (
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/transition-sidecar"}),
            None,
        )
        client.room_send.return_value = nio.RoomSendResponse(event_id="$edit", room_id=_ROOM_ID)
        gateway.deps.runtime.client = client

        outcome = await gateway.deliver_final(
            replace(
                self._final_request("x" * 100_000),
                defer_source_handoff=True,
            ),
        )

        assert outcome.terminal_status == "completed"
        frozen = outbox.rows["$cause", "final"].payload
        assert frozen["file"]["url"] == "mxc://localhost/transition-sidecar"
        assert "url" not in frozen
        assert (
            _calculate_delivery_event_size(
                frozen,
                room_id=_ROOM_ID,
                room_encrypted=True,
                device_id="DEVICE",
            )
            <= _MATRIX_EVENT_HARD_LIMIT
        )

    async def test_unknown_uncached_room_encryption_fails_before_durable_enqueue(
        self,
        tmp_path: Path,
    ) -> None:
        """An unknown encryption state must not leave an ambiguously sized outbox row."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        client = AsyncMock(spec=nio.AsyncClient)
        client.rooms = {}
        client.olm = MagicMock()
        encryption_error = MagicMock(spec=nio.RoomGetStateEventError)
        encryption_error.status_code = "M_FORBIDDEN"
        client.room_get_state_event.return_value = encryption_error
        gateway.deps.runtime.client = client

        outcome = await gateway.deliver_final(self._final_request("answer"))

        assert outcome.terminal_status == "error"
        assert outbox.rows == {}
        client.upload.assert_not_awaited()
        client.room_send.assert_not_awaited()

    @pytest.mark.parametrize("existing_event_id", [None, "$placeholder"], ids=("send", "edit"))
    async def test_an_unrepresentable_final_is_recorded_without_a_send(
        self,
        tmp_path: Path,
        existing_event_id: str | None,
    ) -> None:
        """Irreducible metadata becomes durable terminal state without network I/O."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = _AGENT_USER_ID
        client.device_id = "DEVICE"
        room = MagicMock()
        room.encrypted = False
        client.rooms = {_ROOM_ID: room}
        client.olm = None
        client.upload.return_value = (
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/impossible-edit"}),
            None,
        )
        gateway.deps.runtime.client = client
        request = replace(
            self._final_request("x" * 70_000),
            existing_event_id=existing_event_id,
            defer_source_handoff=True,
            extra_content={"io.mindroom.required_metadata": "m" * 70_000},
        )

        outcome = await gateway.deliver_final(request)
        frozen = outbox.rows["$cause", "final"]
        repeated = await gateway.deliver_final(request)

        assert outcome.terminal_status == "error"
        assert outcome.failure_reason == "delivery_failed"
        assert repeated.terminal_status == "error"
        failed = outbox.rows["$cause", "final"]
        assert failed.permanently_failed
        assert not failed.attempted
        assert failed.edits_event_id == existing_event_id
        assert failed == frozen
        client.upload.assert_not_awaited()
        client.room_send.assert_not_awaited()

    async def test_a_regenerated_answer_cannot_go_out_under_a_frozen_edit(
        self,
        tmp_path: Path,
    ) -> None:
        """A rerun after a claimed edit must send the bytes the first attempt froze.

        The row freezes on claim, transaction ID included. If the second run
        were allowed to send its own text under that same transaction, one of
        two things happens and both are wrong: the first attempt did reach
        Matrix, so the homeserver dedupes the retry and returns the *old*
        event while every local record describes the new one -- or it did not,
        and the new text becomes visible while the durable row still says the
        old. Either way the room and the outbox disagree permanently.

        Reaching this needs a first attempt that is claimed and then fails, so
        the row is frozen but unacknowledged, which is exactly the "Matrix
        accepted it but the client never found out" window.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        request = replace(self._final_request("first answer"), existing_event_id="$placeholder")

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            refused = await gateway.deliver_final(request)
        assert refused.terminal_status == "error"
        assert refused.failure_reason == "delivery_failed"

        frozen = outbox.rows["$cause", "final"].payload
        assert frozen["m.new_content"]["body"] == "first answer"
        assert frozen["body"] == "* first answer"

        delivered = DeliveredMatrixEvent("$placeholder", dict(frozen))
        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)) as resend:
            await gateway.deliver_final(
                replace(self._final_request("regenerated answer"), existing_event_id="$placeholder"),
            )

        sent = resend.await_args.args[2]
        assert sent["m.new_content"]["body"] == "first answer", (
            "the rerun sent its own text under the frozen transaction"
        )
        # The fallback layer is frozen with the rest. A client that ignores
        # m.replace reads only this, so leaving it rebuildable would let the
        # rerun's text reach exactly the readers who cannot see it corrected.
        assert sent["body"] == "* first answer", "the rerun rebuilt the fallback body from its own text"
        assert resend.await_args.kwargs["transaction_id"] == "tx-$cause-final"
        assert outbox.rows["$cause", "final"].payload["m.new_content"]["body"] == "first answer"
        assert outbox.rows["$cause", "final"].payload["body"] == "* first answer"

    async def test_a_rerun_turn_does_not_edit_the_answer_in_twice(
        self,
        tmp_path: Path,
    ) -> None:
        """An acknowledged final edit replays instead of editing again.

        The mirror of the durability test: without it, "always enqueue" would
        pass while still issuing a second edit on every rerun.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "the answer"})
        edit = AsyncMock(return_value=edited)

        with patch("mindroom.delivery_gateway.send_message_outcome", edit):
            first = await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )
            second = await gateway.deliver_final(
                replace(self._final_request("a different answer"), existing_event_id="$placeholder"),
            )

        assert edit.await_count == 1, "a rerun turn edited the answer in a second time"
        assert first.event_id == second.event_id == "$placeholder"

    async def test_a_placeholder_terminal_edit_does_not_settle_the_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """A stream that ends still showing "Thinking..." has not answered.

        Its terminal edit carries the placeholder, and `deliver_final` is what
        delivers the real answer afterwards -- against the same turn. If the
        placeholder edit claimed that turn's final delivery, `deliver_final`
        would find its own delivery already acknowledged, send nothing, and
        leave the placeholder in the room for good.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "x"})
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        # The two edits take different primitives on purpose: a placeholder
        # edit has no delivery turn and rebuilds its envelope, while the
        # answer's edit sends the row the outbox froze.
        direct = AsyncMock(return_value=edited)
        durable = AsyncMock(return_value=edited)
        with (
            patch("mindroom.delivery_gateway.edit_message_result", direct),
            patch("mindroom.delivery_gateway.send_message_outcome", durable),
        ):
            # The stream ends on the placeholder, so its terminal edit is not
            # this turn's answer and must not claim the turn's final delivery.
            await terminal(AsyncMock(), _ROOM_ID, "$placeholder", {"body": PROGRESS_PLACEHOLDER}, PROGRESS_PLACEHOLDER)
            assert outbox.rows == {}, "a placeholder edit claimed the turn's final delivery"

            outcome = await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )

        assert outcome.event_id == "$placeholder"
        assert direct.await_count == 1, "the placeholder edit did not go out"
        assert durable.await_count == 1, "the answer did not go out through the outbox"
        assert list(outbox.rows) == [("$cause", "final")]

    async def test_a_real_terminal_edit_does_settle_the_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """The mirror: a stream that produced an answer records it.

        Without this, gating everything out would pass the test above while
        leaving streamed answers exactly as undurable as before.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        edited = DeliveredMatrixEvent("$streamed", {"msgtype": "m.text", "body": "streamed"})
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=edited)):
            await terminal(AsyncMock(), _ROOM_ID, "$streamed", {"body": "streamed"}, "streamed")

        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$streamed"

    async def test_an_oversized_terminal_edit_sends_the_frozen_payload_verbatim(
        self,
        tmp_path: Path,
    ) -> None:
        """Wire delivery must not prepare an already-frozen outbox payload again.

        A large edit becomes a sidecar-backed ``m.file`` replacement before
        the outbox freezes it. Preparing that envelope a second time promotes
        the inner ``m.file`` type to the outer edit without its required URL,
        so nio rejects the event and later history reads cannot hydrate it.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = _AGENT_USER_ID
        client.device_id = "DEVICE"
        client.room_get_event = AsyncMock(side_effect=_delivered_event_response)
        room = MagicMock()
        room.encrypted = False
        client.rooms = {_ROOM_ID: room}
        client.olm = None
        client.upload.return_value = (
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/sidecar"}),
            None,
        )
        client.room_send.return_value = nio.RoomSendResponse(event_id="$edit", room_id=_ROOM_ID)
        gateway.deps.runtime.client = client
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None
        answer = "x" * 125_000

        delivered = await terminal(
            client,
            _ROOM_ID,
            "$streamed",
            {
                "msgtype": "m.text",
                "body": answer,
                "io.mindroom.stream_status": "completed",
            },
            answer,
        )

        assert delivered is not None
        assert client.upload.await_count == 1, "wire delivery uploaded a second sidecar"
        frozen = outbox.rows["$cause", "final"].payload
        wire_content = client.room_send.await_args.kwargs["content"]
        assert wire_content == frozen
        parsed = nio.Event.parse_event(
            {
                "event_id": "$edit",
                "sender": _AGENT_USER_ID,
                "origin_server_ts": 1,
                "type": "m.room.message",
                "content": wire_content,
            },
        )
        assert not isinstance(parsed, nio.BadEvent)

    async def test_an_unacknowledged_oversized_edit_reuses_its_frozen_payload(
        self,
        tmp_path: Path,
    ) -> None:
        """A live retry must not rebuild an attempted outbox payload.

        Once the first send is attempted, its sidecar URI and transaction ID
        are frozen together even when Matrix refuses the send. A live rerun
        must retry that row directly: uploading a replacement can fail before
        the durable payload gets another chance to reach Matrix.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = _AGENT_USER_ID
        client.device_id = "DEVICE"
        client.room_get_event = AsyncMock(side_effect=_delivered_event_response)
        room = MagicMock()
        room.encrypted = False
        client.rooms = {_ROOM_ID: room}
        client.olm = None
        client.upload.return_value = (
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/sidecar"}),
            None,
        )
        client.room_send.return_value = nio.RoomSendError(message="temporary refusal")
        gateway.deps.runtime.client = client
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None
        answer = "x" * 125_000
        content = {
            "msgtype": "m.text",
            "body": answer,
            "io.mindroom.stream_status": "completed",
        }

        first = await terminal(client, _ROOM_ID, "$streamed", content, answer)

        assert first is None
        frozen = dict(outbox.rows["$cause", "final"].payload)
        assert outbox.rows["$cause", "final"].attempted
        client.upload.side_effect = AssertionError("live retry uploaded a replacement sidecar")
        client.room_send.return_value = nio.RoomSendResponse(event_id="$edit", room_id=_ROOM_ID)

        delivered = await terminal(client, _ROOM_ID, "$streamed", content, answer)

        assert delivered is not None
        assert client.upload.await_count == 1
        assert client.room_send.await_args.kwargs["content"] == frozen

    async def test_a_definitive_oversized_refusal_stops_recovery(
        self,
        tmp_path: Path,
    ) -> None:
        """The server's size refusal is inspectable and never replayed forever."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        client = AsyncMock(spec=nio.AsyncClient)
        client.user_id = _AGENT_USER_ID
        client.device_id = "DEVICE"
        client.room_get_event = AsyncMock(side_effect=_delivered_event_response)
        room = MagicMock()
        room.encrypted = False
        client.rooms = {_ROOM_ID: room}
        client.olm = None
        client.upload.return_value = (
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/sidecar"}),
            None,
        )
        client.room_send.return_value = nio.RoomSendError(
            message="event too large",
            status_code="M_TOO_LARGE",
            room_id=_ROOM_ID,
        )
        gateway.deps.runtime.client = client
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None
        answer = "x" * 125_000
        content = {
            "msgtype": "m.text",
            "body": answer,
            "io.mindroom.stream_status": "completed",
        }

        first = await terminal(client, _ROOM_ID, "$streamed", content, answer)
        second = await terminal(client, _ROOM_ID, "$streamed", content, answer)

        stored = outbox.rows["$cause", "final"]
        assert first is None
        assert second is None
        assert stored.permanent_failure_reason is not None
        assert client.room_send.await_count == 1
        assert client.upload.await_count == 1

    async def test_a_streamed_terminal_edit_freezes_its_fallback_body_too(
        self,
        tmp_path: Path,
    ) -> None:
        """The row a stream freezes has to be renderable by a client that ignores edits.

        A streamed answer's last revision is an ``m.replace`` of the message the
        stream has been editing all along, and the outer ``body`` is the only
        text a client without edit support shows for it. Recovery resends this
        row verbatim rather than rebuilding it, so whatever is stored here is
        final for those clients.

        The stream hands the terminal edit its formatted content and its display
        text as two separate arguments, and they are not the same string
        whenever the answer mentions someone. Distinct values here so the
        assertions say which of the two each layer is built from.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        edited = DeliveredMatrixEvent("$streamed", {"msgtype": "m.text", "body": "done"})
        terminal = gateway._durable_terminal_edit(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=edited)):
            await terminal(
                AsyncMock(),
                _ROOM_ID,
                "$streamed",
                {"msgtype": "m.text", "body": "done, @mindroom_code:localhost"},
                "done, @code",
            )

        stored = outbox.rows["$cause", "final"].payload
        assert stored["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$streamed"}
        assert stored["m.new_content"]["body"] == "done, @mindroom_code:localhost"
        assert stored["body"] == "* done, @code"

    async def test_a_streamed_answer_with_no_placeholder_to_edit_is_still_durable(
        self,
        tmp_path: Path,
    ) -> None:
        """A stream does not always have a placeholder to edit.

        A queued forced compaction suppresses it on purpose, and its own send
        can fail. The answer is then the stream's first visible event, and it
        reaches the room through the send path rather than the edit path --
        which is exactly where a durable row is easiest to forget.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        sent = DeliveredMatrixEvent("$streamed", {"msgtype": "m.text", "body": "streamed"})
        terminal = gateway._durable_terminal_send(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=sent)) as send:
            await terminal(AsyncMock(), _ROOM_ID, {"body": "streamed"}, "streamed")

        assert list(outbox.rows) == [("$cause", "final")]
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$streamed"
        assert outbox.rows["$cause", "final"].edits_event_id is None
        assert send.await_args.kwargs["transaction_id"] == "tx-$cause-final"

    async def test_the_stream_is_given_both_terminal_paths(
        self,
        tmp_path: Path,
    ) -> None:
        """Streaming has to be handed the durable sender, not just the editor.

        The two callbacks are what make a streamed answer recoverable, and a
        stream reaches its answer through whichever one applies. Passing only
        the editor leaves the no-placeholder shape silently direct.
        """
        gateway = _gateway(tmp_path, FakeOutbox())
        request = StreamingDeliveryRequest(
            target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
            response_stream=_empty_stream(),
            identity=_identity(),
        )

        with patch("mindroom.delivery_gateway.send_streaming_response", AsyncMock()) as stream:
            await gateway.deliver_stream(request)

        assert stream.await_args.kwargs["terminal_edit"] is not None
        assert stream.await_args.kwargs["terminal_send"] is not None

    async def test_a_stream_that_only_ever_said_thinking_does_not_settle_the_turn(
        self,
        tmp_path: Path,
    ) -> None:
        """A first visible event reading "Thinking..." is not an answer.

        Recording it as the turn's FINAL would settle the delivery with a
        placeholder, and `deliver_final` -- which delivers the real answer in
        exactly this case -- would find its own row acknowledged and send
        nothing at all.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        sent = DeliveredMatrixEvent("$placeholder", {"body": PROGRESS_PLACEHOLDER})
        terminal = gateway._durable_terminal_send(
            "$cause",
            MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
        )
        assert terminal is not None

        with patch("mindroom.delivery_gateway.send_message_result", AsyncMock(return_value=sent)) as send:
            await terminal(AsyncMock(), _ROOM_ID, {"body": PROGRESS_PLACEHOLDER}, PROGRESS_PLACEHOLDER)

        assert outbox.rows == {}
        send.assert_awaited_once()

    async def test_recovery_replays_a_final_edit_as_an_edit(
        self,
        tmp_path: Path,
    ) -> None:
        """A crash between claiming and acknowledging must not add a message.

        Recovery has no request to rebuild from; it sends the row as frozen.
        If what was frozen were the new body rather than the finished replace
        event, the recovered answer would arrive as a second ordinary message
        with the placeholder still above it -- two visible messages for one
        turn, which is the thing the outbox exists to prevent.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = DeliveredMatrixEvent("$placeholder", {"body": "the answer"})

        # A delivery that reached Matrix but whose acknowledgement was lost.
        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=None)):
            await gateway.deliver_final(
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )
        assert outbox.rows["$cause", "final"].acknowledged_event_id is None

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=edited)) as send:
            recovered = await gateway.recover_deliveries()

        assert recovered.recovered == 1
        sent = send.await_args.args[2]
        assert sent["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$placeholder"}, (
            "recovery sent a new message instead of replaying the edit"
        )
        assert send.await_args.kwargs["transaction_id"] == "tx-$cause-final"

    async def test_live_final_resolves_an_attempted_placeholder_before_the_answer(
        self,
        tmp_path: Path,
    ) -> None:
        """Live FINAL resolves an unknown placeholder before reporting completion."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        hooks = self._hooks()
        gateway.deps.response_hooks._apply_before_response = hooks._apply_before_response
        placeholder = DeliveredMatrixEvent("$placeholder", {"body": PROGRESS_PLACEHOLDER})
        answer = DeliveredMatrixEvent("$answer", {"body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text=PROGRESS_PLACEHOLDER,
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )
        with patch(
            "mindroom.delivery_gateway.send_message_outcome",
            AsyncMock(side_effect=[placeholder, answer]),
        ) as send:
            outcome = await gateway.deliver_final(self._final_request("the answer"))

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$answer"
        assert outbox.rows["$cause", "initial"].acknowledged_event_id == "$placeholder"
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$answer"
        assert [call.args[2].get("body") for call in send.await_args_list] == [
            PROGRESS_PLACEHOLDER,
            "the answer",
        ]

    async def test_cancelled_live_final_finishes_both_stages_before_propagating(
        self,
        tmp_path: Path,
    ) -> None:
        """Cancellation cannot leave FINAL for recovery after resolving INITIAL."""
        outbox = FakeOutbox()
        terminal_committed = AsyncMock()
        gateway = _gateway(
            tmp_path,
            outbox,
            terminal_turn_committed=terminal_committed,
        )
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        placeholder = DeliveredMatrixEvent("$placeholder", {"body": PROGRESS_PLACEHOLDER})
        answer = DeliveredMatrixEvent("$answer", {"body": "the answer"})
        initial_retry_started = asyncio.Event()
        release_initial_retry = asyncio.Event()

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text=PROGRESS_PLACEHOLDER,
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )

        async def send(
            _client: object,
            _room_id: str,
            content: dict[str, object],
            **_kwargs: object,
        ) -> SimpleNamespace:
            if content.get("body") == PROGRESS_PLACEHOLDER:
                initial_retry_started.set()
                await release_initial_retry.wait()
                return placeholder
            return answer

        with patch("mindroom.delivery_gateway.send_message_outcome", side_effect=send):
            delivery = asyncio.create_task(gateway.deliver_final(self._final_request("the answer")))
            await asyncio.wait_for(initial_retry_started.wait(), timeout=5)
            delivery.cancel()
            release_initial_retry.set()
            with pytest.raises(asyncio.CancelledError):
                await delivery

        assert outbox.rows["$cause", "initial"].acknowledged_event_id == "$placeholder"
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$answer"
        terminal_committed.assert_awaited_once_with("$cause", "$answer")

    async def test_live_final_ignores_its_inline_initial_result_when_another_process_wins(
        self,
        tmp_path: Path,
    ) -> None:
        """A cross-process FINAL winner cannot be reported as the placeholder."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        placeholder = DeliveredMatrixEvent("$placeholder", {"body": PROGRESS_PLACEHOLDER})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text=PROGRESS_PLACEHOLDER,
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )

        real_acknowledge = outbox.acknowledge_matrix_delivery

        async def acknowledge(
            *,
            delivery_id: str,
            stage: DeliveryStage,
            event_id: str,
            delivered_projections: tuple[ProjectedEvent, ...],
            terminal_turn: TerminalTurnWrite | None = None,
        ) -> DeliveryAcknowledgement:
            acknowledged = await real_acknowledge(
                delivery_id=delivery_id,
                stage=stage,
                event_id=event_id,
                delivered_projections=delivered_projections,
                terminal_turn=terminal_turn,
            )
            if stage is DeliveryStage.INITIAL:
                await real_acknowledge(
                    delivery_id="$cause",
                    stage=DeliveryStage.FINAL,
                    event_id="$other-final",
                    delivered_projections=(),
                )
            return acknowledged

        outbox.acknowledge_matrix_delivery = acknowledge  # type: ignore[method-assign]
        with patch(
            "mindroom.delivery_gateway.send_message_outcome",
            AsyncMock(return_value=placeholder),
        ) as send:
            outcome = await gateway.deliver_final(self._final_request("the answer"))

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$other-final"
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$other-final"
        assert send.await_count == 1
        assert send.await_args.args[2]["body"] == PROGRESS_PLACEHOLDER

    async def test_live_final_reports_the_durable_winner_when_its_send_loses_acknowledgement(
        self,
        tmp_path: Path,
    ) -> None:
        """The requested-stage callback cannot override a competing durable winner."""
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        real_acknowledge = outbox.acknowledge_matrix_delivery

        async def acknowledge(
            *,
            delivery_id: str,
            stage: DeliveryStage,
            event_id: str,
            delivered_projections: tuple[ProjectedEvent, ...],
            terminal_turn: TerminalTurnWrite | None = None,
        ) -> DeliveryAcknowledgement:
            if stage is DeliveryStage.FINAL:
                await real_acknowledge(
                    delivery_id=delivery_id,
                    stage=stage,
                    event_id="$winner",
                    delivered_projections=(),
                    terminal_turn=terminal_turn,
                )
            return await real_acknowledge(
                delivery_id=delivery_id,
                stage=stage,
                event_id=event_id,
                delivered_projections=delivered_projections,
                terminal_turn=terminal_turn,
            )

        outbox.acknowledge_matrix_delivery = acknowledge  # type: ignore[method-assign]
        sent = DeliveredMatrixEvent("$loser", {"body": "the answer"})
        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=sent)):
            outcome = await gateway.deliver_final(self._final_request("the answer"))

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$winner"
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$winner"

    async def test_a_pass_that_could_not_send_reports_the_debt_it_left(
        self,
        tmp_path: Path,
    ) -> None:
        """A recovery pass that failed is not a recovery pass that finished.

        The caller schedules the next attempt on this, so a pass reporting
        success while leaving an answer unsent would strand it until the
        process restarted.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        answer = DeliveredMatrixEvent("$answer", {"body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            await gateway.deliver_final(self._final_request("the answer"))
        assert outbox.rows["$cause", "final"].acknowledged_event_id is None

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            failed_pass = await gateway.recover_deliveries()

        assert failed_pass.failed == 1
        assert not failed_pass.complete

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=answer)):
            retried = await gateway.recover_deliveries()

        assert retried.recovered == 1
        assert retried.complete
        assert outbox.rows["$cause", "final"].acknowledged_event_id == "$answer"


class TestAnEndedMembershipStopsTheAnswer:
    """A turn that outlived its membership must not reach the room it left."""

    @staticmethod
    def _target() -> MessageTarget:
        """Return the room-mode target these tests deliver into."""
        return MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True)

    async def test_a_turn_fenced_mid_flight_produces_no_visible_answer(
        self,
        tmp_path: Path,
    ) -> None:
        """The fence deleted this conversation; the answer must not rebuild it."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)) as send:
            outcome = await gateway.deliver_final(TestTurnDeliveryGoesThroughTheOutbox._final_request("answer"))

        send.assert_not_awaited()
        assert outcome.event_id is None
        assert outcome.terminal_status == "error"
        assert outbox.rows == {}

    async def test_a_fenced_turns_final_edit_never_reaches_matrix(
        self,
        tmp_path: Path,
    ) -> None:
        """A streamed answer becomes visible by editing, so that edit is refused too."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)
        terminal = gateway._durable_terminal_edit("$cause", self._target())
        assert terminal is not None

        with patch("mindroom.delivery_gateway.edit_message_outcome", AsyncMock()) as edit:
            delivered = await terminal(AsyncMock(), _ROOM_ID, "$placeholder", {"body": "answer"}, "answer")

        edit.assert_not_awaited()
        assert delivered is None
        assert outbox.rows == {}

    async def test_a_stream_is_given_the_gate_that_stops_its_progressive_edits(
        self,
        tmp_path: Path,
    ) -> None:
        """Progressive edits never reach the outbox, so nothing else would stop them."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)
        request = StreamingDeliveryRequest(
            target=self._target(),
            response_stream=_empty_stream(),
            identity=_identity(),
        )

        with patch("mindroom.delivery_gateway.send_streaming_response", AsyncMock()) as stream:
            await gateway.deliver_stream(request)

        gate = stream.await_args.kwargs["transport_is_current"]
        assert gate is not None
        assert not await gate()

    async def test_a_cancellation_note_stops_once_the_membership_ended(self, tmp_path: Path) -> None:
        """The note has nowhere to go: the fence deleted what it would annotate."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock()) as edit:
            outcome = await gateway.deliver_cancelled_visible_note(
                CancelledVisibleNoteRequest(
                    target=self._target(),
                    event_id="$visible",
                    existing_event_is_placeholder=False,
                    cancel_source="user_stop",
                    identity=TestTurnDeliveryGoesThroughTheOutbox._final_request("x").identity,
                ),
            )

        edit.assert_not_awaited()
        assert outcome.terminal_status == "cancelled"

    async def test_a_cancellation_note_under_a_live_membership_still_lands(self, tmp_path: Path) -> None:
        """The gate must only stop the case it exists for."""
        gateway = _gateway(tmp_path, FakeOutbox())
        edited = DeliveredMatrixEvent("$visible", {"body": "stopped"})

        with patch("mindroom.delivery_gateway.edit_message_outcome", AsyncMock(return_value=edited)) as edit:
            outcome = await gateway.deliver_cancelled_visible_note(
                CancelledVisibleNoteRequest(
                    target=self._target(),
                    event_id="$visible",
                    existing_event_is_placeholder=False,
                    cancel_source="user_stop",
                    identity=TestTurnDeliveryGoesThroughTheOutbox._final_request("x").identity,
                ),
            )

        edit.assert_awaited()
        assert outcome.delivery_kind == "edited"

    async def test_suppression_cleanup_stops_once_the_membership_ended(self, tmp_path: Path) -> None:
        """The fence already dropped everything derived from that membership."""
        outbox = FakeOutbox()
        outbox.ended_membership_turn_ids.add("$cause")
        gateway = _gateway(tmp_path, outbox)

        failure = await gateway._redact_visible_response_event(
            room_id=_ROOM_ID,
            event_id="$visible",
            identity=TestTurnDeliveryGoesThroughTheOutbox._final_request("x").identity,
            redaction_reason="suppressed",
        )

        assert failure is None
        gateway.deps.redact_message_event.assert_not_awaited()

    async def test_a_stream_under_a_live_membership_keeps_its_gate_open(self, tmp_path: Path) -> None:
        """The ordinary case must still be allowed to stream."""
        gateway = _gateway(tmp_path, FakeOutbox())
        request = StreamingDeliveryRequest(
            target=self._target(),
            response_stream=_empty_stream(),
            identity=_identity(),
        )

        with patch("mindroom.delivery_gateway.send_streaming_response", AsyncMock()) as stream:
            await gateway.deliver_stream(request)

        assert await stream.await_args.kwargs["transport_is_current"]()


class TestTheFrozenEditSpeaksOneAnswer:
    """A stored edit must read the same to every client that renders it.

    An `m.replace` carries the answer twice: inside `m.new_content`, which a
    client that understands edits renders, and in the top-level `body`, which
    is what every other client shows. They are built from two separate inputs
    -- the replacement content and `new_text` -- so nothing structural stops
    them disagreeing, and the outbox freezes whatever they were.

    A row that disagrees with itself is the same failure the projection exists
    to remove, one layer lower: two readers of one history seeing two answers.
    """

    @staticmethod
    def _target() -> MessageTarget:
        """Return the room-mode target these tests deliver into."""
        return MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True)

    async def test_the_stored_edit_says_the_same_thing_to_both_kinds_of_client(
        self,
        tmp_path: Path,
    ) -> None:
        """The fallback body is the replacement body, marked as an edit.

        `* ` is the Matrix convention for an edit's fallback, so the fallback
        agreeing means the prefix and nothing else.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        terminal = gateway._durable_terminal_edit("$cause", self._target())
        assert terminal is not None
        answer = "the whole answer"
        delivered = DeliveredMatrixEvent("$sent", {})

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=delivered)):
            await terminal(AsyncMock(), _ROOM_ID, "$placeholder", {"msgtype": "m.text", "body": answer}, answer)

        stored = outbox.rows[("$cause", DeliveryStage.FINAL.value)].payload
        replacement = stored["m.new_content"]
        assert isinstance(replacement, dict)
        assert replacement["body"] == answer
        assert stored["body"] == f"* {answer}"

    async def test_a_replacement_body_the_fallback_does_not_carry_is_refused(
        self,
        tmp_path: Path,
    ) -> None:
        """The two inputs are independent, so the disagreement is constructible.

        This is the shape a caller producing its fallback from a different
        string would store: the edit-aware client reads the answer, everyone
        else reads something the agent never said. Nothing rejects it today,
        which is what this pins -- the assertion is on the stored bytes, so it
        fails the moment the two stop being derived from one text.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox)
        terminal = gateway._durable_terminal_edit("$cause", self._target())
        assert terminal is not None
        delivered = DeliveredMatrixEvent("$sent", {})

        with patch("mindroom.delivery_gateway.edit_message_result", AsyncMock(return_value=delivered)):
            await terminal(
                AsyncMock(),
                _ROOM_ID,
                "$placeholder",
                {"msgtype": "m.text", "body": "what the agent said"},
                "something else entirely",
            )

        stored = outbox.rows[("$cause", DeliveryStage.FINAL.value)].payload
        replacement = stored["m.new_content"]
        assert isinstance(replacement, dict)
        # Recorded, not endorsed: today the row keeps both strings. If a later
        # change makes the envelope derive one from the other, this fails and
        # should be replaced by an assertion that the mismatch is impossible.
        assert replacement["body"] == "what the agent said"
        assert stored["body"] == "* something else entirely"


class TestTheTerminalRecordCommitsWithItsAcknowledgement:
    """A delivered answer and the record that names it are one write.

    The acknowledgement is the durable proof that a visible answer exists and
    what its event ID is, which is exactly the fact the turn record is missing.
    Written separately, a crash between them leaves a delivered answer whose
    record does not know its response event -- and an edit of that message is
    then dropped, because there is nothing recorded to edit. Nothing else
    repairs it: outbox recovery walks unacknowledged rows and steps over this
    one, and the journal has no pending source to re-enter through.
    """

    @staticmethod
    def _final_request(text: str) -> FinalDeliveryRequest:
        """Return one final delivery for the turn caused by `$cause`."""
        return TestTurnDeliveryGoesThroughTheOutbox._final_request(text)

    async def test_a_final_acknowledgement_carries_the_bound_record(
        self,
        tmp_path: Path,
    ) -> None:
        """The record travelling with the acknowledgement names the delivered event."""
        outbox = FakeOutbox()
        pending = TurnRecord.create(["$cause"], completed=False, response_owner="agent")

        def bind(turn_id: str, event_id: str) -> TurnRecord | None:
            assert turn_id == "$cause"
            return replace(pending, response_event_id=event_id, completed=True)

        gateway = _gateway(tmp_path, outbox, terminal_turn_for=bind)
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            await gateway.deliver_final(self._final_request("answer"))

        assert len(outbox.acknowledged_terminal_turns) == 1
        turn_id, terminal_turn = outbox.acknowledged_terminal_turns[0]
        assert turn_id == "$cause"
        assert terminal_turn is not None
        assert terminal_turn.agent_name == "agent"
        assert terminal_turn.index_event_ids == ("$cause",)
        assert terminal_turn.anchor_event_id == "$cause"
        record = json.loads(terminal_turn.record_json)
        assert record["response_event_id"] == "$sent"
        assert record["completed"] is True

    async def test_a_final_edit_acknowledgement_binds_the_edited_response(
        self,
        tmp_path: Path,
    ) -> None:
        """The replacement event acknowledges delivery, while the edited event remains the response owner."""
        outbox = FakeOutbox()
        pending = TurnRecord.create(["$cause"], completed=False, response_owner="agent")
        gateway = _gateway(
            tmp_path,
            outbox,
            terminal_turn_for=lambda _turn_id, event_id: replace(
                pending,
                response_event_id=event_id,
                completed=True,
            ),
        )
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = DeliveredMatrixEvent("$replacement", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await gateway.deliver_final(
                replace(self._final_request("answer"), existing_event_id="$waiting"),
            )

        assert outcome.event_id == "$waiting"
        turn_id, terminal_turn = outbox.acknowledged_terminal_turns[0]
        assert turn_id == "$cause"
        assert terminal_turn is not None
        record = json.loads(terminal_turn.record_json)
        assert record["response_event_id"] == "$waiting"

    async def test_a_placeholder_acknowledgement_carries_no_record(
        self,
        tmp_path: Path,
    ) -> None:
        """A placeholder's acknowledgement must not bind the turn's terminal record.

        An INITIAL row is the "Thinking..." message, and calling the turn
        finished on the strength of it would mark the source handled while the
        model is still running -- so the real answer, when it arrives, has
        nowhere to go.
        """
        outbox = FakeOutbox()
        gateway = _gateway(
            tmp_path,
            outbox,
            terminal_turn_for=lambda _turn_id, event_id: TurnRecord.create(
                ["$cause"],
                response_event_id=event_id,
            ),
        )
        delivered = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "..."})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            await gateway.send_text(
                SendTextRequest(
                    target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
                    response_text="...",
                    delivery_turn_id="$cause",
                    delivery_stage=DeliveryStage.INITIAL,
                ),
            )

        assert outbox.acknowledged_terminal_turns == [("$cause", None)]

    async def test_nothing_is_carried_when_there_is_no_record_to_bind(
        self,
        tmp_path: Path,
    ) -> None:
        """A turn with no record, or one that already names its answer, binds nothing.

        The acknowledgement still has to happen -- the answer is in the room
        either way -- so the delivery must not be held up by having nothing to
        write beside it.
        """
        outbox = FakeOutbox()
        gateway = _gateway(tmp_path, outbox, terminal_turn_for=lambda _turn_id, _event_id: None)
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await gateway.deliver_final(self._final_request("answer"))

        assert outcome.event_id == "$sent"
        assert outbox.acknowledged_terminal_turns == [("$cause", None)]


class TestARacedAcknowledgementSpeaksForTheRow:
    """Two flushes can reach one FINAL row, and only one of them binds it.

    That happens whenever a delivery is retried while an earlier attempt is
    still in flight -- a recovery pass overlapping a live turn, or two
    processes sharing one principal. Both claim, both produce an event, and
    the conditional acknowledgement lets exactly one through.

    The loser then owes two things and used to get both wrong. It must report
    the event the *row* names, because everything downstream records what
    ``flush`` returns and the later terminal settlement would otherwise upsert
    the loser's event over the winner's record. And it must publish nothing to
    the in-memory ledger, because it committed nothing to publish.

    Driven through the public ``flush`` against a real store on purpose. A fake
    outbox proves nothing here: what is under test is what the production
    return value carries out of a real conditional update.
    """

    @staticmethod
    async def _enqueue(alice: PrincipalStore) -> None:
        """Record one FINAL answer as durably owed, ready to be flushed twice."""
        transaction_id = await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "answer"},
        )
        assert transaction_id is not None

    async def test_a_send_that_lost_the_row_reports_the_winners_event(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Two sends, two events, one row -- and both callers must name the stored one."""
        await self._enqueue(alice)
        losing_send_started = asyncio.Event()
        finish_losing_send = asyncio.Event()

        async def losing_send(_claimed: MatrixDelivery) -> str:
            losing_send_started.set()
            await finish_losing_send.wait()
            return "$loser"

        async def winning_send(_claimed: MatrixDelivery) -> str:
            return "$winner"

        losing = MatrixDeliveryWorker(
            store=alice,
            send=losing_send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE1",
        )
        winning = MatrixDeliveryWorker(
            store=alice,
            send=winning_send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE1",
        )

        loser = asyncio.create_task(losing.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL))
        await losing_send_started.wait()
        assert await winning.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL) == "$winner"
        finish_losing_send.set()

        assert await loser == "$winner", "the losing send reported its own event upward"
        stored = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$winner"

    async def test_an_adopted_answer_that_lost_the_row_reports_the_winners_event(
        self,
        alice: PrincipalStore,
    ) -> None:
        """The same rule on the branch that adopts an answer instead of sending one.

        A row attempted by a device this process is no longer logged in as
        makes the frozen transaction ID stop being proof, so both flushes read
        the room rather than send. Adoption is still an acknowledgement, and
        still has exactly one winner.
        """
        await self._enqueue(alice)
        await alice.claim_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        await alice.record_matrix_delivery_device(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            device_id="OLD-DEVICE",
        )

        losing_lookup_started = asyncio.Event()
        finish_losing_lookup = asyncio.Event()

        async def losing_lookup(_claimed: MatrixDelivery) -> str | None:
            losing_lookup_started.set()
            await finish_losing_lookup.wait()
            return "$loser"

        async def winning_lookup(_claimed: MatrixDelivery) -> str | None:
            return "$winner"

        async def never_sends(_claimed: MatrixDelivery) -> str:
            msg = "an adopted answer is already in the room"
            raise AssertionError(msg)

        losing = MatrixDeliveryWorker(
            store=alice,
            send=never_sends,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=losing_lookup,
        )
        winning = MatrixDeliveryWorker(
            store=alice,
            send=never_sends,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=winning_lookup,
        )

        loser = asyncio.create_task(losing.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL))
        await losing_lookup_started.wait()
        assert await winning.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL) == "$winner"
        finish_losing_lookup.set()

        assert await loser == "$winner", "the losing adoption reported its own event upward"
        stored = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$winner"

    async def test_only_the_caller_that_bound_the_row_publishes_its_record(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A shared event ID is what both callers get, and it says nothing about who won.

        Both flushes here send the same frozen transaction ID from the same
        device, so Matrix deduplicates and hands each of them the *same* event
        -- exactly as a real homeserver does. Reading ownership off that
        equality told the loser it had won, and it published a terminal record
        the database had refused to take from it.
        """
        await self._enqueue(alice)
        losing_send_started = asyncio.Event()
        finish_losing_send = asyncio.Event()

        async def losing_send(_claimed: MatrixDelivery) -> str:
            losing_send_started.set()
            await finish_losing_send.wait()
            return "$deduplicated"

        async def winning_send(_claimed: MatrixDelivery) -> str:
            return "$deduplicated"

        losing_publishes: list[tuple[str, str]] = []
        winning_publishes: list[tuple[str, str]] = []

        async def losing_publish(turn_id: str, event_id: str) -> None:
            losing_publishes.append((turn_id, event_id))

        async def winning_publish(turn_id: str, event_id: str) -> None:
            winning_publishes.append((turn_id, event_id))

        losing = MatrixDeliveryWorker(
            store=alice,
            send=losing_send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE1",
            terminal_turn_committed=losing_publish,
        )
        winning = MatrixDeliveryWorker(
            store=alice,
            send=winning_send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE1",
            terminal_turn_committed=winning_publish,
        )

        loser = asyncio.create_task(losing.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL))
        await losing_send_started.wait()
        assert await winning.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL) == "$deduplicated"
        finish_losing_send.set()
        assert await loser == "$deduplicated"

        assert winning_publishes == [("turn-1", "$deduplicated")]
        assert losing_publishes == [], "a caller that bound nothing published a record anyway"


class TestGenericDeliveryDeviceChangePolicy:
    """Non-idempotent custom events retain debt when history cannot prove absence."""

    async def test_first_claim_crash_replays_a_card_from_the_same_device(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Claim and device intent commit together before any process can die."""
        await alice.enqueue_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            event_type="io.mindroom.tool_approval",
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"status": "pending"},
        )
        await alice.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            sending_device_id="DEVICE1",
        )
        sent: list[MatrixDelivery] = []

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery)
            return "$approval"

        worker = MatrixDeliveryWorker(
            store=alice,
            send=send,
            event_type="io.mindroom.tool_approval",
            resend_after_reconciliation_miss=False,
            sending_device_id="DEVICE1",
            resolve_delivered=AsyncMock(return_value=None),
        )

        assert await worker.flush(delivery_id="approval-card-1", stage=DeliveryStage.INITIAL) == "$approval"
        assert len(sent) == 1

    async def test_reconciliation_miss_never_resends_a_clickable_event_from_a_new_device(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A room-history miss is uncertainty, not proof that a prior card never landed."""
        await alice.enqueue_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            event_type="io.mindroom.tool_approval",
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"status": "pending"},
        )
        await alice.claim_matrix_delivery(delivery_id="approval-card-1", stage=DeliveryStage.INITIAL)
        await alice.record_matrix_delivery_device(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
            device_id="OLD-DEVICE",
        )
        sent: list[MatrixDelivery] = []

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery)
            return "$duplicate"

        async def history_miss(_delivery: MatrixDelivery) -> str | None:
            return None

        worker = MatrixDeliveryWorker(
            store=alice,
            send=send,
            event_type="io.mindroom.tool_approval",
            resend_after_reconciliation_miss=False,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=history_miss,
        )

        assert await worker.flush(delivery_id="approval-card-1", stage=DeliveryStage.INITIAL) is None
        assert sent == []
        retained = await alice.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.INITIAL,
        )
        assert retained is not None
        assert retained.acknowledged_event_id is None
        assert retained.sending_device_id == "OLD-DEVICE"

    async def test_final_edit_is_adopted_instead_of_replayed_from_a_new_device(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A delayed duplicate edit could otherwise overwrite a newer replacement."""
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "* old answer"},
                edits_event_id="$placeholder",
            )
            is not None
        )
        assert (
            await alice.claim_matrix_delivery(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                sending_device_id="OLD-DEVICE",
            )
            is not None
        )
        send = AsyncMock(return_value="$duplicate-edit")
        resolve = AsyncMock(return_value="$original-edit")
        worker = MatrixDeliveryWorker(
            store=alice,
            send=send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=resolve,
        )

        event_id = await worker.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL)

        assert event_id == "$original-edit"
        resolve.assert_awaited_once()
        send.assert_not_awaited()

    async def test_an_absent_response_edit_replays_from_a_new_device(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A response edit retains liveness when the prior device left no visible event."""
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "* old answer"},
                edits_event_id="$placeholder",
            )
            is not None
        )
        assert (
            await alice.claim_matrix_delivery(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                sending_device_id="OLD-DEVICE",
            )
            is not None
        )
        send = AsyncMock(return_value="$duplicate-edit")
        resolve = AsyncMock(return_value=None)
        worker = MatrixDeliveryWorker(
            store=alice,
            send=send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=resolve,
        )

        assert await worker.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL) == "$duplicate-edit"
        resolve.assert_awaited_once()
        send.assert_awaited_once()
        delivered = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert delivered is not None
        assert delivered.acknowledged_event_id == "$duplicate-edit"
        assert not delivered.retired

    async def test_an_absent_terminal_approval_edit_replays_from_a_new_device(
        self,
        alice: PrincipalStore,
    ) -> None:
        """A terminal edit is safe to replay after its exact prior event is absent."""
        await alice.enqueue_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            event_type="io.mindroom.tool_approval",
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"status": "approved"},
            edits_event_id="$approval-card",
        )
        await alice.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            sending_device_id="OLD-DEVICE",
        )
        send = AsyncMock(return_value="$duplicate-edit")
        resolve = AsyncMock(return_value=None)
        worker = MatrixDeliveryWorker(
            store=alice,
            send=send,
            event_type="io.mindroom.tool_approval",
            resend_after_reconciliation_miss=False,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=resolve,
        )

        assert await worker.flush(delivery_id="approval-card-1", stage=DeliveryStage.FINAL) == "$duplicate-edit"
        resolve.assert_awaited_once()
        send.assert_awaited_once()
        delivered = await alice.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert delivered is not None
        assert delivered.acknowledged_event_id == "$duplicate-edit"
        assert not delivered.retired

    async def test_a_stale_approval_edit_is_adopted_before_its_attempt_retires(
        self,
        alice: PrincipalStore,
    ) -> None:
        """An old-membership edit already in Matrix remains terminal proof."""
        await alice.enqueue_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            event_type="io.mindroom.tool_approval",
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"status": "approved"},
            edits_event_id="$approval-card",
        )
        await alice.claim_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
            sending_device_id="OLD-DEVICE",
        )
        await alice.fence_departure(_ROOM_ID, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(_ROOM_ID)
        send = AsyncMock(return_value="$duplicate-edit")
        resolve = AsyncMock(return_value="$original-edit")
        worker = MatrixDeliveryWorker(
            store=alice,
            send=send,
            event_type="io.mindroom.tool_approval",
            resend_after_reconciliation_miss=False,
            sending_device_id="NEW-DEVICE",
            resolve_delivered=resolve,
        )

        assert await worker.flush(delivery_id="approval-card-1", stage=DeliveryStage.FINAL) == "$original-edit"
        send.assert_not_awaited()
        adopted = await alice.load_matrix_delivery(
            delivery_id="approval-card-1",
            stage=DeliveryStage.FINAL,
        )
        assert adopted is not None
        assert adopted.acknowledged_event_id == "$original-edit"
        assert not adopted.retired

    async def test_stale_attempt_without_a_matrix_event_is_retired_instead_of_sent(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Recovery cannot make an old membership's first physical send after rejoin."""
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "old answer"},
            )
            is not None
        )
        assert (
            await alice.claim_matrix_delivery(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                sending_device_id="DEVICE",
            )
            is not None
        )
        await alice.fence_departure(_ROOM_ID, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(_ROOM_ID)
        send = AsyncMock(return_value="$stale-answer")
        resolve = AsyncMock(return_value=None)
        worker = MatrixDeliveryWorker(
            store=alice,
            send=send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE",
            resolve_delivered=resolve,
        )

        assert await worker.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL) is None
        resolve.assert_awaited_once()
        send.assert_not_awaited()
        retired = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert retired is not None
        assert retired.retired

    async def test_a_send_that_finishes_after_retirement_binds_the_tombstone(
        self,
        alice: PrincipalStore,
    ) -> None:
        """Recovery cannot erase ownership while the first Matrix send is in flight."""
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "old answer"},
            )
            is not None
        )
        send_started = asyncio.Event()
        finish_send = asyncio.Event()

        async def delayed_send(_claimed: MatrixDelivery) -> str:
            send_started.set()
            await finish_send.wait()
            return "$late-answer"

        live = MatrixDeliveryWorker(
            store=alice,
            send=delayed_send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE",
        )
        sending = asyncio.create_task(live.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL))
        await send_started.wait()
        await alice.fence_departure(_ROOM_ID, source=DepartureSource.LOCAL)
        await alice.note_membership_restarted(_ROOM_ID)

        recovery = MatrixDeliveryWorker(
            store=alice,
            send=AsyncMock(return_value="$duplicate"),
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE",
            resolve_delivered=AsyncMock(return_value=None),
        )
        assert await recovery.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL) is None

        finish_send.set()
        assert await sending == "$late-answer"
        retired = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert retired is not None
        assert retired.retired
        assert retired.acknowledged_event_id == "$late-answer"

    @pytest.mark.parametrize("accepted_before_crash", [True, False], ids=["accepted", "history-miss"])
    async def test_router_recovery_never_duplicates_an_unavailable_notice_after_device_change(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
        *,
        accepted_before_crash: bool,
    ) -> None:
        """Generic message recovery adopts the exact notice and treats a miss as uncertainty."""
        delivery_id = "approval-unavailable:approval-1"
        payload = {
            "msgtype": "m.notice",
            "body": "Requesting agent is unavailable.",
            "io.mindroom.approval_unavailable_id": "approval-1",
            "m.relates_to": {"m.in_reply_to": {"event_id": "$waiting"}},
        }
        await alice.enqueue_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload=payload,
        )
        claimed = await alice.claim_matrix_delivery(
            delivery_id=delivery_id,
            stage=DeliveryStage.FINAL,
            sending_device_id="OLD-DEVICE",
        )
        assert claimed is not None
        prior_notice = nio.Event.parse_event(
            {
                "event_id": "$notice-old-device",
                "room_id": _ROOM_ID,
                "sender": _AGENT_USER_ID,
                "origin_server_ts": 2_000,
                "type": "m.room.message",
                "content": dict(claimed.payload),
            },
        )
        assert isinstance(prior_notice, nio.Event)
        gateway = _gateway(tmp_path, alice, sending_device_id="NEW-DEVICE")
        gateway.deps.runtime.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id=_ROOM_ID,
                chunk=[prior_notice] if accepted_before_crash else [],
                start="start",
                end=None,
            ),
        )
        sent: list[MatrixDelivery] = []

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery)
            return "$duplicate"

        outcome = await gateway._response_delivery(send, handoff=None).recover()

        recovered = await alice.load_matrix_delivery(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
        assert recovered is not None
        if accepted_before_crash:
            assert sent == []
            assert outcome == RecoveryOutcome(recovered=1, failed=0)
            assert recovered.acknowledged_event_id == "$notice-old-device"
        else:
            assert len(sent) == 1
            assert outcome == RecoveryOutcome(recovered=1, failed=0)
            assert recovered.acknowledged_event_id == "$duplicate"

    async def test_source_less_delivery_is_adopted_by_its_frozen_content(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """A scheduled delivery has no reply source, but its exact marker is durable."""
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="scheduled-turn",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "scheduled notice"},
            )
            is not None
        )
        claimed = await alice.claim_matrix_delivery(
            delivery_id="scheduled-turn",
            stage=DeliveryStage.FINAL,
            sending_device_id="OLD-DEVICE",
        )
        assert claimed is not None
        prior = nio.Event.parse_event(
            {
                "event_id": "$prior",
                "room_id": _ROOM_ID,
                "sender": _AGENT_USER_ID,
                "origin_server_ts": 1_000,
                "type": "m.room.message",
                "content": dict(claimed.payload),
            },
        )
        assert isinstance(prior, nio.Event)
        gateway = _gateway(tmp_path, alice, sending_device_id="NEW-DEVICE")
        gateway.deps.runtime.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id=_ROOM_ID,
                chunk=[prior],
                start="start",
                end=None,
            ),
        )
        send = AsyncMock(return_value="$replacement")

        outcome = await gateway._response_delivery(send, handoff=None).recover()

        stored = await alice.load_matrix_delivery(delivery_id="scheduled-turn", stage=DeliveryStage.FINAL)
        assert outcome == RecoveryOutcome(recovered=1, failed=0)
        send.assert_not_awaited()
        assert stored is not None
        assert stored.acknowledged_event_id == "$prior"
        gateway.deps.runtime.client.room_messages.assert_awaited_once()

    async def test_final_marker_does_not_adopt_the_initial_placeholder(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The exact stage marker outranks a placeholder's shared logical identity."""
        assert (
            await alice.enqueue_matrix_delivery(
                delivery_id="$source",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "done"},
            )
            is not None
        )
        claimed = await alice.claim_matrix_delivery(
            delivery_id="$source",
            stage=DeliveryStage.FINAL,
            sending_device_id="OLD-DEVICE",
        )
        assert claimed is not None
        placeholder = nio.Event.parse_event(
            {
                "event_id": "$placeholder",
                "room_id": _ROOM_ID,
                "sender": _AGENT_USER_ID,
                "origin_server_ts": 1_000,
                "type": "m.room.message",
                "content": {
                    "msgtype": "m.text",
                    "body": "Thinking...",
                    "io.mindroom.delivery_id": {
                        "principal": "agent@alice",
                        "delivery_id": "$source",
                        "stage": "initial",
                    },
                },
            },
        )
        assert isinstance(placeholder, nio.Event)
        gateway = _gateway(tmp_path, alice, sending_device_id="NEW-DEVICE")
        gateway.deps.runtime.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id=_ROOM_ID,
                chunk=[placeholder],
                start="start",
                end=None,
            ),
        )
        send = AsyncMock(return_value="$replacement")

        outcome = await gateway._response_delivery(send, handoff=None).recover()

        stored = await alice.load_matrix_delivery(delivery_id="$source", stage=DeliveryStage.FINAL)
        assert outcome == RecoveryOutcome(recovered=1, failed=0)
        send.assert_awaited_once()
        assert stored is not None
        assert stored.acknowledged_event_id == "$replacement"

    async def test_permanent_refusal_is_not_returned_to_recovery(self, alice: PrincipalStore) -> None:
        """A definitive refusal is terminal state, not another failed recovery pass."""
        await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "frozen"},
        )
        send = AsyncMock(side_effect=PermanentDeliveryError("matrix event exceeds the hard size limit"))
        worker = MatrixDeliveryWorker(
            store=alice,
            send=send,
            sending_device_id="DEVICE",
        )

        first = await worker.recover()
        second = await worker.recover()

        stored = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert first == RecoveryOutcome(recovered=0, failed=0)
        assert second == RecoveryOutcome(recovered=0, failed=0)
        send.assert_awaited_once()
        assert stored is not None
        assert stored.permanent_failure_reason == "matrix event exceeds the hard size limit"


class TestTurnDeliverySerialization:
    """The gateway shares one turn-scoped delivery order without leaking the lock."""

    @staticmethod
    async def _enqueue(alice: PrincipalStore, stage: DeliveryStage) -> None:
        transaction_id = await alice.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=stage,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": stage.value},
        )
        assert transaction_id is not None

    async def test_gateway_delivery_instances_share_the_turn_lock(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Recovery and live delivery use distinct objects but cannot reorder one turn."""
        await self._enqueue(alice, DeliveryStage.INITIAL)
        gateway = _gateway(tmp_path, alice)
        initial_reached_matrix = asyncio.Event()
        accept_initial = asyncio.Event()
        accepted_stages: list[DeliveryStage] = []

        async def send(delivery: MatrixDelivery) -> str:
            if delivery.stage is DeliveryStage.INITIAL:
                initial_reached_matrix.set()
                await accept_initial.wait()
            accepted_stages.append(delivery.stage)
            return f"${delivery.stage.value}"

        recovery_delivery = gateway._response_delivery(send, handoff=None)
        live_delivery = gateway._response_delivery(send, handoff=ignore_final_delivery_handoff)
        assert recovery_delivery is not live_delivery
        recovery = asyncio.create_task(recovery_delivery.recover())
        await initial_reached_matrix.wait()
        final = asyncio.create_task(
            live_delivery.deliver(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "final"},
            ),
        )
        completed_before_initial_acceptance, _ = await asyncio.wait({final}, timeout=0.2)

        accept_initial.set()
        await recovery
        await final

        assert final not in completed_before_initial_acceptance
        assert accepted_stages == [DeliveryStage.INITIAL, DeliveryStage.FINAL]

    async def test_cancelled_initial_cannot_be_accepted_after_a_distinct_final(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Local cancellation cannot make a still-live Matrix request safe to overtake."""
        await self._enqueue(alice, DeliveryStage.INITIAL)
        gateway = _gateway(tmp_path, alice)
        initial_reached_matrix = asyncio.Event()
        accept_initial = asyncio.Event()
        accepted_stages: list[DeliveryStage] = []
        remote_requests: set[asyncio.Task[str]] = set()

        async def send(delivery: MatrixDelivery) -> str:
            if delivery.stage is DeliveryStage.FINAL:
                accepted_stages.append(delivery.stage)
                return "$final"

            async def remote_initial_request() -> str:
                initial_reached_matrix.set()
                await accept_initial.wait()
                accepted_stages.append(DeliveryStage.INITIAL)
                return "$initial"

            remote = asyncio.create_task(remote_initial_request())
            remote_requests.add(remote)
            return await asyncio.shield(remote)

        initial_delivery = gateway._response_delivery(send, handoff=None)
        final_delivery = gateway._response_delivery(send, handoff=None)
        initial = asyncio.create_task(initial_delivery.flush(delivery_id="turn-1", stage=DeliveryStage.INITIAL))
        await initial_reached_matrix.wait()
        initial.cancel()
        await asyncio.sleep(0)

        final = asyncio.create_task(
            final_delivery.deliver(
                delivery_id="turn-1",
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=None,
                payload={"msgtype": "m.text", "body": "final"},
            ),
        )
        completed_before_initial_acceptance, _ = await asyncio.wait({final}, timeout=0.1)
        accept_initial.set()

        with pytest.raises(asyncio.CancelledError):
            await initial
        await asyncio.gather(*remote_requests)
        await final

        assert final not in completed_before_initial_acceptance
        assert accepted_stages == [DeliveryStage.INITIAL, DeliveryStage.FINAL]

    async def test_cancelled_final_publishes_its_committed_terminal_before_propagating(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """An acknowledged FINAL cannot leave its terminal-record publication behind."""
        await self._enqueue(alice, DeliveryStage.FINAL)
        send_started = asyncio.Event()
        accept_final = asyncio.Event()
        published: list[tuple[str, str]] = []

        async def send(_delivery: MatrixDelivery) -> str:
            send_started.set()
            await accept_final.wait()
            return "$final"

        async def publish(turn_id: str, event_id: str) -> None:
            published.append((turn_id, event_id))

        gateway = _gateway(tmp_path, alice, terminal_turn_committed=publish)
        delivery = gateway._response_delivery(send, handoff=None)
        final = asyncio.create_task(delivery.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL))
        await send_started.wait()
        final.cancel()
        accept_final.set()

        with pytest.raises(asyncio.CancelledError):
            await final

        stored = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$final"
        assert published == [("turn-1", "$final")]

    async def test_cancelled_final_finishes_after_its_enqueue_commits(
        self,
    ) -> None:
        """Cancellation cannot strand a FINAL whose durable handoff already landed."""
        outbox = FakeOutbox()
        enqueue_committed = asyncio.Event()
        return_from_enqueue = asyncio.Event()
        original_enqueue = outbox.enqueue_matrix_delivery
        sent: list[DeliveryStage] = []

        async def enqueue_then_wait(
            *,
            delivery_id: str,
            stage: DeliveryStage,
            event_type: str = "m.room.message",
            room_id: str,
            thread_id: str | None,
            payload: Mapping[str, object],
            result: Mapping[str, object] | None = None,
            edits_event_id: str | None = None,
            settle_source_event_ids: tuple[str, ...] = (),
            permanent_failure_reason: str | None = None,
        ) -> str | None:
            transaction_id = await original_enqueue(
                delivery_id=delivery_id,
                stage=stage,
                event_type=event_type,
                room_id=room_id,
                thread_id=thread_id,
                payload=payload,
                result=result,
                edits_event_id=edits_event_id,
                settle_source_event_ids=settle_source_event_ids,
                permanent_failure_reason=permanent_failure_reason,
            )
            enqueue_committed.set()
            await return_from_enqueue.wait()
            return transaction_id

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery.stage)
            return "$final"

        delivery = MatrixDeliveryWorker(store=outbox, send=send, observe_delivered=ignore_delivered_projection)
        with patch.object(outbox, "enqueue_matrix_delivery", side_effect=enqueue_then_wait):
            final = asyncio.create_task(
                delivery.deliver(
                    delivery_id="turn-1",
                    stage=DeliveryStage.FINAL,
                    room_id=_ROOM_ID,
                    thread_id=None,
                    payload={"msgtype": "m.text", "body": "final"},
                ),
            )
            await enqueue_committed.wait()
            final.cancel()
            return_from_enqueue.set()

            with pytest.raises(asyncio.CancelledError):
                await final

        stored = await outbox.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$final"
        assert sent == [DeliveryStage.FINAL]
        assert await delivery.recover() == RecoveryOutcome(recovered=0, failed=0)

    async def test_cancelled_final_finishes_after_claiming_starts(
        self,
    ) -> None:
        """Cancellation after enqueue cannot leave recovery to overwrite a stop."""
        outbox = FakeOutbox()
        claim_started = asyncio.Event()
        finish_claim = asyncio.Event()
        original_claim = outbox.claim_matrix_delivery
        sent: list[DeliveryStage] = []

        async def claim_then_wait(
            *,
            delivery_id: str,
            stage: DeliveryStage,
            sending_device_id: str | None = None,
        ) -> MatrixDelivery | None:
            claim_started.set()
            await finish_claim.wait()
            return await original_claim(
                delivery_id=delivery_id,
                stage=stage,
                sending_device_id=sending_device_id,
            )

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery.stage)
            return "$final"

        delivery = MatrixDeliveryWorker(store=outbox, send=send, observe_delivered=ignore_delivered_projection)
        with patch.object(outbox, "claim_matrix_delivery", side_effect=claim_then_wait):
            final = asyncio.create_task(
                delivery.deliver(
                    delivery_id="turn-1",
                    stage=DeliveryStage.FINAL,
                    room_id=_ROOM_ID,
                    thread_id=None,
                    payload={"msgtype": "m.text", "body": "final"},
                ),
            )
            await claim_started.wait()
            final.cancel()
            finish_claim.set()

            with pytest.raises(asyncio.CancelledError):
                await final

        stored = await outbox.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == "$final"
        assert sent == [DeliveryStage.FINAL]
        assert await delivery.recover() == RecoveryOutcome(recovered=0, failed=0)

    async def test_terminal_callback_can_reenter_the_same_turn(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Publishing a committed FINAL runs after releasing its visible-delivery lock."""
        await self._enqueue(alice, DeliveryStage.FINAL)
        reentered: list[str | None] = []
        reentrant_delivery: MatrixDeliveryWorker | None = None

        async def publish_committed(_turn_id: str, _event_id: str) -> None:
            assert reentrant_delivery is not None
            reentered.append(await reentrant_delivery.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL))

        async def send(_delivery: MatrixDelivery) -> str:
            return "$answer"

        gateway = _gateway(tmp_path, alice, terminal_turn_committed=publish_committed)
        outer_delivery = gateway._response_delivery(send, handoff=None)
        reentrant_delivery = gateway._response_delivery(send, handoff=None)

        try:
            delivered = await asyncio.wait_for(
                outer_delivery.flush(delivery_id="turn-1", stage=DeliveryStage.FINAL),
                timeout=0.5,
            )
        except TimeoutError:
            pytest.fail("terminal callback deadlocked on the turn's visible-delivery lock")

        assert delivered == "$answer"
        assert reentered == ["$answer"]


class TestTheAcknowledgedRecordOutlivesAConcurrentMutation:
    """The record an acknowledgement commits has to be written, not just published.

    Every other terminal write goes through the ledger's own lock, which
    publishes to memory and enqueues the row while holding it. That pairing is
    the whole reason concurrent mutation is safe: writes reach the database in
    the order they reached memory, so whichever lands last derived from memory
    that already held the other's fact.

    The acknowledgement commits its record in the outbox's transaction instead,
    outside that lock. Telling memory afterwards and stopping there puts the
    acknowledgement outside the pairing: a mutation that derived its record
    before being told, and reaches the database after the transaction, writes
    over the row and takes the answer's event ID with it.

    A live turn survives that because it re-asserts the record durably right
    after delivery. Recovery does not: it acknowledges and returns, nothing
    reads the outbox's event back into a record, and the turn no longer names
    the message an edit would have to edit -- for good, across restarts.
    """

    @staticmethod
    async def _restarted_record(journal_store: EventJournalStore, source_event_id: str) -> TurnRecord | None:
        """Return the record as a restart reads it, from the database alone.

        The live map is dropped first on purpose. Asking the ledger that just
        wrote would answer from memory, which is the half of the state that
        was never in doubt.
        """
        _reset_handled_turn_ledger_runtime()
        restarted = await _store(journal_store, agent_name="agent")
        return restarted.get_turn_record(source_event_id)

    @pytest.mark.ledger_loads_from_disk
    async def test_a_recovered_answer_keeps_its_event_through_a_concurrent_redaction(
        self,
        tmp_path: Path,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """Both facts are durable, whichever of the two writers reaches the row last.

        The redaction is the one that actually happens: it arrives on a lane
        task of its own, it is not turn-backed so nothing defers it behind a
        live turn, and the recovery pass runs after every sync response.

        Asserting on the database rather than on the ledger is the point. Both
        orderings leave memory holding everything and only the stored row
        short, so a test that read the ledger would pass against the loss it
        was written to catch.
        """
        turn_store = await _store(journal_store, agent_name="agent")
        await turn_store.record_pending_turn(TurnRecord.create(["$source"], completed=False))
        gateway = _gateway(
            tmp_path,
            alice,
            terminal_turn_for=turn_store.terminal_turn_record,
            terminal_turn_committed=turn_store.publish_committed_response,
        )
        transaction_id = await alice.enqueue_matrix_delivery(
            delivery_id="$source",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "the answer"},
        )
        assert transaction_id is not None
        send_started = asyncio.Event()
        finish_send = asyncio.Event()

        async def send(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
            send_started.set()
            await finish_send.wait()
            return DeliveredMatrixEvent("$answer", {"body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(side_effect=send)):
            recovery = asyncio.create_task(gateway.recover_deliveries())
            await send_started.wait()
            # Started while the answer is on the wire, so it derives its record
            # from a memory the acknowledgement has not published into yet.
            redaction = asyncio.create_task(turn_store.mark_source_redacted("$source"))
            finish_send.set()
            outcome = await recovery
            await redaction

        assert outcome.recovered == 1
        stored = await self._restarted_record(journal_store, "$source")
        assert stored is not None
        assert stored.redacted_source_event_ids == ("$source",), "the redaction never reached the database"
        assert stored.response_event_id == "$answer", "the delivered answer lost the event it is stored under"
        assert stored.completed, "a delivered turn came back unfinished"
