"""Shrinkable generated conversations across follow-ups, other agents' messages, Stop, and process losses."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule, run_state_machine_as_test

from mindroom.tool_jobs import completion
from tests import tool_job_reply_hold_fuzz
from tests.tool_job_reply_hold_fuzz import ReplyHoldFuzzRunner, Step

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine

    from mindroom.tool_system.events import BackgroundWaitChunk

_INDEXES = st.integers(0, 3)


async def _runner(root: Path, patch: pytest.MonkeyPatch) -> ReplyHoldFuzzRunner:
    runner = ReplyHoldFuzzRunner(root, patch)
    await runner.open()
    return runner


class ReplyHold(RuleBasedStateMachine):
    """Steps on one agent's conversation, each followed by invariant checks."""

    def __init__(self) -> None:
        super().__init__()
        self._directory = tempfile.TemporaryDirectory(prefix="reply-hold-fuzz-")
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
        """A human message this agent answers, whose reply may start jobs."""
        self._step("message", jobs=jobs, hold=hold)

    @rule()
    def other(self) -> None:
        """A human message the turn policy gives to another agent."""
        self._step("other")

    @rule(human=st.booleans())
    def quiet(self, *, human: bool) -> None:
        """A turn of this agent that never joins visible work: a silenced reply, or a silent schedule."""
        self._step("quiet", human=human)

    @precondition(lambda self: any(not gate.is_set() for gate in self.runner.gates.values()))
    @rule(index=_INDEXES)
    def release(self, index: int) -> None:
        """Let one held job finish."""
        self._step("release", index=index)

    @precondition(lambda self: self.runner.model.replies)
    @rule()
    def stop(self) -> None:
        """Stop the latest reply."""
        self._step("stop")

    @rule()
    def crash(self) -> None:
        """Tear the process down between two awaits."""
        self._step("crash")

    def teardown(self) -> None:
        """Finish the conversation, check that no work is left unheld, and release storage."""
        try:
            self._run(self.runner.finish())
        finally:
            self._run(self.runner.close())
            self._loop.close()
            self._patch.undo()
            self._directory.cleanup()


@pytest.mark.timeout(900)
def test_generated_conversations_always_hold_outstanding_work() -> None:
    """No interleaving leaves outstanding work without the latest reply holding it, or retrieves an outcome twice."""
    run_state_machine_as_test(
        ReplyHold,
        settings=settings(
            max_examples=80,
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
            [Step("message", jobs=1, hold=True), Step("other"), Step("release")],
            id="another-agents-message-leaves-the-reply-holding",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("message"), Step("release")],
            id="a-newer-reply-takes-over-earlier-work",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("crash"), Step("message")],
            id="the-next-reply-after-a-restart-retrieves-interrupted-work",
        ),
        pytest.param(
            [Step("message", jobs=1, hold=True), Step("quiet"), Step("quiet", human=False), Step("release")],
            id="silenced-replies-and-silent-schedules-run-while-the-reply-keeps-holding",
        ),
        pytest.param(
            [Step("message", jobs=2, hold=True), Step("message", jobs=1, hold=True), Step("stop"), Step("message")],
            id="stop-on-the-holding-reply-ends-all-earlier-work",
        ),
    ],
)
async def test_reply_hold_regressions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: list[Step]) -> None:
    """Interleavings that exercise each hand-over keep holding the invariants."""
    runner = await _runner(tmp_path, monkeypatch)
    try:
        await runner.run(steps)
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_reply_hold_oracle_detects_a_reply_that_keeps_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fuzzer must fail when a waiting reply keeps other turns of its conversation waiting."""

    @asynccontextmanager
    async def kept() -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(completion, "released_while_waiting", kept)
    runner = await _runner(tmp_path, monkeypatch)
    try:
        await runner.step(Step("message", jobs=1, hold=True))
        with pytest.raises(AssertionError, match="waited for held background work"):
            await runner.step(Step("quiet"))
    finally:
        await runner.close()


@pytest.mark.asyncio
async def test_reply_hold_oracle_detects_an_unheld_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fuzzer must fail when a reply ends without holding its conversation's running work."""

    async def no_join(*_args: object, **_kwargs: object) -> AsyncIterator[BackgroundWaitChunk]:
        return
        yield

    monkeypatch.setattr(tool_job_reply_hold_fuzz, "join_conversation_jobs", no_join)
    runner = await _runner(tmp_path, monkeypatch)
    try:
        with pytest.raises(AssertionError):
            await runner.step(Step("message", jobs=1, hold=True))
    finally:
        await runner.close()
