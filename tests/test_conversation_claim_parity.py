"""One set of ownership scenarios, run by a reply and by compaction's lock-only claim.

Compaction takes its place in a conversation through the claim a reply takes, so every scenario here runs both and
expects one outcome. The outcomes that differ by design are named on ``_Kind``; a change to how a reply owns its
conversation therefore fails the compaction case here instead of leaving it behind.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

import pytest

from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.event_journal.approval_signals import PausedReply
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_lifecycle import response_lifecycle_reservation_context
from mindroom.response_sources import Owned
from tests.conftest import unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _envelope, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine
    from pathlib import Path

    from mindroom.bot_runtime_view import BotRuntimeView, RunPhase
    from mindroom.message_target import MessageTarget
    from mindroom.response_admission import ResponseAdmissionGate
    from mindroom.response_lifecycle import ResponseLifecycleCoordinator, _Conversation
    from mindroom.response_runner import ResponseRunner

_TABLE_CAP = 100


@dataclass(frozen=True)
class _Kind:
    """One way to claim a conversation, and the only outcomes in which it may differ from the other."""

    name: str
    # A begun turn makes the conversation busy, tells the running reply a message waits, and binds its requester.
    begins_turn: bool
    # A pending approval keeps the claim out, and off admission, until the approval ends.
    waits_out_approvals: bool
    # Past admission a reply is owned work, which a stopping runtime drains; a fold's source stays pending and replays.
    starts_while_stopping: bool


_REPLY = _Kind("reply", begins_turn=True, waits_out_approvals=False, starts_while_stopping=True)
_COMPACTION = _Kind("compaction", begins_turn=False, waits_out_approvals=True, starts_while_stopping=False)

_both_kinds = pytest.mark.parametrize("kind", [_REPLY, _COMPACTION], ids=lambda kind: kind.name)
_both_stops = pytest.mark.parametrize(
    "stop",
    [BotRuntimeState.begin_draining, BotRuntimeState.begin_process_shutdown],
    ids=["draining", "shutting_down"],
)


@dataclass
class _Body:
    """What one claim runs under the lock; it checks what it owns as it starts."""

    scene: _Scene
    name: str
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    finish: asyncio.Event = field(default_factory=asyncio.Event)
    runs: int = 0
    error: Exception | None = None

    async def __call__(self) -> Owned:
        scene = self.scene
        self.runs += 1
        scene.order.append(self.name)
        scene.in_body += 1
        try:
            assert scene.in_body == 1, "two claims ran under one conversation's lock at once"
            assert scene.record.lock.locked(), "the body ran without the conversation's lock"
            assert scene.gate.in_flight_response_count >= 1, "the body ran without admission"
            self.entered.set()
            await self.finish.wait()
            if self.error is not None:
                raise self.error
        finally:
            scene.in_body -= 1
        return Owned()


@dataclass
class _Scene:
    """One conversation of a real bot, and the claims a scenario makes on it."""

    kind: _Kind
    runner: ResponseRunner
    target: MessageTarget = field(default_factory=lambda: _target(thread_id="$thread"))
    order: list[str] = field(default_factory=list)
    in_body: int = 0
    tasks: list[asyncio.Task[object]] = field(default_factory=list)
    _crowd: int = 0

    @property
    def coordinator(self) -> ResponseLifecycleCoordinator:
        return self.runner.lifecycle_coordinator

    @property
    def runtime(self) -> BotRuntimeState:
        return cast("BotRuntimeState", self.runner.deps.runtime)

    @property
    def gate(self) -> ResponseAdmissionGate:
        return self.runtime.response_admission_gate

    @property
    def record(self) -> _Conversation:
        return self.coordinator._conversation_for(self.target)

    def body(self, name: str, *, finished: bool = False) -> _Body:
        body = _Body(self, name)
        if finished:
            body.finish.set()
        return body

    def _start(self, claim: Coroutine[None, None, object]) -> asyncio.Task[object]:
        task = asyncio.create_task(claim)
        self.tasks.append(task)
        return task

    def reply(self, body: _Body) -> asyncio.Task[object]:
        """Start a reply as the runner starts one: admitted, then locked."""
        request = _plain_request(self.target, source_event_id=f"${body.name}")
        return self._start(
            self.runner._run_locked_response_lifecycle(request, response_kind="ai", locked_operation=body),
        )

    def claim(self, body: _Body, *, runtime: BotRuntimeView | None = None) -> asyncio.Task[object]:
        """Start the claim under test: a reply, or the lock-only claim compaction makes."""
        if self.kind is _REPLY:
            return self.reply(body)
        return self._start(self.coordinator.run_locked(self.target, runtime or self.runtime, body))

    async def running_reply(self) -> tuple[_Body, asyncio.Task[object]]:
        """Start a reply and return once it runs under the lock."""
        body = self.body("running")
        task = self.reply(body)
        await asyncio.wait_for(body.entered.wait(), 10)
        return body, task

    async def queued_for_lock(self, count: int) -> None:
        """Return once ``count`` claims wait for the conversation's lock."""
        async with asyncio.timeout(10):
            while len(self.record.lock._waiters or ()) != count:  # noqa: ASYNC110
                await asyncio.sleep(0)

    def hold_for_approval(self) -> None:
        principal_id = self.runner.deps.replies.store.principal_id
        paused = PausedReply(principal_id, "approval", self.target.room_id, self.target.resolved_thread_id)
        self.coordinator._hold_for_approval(paused)

    def end_approval(self) -> None:
        self.coordinator._release_approval_hold("approval")

    def crowd(self) -> None:
        """Make more conversations than the table keeps, so every record nothing protects is dropped."""
        for _ in range(_TABLE_CAP + 5):
            self._crowd += 1
            self.coordinator._conversation_for(_target(thread_id=f"$other{self._crowd}"))
        assert len(self.coordinator._conversations) <= _TABLE_CAP

    def assert_released(self) -> None:
        """Nothing is left of the claims: no lock, admission, begun turn, waiting message or pin."""
        record = self.record
        assert not record.lock.locked()
        assert self.gate.in_flight_response_count == 0
        assert not self.coordinator.has_active_response_for_target(self.target)
        assert record.pending_human_messages == 0
        assert record.pins == 0


