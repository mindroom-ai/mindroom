"""Record the rows the current journal writes for each tool-approval state, as SQL fixtures.

A refactor replaces the journal's approval tables and converts an older database once, when the journal opens. These
recordings give that conversion real rows to convert. Each scenario drives the approval code a running MindRoom uses
-- the approval manager, the response coordinator's policy plan and card publication, and the journal's own pause,
claim, Stop, failure, grant, and scheduled-call transactions -- onto a fresh SQLite journal, then writes every row of
every journal table as ``INSERT`` statements to ``tests/fixtures/approval_records/<scenario>.sql``. ``schema.sql``
holds the schema those rows belong to.

The clock is fixed and moves only when a scenario moves it, and every ``uuid4`` a recording reaches returns the next
id of a fixed sequence, so recording again at the same commit writes identical files. Each recording is then loaded
into a fresh database and opened with ``EventJournalStore``, which must read every continuation, card, grant, and
scheduled call back without changing a row.

Skipped unless ``MINDROOM_RECORD_APPROVAL_FIXTURES=1``; ``tests/fixtures/approval_records/README.md`` says how to
record again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import sys
import textwrap
import time
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from mindroom.approval import manager as approval_manager_module
from mindroom.approval import response as approval_response_module
from mindroom.approval.manager import ApprovalManager
from mindroom.approval.response import ApprovalResponseCoordinator, continuation_target, plan_approval_calls
from mindroom.approval.tool_approval import (
    POLICY_CONFIRMATION_APPROVAL_TYPE,
    BackgroundScriptToolOrigin,
    ToolApprovalTransportError,
)
from mindroom.config.main import Config
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.event_journal import (
    INTERRUPTED_FAILURE_REASON,
    ApprovalDecision,
    ApprovalDecisionMetadata,
    DeliveryStage,
    EventClass,
    EventJournalStore,
    EventKind,
    InboundEvent,
    ScheduledCallBinding,
    ScheduledCallClaim,
    UnreadableApprovalCard,
)
from mindroom.event_journal.schema import SQLITE_DIALECT, schema_statements
from mindroom.llm.runs import ToolExecution
from mindroom.matrix.message_builder import build_thread_relation
from mindroom.response_sources import ResponseSources
from mindroom.session_ids import create_session_id
from mindroom.tool_approval_grants import ANY_ARGUMENTS, canonical_arguments, grant_operation
from tests.approval_continuation_helpers import advance_continuation, approval_continuation, claim_continuation
from tests.conftest import handle_card_response_for_test, test_runtime_paths
from tests.journal_membership_helpers import admit_room_membership
from tests.reply_span_helpers import paused_for_approval

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from mindroom.approval.response import _ApprovalPausePlan
    from mindroom.constants import RuntimePaths
    from mindroom.event_journal import ApprovalContinuation, BackgroundApprovalDecision, MatrixDelivery, PrincipalStore

RECORD_ENV = "MINDROOM_RECORD_APPROVAL_FIXTURES"
FIXTURES = Path(__file__).parent / "fixtures" / "approval_records"

pytestmark = pytest.mark.skipif(
    os.environ.get(RECORD_ENV) != "1",
    reason=f"records the approval-record fixtures only when {RECORD_ENV}=1",
)

ROOM = "!room:example.org"
THREAD = "$thread-root"
OTHER_THREAD = "$other-thread-root"
REQUESTER = "@alice:example.org"
AGENT = "code"
# Journal principals are named as a bot names its own: the entity, then its Matrix account.
ROUTER = "router@@mindroom_router:example.org"
RESPONDER = "code@@mindroom_code:example.org"
# The bot instance that owns the responder's replies, and the approval runtime that publishes a pause's cards.
BOT_GENERATION = "code-bot-generation"
PUBLICATION_GENERATION = "code-approval-runtime"
ROUTER_DEVICE = "ROUTERDEVICE"
# 2027-01-15T08:00:00Z, where every recording's clock starts.
START_NS = 1_800_000_000_000_000_000
SECOND_NS = 1_000_000_000

TOOLKITS = {
    "run_shell_command": "shell",
    "save_file": "file",
    "add": "calculator",
    "post_slack_message": "slack",
}
# Every call asks for approval, except a shell command the operator's script lets through: only a push needs a human.
_CONFIG = {
    "tool_approval": {
        "default": "require_approval",
        "rules": [{"match": "run_shell_command", "script": "approve_shell.py"}],
    },
}
_SHELL_POLICY = 'def check(tool_name, arguments, agent_name):\n    return "push" in str(arguments.get("args", ""))\n'
# The reasons production code records, copied where their owner keeps them private.
_SCHEDULE_CANCELLED_REASON = "Schedule cancelled."
_PUBLICATION_FAILED = "Approval card publication failed"

_TABLES = tuple(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", "\n".join(schema_statements(SQLITE_DIALECT))))


@dataclass
class _Clock:
    """The one wall clock every recorded write reads, moved only by the scenario."""

    now_ns: int = START_NS

    def time_ns(self) -> int:
        """Stand in for ``time.time_ns``."""
        return self.now_ns

    def now(self) -> datetime:
        """Stand in for ``datetime.now(UTC)``."""
        return datetime.fromtimestamp(self.now_ns / SECOND_NS, UTC)

    def advance(self, seconds: int = 1) -> None:
        """Move the clock forward."""
        self.now_ns += seconds * SECOND_NS


@dataclass
class _Ids:
    """Stand in for ``uuid.uuid4``: the n-th id a recording asks for is always the same."""

    issued: int = 0

    def uuid4(self) -> uuid.UUID:
        """Return the next id of the sequence."""
        self.issued += 1
        return uuid.UUID(int=self.issued)


def _pin_volatile_values(monkeypatch: pytest.MonkeyPatch, clock: _Clock, ids: _Ids) -> None:
    """Make every clock read and random id the recorded code reaches come from ``clock`` and ``ids``."""
    random_uuid4 = uuid.uuid4
    for name, module in tuple(sys.modules.items()):
        if module is not None and name.startswith(("mindroom", "tests")) and vars(module).get("uuid4") is random_uuid4:
            monkeypatch.setattr(module, "uuid4", ids.uuid4)
    monkeypatch.setattr(uuid, "uuid4", ids.uuid4)
    monkeypatch.setattr(time, "time_ns", clock.time_ns)
    monkeypatch.setattr(approval_manager_module, "_utcnow", clock.now)

    class _ClockDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return clock.now() if tz is None else clock.now().astimezone(tz)

    # The policy plan stamps each call's deadline with ``datetime.now``.
    monkeypatch.setattr(approval_response_module, "datetime", _ClockDatetime)


def _event_id(delivery: MatrixDelivery) -> str:
    """Return the event ID Matrix gives one delivery: the card's own for an INITIAL, its edit's otherwise."""
    suffix = "" if delivery.stage is DeliveryStage.INITIAL else f":{delivery.stage.value}"
    return f"${delivery.delivery_id}{suffix}"


def _card(delivery_id: str) -> str:
    """Return the event ID of the card or receipt delivered as ``delivery_id``."""
    return f"${delivery_id}"


def _call(call_id: str, tool_name: str, **arguments: object) -> ToolExecution:
    """Return one call a policy rule gates, as the agent's model made it."""
    return ToolExecution(
        tool_call_id=call_id,
        tool_name=tool_name,
        tool_args=arguments,
        requires_confirmation=True,
        approval_type=POLICY_CONFIRMATION_APPROVAL_TYPE,
    )


