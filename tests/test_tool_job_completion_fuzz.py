"""Shrinkable generated idle completion wakes across Stop, held replies, spawned work, and process losses."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, precondition, rule, run_state_machine_as_test

from tests.tool_job_completion_fuzz import CompletionFuzzRunner, ReplyScript, Step

if TYPE_CHECKING:
    from collections.abc import Coroutine

# Every incarnation reads the turn records the previous one saved.
pytestmark = pytest.mark.ledger_loads_from_disk

_REPLIES = st.builds(
    ReplyScript,
    visible=st.sampled_from((True, True, False)),
    consume=st.sampled_from((True, True, False)),
    spawn=st.booleans(),
    hold=st.booleans(),
)
_INDEXES = st.integers(0, 3)


async def _runner(root: Path, patch: pytest.MonkeyPatch) -> CompletionFuzzRunner:
    runner = CompletionFuzzRunner(root, patch)
    await runner.open()
    return runner


class CompletionWakes(RuleBasedStateMachine):
    """Steps on one bot's conversation, each followed by invariant checks."""

    def __init__(self) -> None:
        super().__init__()
        self._directory = tempfile.TemporaryDirectory(prefix="completion-fuzz-")
        self._loop = asyncio.new_event_loop()
        self._patch = pytest.MonkeyPatch()
        self.runner = self._run(_runner(Path(self._directory.name), self._patch))

    def _run[Result](self, coroutine: Coroutine[object, object, Result]) -> Result:
        return self._loop.run_until_complete(coroutine)

    def _step(self, kind: str, **options: object) -> None:
        self._run(self.runner.step(Step(kind, **options)))  # type: ignore[arg-type]

    @precondition(lambda self: len(self.runner.model.humans) < 5)
    @rule(hold=st.booleans(), pending_human=st.sampled_from((False, False, True)))
    def job(self, *, hold: bool, pending_human: bool) -> None:
        """Start a job from a human turn that finished, or is still being answered."""
        self._step("job", hold=hold, pending_human=pending_human)

    @precondition(lambda self: not all(self.runner.model.humans.values()))
    @rule(index=_INDEXES)
    def settle_human(self, index: int) -> None:
        """Finish answering a human turn."""
        self._step("settle_human", index=index)

    @rule(reply=_REPLIES)
    def deliver(self, reply: ReplyScript) -> None:
        """Wake the conversation for each ready outcome, with replies that follow `reply`."""
        self._step("deliver", reply=reply)

    @rule(kind=st.sampled_from(("release", "retry", "crash")))
    def lifecycle(self, kind: str) -> None:
        """Release held work, hand pending wakes to the runner again, or tear the process down."""
        self._step(kind)

    @precondition(lambda self: self.runner.model.replies)
    @rule(index=_INDEXES)
    def stop(self, index: int) -> None:
        """Stop a visible reply, running or finished."""
        self._step("stop", index=index)

    def teardown(self) -> None:
        """Finish every turn, check that every wake settled, and release storage."""
        try:
            self._run(self.runner.finish())
        finally:
            self._run(self.runner.close())
            self._loop.close()
            self._patch.undo()
            self._directory.cleanup()


@pytest.mark.timeout(900)
def test_generated_completion_wakes_settle_once_and_stay_stoppable() -> None:
    """No interleaving leaves a wake unsettled, shows a second reply, reruns a reply unasked, or loses a Stop."""
    run_state_machine_as_test(
        CompletionWakes,
        settings=settings(
            max_examples=60,
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
            [Step("job"), Step("deliver", reply=ReplyScript(hold=True, spawn=True)), Step("crash")],
            id="crash-during-reply-reuses-its-placeholder",
        ),
        pytest.param(
            [Step("job"), Step("deliver", reply=ReplyScript(hold=True, spawn=True)), Step("stop")],
            id="stop-on-a-running-wake-reply-stops-its-work",
        ),
        pytest.param(
            [Step("job", pending_human=True), Step("deliver"), Step("crash"), Step("settle_human"), Step("retry")],
            id="wake-waits-for-the-human-turn-across-a-crash",
        ),
    ],
)
async def test_completion_wake_regressions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    steps: list[Step],
) -> None:
    """Interleavings that once broke an invariant keep holding it."""
    runner = await _runner(tmp_path, monkeypatch)
    try:
        await runner.run(steps)
    finally:
        await runner.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["second_reply", "rerun", "unsettled"])
async def test_completion_fuzz_oracle_detects_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    """The fuzzer must fail when a wake shows two replies, reruns unasked, or never settles."""
    runner = await _runner(tmp_path, monkeypatch)
    try:
        await runner.step(Step("job"))
        if corruption == "unsettled":
            # A runner that never takes ownership leaves the wake pending.
            monkeypatch.setattr(runner.runner, "handoff_tool_job_completion", AsyncMock(return_value=False))
            with pytest.raises(AssertionError):
                await runner.run([Step("deliver")])
            return
        await runner.step(Step("deliver"))
        (source,) = runner.model.scripts
        if corruption == "second_reply":
            runner.model.replies[source].add("$another")
        else:
            runner.model.runs[source] += 1
        with pytest.raises(AssertionError):
            await runner.check()
    finally:
        await runner.close()