@asynccontextmanager
async def _conversation(kind: _Kind, tmp_path: Path) -> AsyncIterator[_Scene]:
    scene = _Scene(kind, unwrap_extracted_collaborator(_bot(tmp_path)._response_runner))
    try:
        yield scene
    finally:
        for task in scene.tasks:
            task.cancel()
        await asyncio.gather(*scene.tasks, return_exceptions=True)


async def _settle() -> None:
    """Give every task time to run as far as it can without the test's help, store reads included."""
    await asyncio.sleep(0.05)


async def _finish(scene: _Scene, kind: _Kind, body: _Body, claim: asyncio.Task[object]) -> None:
    """Expect a claim made while the runtime stops to start only if its kind drains, and to leave nothing behind."""
    if kind.starts_while_stopping:
        await asyncio.wait_for(claim, 10)
    else:
        with pytest.raises(ResponseAdmissionRefusedError):
            await asyncio.wait_for(claim, 10)
    assert body.runs == int(kind.starts_while_stopping)
    scene.assert_released()


@_both_kinds
@_both_stops
@pytest.mark.asyncio
async def test_stop_while_waiting_for_admission_refuses_the_claim(
    kind: _Kind,
    stop: Callable[[BotRuntimeState], None],
    tmp_path: Path,
) -> None:
    """A claim that waits for a runtime replacement to finish is refused once its runtime stops."""
    async with _conversation(kind, tmp_path) as scene:
        assert scene.gate.close_if_idle()
        body = scene.body("claim", finished=True)
        claim = scene.claim(body)
        await _settle()
        assert not claim.done()
        stop(scene.runtime)
        with pytest.raises(ResponseAdmissionRefusedError):
            await asyncio.wait_for(claim, 10)
        assert body.runs == 0
        scene.assert_released()