def _push(call_id: str = "call-push") -> ToolExecution:
    return _call(call_id, "run_shell_command", args="git push origin main")


@dataclass
class _Recording:
    """One fresh journal, and the approval runtime a router and the ``code`` agent run over it."""

    database: Path
    journal: EventJournalStore
    clock: _Clock
    runtime_paths: RuntimePaths
    config: Config
    manager: ApprovalManager
    coordinator: ApprovalResponseCoordinator
    # Sends Matrix refuses, by delivery and stage.
    refused_sends: set[tuple[str, DeliveryStage]]
    workflow_digests: dict[str, str] = field(default_factory=dict)
    closed: bool = False

    @property
    def router(self) -> PrincipalStore:
        """Return the router's journal, which owns every card."""
        return self.journal.principal(ROUTER)

    @property
    def responder(self) -> PrincipalStore:
        """Return the ``code`` agent's journal, which owns its replies and paused runs."""
        return self.journal.principal(RESPONDER)

    async def close(self) -> None:
        """Stop the approval runtime and close the journal, once."""
        if not self.closed:
            self.closed = True
            await self.manager.shutdown()
            await self.journal.close()

    async def message(self, event_id: str, *, body: str, thread_id: str = THREAD) -> None:
        """Admit one message the requester sent in the conversation, as the agent's sync does."""
        self.clock.advance()
        origin_server_ts = self.clock.now_ns // 1_000_000
        await self.responder.admit(
            InboundEvent(
                event_id=event_id,
                room_id=ROOM,
                thread_id=thread_id,
                kind=EventKind.MESSAGE,
                event_class=EventClass.ACTIONABLE,
                sender=REQUESTER,
                origin_server_ts=origin_server_ts,
                source={
                    "type": "m.room.message",
                    "event_id": event_id,
                    "sender": REQUESTER,
                    "origin_server_ts": origin_server_ts,
                    "content": {
                        "msgtype": "m.text",
                        "body": body,
                        "m.relates_to": build_thread_relation(
                            thread_event_id=thread_id,
                            latest_thread_event_id=thread_id,
                        ),
                    },
                },
            ),
        )

    async def plan(self, *calls: ToolExecution, thread_id: str = THREAD) -> _ApprovalPausePlan:
        """Evaluate the approval policy for one paused generation, as a pausing response does."""
        return await plan_approval_calls(
            tuple((call, AGENT) for call in calls),
            config=self.config,
            runtime_paths=self.runtime_paths,
            requester_id=REQUESTER,
            toolkit_owners={(AGENT, call.tool_name): TOOLKITS[call.tool_name] for call in calls},
            room_id=ROOM,
            thread_id=thread_id,
        )

    async def publish(self, continuation: ApprovalContinuation, plan: _ApprovalPausePlan) -> ApprovalContinuation:
        """Publish a waiting generation's cards and release its publication lease, as the response coordinator does."""
        self.clock.advance()
        await self.coordinator.publish_generation(
            continuation,
            plan,
            target=continuation_target(continuation),
            failure_reason=_PUBLICATION_FAILED,
        )
        published = await self.responder.approvals.approval_continuation(continuation.approval_id)
        assert published is not None
        return published

    async def pause(self, name: str, *calls: ToolExecution, thread_id: str = THREAD) -> ApprovalContinuation:
        """Pause the agent's reply to a new message on ``calls`` and publish the cards its policy asks for.

        The continuation is ``name``; its message is ``$message-<name>`` and its reply ``$reply-<name>``. Each card's
        delivery is ``<name>-<generation>-<call index>``, as the coordinator names them.
        """
        source = f"$message-{name}"
        await self.message(source, body=f"Please {name}.", thread_id=thread_id)
        plan = await self.plan(*calls, thread_id=thread_id)
        session_id = create_session_id(ROOM, thread_id)
        self.clock.advance()
        paused = await paused_for_approval(
            self.responder,
            approval_continuation(
                approval_id=name,
                run_id=f"run-{name}",
                session_id=session_id,
                entity_name=AGENT,
                room_id=ROOM,
                thread_id=thread_id,
                requester_id=REQUESTER,
                response_event_id=f"$reply-{name}",
                sources=ResponseSources((source,), (source,)),
                calls=plan.calls,
                state="waiting",
                runtime_generation=PUBLICATION_GENERATION,
                request_body=f"Please {name}.",
            ),
            text=plan.waiting_text or "",
        )
        assert paused is not None
        return await self.publish(paused, plan)

    async def decide(
        self,
        card_event_id: str,
        status: str,
        *,
        reason: str | None = None,
        seconds: int | None = None,
        scheduled_scope: str | None = None,
    ) -> None:
        """Answer one card as the requester does from their client."""
        self.clock.advance()
        result = await handle_card_response_for_test(
            self.manager,
            room_id=ROOM,
            sender_id=REQUESTER,
            card_event_id=card_event_id,
            status=status,
            reason=reason,
            authorize_responder=lambda _agent: True,
            auto_approve_seconds=seconds,
            scheduled_scope=scheduled_scope,
        )
        assert result.consumed

    async def claim(self, name: str) -> ApprovalContinuation:
        """Claim a ready continuation with its reply's resume span, as the bot instance that owns the replies does."""
        self.clock.advance()
        claimed = await claim_continuation(self.responder, name, runtime_generation=BOT_GENERATION)
        assert claimed is not None
        assert claimed.state == "claimed"
        return claimed

    async def stop(self, name: str) -> None:
        """Record the requester's 🛑 on the reply of ``name``, as the agent's Stop handling does."""
        self.clock.advance()
        stop_event_id = f"$stop-{name}"
        reply_event_id = f"$reply-{name}"
        origin_server_ts = self.clock.now_ns // 1_000_000
        content = {"m.relates_to": {"rel_type": "m.annotation", "event_id": reply_event_id, "key": "🛑"}}
        await self.responder.admit(
            InboundEvent(
                event_id=stop_event_id,
                room_id=ROOM,
                thread_id=THREAD,
                kind=EventKind.REACTION,
                event_class=EventClass.ACTIONABLE,
                sender=REQUESTER,
                origin_server_ts=origin_server_ts,
                source={
                    "type": "m.reaction",
                    "event_id": stop_event_id,
                    "sender": REQUESTER,
                    "origin_server_ts": origin_server_ts,
                    "content": content,
                },
            ),
        )
        admitted = await self.responder.load_event(stop_event_id)
        assert admitted is not None
        stopped = await self.responder.replies.record_stop(
            reply_event_id,
            room_id=ROOM,
            receipt_order=admitted.receipt_order,
            may_wait=False,
            now_ns=self.clock.now_ns,
        )
        assert stopped
        await self.responder.settle(stop_event_id)

    async def fail(self, name: str, reason: str) -> None:
        """Fence the continuation ``name`` for failure in the state it holds, as its owner does."""
        self.clock.advance()
        current = await self.responder.approvals.approval_continuation(name)
        assert current is not None
        failing = await self.responder.approvals.request_approval_failure(
            name,
            reason,
            expected_state=current.state,
            expected_generation=current.generation,
            expected_runtime_generation=current.runtime_generation,
        )
        assert failing is not None
        assert failing.state == "failing"

    async def detached_call(
        self,
        run_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        *,
        timed: bool,
    ) -> asyncio.Task[BackgroundApprovalDecision]:
        """Ask about one detached call and return its waiter once its card is on Matrix.

        A background script asks without a responder; a background job's card is scoped to the conversation its
        reply answered, so it offers timed approvals (``timed``).
        """
        self.clock.advance()
        waiter = asyncio.create_task(
            self.manager.request_background_approval(
                origin=BackgroundScriptToolOrigin(
                    run_id=run_id,
                    call_id=call_id,
                    requester_id=REQUESTER,
                    toolkit_name=TOOLKITS[tool_name],
                    function_name=tool_name,
                ),
                room_id=ROOM,
                thread_id=THREAD,
                agent_name=AGENT,
                requester_id=REQUESTER,
                approver_user_id=REQUESTER,
                tool_name=tool_name,
                arguments=arguments,
                timeout_seconds=600,
                entity_name=AGENT if timed else None,
                grant_operation=grant_operation(self.config, tool_name, arguments) if timed else None,
            ),
        )
        card_event_id = _card(f"script-approval:{run_id}:{call_id}")
        for _attempt in range(2_000):
            if await self.router.approvals.pending_approval_card(room_id=ROOM, card_event_id=card_event_id) is not None:
                return waiter
            await asyncio.sleep(0.005)
        msg = f"The card of {run_id}:{call_id} never reached Matrix"
        raise AssertionError(msg)

    async def schedule(self, task_id: str, *, any_arguments_offered: bool = False) -> None:
        """Schedule one gated call a minute from now, and post its scheduling-time card, as the scheduler tool does."""
        self.clock.advance()
        execute_at_ns = self.clock.now_ns + 60 * SECOND_NS
        execute_at = datetime.fromtimestamp(execute_at_ns / SECOND_NS, UTC)
        message = f"Post the standup reminder ({task_id})."
        # The scheduler's fingerprint of the task fields the approval was given for.
        digest = hashlib.sha256(
            json.dumps(
                [task_id, ROOM, THREAD, REQUESTER, message, execute_at.isoformat()],
                separators=(",", ":"),
            ).encode(),
        ).hexdigest()
        self.workflow_digests[task_id] = digest
        assert await self.manager.request_scheduled_call_approval(
            ScheduledCallBinding(
                task_id=task_id,
                room_id=ROOM,
                thread_id=THREAD,
                requester_id=REQUESTER,
                agent_name=AGENT,
                toolkit_name=TOOLKITS["post_slack_message"],
                tool_name="post_slack_message",
                arguments_json=canonical_arguments({"channel": "C0STANDUP", "text": "Standup in 5 minutes!"}),
                workflow_digest=digest,
                execute_at_ns=execute_at_ns,
            ),
            approver_user_id=REQUESTER,
            scheduled_for_text=execute_at.strftime("%-I:%M %p UTC"),
            any_arguments_offered=any_arguments_offered,
        )

    async def arm(self, task_id: str) -> None:
        """Arm an approved scheduled call as its task fires, which is within the window around its send time."""
        self.clock.advance()
        armed = await self.manager.arm_scheduled_call_approval(
            task_id,
            self.workflow_digests[task_id],
            any_arguments_allowed=True,
        )
        assert armed == "armed"


