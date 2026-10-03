"""Shrinkable generated conversations across held messages, wakes, Stops, failed continuations, and process losses."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule, run_state_machine_as_test

from mindroom.response_runner import ResponseRunner
from tests.tool_job_held_reply_fuzz import HeldReplyFuzzRunner, Step

if TYPE_CHECKING:
    from collections.abc import Coroutine

_INDEXES = st.integers(0, 3)


async def _runner(root: Path, patch: pytest.MonkeyPatch) -> HeldReplyFuzzRunner:
    runner = HeldReplyFuzzRunner(root, patch)
    await runner.open()
    return runner


class HeldReplies(RuleBasedStateMachine):
    """Steps on one agent's conversation, each followed by invariant checks."""

    def __init__(self) -> None:
        super().__init__()
        self._directory = tempfile.TemporaryDirectory(prefix="held-reply-fuzz-")
        self._loop = asyncio.new_event_loop()
        self._patch = pytest.MonkeyPatch()
        self.runner = self._run(_runner(Path(self._directory.name), self._patch))

    def _run[Result](self, coroutine: Coroutine[object, object, Result]) -> Result:
        return self._loop.run_until_complete(coroutine)

    def _step(self, kind: str, **options: object) -> None:
        self._run(self.runner.step(Step(kind, **options)))  # type: ignore[arg-type]

    @precondition(lambda self: len(self.runner.model.sources) < 12)
    @rule(jobs=st.integers(0, 2), hold=st.booleans())
    def message(self, *, jobs: int, hold: bool) -> None:
        """A message this agent answers, whose reply may start jobs."""
        self._step("message", jobs=jobs, hold=hold)

    @rule()
    def silenced(self) -> None:
        """A reply that never reaches its response boundary, as when its participation check stays silent."""
        self._step("silenced")

    @precondition(lambda self: any(not gate.is_set() for gate in self.runner.gates.values()))
    @rule(index=_INDEXES)
    def release(self, index: int) -> None:
        """Let one held job finish."""
        self._step("release", index=index)

    @rule()
    def wake(self) -> None:
        """One coordinator pass over the held messages."""
        self._step("wake")

    @precondition(lambda self: len(self.runner.model.sources) < 12)
    @rule(jobs=st.integers(0, 2), hold=st.booleans())
    def race(self, *, jobs: int, hold: bool) -> None:
        """A wake and a new message whose turns queue for the conversation together."""
        self._step("race", jobs=jobs, hold=hold)

    @rule()
    def stop_held(self) -> None:
        """Stop on the held message while no turn runs on it."""
        self._step("stop_held")

    @rule()
    def stop_live(self) -> None:
        """Stop the running turn."""
        self._step("stop_live")

    @rule()
    def fail_next(self) -> None:
        """Make the next continuation fail before its response boundary."""
        self._step("fail_next")

    @rule()
    def ignore_next(self) -> None:
        """Make the model leave the next outcomes it is asked to retrieve unread."""
        self._step("ignore_next")

    @rule()
    def crash(self) -> None:
        """Tear the process down between two awaits."""
        self._step("crash")

    def teardown(self) -> None:
        """Finish the conversation, check that no work is left held or outstanding, and release storage."""
        try:
            self._run(self.runner.finish())
        finally:
            self._run(self.runner.close())
            self._loop.close()
            self._patch.undo()
            self._directory.cleanup()


@pytest.mark.timeout(900)
def test_generated_conversations_always_hold_outstanding_work() -> None:
    """No interleaving leaves outstanding work without the latest reply's message holding it, or reads it twice."""
    run_state_machine_as_test(
        HeldReplies,
        settings=settings(
            max_examples=200,
            stateful_step_count=25,
            deadline=None,
            print_blob=True,
            suppress_health_check=[HealthCheck.too_slow],
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "steps",
    [
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("release"), Step("wake")],
            id="a-wake-continues-the-held-message-with-the-result",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("message"), Step("release"), Step("wake")],
            id="a-newer-reply-takes-the-work-over",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("message", jobs=1, hold=True), Step("release", index=1)],
            id="two-replies-two-jobs-one-holder",
        ),
        pytest.param(
            [Step("message", jobs=2, hold=True), Step("release"), Step("wake"), Step("release"), Step("wake")],
            id="a-continuation-holds-what-is-still-running",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("release"), Step("race", jobs=1, hold=True), Step("wake")],
            id="a-wake-and-a-new-reply-queue-together",
        ),
        pytest.param(
            [Step("message", jobs=2, hold=True), Step("release"), Step("ignore_next"), Step("wake"), Step("wake")],
            id="an-unread-outcome-is-not-offered-again-by-wakes",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("crash"), Step("wake")],
            id="a-restart-wakes-the-held-message-with-the-interruption",
        ),
        pytest.param(
            [Step("message", jobs=2, hold=True), Step("stop_held"), Step("wake")],
            id="stop-on-the-held-message-ends-its-work",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("fail_next"), Step("release"), Step("wake")],
            id="a-failed-continuation-leaves-the-work-for-the-next-reply",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("silenced"), Step("release"), Step("wake")],
            id="a-silenced-reply-leaves-the-hold-alone",
        ),
    ],
)
async def test_held_reply_regressions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: list[Step]) -> None:
    """Interleavings that exercise each hand-over keep holding the invariants."""
    runner = await _runner(tmp_path, monkeypatch)
    try:
        await runner.run(steps)
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_held_reply_oracle_detects_work_no_message_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fuzzer must fail when a reply ends without its message holding its running work."""

    async def never_hold(self: ResponseRunner, *_args: object, **_kwargs: object) -> None:
        del self

    monkeypatch.setattr(ResponseRunner, "_save_held_reply", never_hold)
    runner = await _runner(tmp_path, monkeypatch)
    try:
        with pytest.raises(AssertionError):
            await runner.step(Step("message", jobs=1, hold=True))
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_held_reply_oracle_detects_a_message_left_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fuzzer must fail when a newer reply takes the work over but the older message keeps its notice."""

    async def keep_waiting(self: ResponseRunner, *_args: object, **_kwargs: object) -> None:
        del self

    monkeypatch.setattr(ResponseRunner, "_release_held_message", keep_waiting)
    runner = await _runner(tmp_path, monkeypatch)
    try:
        await runner.step(Step("message", jobs=1, hold=True))
        with pytest.raises(AssertionError):
            await runner.step(Step("message", jobs=1, hold=True))
    finally:
        await runner.close()