@_both_kinds
@_both_stops
@pytest.mark.asyncio
async def test_stop_before_an_uncontended_claim(
    kind: _Kind,
    stop: Callable[[BotRuntimeState], None],
    tmp_path: Path,
) -> None:
    """With the lock free and admission open, only owned work still starts once the runtime stopped accepting."""
    async with _conversation(kind, tmp_path) as scene:
        stop(scene.runtime)
        body = scene.body("claim", finished=True)
        await _finish(scene, kind, body, scene.claim(body))


@_both_kinds
@_both_stops
@pytest.mark.asyncio
async def test_stop_while_queued_for_the_lock(
    kind: _Kind,
    stop: Callable[[BotRuntimeState], None],
    tmp_path: Path,
) -> None:
    """A runtime that stops while the claim waits behind a running reply is seen under the lock, before the body."""
    async with _conversation(kind, tmp_path) as scene:
        running, reply = await scene.running_reply()
        body = scene.body("claim", finished=True)
        claim = scene.claim(body)
        await scene.queued_for_lock(1)
        stop(scene.runtime)
        running.finish.set()
        await asyncio.wait_for(reply, 10)
        await _finish(scene, kind, body, claim)


@_both_kinds
@pytest.mark.asyncio
async def test_cancel_while_queued_for_the_lock_leaves_the_running_reply_its_lock(kind: _Kind, tmp_path: Path) -> None:
    """A claim cancelled in the queue never runs and takes nothing from the reply that holds the lock."""
    async with _conversation(kind, tmp_path) as scene:
        running, reply = await scene.running_reply()
        body = scene.body("claim", finished=True)
        claim = scene.claim(body)
        await scene.queued_for_lock(1)
        claim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await claim
        assert body.runs == 0
        assert scene.record.lock.locked()
        assert scene.gate.in_flight_response_count == 1
        assert scene.record.pending_human_messages == 0
        running.finish.set()
        await asyncio.wait_for(reply, 10)
        scene.assert_released()


@_both_kinds
@pytest.mark.asyncio
async def test_cancel_in_the_body_frees_the_conversation_at_once(kind: _Kind, tmp_path: Path) -> None:
    """A claim cancelled in its body has released the lock and admission by the time it returns."""
    async with _conversation(kind, tmp_path) as scene:
        body = scene.body("claim")
        claim = scene.claim(body)
        await asyncio.wait_for(body.entered.wait(), 10)
        claim.cancel()
        with pytest.raises(asyncio.CancelledError):
            await claim
        scene.assert_released()
        again = scene.body("again", finished=True)
        await asyncio.wait_for(scene.claim(again), 10)
        assert scene.order == ["claim", "again"]
        scene.assert_released()


@_both_kinds
@pytest.mark.asyncio
async def test_approval_hold_before_the_claim(kind: _Kind, tmp_path: Path) -> None:
    """A pending approval keeps lock-only work out, off admission and off the lock, until the approval ends."""
    async with _conversation(kind, tmp_path) as scene:
        scene.hold_for_approval()
        body = scene.body("claim")
        claim = scene.claim(body)
        if kind.waits_out_approvals:
            await _settle()
            assert body.runs == 0
            assert not claim.done()
            # A runtime replacement can still close admission, and the approval's own resume can still take the lock.
            assert scene.gate.in_flight_response_count == 0
            assert not scene.record.lock.locked()
            scene.end_approval()
        await asyncio.wait_for(body.entered.wait(), 10)
        assert scene.gate.in_flight_response_count == 1
        body.finish.set()
        await asyncio.wait_for(claim, 10)
        assert body.runs == 1
        scene.end_approval()
        scene.assert_released()