async def _open_recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Recording:
    clock, ids = _Clock(), _Ids()
    _pin_volatile_values(monkeypatch, clock, ids)
    runtime_paths = test_runtime_paths(tmp_path / "runtime")
    (tmp_path / "runtime" / "approve_shell.py").write_text(_SHELL_POLICY, encoding="utf-8")
    config = Config.model_validate(_CONFIG)
    database = tmp_path / "event_journal.db"
    journal = EventJournalStore.open_sqlite(database)
    refused_sends: set[tuple[str, DeliveryStage]] = set()

    async def prepare(_room_id: str, thread_id: str | None, content: dict) -> dict:
        # The router's transport threads each card under the conversation's latest event, the root here.
        prepared = dict(content)
        if thread_id is not None:
            prepared["m.relates_to"] = build_thread_relation(
                thread_event_id=thread_id,
                latest_thread_event_id=thread_id,
            )
        return prepared

    async def send(delivery: MatrixDelivery) -> str:
        if (delivery.delivery_id, delivery.stage) in refused_sends:
            msg = f"Matrix refused approval delivery {delivery.delivery_id!r}"
            raise ToolApprovalTransportError(msg)
        return _event_id(delivery)

    manager = ApprovalManager(
        runtime_paths,
        cards=journal.principal(ROUTER),
        prepare_event=prepare,
        send_delivery=send,
        transport_sender=lambda: "@mindroom_router:example.org",
        sending_device=lambda: ROUTER_DEVICE,
        responder_principal=lambda entity_name: f"{entity_name}@@mindroom_{entity_name}:example.org",
    )
    # The deadline sweep runs concurrently with the scenario; a scenario that wants one runs it itself.
    monkeypatch.setattr(manager, "_ensure_deadline_sweep", lambda: None)
    monkeypatch.setattr(approval_manager_module, "_MANAGER", manager)
    responder = journal.principal(RESPONDER)
    coordinator = ApprovalResponseCoordinator(
        config=lambda: config,
        runtime_paths=runtime_paths,
        store=responder.approvals,
        load_span=responder.replies.span,
        delivery_gateway=MagicMock(spec=DeliveryGateway),
        retry_sources=lambda _room_id, _sources: None,
        finish_approval=AsyncMock(return_value=True),
        release_approval=AsyncMock(return_value=True),
    )
    # The router left the room once and was invited back, so its cards belong to its second membership.
    router = journal.principal(ROUTER)
    for membership in ("join", "leave", "join"):
        await admit_room_membership(router, ROOM, membership)
    assert await router.membership_epoch(ROOM) == 1
    await admit_room_membership(responder, ROOM, "join")
    await responder.replies.write_generation(BOT_GENERATION)
    return _Recording(
        database=database,
        journal=journal,
        clock=clock,
        runtime_paths=runtime_paths,
        config=config,
        manager=manager,
        coordinator=coordinator,
        refused_sends=refused_sends,
    )


