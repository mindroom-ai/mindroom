"""The outbox is what makes one turn produce at most one visible answer.

A turn's final answer is durable before it is attempted and carries a
transaction ID derived from the turn, so a resend after a crash collapses onto
the event the homeserver already accepted rather than posting a second answer.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import reply_lifecycle as rl
from mindroom.bot import AgentBot
from mindroom.cancellation import current_task_is_process_shutdown, request_task_cancel
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, _LargeMessageStrategy
from mindroom.constants import (
    ACTING_REQUESTER_KEY,
    DURABLE_FINAL_OUTCOME_KEY,
    SILENT_SCHEDULE_NO_REPLY_TOKEN,
)
from mindroom.delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    DeliveryStage,
    FinalDeliveryRequest,
    ResponseIdentity,
    SendTextRequest,
    StreamingDeliveryRequest,
    _reply_body,
    _segment_transaction_id,
    _take_published,
)
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_journal import DepartureSource, EventClass, EventKind, InboundEvent
from mindroom.event_journal.sqlite_backend import SqliteBackend
from mindroom.handled_turns import TurnRecord, _reset_handled_turn_ledger_runtime
from mindroom.hooks.context import ResponseDraft
from mindroom.matrix.client_delivery import DeliveredMatrixEvent, MatrixDeliveryFailure, MatrixDeliveryFailureKind
from mindroom.matrix.large_messages import (
    _MATRIX_EVENT_HARD_LIMIT,
    _calculate_delivery_event_size,
    calculate_event_size,
)
from mindroom.matrix_delivery import MatrixDeliveryWorker, PermanentDeliveryError, RecoveryOutcome, TurnHandoff
from mindroom.message_target import MessageTarget
from mindroom.reply_presentation import NoteKind, Presentation, Segment, note_segment
from mindroom.reply_scope import ReplyRuntime
from mindroom.response_runner import ResponseRunner
from mindroom.response_sources import ResponseSources
from mindroom.runtime_shutdown import ORDERLY_SHUTDOWN
from mindroom.tool_system.events import ToolTraceEntry
from tests.conftest import (
    FakeOutbox,
    bind_runtime_paths,
    ignore_delivered_projection,
    ignore_final_delivery_handoff,
    make_outbox_mock,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.journal_helpers import admit_room_event
from tests.journal_membership_helpers import admit_room_membership
from tests.reply_span_helpers import final_in_resume_span, final_in_span, reply_span
from tests.test_turn_store import _store

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
    from pathlib import Path

    from mindroom.event_journal import (
        EventJournalStore,
        MatrixDelivery,
        MatrixDeliveryView,
        PrincipalStore,
        TerminalTurnWrite,
    )
    from mindroom.event_journal.backend import Transaction
    from mindroom.turn_store import TurnStore


async def _empty_stream() -> AsyncIterator[str]:
    """Return a stream with nothing in it; these tests never run one."""
    return
    yield ""


pytestmark = pytest.mark.asyncio

_ROOM_ID = "!room:localhost"
_AGENT_USER_ID = "@agent:localhost"
_SEGMENT_PAYLOADS_RESULT_KEY = "io.mindroom.matrix_segment_payloads"


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
        response_envelope=request_envelope(
            room_id=_ROOM_ID,
            reply_to_event_id=source_event_id,
            prompt="Test response request",
            agent_name="agent",
            source_kind=source_kind,
        ),
        correlation_id="c1",
        sources=ResponseSources(
            (
                request_envelope(
                    room_id=_ROOM_ID,
                    reply_to_event_id=source_event_id,
                    prompt="Test response request",
                    agent_name="agent",
                    source_kind=source_kind,
                ).source_event_id,
            ),
            (
                request_envelope(
                    room_id=_ROOM_ID,
                    reply_to_event_id=source_event_id,
                    prompt="Test response request",
                    agent_name="agent",
                    source_kind=source_kind,
                ).source_event_id,
            ),
        ),
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
    terminal_turn_committed: Callable[[str, str, TurnRecord | None], Awaitable[None]] | None = None,
    turn_handoff: TurnHandoff = ignore_final_delivery_handoff,
    large_message_strategy: _LargeMessageStrategy = "sidecar",
) -> DeliveryGateway:
    """Return a delivery gateway whose only real collaborator is the outbox."""
    config = bind_runtime_paths(
        Config(
            agents={"agent": AgentConfig(display_name="Agent")},
            defaults=DefaultsConfig(large_message_strategy=large_message_strategy),
        ),
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
            turn_handoff=turn_handoff,
            sending_device_id=lambda: sending_device_id,
            terminal_turn_for=terminal_turn_for,
            terminal_turn_committed=terminal_turn_committed,
        ),
    )


async def _turn_rows(principal: PrincipalStore, turn_id: str = "$cause") -> list[MatrixDelivery]:
    """Return the delivery rows recorded for one turn's reply."""
    rows = [
        await principal.load_matrix_delivery(delivery_id=turn_id, stage=stage)
        for stage in (DeliveryStage.INITIAL, DeliveryStage.FINAL)
    ]
    return [row for row in rows if row is not None]


async def _turn_row(principal: PrincipalStore, stage: DeliveryStage, turn_id: str = "$cause") -> MatrixDelivery:
    """Return one recorded row of a turn's reply."""
    row = await principal.load_matrix_delivery(delivery_id=turn_id, stage=stage)
    assert row is not None
    return row


def _response_recovery_bot(journal_store: EventJournalStore, turn_store: TurnStore) -> AgentBot:
    """Return the minimal real proof owner used by delivery integration tests."""
    bot = object.__new__(AgentBot)
    bot._journal_store = journal_store
    bot._journal_principal_id = "agent@alice"
    bot._turn_store = turn_store
    bot._response_recovery_diagnostic_classes = set()
    bot.logger = MagicMock()
    # No reply runs here, so a deletion ends none and nothing owes debt.
    bot._reply_runtime = ReplyRuntime(
        store=journal_store.principal("agent@alice"),
        entity_name="agent",
        generation="gen-1",
        retry_sources=lambda _room_id, _sources: None,
        complete_turn=AsyncMock(),
    )
    return bot


@pytest.mark.parametrize("kind", ["participation_decline", "mid_turn_defer"])
async def test_judgment_reaction_reuses_transaction_across_gateway_restarts(tmp_path: Path, kind: str) -> None:
    """Replaying an acknowledgement must use the same transaction, even if its configured emoji changes."""
    sent: list[dict[str, object]] = []

    async def send(**kwargs: object) -> nio.RoomSendResponse:
        sent.append(kwargs)
        return nio.RoomSendResponse.from_dict({"event_id": "$reaction"}, _ROOM_ID)

    for emoji in ("👍", "👀"):
        gateway = _gateway(tmp_path)
        gateway.deps.runtime.client.room_send = send
        await gateway.send_judgment_reaction(
            kind=kind,
            identity=_identity(),
            room_id=_ROOM_ID,
            event_id="$latest",
            key=emoji,
        )
    assert len(sent) == 2
    assert sent[0]["tx_id"] == sent[1]["tx_id"]
    assert sent[0]["message_type"] == "m.reaction"
    assert sent[0]["content"] == {
        "m.relates_to": {"rel_type": "m.annotation", "event_id": "$latest", "key": "👍"},
    }
    await gateway.send_judgment_reaction(
        kind=kind,
        identity=_identity(),
        room_id=_ROOM_ID,
        event_id="$another",
        key="👍",
    )
    assert sent[2]["tx_id"] != sent[0]["tx_id"]
    await gateway.send_judgment_reaction(
        kind="mid_turn_defer" if kind == "participation_decline" else "participation_decline",
        identity=_identity(),
        room_id=_ROOM_ID,
        event_id="$latest",
        key="👍",
    )
    assert sent[3]["tx_id"] != sent[0]["tx_id"]


@pytest.mark.parametrize("kind", ["participation_decline", "mid_turn_defer"])
async def test_judgment_reaction_respects_retired_membership(tmp_path: Path, kind: str) -> None:
    """An agent that lost this turn's room membership must not leave an acknowledgement."""
    outbox = FakeOutbox()
    outbox.ended_membership_turn_ids.add("$cause")
    gateway = _gateway(tmp_path, outbox=outbox)
    await gateway.send_judgment_reaction(kind=kind, identity=_identity(), room_id=_ROOM_ID, event_id="$cause", key="👍")
    gateway.deps.runtime.client.room_send.assert_not_awaited()