@_both_kinds
@pytest.mark.asyncio
async def test_approval_hold_that_appears_while_the_claim_is_queued(kind: _Kind, tmp_path: Path) -> None:
    """A reply that pauses for approval ahead of lock-only work makes it give up the lock and admission, then return."""
    async with _conversation(kind, tmp_path) as scene:
        running, reply = await scene.running_reply()
        body = scene.body("claim")
        claim = scene.claim(body)
        await scene.queued_for_lock(1)
        assert scene.gate.in_flight_response_count == 2
        scene.hold_for_approval()
        running.finish.set()
        await asyncio.wait_for(reply, 10)
        if kind.waits_out_approvals:
            await _settle()
            assert body.runs == 0
            assert not claim.done()
            assert scene.gate.in_flight_response_count == 0
            assert not scene.record.lock.locked()
            scene.end_approval()
        # The body checks, as it starts, that it holds the lock and one admission after waiting a second time.
        await asyncio.wait_for(body.entered.wait(), 10)
        assert scene.gate.in_flight_response_count == 1
        body.finish.set()
        await asyncio.wait_for(claim, 10)
        assert body.runs == 1
        scene.end_approval()
        scene.assert_released()


@_both_kinds
@pytest.mark.asyncio
async def test_eviction_pressure_while_the_claim_waits_keeps_one_lock(kind: _Kind, tmp_path: Path) -> None:
    """More conversations than the table keeps never give a waiting or running claim's conversation a second lock."""
    async with _conversation(kind, tmp_path) as scene:
        assert scene.gate.close_if_idle()
        body = scene.body("claim")
        claim = scene.claim(body)
        await _settle()
        assert body.runs == 0
        scene.crowd()
        scene.gate.reopen()
        await asyncio.wait_for(body.entered.wait(), 10)
        scene.crowd()
        later = scene.body("later", finished=True)
        reply = scene.reply(later)
        await scene.queued_for_lock(1)
        assert later.runs == 0
        body.finish.set()
        await asyncio.wait_for(asyncio.gather(claim, reply), 10)
        assert scene.order == ["claim", "later"]
        scene.assert_released()


@_both_kinds
@pytest.mark.asyncio
async def test_claims_take_the_lock_in_the_order_they_queued(kind: _Kind, tmp_path: Path) -> None:
    """Behind a running reply, the claim and a later reply run first in, first out."""
    async with _conversation(kind, tmp_path) as scene:
        running, reply = await scene.running_reply()
        body = scene.body("claim")
        claim = scene.claim(body)
        await scene.queued_for_lock(1)
        later = scene.body("later", finished=True)
        later_reply = scene.reply(later)
        await scene.queued_for_lock(2)
        running.finish.set()
        await asyncio.wait_for(body.entered.wait(), 10)
        await _settle()
        assert later.runs == 0
        body.finish.set()
        await asyncio.wait_for(asyncio.gather(reply, claim, later_reply), 10)
        assert scene.order == ["running", "claim", "later"]
        scene.assert_released()


@_both_kinds
@pytest.mark.asyncio
async def test_only_a_reply_begins_a_turn(kind: _Kind, tmp_path: Path) -> None:
    """Lock-only work holds the lock a reply holds, yet is no turn: not busy, no notice, no requester."""
    async with _conversation(kind, tmp_path) as scene:
        running, reply = await scene.running_reply()
        body = scene.body("claim")
        claim = scene.claim(body)
        await scene.queued_for_lock(1)
        assert scene.record.pending_human_messages == int(kind.begins_turn)
        running.finish.set()
        await asyncio.wait_for(reply, 10)
        await asyncio.wait_for(body.entered.wait(), 10)
        assert scene.coordinator.has_active_response_for_target(scene.target) is kind.begins_turn
        assert scene.record.active_requester_id == ("@user:localhost" if kind.begins_turn else None)
        body.finish.set()
        await asyncio.wait_for(claim, 10)
        scene.assert_released()