@pytest_asyncio.fixture
async def recording(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Recording]:
    """Return a fresh journal with the approval runtime running over it."""
    run = await _open_recording(tmp_path, monkeypatch)
    try:
        yield run
    finally:
        await run.close()


async def _call_decisions(run: _Recording, name: str) -> list[ApprovalDecision | None]:
    continuation = await run.responder.approvals.approval_continuation(name)
    assert continuation is not None
    return [call.decision for call in continuation.calls]


async def _card_on_matrix(run: _Recording, delivery_id: str) -> bool:
    """Return whether the card delivered as ``delivery_id`` is acknowledged and still undecided."""
    stored = await run.router.approvals.pending_approval_card(room_id=ROOM, card_event_id=_card(delivery_id))
    return stored is not None and stored.resolution is None


async def _retired(run: _Recording, delivery_id: str) -> bool:
    """Return whether the card or receipt delivered as ``delivery_id`` is retired into its terminal tombstone."""
    initial = await run.router.load_matrix_delivery(delivery_id=delivery_id, stage=DeliveryStage.INITIAL)
    return initial is None and await run.router.approvals.is_terminal_approval_card(
        room_id=ROOM,
        card_event_id=_card(delivery_id),
    )


async def _stop_waiter(waiter: asyncio.Task[BackgroundApprovalDecision]) -> None:
    """Stop waiting for a detached call's decision, leaving its card and call as they are."""
    waiter.cancel()
    with suppress(asyncio.CancelledError):
        await waiter