@pytest.mark.parametrize("failure", [RuntimeError("offline"), nio.RoomSendError("denied", "M_FORBIDDEN")])
@pytest.mark.parametrize("kind", ["participation_decline", "mid_turn_defer"])
async def test_judgment_reaction_delivery_failure_is_best_effort(tmp_path: Path, failure: object, kind: str) -> None:
    """Transport errors are logged without reopening a declined response."""
    gateway = _gateway(tmp_path)
    gateway.deps.runtime.client.room_send.side_effect = failure if isinstance(failure, Exception) else None
    gateway.deps.runtime.client.room_send.return_value = failure
    await gateway.send_judgment_reaction(kind=kind, identity=_identity(), room_id=_ROOM_ID, event_id="$cause", key="👍")
    gateway.deps.logger.warning.assert_called_once()


@pytest.mark.parametrize("kind", ["participation_decline", "mid_turn_defer"])
async def test_judgment_reaction_preserves_cancellation(tmp_path: Path, kind: str) -> None:
    """Stopping a reaction send must still cancel its owning response turn."""
    gateway = _gateway(tmp_path)
    gateway.deps.runtime.client.room_send.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await gateway.send_judgment_reaction(
            kind=kind,
            identity=_identity(),
            room_id=_ROOM_ID,
            event_id="$cause",
            key="👍",
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
        alice: PrincipalStore,
        text: str,
    ) -> None:
        """Silent whitespace settles as suppressed without creating a Matrix event."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": text}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await final_in_span(
                gateway,
                alice,
                self._final_request(text, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert outcome.event_id is None
        assert outcome.failure_reason == "silent_no_report"
        assert not await _turn_rows(alice)
        send.assert_not_awaited()

    async def test_silent_schedule_writes_machine_readable_run_receipt(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Removing the workspace receipt must make an evidence-free silent completion fail this test."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        request = replace(
            self._final_request(SILENT_SCHEDULE_NO_REPLY_TOKEN, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            identity=ResponseIdentity(
                response_kind="agent",
                response_envelope=request_envelope(
                    room_id=_ROOM_ID,
                    reply_to_event_id="$cause",
                    prompt="Check the inbox",
                    agent_name="agent",
                    source_kind=SILENT_SCHEDULE_SOURCE_KIND,
                ),
                correlation_id="c1",
                sources=ResponseSources(
                    (
                        request_envelope(
                            room_id=_ROOM_ID,
                            reply_to_event_id="$cause",
                            prompt="Check the inbox",
                            agent_name="agent",
                            source_kind=SILENT_SCHEDULE_SOURCE_KIND,
                        ).source_event_id,
                    ),
                    (
                        request_envelope(
                            room_id=_ROOM_ID,
                            reply_to_event_id="$cause",
                            prompt="Check the inbox",
                            agent_name="agent",
                            source_kind=SILENT_SCHEDULE_SOURCE_KIND,
                        ).source_event_id,
                    ),
                ),
            ),
        )

        outcome = await final_in_span(gateway, alice, request)

        assert outcome.failure_reason == "silent_no_report"
        receipt_path = (
            tmp_path
            / "mindroom_data"
            / "agents"
            / "agent"
            / "workspace"
            / ".mindroom"
            / "scheduled_runs"
            / "d70fd85d0319a4c275c5df743feff6424bb8b85982e28a97e317285e7c441830.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt == {
            "agent_name": "agent",
            "completed_at": receipt["completed_at"],
            "entity_name": "agent",
            "prompt": "Check the inbox",
            "result": "no_report",
            "response_text": SILENT_SCHEDULE_NO_REPLY_TOKEN,
            "room_id": _ROOM_ID,
            "schema_version": 1,
            "source_event_id": "$cause",
            "started_at": receipt["started_at"],
            "status": "completed",
            "thread_id": None,
        }
        assert receipt["completed_at"].endswith("Z")
        assert receipt["started_at"].endswith("Z")

    async def test_silent_schedule_receipt_uses_original_envelope_after_hooks(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """A hook may transform presentation fields but cannot redirect durable run identity."""
        gateway = _gateway(tmp_path, alice)
        hook_service = self._hooks()

        async def replace_envelope(**kwargs: object) -> object:
            draft = await hook_service._apply_before_response(**kwargs)  # type: ignore[arg-type]
            draft.envelope = replace(draft.envelope, source_event_id="$hook-replaced")
            return draft

        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=replace_envelope)

        outcome = await final_in_span(
            gateway,
            alice,
            self._final_request(SILENT_SCHEDULE_NO_REPLY_TOKEN, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
        )

        assert outcome.failure_reason == "silent_no_report"
        receipt_directory = (
            tmp_path / "mindroom_data" / "agents" / "agent" / "workspace" / ".mindroom" / "scheduled_runs"
        )
        receipts = list(receipt_directory.glob("*.json"))
        assert len(receipts) == 1
        assert json.loads(receipts[0].read_text(encoding="utf-8"))["source_event_id"] == "$cause"

    @pytest.mark.parametrize(
        "invalid_update",
        [None, {"result": []}, {"schema_version": True}],
    )
    async def test_silent_schedule_completion_repairs_malformed_receipt(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
        invalid_update: dict[str, object] | None,
    ) -> None:
        """A damaged start receipt cannot prevent the final machine-readable record."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        receipt_path = (
            tmp_path
            / "mindroom_data"
            / "agents"
            / "agent"
            / "workspace"
            / ".mindroom"
            / "scheduled_runs"
            / "d70fd85d0319a4c275c5df743feff6424bb8b85982e28a97e317285e7c441830.json"
        )
        receipt_path.parent.mkdir(parents=True)
        invalid_receipt: dict[str, object] = {
            "agent_name": "agent",
            "completed_at": "2026-08-24T12:01:00Z",
            "entity_name": "agent",
            "prompt": "Test response request",
            "result": "reported",
            "response_text": "stale",
            "room_id": _ROOM_ID,
            "schema_version": 1,
            "source_event_id": "$cause",
            "started_at": "2026-08-24T12:00:00Z",
            "status": "completed",
            "thread_id": None,
        }
        if invalid_update is None:
            receipt_path.write_text("not json", encoding="utf-8")
        else:
            invalid_receipt.update(invalid_update)
            receipt_path.write_text(json.dumps(invalid_receipt), encoding="utf-8")

        outcome = await final_in_span(
            gateway,
            alice,
            self._final_request(SILENT_SCHEDULE_NO_REPLY_TOKEN, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
        )

        assert outcome.failure_reason == "silent_no_report"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["source_event_id"] == "$cause"
        assert receipt["status"] == "completed"

    async def test_silent_schedule_receipt_rejects_symlinked_metadata_directory(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Agent-controlled workspace symlinks cannot redirect a host receipt write."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        workspace = tmp_path / "mindroom_data" / "agents" / "agent" / "workspace"
        outside = tmp_path / "outside"
        workspace.mkdir(parents=True)
        outside.mkdir()
        (workspace / ".mindroom").symlink_to(outside, target_is_directory=True)

        with pytest.raises(OSError, match=r"Too many levels|Not a directory"):
            await final_in_span(
                gateway,
                alice,
                self._final_request(SILENT_SCHEDULE_NO_REPLY_TOKEN, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert list(outside.iterdir()) == []

    async def test_silent_schedule_tool_trace_no_reply_is_suppressed(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Display-only tool markers must not turn a silent no-report result into a visible response."""
        gateway = _gateway(tmp_path, alice)
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
            outcome = await final_in_span(gateway, alice, request)

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert outcome.event_id is None
        assert outcome.failure_reason == "silent_no_report"
        assert not await _turn_rows(alice)
        send.assert_not_awaited()

    async def test_silent_schedule_unmatched_tool_marker_no_reply_remains_visible(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Marker-shaped findings must not be stripped when they do not match the trace."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        text = f"🔧 `reported_finding` [1]\n\n{SILENT_SCHEDULE_NO_REPLY_TOKEN}"
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": text}))
        request = replace(
            self._final_request(text, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
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
            outcome = await final_in_span(gateway, alice, request)

        assert outcome.terminal_status == "completed"
        assert outcome.suppressed is False
        assert outcome.event_id == "$sent"
        send.assert_awaited_once()

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
        alice: PrincipalStore,
        text: str,
        source_kind: str,
    ) -> None:
        """Automatic suppression never swallows findings or ordinary responses."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": text}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await final_in_span(gateway, alice, self._final_request(text, source_kind=source_kind))

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$sent"
        assert outcome.suppressed is False
        send.assert_awaited_once()

    @pytest.mark.parametrize("generated_text", ["", SILENT_SCHEDULE_NO_REPLY_TOKEN])
    async def test_silent_schedule_hook_finding_delivers_after_no_report_generation(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
        generated_text: str,
    ) -> None:
        """A before-response hook can turn a silent completion into a visible finding."""
        gateway = _gateway(tmp_path, alice)
        hooks = self._hooks()

        async def add_finding(**kwargs: object) -> ResponseDraft:
            draft = await hooks._apply_before_response(**kwargs)
            draft.response_text = "Finding from hook"
            return draft

        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=add_finding)
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "Finding from hook"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await final_in_span(
                gateway,
                alice,
                self._final_request(generated_text, source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert outcome.terminal_status == "completed"
        assert outcome.event_id == "$sent"
        assert send.await_args.args[2]["body"] == "Finding from hook"

    async def test_final_reply_names_its_human_requester(self, tmp_path: Path, alice: PrincipalStore) -> None:
        """Entities the reply mentions act for the human the reply was written for."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "answer"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            await final_in_span(gateway, alice, self._final_request("answer"))

        assert send.await_args.args[2][ACTING_REQUESTER_KEY] == "@user:localhost"

    async def test_final_reply_names_its_bot_account_requester(self, tmp_path: Path, alice: PrincipalStore) -> None:
        """Entities the reply mentions apply their access to a configured bot account, as they would to a human."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.runtime.config.bot_accounts = ["@user:localhost"]
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "answer"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            await final_in_span(gateway, alice, self._final_request("answer"))

        assert send.await_args.args[2][ACTING_REQUESTER_KEY] == "@user:localhost"

    async def test_final_reply_for_an_entity_requester_names_no_requester(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """A reply to an agent or system requester leaves mentioned entities acting as today."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        agent_id = entity_identity_registry(gateway.deps.runtime.config, gateway.deps.runtime_paths).current_id("agent")
        request = self._final_request("answer")
        request = replace(
            request,
            identity=replace(
                request.identity,
                response_envelope=request_envelope(
                    room_id=_ROOM_ID,
                    reply_to_event_id="$cause",
                    agent_name="agent",
                    user_id=agent_id.full_id,
                ),
            ),
        )
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "answer"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            await final_in_span(gateway, alice, request)

        assert ACTING_REQUESTER_KEY not in send.await_args.args[2]

    async def test_silent_schedule_hook_can_replace_a_finding_with_no_reply(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The no-report acknowledgment is interpreted after before-response hooks."""
        gateway = _gateway(tmp_path, alice)
        hooks = self._hooks()

        async def replace_with_no_reply(**kwargs: object) -> ResponseDraft:
            draft = await hooks._apply_before_response(**kwargs)
            draft.response_text = SILENT_SCHEDULE_NO_REPLY_TOKEN
            return draft

        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=replace_with_no_reply)
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "Finding"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await final_in_span(
                gateway,
                alice,
                self._final_request("Finding", source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert outcome.event_id is None
        assert not await _turn_rows(alice)
        send.assert_not_awaited()

    async def test_explicit_hook_suppression_wins_for_silent_schedule_finding(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Explicit suppression remains authoritative even when a hook adds visible text."""
        gateway = _gateway(tmp_path, alice)
        hooks = self._hooks()

        async def add_suppressed_finding(**kwargs: object) -> ResponseDraft:
            draft = await hooks._apply_before_response(**kwargs)
            draft.response_text = "Finding from hook"
            draft.suppress = True
            return draft

        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=add_suppressed_finding)
        send = AsyncMock(return_value=DeliveredMatrixEvent("$sent", {"body": "Finding from hook"}))

        with patch("mindroom.delivery_gateway.send_message_outcome", send):
            outcome = await final_in_span(
                gateway,
                alice,
                self._final_request("", source_kind=SILENT_SCHEDULE_SOURCE_KIND),
            )

        assert outcome.terminal_status == "cancelled"
        assert outcome.suppressed is True
        assert outcome.failure_reason == "suppressed_by_hook"
        send.assert_not_awaited()

    async def test_ordinary_before_hook_failure_keeps_existing_eventless_behavior(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The silent-source repair must not change ordinary interactive delivery."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = AsyncMock(side_effect=RuntimeError("hook failed"))
        send_text = AsyncMock(return_value="$unexpected")

        with patch.object(DeliveryGateway, "send_text", new=send_text):
            outcome = await final_in_span(gateway, alice, self._final_request("answer"))

        assert outcome.terminal_status == "error"
        assert outcome.event_id is None
        send_text.assert_not_awaited()

    async def test_a_final_answer_is_enqueued_before_it_is_sent(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The row must exist before the network call, keyed on the causing event.

        That ordering is the whole point: a crash after Matrix accepted the
        message leaves a row recovery can find, and the turn that caused it is
        the only name for it that survives a restart.
        """
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await final_in_span(gateway, alice, self._final_request("answer"))

        assert outcome.event_id == "$sent"
        assert [(row.delivery_id, row.stage.value) for row in await _turn_rows(alice)] == [("$cause", "final")]
        assert (await _turn_row(alice, DeliveryStage.FINAL)).acknowledged_event_id == "$sent"
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

    async def test_interactive_prompt_is_frozen_in_the_terminal_matrix_payload(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Projection ownership requires prompt metadata to cross Matrix with the answer."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "Choose"})
        response = """```interactive
{"question":"Pick","options":[{"emoji":"✅","label":"Yes","value":"yes"}]}
```"""

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await final_in_span(gateway, alice, self._final_request(response))

        assert outcome.event_id == "$sent"
        assert (await _turn_row(alice, DeliveryStage.FINAL)).payload["io.mindroom.interactive"] == {
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

    async def test_an_unreadable_delivered_event_stays_unacknowledged_for_recovery(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The outbox must retry rather than invent projection ordering metadata."""
        gateway = _gateway(tmp_path, alice)
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
            await final_in_span(gateway, alice, self._final_request(response))

        stored = await _turn_row(alice, DeliveryStage.FINAL)
        assert stored.attempted
        assert stored.acknowledged_event_id is None

    async def test_a_redacted_delivery_acknowledges_without_resurrecting_its_content(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Server redaction wins over the frozen plaintext payload."""
        gateway = _gateway(tmp_path, alice)
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
            outcome = await final_in_span(gateway, alice, self._final_request(interactive_text))

        assert outcome.event_id == "$sent"

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

    async def test_the_final_answer_is_durable_even_when_it_is_an_edit(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Once a placeholder exists the answer arrives as an edit of it.

        That is the normal path, not a corner: every turn that shows
        "Thinking..." reaches its answer this way. An edit sent outside the
        outbox leaves nothing to recover, so a crash between generating the
        answer and editing it in leaves the user reading the placeholder for
        good.
        """
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=edited)) as edit:
            outcome = await final_in_span(
                gateway,
                alice,
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )

        assert outcome.event_id == "$placeholder"
        assert [(row.delivery_id, row.stage.value) for row in await _turn_rows(alice)] == [
            ("$cause", "initial"),
            ("$cause", "final"),
        ]
        assert (await _turn_row(alice, DeliveryStage.FINAL)).edits_event_id == "$placeholder"
        assert edit.await_args.kwargs["transaction_id"] == (await _turn_row(alice, DeliveryStage.FINAL)).transaction_id
        assert edit.await_args.kwargs["operation"] == "edit_message"
        # The stored payload is the finished replace event, because recovery
        # sends the row verbatim and cannot rebuild an envelope.
        stored = (await _turn_row(alice, DeliveryStage.FINAL)).payload
        assert stored["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$placeholder"}
        assert stored["m.new_content"]["body"] == "the answer"
        # Both layers are frozen, and the outer one is the only text a client
        # that does not understand m.replace ever renders. Recovery resends
        # this row byte for byte, so an outer body still reading "Thinking..."
        # would be permanent for those clients, not a one-attempt glitch.
        assert stored["body"] == "* the answer"

    async def test_an_approved_runs_final_edit_freezes_its_interactive_question(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Approval recovery must restore the interactive registration facts of an approved run's answer."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = DeliveredMatrixEvent("$placeholder", {"msgtype": "m.text", "body": "Choose"})
        interactive_text = (
            'Choose one.\n```interactive\n{"question":"Pick",'
            '"options":[{"emoji":"✅","label":"Yes","value":"yes"}]}\n```'
        )

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=edited)):
            await final_in_resume_span(
                gateway,
                alice,
                replace(
                    self._final_request(interactive_text),
                    existing_event_id="$placeholder",
                ),
            )

        delivery = await _turn_row(alice, DeliveryStage.FINAL)
        frozen = delivery.payload
        new_content = frozen["m.new_content"]
        prompt = new_content["io.mindroom.interactive"]
        assert prompt["question_text"] == "Pick"
        assert prompt["options"] == {"1": "yes", "✅": "yes"}
        # The reply's records hold the body and its success; the result keeps only the question's registration facts.
        assert DURABLE_FINAL_OUTCOME_KEY not in new_content
        assert delivery.result is not None
        assert set(delivery.result) == {"interactive"}
        assert delivery.result["interactive"]["question_text"] == "Pick"
        assert delivery.result["interactive"]["option_map"] == {"1": "yes", "✅": "yes"}

    async def test_large_final_edit_freezes_a_sendable_payload(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """A recoverable final edit must not leave an impossible outbox retry."""
        gateway = _gateway(tmp_path, alice, large_message_strategy="split")
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

        outcome = await final_in_span(
            gateway,
            alice,
            replace(
                self._final_request(answer),
                existing_event_id="$placeholder",
            ),
        )

        assert outcome.terminal_status == "completed"
        delivery = await _turn_row(alice, DeliveryStage.FINAL)
        frozen = delivery.payload
        assert calculate_event_size(frozen) <= _MATRIX_EVENT_HARD_LIMIT
        assert delivery.result is not None
        continuations = delivery.result[_SEGMENT_PAYLOADS_RESULT_KEY]
        assert isinstance(continuations, list)
        assert continuations
        parts = [frozen["m.new_content"], *continuations]
        assert "".join(part["body"] for part in parts) == answer
        assert all(part["format"] == "org.matrix.custom.html" for part in parts)
        assert all("formatted_body" in part for part in parts)
        assert all("file" not in part and "url" not in part for part in parts)
        sent_contents = [call.kwargs["content"] for call in client.room_send.await_args_list]
        assert sent_contents == [frozen, *continuations]

    async def test_sidecar_strategy_keeps_oversized_final_on_the_attachment_path(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The default strategy uploads one sidecar instead of segmenting the answer."""
        gateway = _gateway(tmp_path, alice)
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
            nio.UploadResponse.from_dict({"content_uri": "mxc://localhost/sidecar-strategy"}),
            None,
        )
        client.room_send.return_value = nio.RoomSendResponse(event_id="$sent", room_id=_ROOM_ID)
        gateway.deps.runtime.client = client

        outcome = await final_in_span(
            gateway,
            alice,
            self._final_request("x" * 100_000),
        )

        assert outcome.terminal_status == "completed"
        delivery = await _turn_row(alice, DeliveryStage.FINAL)
        frozen = delivery.payload
        assert frozen["msgtype"] == "m.file"
        assert calculate_event_size(frozen) <= _MATRIX_EVENT_HARD_LIMIT
        assert client.upload.await_count == 1
        assert client.room_send.await_count == 1
        assert delivery.result is None

    async def test_recovery_from_a_new_device_sends_only_the_missing_continuations(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Adopting the primary event must not strand or duplicate continuations.

        The earlier device crashed between the primary send and the
        continuations, and one continuation did land. Transaction IDs are
        scoped to the dead device, so the replacement device cannot resend
        blindly: each segment is matched by its exact frozen content and only
        the missing one goes out, under its stable derived transaction ID.
        """
        gateway = _gateway(tmp_path, alice, large_message_strategy="split")
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        answer = "\n\n".join(f"## Part {index}\n\n" + "x" * 500 for index in range(200))

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            await final_in_span(gateway, alice, self._final_request(answer))
        row = await _turn_row(alice, DeliveryStage.FINAL)
        assert row.acknowledged_event_id is None
        assert row.result is not None
        continuations = row.result[_SEGMENT_PAYLOADS_RESULT_KEY]
        assert isinstance(continuations, list)
        assert len(continuations) >= 2

        async def found_events(
            _client: object,
            _room_id: str,
            *,
            delivery_content: Mapping[str, object],
            **_kwargs: object,
        ) -> str | None:
            if delivery_content == row.payload:
                return "$primary"
            return None

        missing_indices = [index for index in range(len(continuations)) if index != 1]
        recovered_gateway = _gateway(tmp_path, alice, sending_device_id="NEW-DEVICE", large_message_strategy="split")
        delivered = DeliveredMatrixEvent("$continuation", {})
        with (
            patch(
                "mindroom.delivery_gateway.find_outbox_delivery_event_id_via_room_messages",
                AsyncMock(side_effect=found_events),
            ),
            patch(
                "mindroom.delivery_gateway.missing_outbox_delivery_copy_indices_via_room_messages",
                AsyncMock(return_value=missing_indices),
            ),
            patch(
                "mindroom.delivery_gateway.send_message_outcome",
                AsyncMock(return_value=delivered),
            ) as send,
        ):
            recovered = await recovered_gateway.recover_deliveries()

        assert recovered.recovered == 1
        assert [call.args[2] for call in send.await_args_list] == [continuations[i] for i in missing_indices]
        assert [call.kwargs["transaction_id"] for call in send.await_args_list] == [
            _segment_transaction_id(row.transaction_id, index + 1) for index in missing_indices
        ]
        assert (await _turn_row(alice, DeliveryStage.FINAL)).acknowledged_event_id == "$primary"

    async def test_delivery_identity_is_included_in_the_validated_event_size(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The exact persisted and sent event must fit after identity is attached."""
        gateway = _gateway(tmp_path, alice)
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
            extra_content={"io.mindroom.test_metadata": "m" * 10_500},
        )

        outcome = await final_in_span(gateway, alice, request)

        assert outcome.terminal_status == "completed"
        frozen = (await _turn_row(alice, DeliveryStage.FINAL)).payload
        assert calculate_event_size(frozen) <= _MATRIX_EVENT_HARD_LIMIT
        assert client.room_send.await_args.kwargs["content"] == frozen

    async def test_uncached_encrypted_room_is_fitted_before_durable_enqueue(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Remote encryption state must shape the payload before the outbox freezes it."""
        gateway = _gateway(tmp_path, alice, large_message_strategy="split")
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
            outcome = await final_in_span(gateway, alice, self._final_request("x" * 50_000))

        assert outcome.event_id == "$sent"
        delivery = await _turn_row(alice, DeliveryStage.FINAL)
        frozen = delivery.payload
        assert frozen["msgtype"] == "m.text"
        assert delivery.result is not None
        continuations = delivery.result[_SEGMENT_PAYLOADS_RESULT_KEY]
        assert isinstance(continuations, list)
        assert continuations
        assert all(
            _calculate_delivery_event_size(
                candidate,
                room_id=_ROOM_ID,
                room_encrypted=True,
                device_id="DEVICE",
            )
            <= _MATRIX_EVENT_HARD_LIMIT
            for candidate in [frozen, *continuations]
        )
        client.upload.assert_not_awaited()
        client.room_get_state_event.assert_awaited_once_with(_ROOM_ID, "m.room.encryption")

    async def test_plaintext_durable_payload_is_fitted_for_a_later_encrypted_send(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Persistence must freeze bytes that remain valid if encryption is enabled."""
        gateway = _gateway(tmp_path, alice, large_message_strategy="split")
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

        outcome = await final_in_span(
            gateway,
            alice,
            replace(
                self._final_request("x" * 100_000),
            ),
        )

        assert outcome.terminal_status == "completed"
        delivery = await _turn_row(alice, DeliveryStage.FINAL)
        frozen = delivery.payload
        assert frozen["msgtype"] == "m.text"
        assert "file" not in frozen
        assert "url" not in frozen
        assert delivery.result is not None
        continuations = delivery.result[_SEGMENT_PAYLOADS_RESULT_KEY]
        assert isinstance(continuations, list)
        assert continuations
        assert all(
            _calculate_delivery_event_size(
                candidate,
                room_id=_ROOM_ID,
                room_encrypted=True,
                device_id="DEVICE",
            )
            <= _MATRIX_EVENT_HARD_LIMIT
            for candidate in [frozen, *continuations]
        )
        client.upload.assert_not_awaited()

    async def test_unknown_uncached_room_encryption_fails_before_durable_enqueue(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """An unknown encryption state must not leave an ambiguously sized outbox row."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        client = AsyncMock(spec=nio.AsyncClient)
        client.rooms = {}
        client.olm = MagicMock()
        encryption_error = MagicMock(spec=nio.RoomGetStateEventError)
        encryption_error.status_code = "M_FORBIDDEN"
        client.room_get_state_event.return_value = encryption_error
        gateway.deps.runtime.client = client

        outcome = await final_in_span(gateway, alice, self._final_request("answer"))

        assert outcome.terminal_status == "error"
        assert await _turn_rows(alice) == []
        client.upload.assert_not_awaited()
        client.room_send.assert_not_awaited()

    @pytest.mark.parametrize("existing_event_id", [None, "$placeholder"], ids=("send", "edit"))
    async def test_an_unrepresentable_final_is_recorded_without_a_send(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
        existing_event_id: str | None,
    ) -> None:
        """Irreducible metadata becomes durable terminal state without network I/O."""
        gateway = _gateway(tmp_path, alice)
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
            extra_content={"io.mindroom.required_metadata": "m" * 70_000},
        )

        await final_in_span(gateway, alice, request)

        failed = await _turn_row(alice, DeliveryStage.FINAL)
        assert failed.permanently_failed
        assert not failed.attempted
        assert failed.edits_event_id == existing_event_id
        client.upload.assert_not_awaited()
        client.room_send.assert_not_awaited()

    async def test_the_stream_is_given_both_terminal_paths(self, tmp_path: Path, alice: PrincipalStore) -> None:
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
            async with reply_span(alice, source_event_id="$cause", room_id=request.target.room_id):
                await gateway.deliver_stream(request)

        assert stream.await_args.kwargs["terminal_edit"] is not None
        assert stream.await_args.kwargs["terminal_send"] is not None

    async def test_streamed_reply_names_its_human_requester(self, tmp_path: Path, alice: PrincipalStore) -> None:
        """A streamed reply carries its human requester like a sent one, without freezing the caller's metadata."""
        gateway = _gateway(tmp_path, FakeOutbox())
        run_metadata: dict[str, object] = {}
        request = StreamingDeliveryRequest(
            target=MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True),
            response_stream=_empty_stream(),
            identity=_identity(),
            extra_content=run_metadata,
        )

        with patch("mindroom.delivery_gateway.send_streaming_response", AsyncMock()) as stream:
            async with reply_span(alice, source_event_id="$cause", room_id=request.target.room_id):
                await gateway.deliver_stream(request)
        run_metadata["io.mindroom.ai_run"] = {"model": "late"}

        assert dict(stream.await_args.kwargs["extra_content"]) == {
            "io.mindroom.ai_run": {"model": "late"},
            ACTING_REQUESTER_KEY: "@user:localhost",
        }
        assert ACTING_REQUESTER_KEY not in run_metadata

    async def test_recovery_replays_a_final_edit_as_an_edit(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """A crash between claiming and acknowledging must not add a message.

        Recovery has no request to rebuild from; it sends the row as frozen.
        If what was frozen were the new body rather than the finished replace
        event, the recovered answer would arrive as a second ordinary message
        with the placeholder still above it -- two visible messages for one
        turn, which is the thing the outbox exists to prevent.
        """
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        edited = DeliveredMatrixEvent("$placeholder", {"body": "the answer"})

        # A delivery that reached Matrix but whose acknowledgement was lost.
        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            await final_in_span(
                gateway,
                alice,
                replace(self._final_request("the answer"), existing_event_id="$placeholder"),
            )
        assert (await _turn_row(alice, DeliveryStage.FINAL)).acknowledged_event_id is None

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=edited)) as send:
            recovered = await gateway.recover_deliveries()

        assert recovered.recovered == 1
        sent = send.await_args.args[2]
        assert sent["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$placeholder"}, (
            "recovery sent a new message instead of replaying the edit"
        )
        assert send.await_args.kwargs["transaction_id"] == (await _turn_row(alice, DeliveryStage.FINAL)).transaction_id

    async def test_a_pass_that_could_not_send_reports_the_debt_it_left(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """A recovery pass that failed is not a recovery pass that finished.

        The caller schedules the next attempt on this, so a pass reporting
        success while leaving an answer unsent would strand it until the
        process restarted.
        """
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = self._hooks()._apply_before_response
        answer = DeliveredMatrixEvent("$answer", {"body": "the answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            await final_in_span(gateway, alice, self._final_request("the answer"))
        assert (await _turn_row(alice, DeliveryStage.FINAL)).acknowledged_event_id is None

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=_failed_delivery())):
            failed_pass = await gateway.recover_deliveries()

        assert failed_pass.failed == 1
        assert not failed_pass.complete

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=answer)):
            retried = await gateway.recover_deliveries()

        assert retried.recovered == 1
        assert retried.complete
        assert (await _turn_row(alice, DeliveryStage.FINAL)).acknowledged_event_id == "$answer"


class TestAnEndedMembershipStopsTheAnswer:
    """A turn that outlived its membership must not reach the room it left."""

    @staticmethod
    def _target() -> MessageTarget:
        """Return the room-mode target these tests deliver into."""
        return MessageTarget.resolve(_ROOM_ID, None, None, room_mode=True)

    async def test_a_turn_fenced_mid_flight_produces_no_visible_answer(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The fence deleted this conversation; the answer must not rebuild it."""
        gateway = _gateway(tmp_path, alice)
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)) as send:
            async with reply_span(alice, source_event_id="$cause", room_id=_ROOM_ID):
                await admit_room_membership(alice, _ROOM_ID, "leave")
                outcome = await gateway.deliver_final(TestTurnDeliveryGoesThroughTheOutbox._final_request("answer"))

        send.assert_not_awaited()
        assert outcome.event_id is None
        assert outcome.terminal_status == "error"
        assert await _turn_rows(alice) == []

    async def test_a_stream_is_given_the_gate_that_stops_its_progressive_edits(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
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
            async with reply_span(alice, source_event_id="$cause", room_id=request.target.room_id):
                await gateway.deliver_stream(request)

        gate = stream.await_args.kwargs["transport_is_current"]
        assert gate is not None
        assert not await gate()

    async def test_a_stream_under_a_live_membership_keeps_its_gate_open(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """The ordinary case must still be allowed to stream."""
        gateway = _gateway(tmp_path, FakeOutbox())
        request = StreamingDeliveryRequest(
            target=self._target(),
            response_stream=_empty_stream(),
            identity=_identity(),
        )

        with patch("mindroom.delivery_gateway.send_streaming_response", AsyncMock()) as stream:
            async with reply_span(alice, source_event_id="$cause", room_id=request.target.room_id):
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

    @staticmethod
    async def _send_final(gateway: DeliveryGateway, text: str) -> str | None:
        """Send one answer that no reply owns, as a command's or a rejection's answer is sent."""
        return await gateway.send_text(
            SendTextRequest(
                target=MessageTarget.resolve(_ROOM_ID, None, "$cause", room_mode=True),
                response_text=text,
                delivery_turn_id="$cause",
            ),
        )

    async def test_a_final_acknowledgement_carries_the_bound_record(
        self,
        tmp_path: Path,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """The record travelling with the acknowledgement names the delivered event."""
        pending = TurnRecord.create(["$cause"], completed=False, response_owner="agent")

        def bind(turn_id: str, event_id: str) -> TurnRecord | None:
            assert turn_id == "$cause"
            return replace(pending, response_event_id=event_id, completed=True)

        gateway = _gateway(tmp_path, alice, terminal_turn_for=bind)
        await admit_room_event(alice, _ROOM_ID, "$cause")
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            assert await self._send_final(gateway, "answer") == "$sent"

        ((index_event_ids, anchor_event_id, record_json),) = await journal_store.turn_records("agent").load_all()
        assert index_event_ids == "$cause"
        assert anchor_event_id == "$cause"
        record = json.loads(record_json)
        assert record["response_event_id"] == "$sent"
        assert record["completed"] is True

    async def test_a_replys_acknowledgement_binds_no_turn_record(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """A reply's records own its answer, so its acknowledgement commits no turn record beside it."""
        bind = MagicMock(return_value=None)
        gateway = _gateway(tmp_path, alice, terminal_turn_for=bind)
        gateway.deps.response_hooks._apply_before_response = (
            TestTurnDeliveryGoesThroughTheOutbox._hooks()._apply_before_response
        )
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            outcome = await final_in_span(gateway, alice, self._final_request("answer"))

        assert outcome.event_id == "$sent"
        bind.assert_not_called()

    async def test_nothing_is_carried_when_there_is_no_record_to_bind(
        self,
        tmp_path: Path,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A turn with no record, or one that already names its answer, binds nothing.

        The acknowledgement still has to happen -- the answer is in the room
        either way -- so the delivery must not be held up by having nothing to
        write beside it.
        """
        gateway = _gateway(tmp_path, alice, terminal_turn_for=lambda _turn_id, _event_id: None)
        await admit_room_event(alice, _ROOM_ID, "$cause")
        delivered = DeliveredMatrixEvent("$sent", {"msgtype": "m.text", "body": "answer"})

        with patch("mindroom.delivery_gateway.send_message_outcome", AsyncMock(return_value=delivered)):
            assert await self._send_final(gateway, "answer") == "$sent"

        assert (await _turn_row(alice, DeliveryStage.FINAL)).acknowledged_event_id == "$sent"
        assert await journal_store.turn_records("agent").load_all() == ()


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

        async def losing_publish(turn_id: str, event_id: str, _committed: TerminalTurnWrite | None) -> None:
            losing_publishes.append((turn_id, event_id))

        async def winning_publish(turn_id: str, event_id: str, _committed: TerminalTurnWrite | None) -> None:
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


async def test_process_shutdown_recovery_bypasses_saturated_ordinary_journal_reads(
    journal_store: EventJournalStore,
    alice: PrincipalStore,
) -> None:
    """A saturated ordinary read lane cannot starve an exact shutdown handoff proof."""
    source_event_id = "$shutdown-owned:localhost"
    turn = TurnRecord.create([source_event_id])
    await alice.admit(
        InboundEvent(
            event_id=source_event_id,
            room_id=_ROOM_ID,
            thread_id=source_event_id,
            kind=EventKind.MESSAGE,
            event_class=EventClass.ACTIONABLE,
            sender="@user:localhost",
            origin_server_ts=1_000,
            source={
                "event_id": source_event_id,
                "content": {"msgtype": "m.text", "body": "question"},
            },
        ),
    )
    bot = _response_recovery_bot(journal_store, await _store(journal_store))

    backend = journal_store.backend
    ordinary_capacity = (
        backend._offload._executor._max_workers if isinstance(backend, SqliteBackend) else len(backend._pool)
    )
    ordinary_started = threading.Event()
    release_ordinary = threading.Event()
    started_count = 0
    started_lock = threading.Lock()

    def block_ordinary_read(transaction: Transaction) -> None:
        nonlocal started_count
        transaction.fetchone("SELECT 1 AS one")
        with started_lock:
            started_count += 1
            if started_count == ordinary_capacity:
                ordinary_started.set()
        release_ordinary.wait()

    ordinary_reads = tuple(asyncio.create_task(backend.read(block_ordinary_read)) for _ in range(ordinary_capacity))
    proof: asyncio.Task[bool] | None = None
    try:
        assert await asyncio.to_thread(ordinary_started.wait, 10), "ordinary journal reads did not saturate"
        proof = asyncio.create_task(bot._response_recovery_ready(turn))
        ready = await asyncio.wait_for(asyncio.shield(proof), timeout=1)
    finally:
        release_ordinary.set()
        await asyncio.gather(*ordinary_reads)
        if proof is not None and not proof.done():
            await proof

    assert ready is True


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
        await admit_room_membership(alice, _ROOM_ID, "leave", source=DepartureSource.LOCAL)
        await admit_room_membership(alice, _ROOM_ID, "join")
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
        await admit_room_membership(alice, _ROOM_ID, "leave", source=DepartureSource.LOCAL)
        await admit_room_membership(alice, _ROOM_ID, "join")
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
        await admit_room_membership(alice, _ROOM_ID, "leave", source=DepartureSource.LOCAL)
        await admit_room_membership(alice, _ROOM_ID, "join")

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

    async def test_identical_continuations_reconcile_by_copy_count(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """One delivered copy must not satisfy two byte-identical continuations.

        Long homogeneous responses can repeat a continuation payload exactly.
        Existence-based reconciliation would see the first copy in the room and
        acknowledge the row with the second copy never sent; counting copies
        leaves exactly the missing positions to resend.
        """
        duplicate = {
            "msgtype": "m.text",
            "body": "z" * 1_000,
            "format": "org.matrix.custom.html",
            "formatted_body": "<p>zzz</p>\n",
        }
        tail = {**duplicate, "body": "the end", "formatted_body": "<p>the end</p>\n"}
        await alice.enqueue_matrix_delivery(
            delivery_id="segmented-turn",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "the start"},
            result={_SEGMENT_PAYLOADS_RESULT_KEY: [duplicate, duplicate, tail]},
        )
        claimed = await alice.claim_matrix_delivery(
            delivery_id="segmented-turn",
            stage=DeliveryStage.FINAL,
            sending_device_id="OLD-DEVICE",
        )
        assert claimed is not None
        assert claimed.result is not None
        continuations = claimed.result[_SEGMENT_PAYLOADS_RESULT_KEY]

        def room_event(event_id: str, content: Mapping[str, object]) -> nio.Event:
            event = nio.Event.parse_event(
                {
                    "event_id": event_id,
                    "room_id": _ROOM_ID,
                    "sender": _AGENT_USER_ID,
                    "origin_server_ts": 1_000,
                    "type": "m.room.message",
                    "content": dict(content),
                },
            )
            assert isinstance(event, nio.Event)
            return event

        # The earlier device delivered the primary and one copy of the
        # duplicated continuation before crashing.
        prior_primary = room_event("$prior-primary", dict(claimed.payload))
        prior_continuation = room_event("$prior-continuation", dict(continuations[0]))
        gateway = _gateway(tmp_path, alice, sending_device_id="NEW-DEVICE")
        gateway.deps.runtime.client.room_messages = AsyncMock(
            return_value=nio.RoomMessagesResponse(
                room_id=_ROOM_ID,
                chunk=[prior_continuation, prior_primary],
                start="start",
                end=None,
            ),
        )
        sent = AsyncMock(return_value=DeliveredMatrixEvent("$resent", {}))

        async def never_sends(_claimed: MatrixDelivery) -> str:
            msg = "an adopted primary is never resent"
            raise AssertionError(msg)

        with patch("mindroom.delivery_gateway.send_message_outcome", sent):
            outcome = await gateway._response_delivery(never_sends, handoff=None).recover()

        stored = await alice.load_matrix_delivery(delivery_id="segmented-turn", stage=DeliveryStage.FINAL)
        assert outcome == RecoveryOutcome(recovered=1, failed=0)
        assert stored is not None
        assert stored.acknowledged_event_id == "$prior-primary"
        # The second duplicate and the tail were missing: exactly those go out.
        assert [call.args[2] for call in sent.await_args_list] == [continuations[1], continuations[2]]
        assert [call.kwargs["transaction_id"] for call in sent.await_args_list] == [
            _segment_transaction_id(claimed.transaction_id, 2),
            _segment_transaction_id(claimed.transaction_id, 3),
        ]

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

        async def publish(turn_id: str, event_id: str, _committed: TurnRecord | None) -> None:
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

    async def test_process_shutdown_accepts_exact_completed_final(
        self,
        tmp_path: Path,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A cancelled task is safe when FINAL and its exact terminal turn committed."""
        source_event_id = "$source"
        response_event_id = "$answer"
        turn = TurnRecord.create([source_event_id], completed=False)
        turn_store = await _store(journal_store, agent_name="agent")
        await turn_store.record_pending_turn(turn)
        await alice.admit(
            InboundEvent(
                event_id=source_event_id,
                room_id=_ROOM_ID,
                thread_id=source_event_id,
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender="@user:localhost",
                origin_server_ts=1_000,
                source={
                    "event_id": source_event_id,
                    "content": {"msgtype": "m.text", "body": "question"},
                },
            ),
        )
        handoff = TurnHandoff(
            sources_for_turn=lambda turn_id: turn.source_event_ids if turn_id == source_event_id else (),
            released=lambda _source_event_ids: None,
        )
        gateway = _gateway(
            tmp_path,
            alice,
            terminal_turn_for=turn_store.terminal_turn_record,
            terminal_turn_committed=turn_store.publish_committed_response,
            turn_handoff=handoff,
        )
        bot = _response_recovery_bot(journal_store, turn_store)
        send_started = asyncio.Event()
        finish_send = asyncio.Event()

        async def send(_delivery: MatrixDelivery) -> str:
            send_started.set()
            await finish_send.wait()
            return response_event_id

        delivery = gateway._response_delivery(send, handoff=handoff)
        runner = ResponseRunner(deps=MagicMock())
        response_task = runner.track_inbox_response(
            delivery.deliver(
                delivery_id=source_event_id,
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=source_event_id,
                payload={"msgtype": "m.text", "body": "answer"},
            ),
            name="test_process_shutdown_completed_final",
            recovery_proof_ready=lambda: bot._response_recovery_ready(turn),
            room_id=_ROOM_ID,
        )
        await send_started.wait()
        runner.begin_process_shutdown()
        finish_send.set()

        assert (
            await runner.drain_inbox_responses(
                cancel_after_seconds=0.1,
                shutdown_intent=ORDERLY_SHUTDOWN,
            )
            is True
        )
        assert response_task.cancelled()
        stored = await alice.load_matrix_delivery(delivery_id=source_event_id, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == response_event_id
        completed_turn = turn_store.get_turn_record(source_event_id)
        assert completed_turn is not None
        assert completed_turn.completed
        assert completed_turn.response_event_id == response_event_id

    async def test_process_shutdown_finishes_terminal_publication_across_second_cancel(
        self,
        tmp_path: Path,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A repeated shutdown cancel cannot strand memory behind the atomic ack."""
        source_event_id = "$source"
        response_event_id = "$answer"
        turn = TurnRecord.create([source_event_id], completed=False)
        turn_store = await _store(journal_store, agent_name="agent")
        await turn_store.record_pending_turn(turn)
        await alice.admit(
            InboundEvent(
                event_id=source_event_id,
                room_id=_ROOM_ID,
                thread_id=source_event_id,
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender="@user:localhost",
                origin_server_ts=1_000,
                source={
                    "event_id": source_event_id,
                    "content": {"msgtype": "m.text", "body": "question"},
                },
            ),
        )
        handoff = TurnHandoff(
            sources_for_turn=lambda turn_id: turn.source_event_ids if turn_id == source_event_id else (),
            released=lambda _source_event_ids: None,
        )
        publication_started = asyncio.Event()
        allow_publication = asyncio.Event()

        async def publish_committed_response(turn_id: str, event_id: str, committed: TurnRecord | None) -> None:
            publication_started.set()
            await allow_publication.wait()
            await turn_store.publish_committed_response(turn_id, event_id, committed)

        gateway = _gateway(
            tmp_path,
            alice,
            terminal_turn_for=turn_store.terminal_turn_record,
            terminal_turn_committed=publish_committed_response,
            turn_handoff=handoff,
        )
        bot = _response_recovery_bot(journal_store, turn_store)
        send_started = asyncio.Event()
        finish_send = asyncio.Event()

        async def send(_delivery: MatrixDelivery) -> str:
            send_started.set()
            await finish_send.wait()
            return response_event_id

        delivery = gateway._response_delivery(send, handoff=handoff)
        runner = ResponseRunner(deps=MagicMock())
        response_task = runner.track_inbox_response(
            delivery.deliver(
                delivery_id=source_event_id,
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=source_event_id,
                payload={"msgtype": "m.text", "body": "answer"},
            ),
            name="test_process_shutdown_repeated_cancel_after_final_ack",
            recovery_proof_ready=lambda: bot._response_recovery_ready(turn),
            room_id=_ROOM_ID,
        )
        await send_started.wait()
        original_request_task_cancel = request_task_cancel
        cancellation_count = 0

        def cancel_and_release_publication(task: asyncio.Task[None], **kwargs: object) -> None:
            nonlocal cancellation_count
            cancellation_count += 1
            original_request_task_cancel(task, **kwargs)  # type: ignore[arg-type]
            if cancellation_count == 2:
                allow_publication.set()

        with patch(
            "mindroom.response_runner.request_task_cancel",
            side_effect=cancel_and_release_publication,
        ):
            runner.begin_process_shutdown()
            finish_send.set()
            await publication_started.wait()
            assert (
                await runner.drain_inbox_responses(
                    # Exercise repeated cancellation without imposing a 50 ms database deadline.
                    cancel_after_seconds=0.5,
                    shutdown_intent=ORDERLY_SHUTDOWN,
                )
                is True
            )

        assert cancellation_count >= 2
        assert response_task.cancelled()
        stored = await alice.load_matrix_delivery(delivery_id=source_event_id, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == response_event_id
        completed_turn = turn_store.get_turn_record(source_event_id)
        assert completed_turn is not None
        assert completed_turn.completed
        assert completed_turn.response_event_id == response_event_id

    async def test_process_shutdown_accepts_final_edit_of_persisted_placeholder(
        self,
        tmp_path: Path,
        journal_store: EventJournalStore,
        alice: PrincipalStore,
    ) -> None:
        """A FINAL edit transfers ownership without rebinding its visible message."""
        source_event_id = "$source"
        placeholder_event_id = "$placeholder"
        final_edit_event_id = "$final-edit"
        turn = TurnRecord.create(
            [source_event_id],
            completed=False,
            response_event_id=placeholder_event_id,
            response_owner="agent",
        )
        turn_store = await _store(journal_store, agent_name="agent")
        await turn_store.record_pending_turn(turn)
        await alice.admit(
            InboundEvent(
                event_id=source_event_id,
                room_id=_ROOM_ID,
                thread_id=source_event_id,
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender="@user:localhost",
                origin_server_ts=1_000,
                source={
                    "event_id": source_event_id,
                    "content": {"msgtype": "m.text", "body": "question"},
                },
            ),
        )
        handoff = TurnHandoff(
            sources_for_turn=lambda turn_id: turn.source_event_ids if turn_id == source_event_id else (),
            released=lambda _source_event_ids: None,
        )
        gateway = _gateway(
            tmp_path,
            alice,
            terminal_turn_for=turn_store.terminal_turn_record,
            terminal_turn_committed=turn_store.publish_committed_response,
            turn_handoff=handoff,
        )
        bot = _response_recovery_bot(journal_store, turn_store)
        send_started = asyncio.Event()
        finish_send = asyncio.Event()

        async def send(_delivery: MatrixDelivery) -> str:
            send_started.set()
            await finish_send.wait()
            return final_edit_event_id

        delivery = gateway._response_delivery(send, handoff=handoff)
        runner = ResponseRunner(deps=MagicMock())
        response_task = runner.track_inbox_response(
            delivery.deliver(
                delivery_id=source_event_id,
                stage=DeliveryStage.FINAL,
                room_id=_ROOM_ID,
                thread_id=source_event_id,
                payload={"msgtype": "m.text", "body": "answer"},
                edits_event_id=placeholder_event_id,
            ),
            name="test_process_shutdown_completed_final_edit",
            recovery_proof_ready=lambda: bot._response_recovery_ready(turn),
            room_id=_ROOM_ID,
        )
        await send_started.wait()
        runner.begin_process_shutdown()
        finish_send.set()

        assert (
            await runner.drain_inbox_responses(
                cancel_after_seconds=0.1,
                shutdown_intent=ORDERLY_SHUTDOWN,
            )
            is True
        )
        assert response_task.cancelled()
        stored = await alice.load_matrix_delivery(delivery_id=source_event_id, stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id == final_edit_event_id
        assert stored.edits_event_id == placeholder_event_id
        completed_turn = turn_store.get_turn_record(source_event_id)
        assert completed_turn is not None
        assert completed_turn.completed
        assert completed_turn.response_event_id == placeholder_event_id
        assert turn_store.is_handled(source_event_id)

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

    async def test_process_shutdown_after_user_cancel_leaves_enqueued_send_for_recovery(
        self,
    ) -> None:
        """Process stop supersedes an earlier user cancel before a committed send."""
        outbox = FakeOutbox()
        enqueue_committed = asyncio.Event()
        return_from_enqueue = asyncio.Event()
        first_cancellation_observed = asyncio.Event()
        original_enqueue = outbox.enqueue_matrix_delivery
        sent: list[DeliveryStage] = []

        def process_shutdown_requested() -> bool:
            first_cancellation_observed.set()
            return current_task_is_process_shutdown()

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

        delivery = MatrixDeliveryWorker(
            store=outbox,
            send=send,
            observe_delivered=ignore_delivered_projection,
            process_shutdown_requested=process_shutdown_requested,
        )
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
            request_task_cancel(final, cancel_source="user_stop")
            await asyncio.wait_for(first_cancellation_observed.wait(), timeout=5)
            request_task_cancel(final, process_shutdown=True)
            return_from_enqueue.set()

            with pytest.raises(asyncio.CancelledError):
                await final

        stored = await outbox.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id is None
        assert sent == []
        assert await delivery.recover() == RecoveryOutcome(recovered=1, failed=0)
        assert sent == [DeliveryStage.FINAL]

    async def test_process_shutdown_during_recovery_marker_write_defers_matrix_send(
        self,
    ) -> None:
        """Recovery completes its device marker but starts no new Matrix request."""
        outbox = FakeOutbox()
        await outbox.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "final"},
        )
        device_write_started = asyncio.Event()
        finish_device_write = asyncio.Event()
        original_record_sending_device = outbox.record_matrix_delivery_device
        sent: list[DeliveryStage] = []

        async def record_device_then_wait(
            *,
            delivery_id: str,
            stage: DeliveryStage,
            device_id: str | None,
        ) -> None:
            device_write_started.set()
            await finish_device_write.wait()
            await original_record_sending_device(
                delivery_id=delivery_id,
                stage=stage,
                device_id=device_id,
            )

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery.stage)
            return "$final"

        delivery = MatrixDeliveryWorker(
            store=outbox,
            send=send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE1",
            process_shutdown_requested=current_task_is_process_shutdown,
        )
        with patch.object(outbox, "record_matrix_delivery_device", side_effect=record_device_then_wait):
            recovery = asyncio.create_task(delivery.recover())
            await device_write_started.wait()
            request_task_cancel(recovery, process_shutdown=True)
            finish_device_write.set()

            with pytest.raises(asyncio.CancelledError):
                await recovery

        stored = await outbox.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.sending_device_id == "DEVICE1"
        assert stored.acknowledged_event_id is None
        assert sent == []
        assert await delivery.recover() == RecoveryOutcome(recovered=1, failed=0)
        assert sent == [DeliveryStage.FINAL]

    async def test_process_shutdown_after_recovery_send_starts_finishes_acknowledgement(
        self,
    ) -> None:
        """A Matrix request that already started remains indivisible from its ack."""
        outbox = FakeOutbox()
        await outbox.enqueue_matrix_delivery(
            delivery_id="turn-1",
            stage=DeliveryStage.FINAL,
            room_id=_ROOM_ID,
            thread_id=None,
            payload={"msgtype": "m.text", "body": "final"},
        )
        send_started = asyncio.Event()
        finish_send = asyncio.Event()
        sent: list[DeliveryStage] = []

        async def send(delivery: MatrixDelivery) -> str:
            send_started.set()
            await finish_send.wait()
            sent.append(delivery.stage)
            return "$final"

        delivery = MatrixDeliveryWorker(
            store=outbox,
            send=send,
            observe_delivered=ignore_delivered_projection,
            process_shutdown_requested=current_task_is_process_shutdown,
        )
        recovery = asyncio.create_task(delivery.recover())
        await send_started.wait()
        request_task_cancel(recovery, process_shutdown=True)
        finish_send.set()

        with pytest.raises(asyncio.CancelledError):
            await recovery

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

    async def test_process_shutdown_after_claim_starts_leaves_send_for_recovery(
        self,
    ) -> None:
        """A process stop after claim must leave the unsent row for startup recovery."""
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

        delivery = MatrixDeliveryWorker(
            store=outbox,
            send=send,
            observe_delivered=ignore_delivered_projection,
            process_shutdown_requested=current_task_is_process_shutdown,
        )
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
            request_task_cancel(final, process_shutdown=True)
            finish_claim.set()

            with pytest.raises(asyncio.CancelledError):
                await final

        stored = await outbox.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id is None
        assert sent == []
        assert await delivery.recover() == RecoveryOutcome(recovered=1, failed=0)
        assert sent == [DeliveryStage.FINAL]

    async def test_process_shutdown_before_matrix_send_leaves_marked_row_for_recovery(
        self,
    ) -> None:
        """A committed device marker is recoverable when shutdown precedes the send."""
        outbox = FakeOutbox()
        device_write_started = asyncio.Event()
        finish_device_write = asyncio.Event()
        original_record_sending_device = outbox.record_matrix_delivery_device
        sent: list[DeliveryStage] = []

        async def record_device_then_wait(
            *,
            delivery_id: str,
            stage: DeliveryStage,
            device_id: str | None,
        ) -> None:
            device_write_started.set()
            await finish_device_write.wait()
            await original_record_sending_device(
                delivery_id=delivery_id,
                stage=stage,
                device_id=device_id,
            )

        async def send(delivery: MatrixDelivery) -> str:
            sent.append(delivery.stage)
            return "$final"

        delivery = MatrixDeliveryWorker(
            store=outbox,
            send=send,
            observe_delivered=ignore_delivered_projection,
            sending_device_id="DEVICE1",
            process_shutdown_requested=current_task_is_process_shutdown,
        )
        with patch.object(outbox, "record_matrix_delivery_device", side_effect=record_device_then_wait):
            final = asyncio.create_task(
                delivery.deliver(
                    delivery_id="turn-1",
                    stage=DeliveryStage.FINAL,
                    room_id=_ROOM_ID,
                    thread_id=None,
                    payload={"msgtype": "m.text", "body": "final"},
                ),
            )
            await device_write_started.wait()
            request_task_cancel(final, process_shutdown=True)
            finish_device_write.set()

            with pytest.raises(asyncio.CancelledError):
                await final

        stored = await outbox.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
        assert stored is not None
        assert stored.acknowledged_event_id is None
        assert stored.sending_device_id == "DEVICE1"
        assert sent == []
        assert await delivery.recover() == RecoveryOutcome(recovered=1, failed=0)
        assert sent == [DeliveryStage.FINAL]

    async def test_terminal_callback_can_reenter_the_same_turn(
        self,
        tmp_path: Path,
        alice: PrincipalStore,
    ) -> None:
        """Publishing a committed FINAL runs after releasing its visible-delivery lock."""
        await self._enqueue(alice, DeliveryStage.FINAL)
        reentered: list[str | None] = []
        reentrant_delivery: MatrixDeliveryWorker | None = None

        async def publish_committed(_turn_id: str, _event_id: str, _committed: TurnRecord | None) -> None:
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
        await admit_room_event(alice, _ROOM_ID, "$source")
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
            redaction = asyncio.create_task(turn_store.mark_source_redacted("$source", room_id=_ROOM_ID))
            finish_send.set()
            outcome = await recovery
            await redaction

        assert outcome.recovered == 1
        stored = await self._restarted_record(journal_store, "$source")
        assert stored is not None
        assert stored.redacted_source_event_ids == ("$source",), "the redaction never reached the database"
        assert stored.response_event_id == "$answer", "the delivered answer lost the event it is stored under"
        assert stored.completed, "a delivered turn came back unfinished"


async def test_a_published_body_is_remembered_until_a_later_one_is_written_ahead() -> None:
    """However many newer bodies arrive while one edit is formatted, that edit still finds what it shows."""
    shows = {f"body-{index}": Presentation(placeholder=f"shown-{index}") for index in range(12)}
    published = dict(shows)

    assert _take_published(published, "body-3") == shows["body-3"]
    assert list(published) == [f"body-{index}" for index in range(3, 12)]
    assert _take_published(published, "body-1") is None
    assert _take_published(published, "body-11") == shows["body-11"]
    assert list(published) == ["body-11"]


@pytest.mark.parametrize("show_tool_calls", [True, False])
async def test_a_whole_reply_write_sends_its_trace_only_when_tool_calls_show(show_tool_calls: bool) -> None:
    """A write over earlier spans' work renders the whole reply, and a reply that hides tool calls sends no trace."""
    lookup = ToolTraceEntry(type="tool_call_completed", tool_name="lookup", args_preview="q=secret")
    shown = Presentation(
        segments=(
            Segment(kind="answer", text="Earlier work", span_id="span-1", tool_trace=(lookup,)),
            note_segment(NoteKind.RESTART),
            Segment(kind="answer", text="Waiting for approval", span_id="span-2"),
        ),
        show_tool_calls=show_tool_calls,
    )

    body, trace = _reply_body("Waiting for approval", None, shown)

    assert body.startswith("Earlier work\n\n")
    assert body.endswith("Waiting for approval")
    assert trace == ([lookup] if show_tool_calls else None)


@pytest.mark.asyncio
async def test_superseding_the_replay_of_an_ended_reply_leaves_its_sources_to_the_caller(
    tmp_path: Path,
    alice: PrincipalStore,
) -> None:
    """A reply that ended without settling its sources, as a departure does, holds nothing for the replay to end."""
    gateway = _gateway(tmp_path, alice)
    async with reply_span(alice, source_event_id="$cause", room_id=_ROOM_ID) as handle:
        await gateway.end_reply_span(handle, lambda reply, span: rl.departed(reply, span, now_ns=1))
    assert await alice.is_pending("$cause")
    assert await gateway.supersede_replay(("$cause",)) is None


@pytest.mark.asyncio
async def test_a_late_bound_reply_edit_is_found_by_what_it_sent(tmp_path: Path, alice: PrincipalStore) -> None:
    """A reply edit gets its edit envelope only when claimed; an earlier device's copy is matched by that envelope."""
    await alice.enqueue_matrix_delivery(
        delivery_id="turn-1",
        stage=DeliveryStage.FINAL,
        room_id=_ROOM_ID,
        thread_id=None,
        payload={"msgtype": "m.text", "body": "answer"},
    )
    row = await alice.load_matrix_delivery(delivery_id="turn-1", stage=DeliveryStage.FINAL)
    assert row is not None
    claimed = replace(row, reply_id="reply-1", edits_event_id="$reply")
    find = AsyncMock(return_value="$earlier-copy")
    with patch("mindroom.delivery_gateway.find_outbox_delivery_event_id_via_room_messages", find):
        assert await _gateway(tmp_path, alice)._delivered_under_a_previous_device(claimed) == "$earlier-copy"
    sent = find.await_args.kwargs["delivery_content"]
    assert sent["m.new_content"]["body"] == "answer"
    assert sent["m.relates_to"] == {"rel_type": "m.replace", "event_id": "$reply"}