# What reviews of an earlier lock-only helper found; each is ruled out by how ``run_locked`` is written.


@pytest.mark.asyncio
async def test_lock_only_work_queues_behind_a_reply_reservation_instead_of_using_it(tmp_path: Path) -> None:
    """Lock-only work started where a reply's reservation is current still takes the lock itself, in its turn."""
    async with _conversation(_COMPACTION, tmp_path) as scene:
        reservation = await scene.coordinator.reserve_response_lifecycle(_envelope(scene.target))
        body = scene.body("claim", finished=True)
        with response_lifecycle_reservation_context(reservation):
            claim = scene.claim(body)
        await scene.queued_for_lock(1)
        assert body.runs == 0
        assert not reservation._consumed
        await reservation.release()
        await asyncio.wait_for(claim, 10)
        assert body.runs == 1
        scene.assert_released()


@pytest.mark.asyncio
async def test_stop_as_the_approval_wait_ends_keeps_the_body_from_starting(tmp_path: Path) -> None:
    """The approval wait's answer never starts the body: a stop that lands while it cleans up is seen under the lock."""
    async with _conversation(_COMPACTION, tmp_path) as scene:
        runtime = scene.runtime

        class _StopsWhenCancelled(asyncio.Event):
            async def wait(self) -> Literal[True]:
                try:
                    return await super().wait()
                except asyncio.CancelledError:
                    runtime.begin_draining()
                    raise

        runtime._stopping = _StopsWhenCancelled()
        scene.hold_for_approval()
        body = scene.body("claim", finished=True)
        claim = scene.claim(body)
        await _settle()
        scene.end_approval()
        with pytest.raises(ResponseAdmissionRefusedError):
            await asyncio.wait_for(claim, 10)
        assert body.runs == 0
        scene.assert_released()


@dataclass
class _SwallowingRuntime:
    """A runtime whose admission swallows whatever is raised while it is held, which the real one never does."""

    runtime: BotRuntimeState
    resume_on_exit: bool = False
    admissions: int = 0

    @property
    def phase(self) -> RunPhase:
        return self.runtime.phase

    async def wait_for_admission_or_shutdown(self, ready: Coroutine[None, None, object] | None = None) -> bool:
        return await self.runtime.wait_for_admission_or_shutdown(ready)

    @asynccontextmanager
    async def admission_fence(self) -> AsyncIterator[None]:
        self.admissions += 1
        assert self.admissions == 1, "lock-only work was admitted a second time"
        async with self.runtime.admission_fence():
            with suppress(BaseException):
                yield
            if self.resume_on_exit:
                self.runtime.resume()


@pytest.mark.parametrize("failure", ["body_error", "cancelled", "stopped", "stopped_then_resumed"])
@pytest.mark.asyncio
async def test_admission_that_swallows_an_error_is_never_entered_twice(failure: str, tmp_path: Path) -> None:
    """Whatever ends lock-only work ends the call: it admits a second time only for an approval that appeared."""
    async with _conversation(_COMPACTION, tmp_path) as scene:
        runtime = _SwallowingRuntime(scene.runtime, resume_on_exit=failure == "stopped_then_resumed")
        running, reply = await scene.running_reply()
        body = scene.body("claim", finished=True)
        if failure == "body_error":
            body.error = ValueError("boom")
        claim = scene.claim(body, runtime=cast("BotRuntimeView", runtime))
        await scene.queued_for_lock(1)
        if failure == "cancelled":
            claim.cancel()
        elif failure != "body_error":
            scene.runtime.begin_draining()
        running.finish.set()
        await asyncio.wait_for(reply, 10)
        with pytest.raises(ResponseAdmissionRefusedError):
            await asyncio.wait_for(claim, 10)
        await _settle()
        assert runtime.admissions == 1
        assert body.runs == int(failure == "body_error")
        scene.assert_released()