type _Scenario = Callable[[_Recording], Awaitable[None]]
SCENARIOS: dict[str, _Scenario] = {}


def _scenario(name: str) -> Callable[[_Scenario], _Scenario]:
    """Register one recording; its docstring's first line heads its fixture."""

    def register(record: _Scenario) -> _Scenario:
        SCENARIOS[name] = record
        return record

    return register


@_scenario("01_waiting_card_acknowledged")
async def _waiting_card_acknowledged(run: _Recording) -> None:
    """A reply paused on one call, waiting for its approval card, which Matrix acknowledged."""
    paused = await run.pause("waiting", _push())
    assert paused.state == "waiting"
    assert await _card_on_matrix(run, "waiting-0-0")


@_scenario("02_waiting_card_unacknowledged")
async def _waiting_card_unacknowledged(run: _Recording) -> None:
    """A reply paused on one call, waiting for its approval card, whose send Matrix refused, so it has no event yet."""
    run.refused_sends.add(("unacknowledged-0-0", DeliveryStage.INITIAL))
    paused = await run.pause("unacknowledged", _push())
    assert paused.state == "waiting"
    card = await run.router.load_matrix_delivery(delivery_id="unacknowledged-0-0", stage=DeliveryStage.INITIAL)
    assert card is not None
    assert card.acknowledged_event_id is None


@_scenario("03_waiting_policy_decided_and_card")
async def _waiting_policy_decided_and_card(run: _Recording) -> None:
    """A reply paused on two calls: the policy script approved one without a card, the other waits for its card."""
    paused = await run.pause(
        "policy",
        _call("call-status", "run_shell_command", args="git status"),
        _push(),
    )
    assert paused.state == "waiting"
    assert [call.decision for call in paused.calls] == [ApprovalDecision.APPROVED, None]
    assert [call.human_approval_required for call in paused.calls] == [False, True]
    assert await _card_on_matrix(run, "policy-0-1")


