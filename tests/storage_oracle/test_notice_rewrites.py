"""The notice rewrites comparison 1 replays on its view of the old store follow the rules of the releases it models."""

from __future__ import annotations

from mindroom.history.run_visibility import QUEUED_MESSAGE_NOTICE_MARKER_KEY
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from tests.storage_oracle.backend import NOTICE_TURN_KEY
from tests.storage_oracle.notice_rewrites import finalize, recover


def _notice(turn: str, marker: object = True) -> Message:
    return Message(
        role="user",
        content="Pause.",
        provider_data={QUEUED_MESSAGE_NOTICE_MARKER_KEY: marker, NOTICE_TURN_KEY: turn},
    )


def _run(run_id: str, *messages: Message, status: RunStatus = RunStatus.completed) -> RunRecord:
    return RunRecord(run_id=run_id, agent_id="code", status=status, messages=list(messages))


def _shape(run: RunRecord) -> list[tuple[object, object]]:
    return [
        (message.content, (message.provider_data or {}).get(QUEUED_MESSAGE_NOTICE_MARKER_KEY))
        for message in run.messages
    ]


def test_finalization_keeps_one_persisted_notice_where_the_newest_visible_run_first_held_it() -> None:
    """Every other copy of the reply's notice goes, an unfinished run's included; the others' notices stay."""
    first = _run("first", Message(role="user", content="Ask"), _notice("t"), Message(role="assistant", content="One"))
    newest = _run("newest", _notice("t"), Message(role="assistant", content="Two"), _notice("t"))
    paused = _run("paused", _notice("t"), status=RunStatus.paused)
    other = _run("other", _notice("u"))

    changed = {run.run_id: run for run in finalize([first, newest, paused, other], "t")}

    assert sorted(changed) == ["first", "newest", "paused"]
    assert _shape(changed["first"]) == [("Ask", None), ("One", None)]
    assert _shape(changed["newest"]) == [("Pause.", "persisted"), ("Two", None)]
    assert _shape(changed["paused"]) == []
    assert _shape(newest) == [("Pause.", True), ("Two", None), ("Pause.", True)]


def test_recovery_finalizes_only_the_replies_left_live() -> None:
    """A reply whose notice was finalized keeps every copy, even a live one beside the persisted one."""
    runs = [
        _run("crashed", _notice("crashed"), Message(role="assistant", content="Stopped")),
        _run("finalized", _notice("finalized", "persisted"), _notice("finalized")),
    ]

    changed = recover(runs)

    assert [(run.run_id, _shape(run)) for run in changed] == [("crashed", [("Pause.", "persisted"), ("Stopped", None)])]