@_scenario("04_ready")
async def _ready(run: _Recording) -> None:
    """A reply paused on two calls the requester approved on their cards, ready for its resume to claim it."""
    await run.pause("ready", _push(), _call("call-save", "save_file", file_name="CHANGELOG.md", contents="## 1.2.0\n"))
    await run.decide(_card("ready-0-0"), "approved")
    await run.decide(_card("ready-0-1"), "approved")
    continuation = await run.responder.approvals.approval_continuation("ready")
    assert continuation is not None
    assert continuation.state == "ready"
    assert await _retired(run, "ready-0-0")


@_scenario("05_claimed")
async def _claimed(run: _Recording) -> None:
    """A reply paused on one approved call, whose resume span claimed it and runs it."""
    await run.pause("claimed", _push())
    await run.decide(_card("claimed-0-0"), "approved")
    await run.claim("claimed")


@_scenario("06_failing")
async def _failing(run: _Recording) -> None:
    """Paused replies failing for each stored reason: a Stop, a sync restart, an interruption, a shutdown, an error."""
    # The requester stopped a reply still waiting for its card; the card is still on Matrix, undecided.
    await run.pause("stopped", _push())
    await run.stop("stopped")
    # Resumes cut short after their call was approved: a sync restart, a cancellation of unknown origin, and a
    # shutdown that left the run for the next instance.
    for name, reason in (
        ("sync-restart", "sync_restart_cancelled"),
        ("interrupted", "interrupted"),
        ("shutdown", INTERRUPTED_FAILURE_REASON),
    ):
        await run.pause(name, _push())
        await run.decide(_card(f"{name}-0-0"), "approved")
        await run.claim(name)
        await run.fail(name, reason)
    # A ready run its owner could not resume, failed with the error it met.
    await run.pause("errored", _push())
    await run.decide(_card("errored-0-0"), "approved")
    await run.fail("errored", "Tool approval resume failed: the model provider returned HTTP 502.")
    for name, reason in (
        ("stopped", "cancelled_by_user"),
        ("sync-restart", "sync_restart_cancelled"),
        ("interrupted", "interrupted"),
        ("shutdown", INTERRUPTED_FAILURE_REASON),
    ):
        continuation = await run.responder.approvals.approval_continuation(name)
        assert continuation is not None
        assert (continuation.state, continuation.failure_reason) == ("failing", reason)


@_scenario("07_chained_generation")
async def _chained_generation(run: _Recording) -> None:
    """A resumed run that paused again: its second generation waits for its own card, the first stays decided."""
    await run.pause("chained", _push())
    await run.decide(_card("chained-0-0"), "approved")
    claimed = await run.claim("chained")
    run.clock.advance()
    plan = await run.plan(_call("call-save", "save_file", file_name="RELEASE.md", contents="Released.\n"))
    advanced = await advance_continuation(
        run.responder,
        "chained",
        claimant_generation=claimed.generation,
        run_id="run-chained-2",
        session_id=claimed.session_id,
        calls=plan.calls,
    )
    assert advanced is not None
    chained = await run.publish(advanced, plan)
    assert (chained.state, chained.generation) == ("waiting", 1)
    assert [call.decision for call in chained.earlier_calls] == [ApprovalDecision.APPROVED]
    assert await _card_on_matrix(run, "chained-1-0")


@_scenario("08_denied_edit_unacknowledged")
async def _denied_edit_unacknowledged(run: _Recording) -> None:
    """Cards the requester denied whose terminal edit Matrix has not acknowledged: one never sent, one refused."""
    await run.pause("denied-unsent", _push())
    # The decision committed and the process stopped before the edit went out.
    run.clock.advance()
    recorded = await run.router.approvals.resolve_continuation_approval_card(
        card_event_id=_card("denied-unsent-0-0"),
        requested_status="denied",
        reason="Not on a Friday.",
        metadata=ApprovalDecisionMetadata(resolved_by=REQUESTER, resolved_at=run.clock.now().isoformat()),
    )
    assert recorded.recorded
    await run.pause("denied-refused", _push())
    run.refused_sends.add(("denied-refused-0-0", DeliveryStage.FINAL))
    await run.decide(_card("denied-refused-0-0"), "denied", reason="Not on a Friday.")
    for delivery_id in ("denied-unsent-0-0", "denied-refused-0-0"):
        edit = await run.router.load_matrix_delivery(delivery_id=delivery_id, stage=DeliveryStage.FINAL)
        assert edit is not None
        assert edit.acknowledged_event_id is None
    assert await _call_decisions(run, "denied-unsent") == [ApprovalDecision.DENIED]


@_scenario("09_retired_card")
async def _retired_card(run: _Recording) -> None:
    """A card the requester denied whose edit Matrix acknowledged, retired to its tombstone with its delivery rows."""
    await run.pause("retired", _push())
    await run.decide(_card("retired-0-0"), "denied", reason="Use a pull request instead.")
    assert await _retired(run, "retired-0-0")
    assert await _call_decisions(run, "retired") == [ApprovalDecision.DENIED]


@_scenario("10_background_script_pending")
async def _background_script_pending(run: _Recording) -> None:
    """A background script's call waiting for its approval card, which Matrix acknowledged."""
    waiter = await run.detached_call(
        "script-7d3f0c2a9b8e4f61a5c2d0e9b7a6f4c1",
        "call-1",
        "add",
        {"a": 6, "b": 7},
        timed=False,
    )
    await _stop_waiter(waiter)


@_scenario("11_tool_job_calls")
async def _tool_job_calls(run: _Recording) -> None:
    """Background jobs' calls: one waiting for its card, one the requester denied, its card retired."""
    pending = await run.detached_call(
        "tool-job:job-pending",
        "call-pending",
        "run_shell_command",
        {"args": "make deploy"},
        timed=True,
    )
    denied = await run.detached_call(
        "tool-job:job-denied",
        "call-denied",
        "run_shell_command",
        {"args": "make clean"},
        timed=True,
    )
    await run.decide(_card("script-approval:tool-job:job-denied:call-denied"), "denied", reason="Keep the build cache.")
    decision = await asyncio.wait_for(denied, timeout=10)
    assert decision.status == "denied"
    assert await _retired(run, "script-approval:tool-job:job-denied:call-denied")
    await _stop_waiter(pending)


@_scenario("12_scheduled_calls")
async def _scheduled_calls(run: _Recording) -> None:
    """Scheduled calls: pending, approved for any arguments, armed, consumed with its retired receipt, and withdrawn."""
    await run.schedule("task-pending")
    await run.schedule("task-approved", any_arguments_offered=True)
    await run.decide(_card("scheduled-approval:task-approved"), "approved", scheduled_scope=ANY_ARGUMENTS)
    await run.schedule("task-armed")
    await run.decide(_card("scheduled-approval:task-armed"), "approved")
    await run.arm("task-armed")
    await run.schedule("task-consumed")
    await run.decide(_card("scheduled-approval:task-consumed"), "approved")
    await run.arm("task-consumed")
    run.clock.advance()
    call = await run.manager.scheduled_call("task-consumed")
    assert call is not None
    claimed = await run.manager.claim_scheduled_call(
        call,
        arguments_json=call.arguments_json,
        approver_user_id=REQUESTER,
    )
    assert isinstance(claimed, ScheduledCallClaim)
    run.clock.advance()
    await run.manager.record_scheduled_call_outcome("task-consumed", "completed")
    run.clock.advance()
    await run.router.approvals.maintain_automatic_approvals()
    assert await _retired(run, "scheduled-receipt:task-consumed")
    await run.schedule("task-withdrawn")
    run.clock.advance()
    await run.manager.withdraw_scheduled_call_approval("task-withdrawn", reason=_SCHEDULE_CANCELLED_REASON)
    assert await _retired(run, "scheduled-approval:task-withdrawn")


@_scenario("13_timed_grants")
async def _timed_grants(run: _Recording) -> None:
    """Timed grants: one batched and applied to later calls, with one live and one retired receipt, and one revoked."""
    await run.pause("grant-origin", _push())
    await run.pause("grant-batched", _push())
    # Approving for ten minutes also approves the other card waiting in the same scope.
    await run.decide(_card("grant-origin-0-0"), "approved", seconds=600)
    assert await _call_decisions(run, "grant-batched") == [ApprovalDecision.APPROVED]
    assert await _retired(run, "grant-batched-0-0")
    # Later calls in the scope are approved as they pause, each with a receipt instead of a card.
    await run.pause("receipt-retired", _push())
    run.clock.advance()
    await run.router.approvals.maintain_automatic_approvals()
    assert await _retired(run, "receipt-retired-0-0")
    receipted = await run.pause("receipt-live", _push())
    assert receipted.state == "ready"
    receipt = await run.router.load_matrix_delivery(delivery_id="receipt-live-0-0", stage=DeliveryStage.INITIAL)
    assert receipt is not None
    assert receipt.acknowledged_event_id == _card("receipt-live-0-0")
    # A grant in another conversation, which the requester revoked.
    await run.pause("revoked", _push(), thread_id=OTHER_THREAD)
    await run.decide(_card("revoked-0-0"), "approved", seconds=600)
    grant = await run.router.approvals.approval_grant_for_card(room_id=ROOM, card_event_id=_card("revoked-0-0"))
    assert grant is not None
    run.clock.advance()
    revoked = await run.manager.handle_grant_revocation(
        room_id=ROOM,
        sender_id=REQUESTER,
        card_event_id=_card("revoked-0-0"),
        grant_id=grant.grant_id,
        authorize_responder=lambda _agent: True,
        before_consume=None,
    )
    assert revoked.consumed
    revocation = await run.router.load_matrix_delivery(
        delivery_id=f"approval-grant-revoked:{grant.grant_id}",
        stage=DeliveryStage.FINAL,
    )
    assert revocation is not None
    assert revocation.acknowledged_event_id is not None


@_scenario("14_expired_calls")
async def _expired_calls(run: _Recording) -> None:
    """Calls whose deadline passed before anyone answered: a paused reply's and a background job's, expired by the sweep."""
    paused = await run.pause("expired", _push())
    waiter = await run.detached_call(
        "tool-job:job-expired",
        "call-expired",
        "run_shell_command",
        {"args": "make release"},
        timed=True,
    )
    await _stop_waiter(waiter)
    run.clock.advance((paused.calls[0].expires_at_ns - run.clock.now_ns) // SECOND_NS + 1)
    await run.manager.recover_cards_on_startup()
    assert await _call_decisions(run, "expired") == [ApprovalDecision.EXPIRED]
    decision = await run.router.approvals.background_approval_decision(
        run_id="tool-job:job-expired",
        call_id="call-expired",
    )
    assert decision is not None
    assert decision.status == "expired"
    assert await _retired(run, "expired-0-0")


def _schema_sql() -> str:
    statements = (textwrap.dedent(statement).strip() + ";" for statement in schema_statements(SQLITE_DIALECT))
    header = "-- The event-journal schema these recordings were written under: schema_statements(SQLITE_DIALECT).\n\n"
    return header + "\n\n".join(statements) + "\n"


def _dump(database: Path) -> str:
    """Return every row of every journal table as INSERT statements, in schema order and then column order."""
    statements = []
    with closing(sqlite3.connect(database)) as connection:
        for table in _TABLES:
            columns = [str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table}")')]
            values = " || ', ' || ".join(f'quote("{column}")' for column in columns)
            order = ", ".join(f'"{column}"' for column in columns)
            names = ", ".join(columns)
            # Table and column names come from the schema; SQLite's ``quote`` spells each value as a literal.
            rows = connection.execute(f'SELECT {values} FROM "{table}" ORDER BY {order}').fetchall()  # noqa: S608
            statements.extend(f"INSERT INTO {table} ({names}) VALUES ({row});" for (row,) in rows)  # noqa: S608
    return "\n".join(statements) + "\n"


async def _read_back(store: EventJournalStore, database: Path) -> None:
    """Read every continuation, card, grant, and scheduled call the recording holds through the store's own readers."""
    with closing(sqlite3.connect(database)) as connection:
        principals = sorted({str(row[0]) for row in connection.execute("SELECT principal_id FROM room_membership")})
        grants = connection.execute(
            "SELECT principal_id, room_id, card_event_id, grant_id FROM approval_grants",
        ).fetchall()
        tasks = connection.execute("SELECT principal_id, task_id FROM scheduled_call_approvals").fetchall()
        detached = connection.execute("SELECT principal_id, run_id, call_id FROM background_approval_calls").fetchall()
    for principal_id, continuation in await store.approval_continuations(limit=1_000):
        assert (
            await store.principal(principal_id).approvals.approval_continuation(continuation.approval_id)
            == continuation
        )
    for principal_id in principals:
        principal = store.principal(principal_id)
        await principal.approvals.pending_approvals()
        for room_id in await principal.approvals.pending_approval_room_ids():
            cards = await principal.approvals.pending_approval_cards(room_id=room_id)
            assert not [card for card in cards if isinstance(card, UnreadableApprovalCard)]
    for principal_id, room_id, card_event_id, grant_id in grants:
        grant = await store.principal(principal_id).approvals.approval_grant_for_card(
            room_id=room_id,
            card_event_id=card_event_id,
        )
        assert grant is not None
        assert grant.grant_id == grant_id
    for principal_id, task_id in tasks:
        assert await store.principal(principal_id).approvals.scheduled_call(task_id=task_id) is not None
    for principal_id, run_id, call_id in detached:
        await store.principal(principal_id).approvals.background_approval_decision(run_id=run_id, call_id=call_id)


async def _assert_opens_cleanly(fixture: Path, database: Path, *, recorded: str) -> None:
    """Load one fixture into a fresh database, open it with the journal, and read it back without changing a row."""
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(_schema_sql())
        connection.executescript(fixture.read_text(encoding="utf-8"))
    assert _dump(database) == recorded
    store = EventJournalStore.open_sqlite(database)
    try:
        await _read_back(store, database)
    finally:
        await store.close()
    assert _dump(database) == recorded


def test_record_schema() -> None:
    """Write the schema the recordings belong to."""
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "schema.sql").write_text(_schema_sql(), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(SCENARIOS))
async def test_record(name: str, recording: _Recording, tmp_path: Path) -> None:
    """Record one scenario's rows, then prove the journal opens and reads them back unchanged."""
    record = SCENARIOS[name]
    await record(recording)
    await recording.close()
    recorded = _dump(recording.database)
    description = (record.__doc__ or "").strip().splitlines()[0]
    FIXTURES.mkdir(parents=True, exist_ok=True)
    fixture = FIXTURES / f"{name}.sql"
    fixture.write_text(f"-- {description}\n{recorded}", encoding="utf-8")
    await _assert_opens_cleanly(fixture, tmp_path / "reloaded.db", recorded=recorded)
