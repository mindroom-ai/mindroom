"""Owned history request assembly reproduces Agno's history selection and MindRoom's replay policy.

Selection parity reads the history goldens Agno 3.0.9 produced for each stored session
(``tests/fixtures/agno_parity/history``) and the loaded sessions they were selected from.
"""
# ruff: noqa: D103

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from mindroom.history import message_content
from mindroom.history.request_messages import (
    TurnMessages,
    build_request,
    build_resume_request,
    filter_tool_calls,
    scope_history_messages,
)
from mindroom.history.types import HistoryPolicy, HistoryScope, ResolvedHistorySettings, ScopeSummary
from mindroom.llm.media import Audio, File, Image, Video
from mindroom.llm.messages import Message
from mindroom.llm.runs import AnyRunRecord, RunRecord, RunStatus, TeamRunRecord, run_record_from_dict
from mindroom.media_delivery import VIEWED_IMAGE_ID_PREFIX

_PARITY = Path(__file__).parent / "fixtures" / "agno_parity"
_LOADED_SESSIONS: dict[str, dict[str, dict[str, Any]]] = json.loads(
    (_PARITY / "storage" / "loaded_sessions.json").read_text(),
)
_SCOPED_SELECTIONS: dict[str, dict[str, dict[str, Any]]] = json.loads(
    (_PARITY / "history" / "scoped_selections.json").read_text(),
)
_HISTORY_GOLDENS = sorted(
    path for path in (_PARITY / "history").glob("*.json") if path.name != "scoped_selections.json"
)
_SETTINGS = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)
_AGENT = HistoryScope(kind="agent", scope_id="code")
_TEAM = HistoryScope(kind="team", scope_id="squad")
_SUMMARY = ScopeSummary(
    text="Goal: ship the release.\nProgress: tests pass.",
    updated_at=datetime(2027, 1, 15, tzinfo=UTC),
)
_SUMMARY_FOLLOWS = {"mindroom_compaction_summary_follows": True}


def _golden(name: str) -> dict[str, Any]:
    return json.loads((_PARITY / "history" / name).read_text())


def _session(golden: dict[str, Any]) -> dict[str, Any]:
    return _LOADED_SESSIONS[golden["store"]][golden["session_id"]]


def _stored_runs(golden: dict[str, Any]) -> list[AnyRunRecord]:
    return [run_record_from_dict(run) for run in _session(golden)["runs"] or []]


def _stored_scope(golden: dict[str, Any]) -> HistoryScope:
    session = _session(golden)
    if session.get("team_id"):
        return HistoryScope(kind="team", scope_id=session["team_id"])
    return HistoryScope(kind="agent", scope_id=session["agent_id"])


def _as_replayed(stored: dict[str, Any]) -> dict[str, Any]:
    """Return a stored message dict as history replays it: a history copy whose only media are viewed images."""
    replayed = {key: value for key, value in stored.items() if key not in {"audio", "videos", "files", "images"}}
    viewed = [image for image in stored.get("images", []) if image["id"].startswith(VIEWED_IMAGE_ID_PREFIX)]
    if viewed:
        replayed["images"] = viewed
    return {**replayed, "from_history": True}


def _selection_cases() -> list[Any]:
    cases = []
    for path in _HISTORY_GOLDENS:
        for name, selection in _golden(path.name)["get_messages"].items():
            # Asked for every status without skipping roles, Agno keeps one stored system message first and counts it
            # toward the limit. Only paused runs keep a system message, and MindRoom replays neither those runs nor
            # prompt roles, so such a selection has no MindRoom counterpart.
            if selection["params"].get("skip_statuses") == [] and any(
                message["role"] == "system" for message in selection["messages"]
            ):
                continue
            cases.append(pytest.param(path.name, name, id=f"{path.stem}/{name}"))
    return cases


def _filter_cases() -> list[Any]:
    return [
        pytest.param(path.name, name, id=f"{path.stem}/{name}")
        for path in _HISTORY_GOLDENS
        for name in _golden(path.name)["filter_tool_calls"]
    ]


@pytest.mark.parametrize(("golden_name", "selection_name"), _selection_cases())
def test_selection_matches_agno_get_messages(golden_name: str, selection_name: str) -> None:
    golden = _golden(golden_name)
    selection = golden["get_messages"][selection_name]
    # MindRoom always skips prompt roles; finished stored runs hold none, so Agno's skip_roles changes nothing here.
    params = selection["params"]
    runs = _stored_runs(golden)
    if params.get("skip_statuses") == []:
        # Every run counts as finished, as Agno's empty status filter selected them all.
        runs = [replace(run, status=RunStatus.completed) for run in runs]

    history = scope_history_messages(
        runs,
        _SETTINGS,
        scope=_stored_scope(golden),
        num_history_runs=params.get("last_n_runs"),
        num_history_messages=params.get("limit"),
    )

    assert [message.to_dict() for message in history] == [_as_replayed(message) for message in selection["messages"]]


@pytest.mark.parametrize(
    ("session_key", "selection_name"),
    [
        pytest.param(session_key, name, id=f"{session_key}/{name}")
        for session_key, selections in _SCOPED_SELECTIONS.items()
        for name in selections
    ],
)
def test_scoped_selection_matches_agno_get_messages(session_key: str, selection_name: str) -> None:
    store, session_id = session_key.split(":", 1)
    selection = _SCOPED_SELECTIONS[session_key][selection_name]
    params = selection["params"]
    scope = (
        HistoryScope(kind="team", scope_id=params["team_id"])
        if "team_id" in params
        else HistoryScope(kind="agent", scope_id=params["agent_id"])
    )
    runs = [run_record_from_dict(run) for run in _LOADED_SESSIONS[store][session_id]["runs"] or []]

    history = scope_history_messages(
        runs,
        _SETTINGS,
        scope=scope,
        num_history_runs=params["last_n_runs"],
        num_history_messages=params["limit"],
    )

    assert [message.to_dict() for message in history] == [_as_replayed(message) for message in selection["messages"]]


@pytest.mark.parametrize(("golden_name", "case_name"), _filter_cases())
def test_filter_tool_calls_matches_agno(golden_name: str, case_name: str) -> None:
    golden = _golden(golden_name)
    case = golden["filter_tool_calls"][case_name]
    copies = [
        replace(Message.from_dict(message), from_history=True)
        for message in golden["get_messages"][case["selection"]]["messages"]
    ]

    filtered = filter_tool_calls(copies, case["max_tool_calls"])

    assert [message.to_dict() for message in filtered] == case["messages"]


@pytest.mark.parametrize("golden_path", _HISTORY_GOLDENS, ids=lambda path: path.stem)
@pytest.mark.parametrize("max_tool_calls", [0, 1, 2])
def test_replayed_history_limits_tool_calls_as_agno_did(golden_path: Path, max_tool_calls: int) -> None:
    golden = _golden(golden_path.name)
    expected = golden["filter_tool_calls"][f"all/max_tool_calls_{max_tool_calls}"]["messages"]

    history = scope_history_messages(
        _stored_runs(golden),
        replace(_SETTINGS, max_tool_calls_from_history=max_tool_calls),
        scope=_stored_scope(golden),
        num_history_runs=None,
        num_history_messages=None,
    )

    assert [message.to_dict() for message in history] == [_as_replayed(message) for message in expected]


def _exchange(prefix: str) -> list[Message]:
    return [Message(role="user", content=f"{prefix} question"), Message(role="assistant", content=f"{prefix} answer")]


def _agent_run(
    run_id: str,
    *,
    agent_id: str = "code",
    messages: list[Message] | None = None,
    status: RunStatus = RunStatus.completed,
    parent_run_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id=agent_id,
        messages=_exchange(run_id) if messages is None else messages,
        status=status,
        parent_run_id=parent_run_id,
    )


def _team_run(
    run_id: str,
    *,
    team_id: str = "squad",
    status: RunStatus = RunStatus.completed,
    parent_run_id: str | None = None,
) -> TeamRunRecord:
    return TeamRunRecord(
        run_id=run_id,
        team_id=team_id,
        messages=_exchange(run_id),
        status=status,
        parent_run_id=parent_run_id,
    )


def _replay(
    runs: list[AnyRunRecord],
    *,
    scope: HistoryScope = _AGENT,
    settings: ResolvedHistorySettings = _SETTINGS,
    num_history_runs: int | None = None,
    num_history_messages: int | None = None,
) -> list[Message]:
    return scope_history_messages(
        runs,
        settings,
        scope=scope,
        num_history_runs=num_history_runs,
        num_history_messages=num_history_messages,
    )


def _contents(messages: list[Message]) -> list[object]:
    return [message.content for message in messages]


def _shape(messages: list[Message]) -> list[dict[str, Any]]:
    """Return what the model reads of each message: its dict without the per-object id and timestamp."""
    return [
        {key: value for key, value in message.to_dict().items() if key not in {"id", "created_at"}}
        for message in messages
    ]


def test_agent_scope_replays_only_its_own_finished_top_level_runs() -> None:
    runs: list[AnyRunRecord] = [
        _agent_run("own"),
        _agent_run("other-agent", agent_id="helper"),
        _agent_run("delegated-member", parent_run_id="team-run"),
        _agent_run("paused", status=RunStatus.paused),
        _agent_run("cancelled", status=RunStatus.cancelled),
        _agent_run("failed", status=RunStatus.error),
        _team_run("team-named-like-the-agent", team_id="code"),
        _agent_run("still-running", status=RunStatus.running),
    ]

    assert _contents(_replay(runs)) == [
        "own question",
        "own answer",
        "still-running question",
        "still-running answer",
    ]


def test_team_scope_replays_only_its_own_finished_top_level_runs() -> None:
    runs: list[AnyRunRecord] = [
        _agent_run("member", agent_id="squad", parent_run_id="team"),
        _team_run("team"),
        _team_run("other-team", team_id="panel"),
        _team_run("nested-member-team", parent_run_id="outer"),
        _team_run("paused-team", status=RunStatus.paused),
        _agent_run("agent-named-like-the-team", agent_id="squad"),
    ]

    assert _contents(_replay(runs, scope=_TEAM)) == ["team question", "team answer"]


def test_prompt_roles_and_stored_history_copies_never_replay() -> None:
    stored_copy = Message(role="user", content="replayed earlier", from_history=True)
    runs: list[AnyRunRecord] = [
        _agent_run(
            "run",
            messages=[
                Message(role="system", content="old system prompt"),
                Message(role="developer", content="old developer prompt"),
                Message(role="instructions", content="custom prompt role"),
                stored_copy,
                *_exchange("run"),
            ],
        ),
    ]
    settings = replace(_SETTINGS, system_message_role="instructions")

    assert _contents(_replay(runs, settings=settings)) == ["run question", "run answer"]


def test_message_limit_applies_after_the_run_limit_and_drops_orphaned_tool_results() -> None:
    tool_turn = [
        Message(role="user", content="add"),
        Message(
            role="assistant",
            tool_calls=[{"id": "call", "type": "function", "function": {"name": "add", "arguments": "{}"}}],
        ),
        Message(role="tool", tool_call_id="call", content="5"),
        Message(role="assistant", content="It is 5."),
    ]
    runs: list[AnyRunRecord] = [_agent_run("old"), _agent_run("tools", messages=tool_turn)]

    assert _contents(_replay(runs, num_history_messages=2)) == ["It is 5."]
    assert _contents(_replay(runs, num_history_messages=5)) == ["old answer", *_contents(tool_turn)]
    assert _contents(_replay(runs, num_history_runs=1, num_history_messages=5)) == _contents(tool_turn)
    assert _replay(runs, num_history_runs=0) == []
    assert _replay(runs, num_history_messages=0) == []


def test_replayed_history_is_a_marked_copy_that_never_changes_stored_runs() -> None:
    blocks = [{"type": "text", "text": "block"}]
    stored = [Message(role="user", content="question"), Message(role="assistant", content=blocks)]
    runs: list[AnyRunRecord] = [_agent_run("run", messages=stored)]

    history = _replay(runs)
    history[0].content = "changed"
    content = history[1].content
    assert isinstance(content, list)
    content[0]["text"] = "changed"

    assert all(message.from_history for message in history)
    assert stored[0].content == "question"
    assert blocks == [{"type": "text", "text": "block"}]
    assert not any(message.from_history for message in stored)


def test_filter_tool_calls_copies_what_it_changes() -> None:
    call = {"id": "old", "type": "function", "function": {"name": "add", "arguments": "{}"}}
    assistant = Message(role="assistant", content="Checking.", tool_calls=[call])
    messages = [
        assistant,
        Message(role="tool", tool_call_id="old", content="5"),
        Message(role="tool", tool_call_id="new", content="6"),
    ]

    filtered = filter_tool_calls(messages, 1)

    assert [(message.role, message.content, message.tool_calls) for message in filtered] == [
        ("assistant", "Checking.", None),
        ("tool", "6", None),
    ]
    assert assistant.tool_calls == [call]
    assert len(messages) == 3


def test_filter_tool_calls_within_the_limit_returns_the_same_messages() -> None:
    messages = [
        Message(role="assistant", tool_calls=[{"id": "call", "type": "function", "function": {"name": "add"}}]),
        Message(role="tool", tool_call_id="call", content="5"),
        Message(role="tool", content="a result without a call id"),
    ]

    filtered = filter_tool_calls(messages, 2)

    assert len(filtered) == len(messages)
    assert all(kept is message for kept, message in zip(filtered, messages, strict=True))


def _viewed_image(index: int, content: bytes) -> Image:
    return Image(id=f"{VIEWED_IMAGE_ID_PREFIX}{index}", content=content, mime_type="image/png")


def _media_follow_up(*images: Image) -> Message:
    return Message(role="user", content="The tool call above generated the attached media.", images=list(images))


def test_history_replays_only_the_newest_four_viewed_images_and_says_so() -> None:
    stored = [_media_follow_up(_viewed_image(index, f"image-{index}".encode())) for index in range(6)]

    history = _replay([_agent_run("run", messages=stored)])

    assert [image.id for message in history for image in message.images or []] == [
        f"{VIEWED_IMAGE_ID_PREFIX}{index}" for index in range(2, 6)
    ]
    notice = (
        "The tool call above generated the attached media.\n\n"
        "[1 historical viewed image omitted from replay to keep history within the 4-image, 10 MiB limit. "
        "Reopen the original workspace path with view_file, or use an authorized attachment ID.]"
    )
    assert _contents(history[:2]) == [notice, notice]
    assert all(message.images for message in stored)


def test_history_viewed_images_fit_the_aggregate_byte_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(message_content, "_MAX_HISTORY_VIEWED_IMAGE_BYTES", 5)
    stored = [_media_follow_up(_viewed_image(index, b"x" * size)) for index, size in enumerate((3, 2, 3))]
    stored[0].compressed_content = "compressed earlier content"

    history = _replay([_agent_run("run", messages=stored)])

    assert [image.id for message in history for image in message.images or []] == [
        f"{VIEWED_IMAGE_ID_PREFIX}1",
        f"{VIEWED_IMAGE_ID_PREFIX}2",
    ]
    assert str(history[0].content).startswith("compressed earlier content\n\n[1 historical viewed image omitted")
    assert history[0].compressed_content is None


def test_history_drops_every_other_media_kind() -> None:
    stored = Message(
        role="user",
        content="voice note",
        audio=[Audio(content=b"audio", mime_type="audio/ogg")],
        images=[Image(content=b"photo"), _viewed_image(1, b"viewed")],
        files=[File(content=b"file", mime_type="text/plain")],
        videos=[Video(content=b"video")],
    )

    (replayed,) = _replay([_agent_run("run", messages=[stored])])

    assert (replayed.audio, replayed.files, replayed.videos) == (None, None, None)
    assert [image.id for image in replayed.images or []] == [f"{VIEWED_IMAGE_ID_PREFIX}1"]
    assert replayed.content == "voice note"
    assert stored.audio
    assert stored.files
    assert stored.videos
    assert len(stored.images or []) == 2


def _system() -> Message:
    return Message(role="system", content="Be precise.")


def _rendered_summary(text: str) -> str:
    return (
        "The earlier part of this conversation was compacted into this summary:\n\n"
        f"<compacted_history>\n{text}\n</compacted_history>\n\n"
        "The conversation continues after this summary. If it records progress on the current request, "
        "continue that work from its next steps instead of starting over."
    )


def test_new_request_orders_system_summary_history_context_prompt_then_transient_context() -> None:
    history = _replay([_agent_run("earlier")])
    turn = TurnMessages(
        context=(Message(role="user", content="unseen thread message"),),
        prompt=Message(role="user", content="current request"),
        transient=(Message(role="user", content="memories", add_to_agent_memory=False),),
    )

    request = build_request(system=[_system()], summary=_SUMMARY, history=history, turn=turn)

    assert _shape(request) == [
        {"role": "system", "content": "Be precise.", "from_history": False, "stop_after_tool_call": False},
        {
            "role": "user",
            "content": _rendered_summary(_SUMMARY.text),
            "provider_data": {"mindroom_compaction_summary": "scope"},
            "from_history": True,
            "stop_after_tool_call": False,
        },
        *_shape(history),
        *_shape([*turn.context, turn.prompt, *turn.transient]),
    ]
    assert request[-2] is turn.prompt


def test_next_request_repeats_the_previous_one_up_to_its_transient_context() -> None:
    """Unstored turn context follows the prompt, so the next turn's request shares the whole stored prefix."""
    first_turn = TurnMessages(
        context=(Message(role="user", content="thread message"),),
        prompt=Message(role="user", content="question one"),
        transient=(Message(role="user", content="context one", add_to_agent_memory=False),),
    )
    first = build_request(system=[_system()], summary=None, history=[], turn=first_turn)
    stored = [
        *(message for message in first[1:] if message.add_to_agent_memory),
        Message(role="assistant", content="answer one"),
    ]
    second_turn = TurnMessages(
        prompt=Message(role="user", content="question two"),
        transient=(Message(role="user", content="context two", add_to_agent_memory=False),),
    )

    second = build_request(
        system=[_system()],
        summary=None,
        history=_replay([_agent_run("first", messages=stored)]),
        turn=second_turn,
    )

    assert _contents(first) == ["Be precise.", "thread message", "question one", "context one"]
    assert _contents(second) == [
        "Be precise.",
        "thread message",
        "question one",
        "answer one",
        "question two",
        "context two",
    ]


@pytest.mark.parametrize("summary", [None, ScopeSummary(text=" \n ", updated_at=_SUMMARY.updated_at)])
def test_no_summary_message_without_summary_text(summary: ScopeSummary | None) -> None:
    turn = TurnMessages(prompt=Message(role="user", content="current request"))

    request = build_request(system=[_system()], summary=summary, history=[], turn=turn)

    assert _contents(request) == ["Be precise.", "current request"]


def test_summary_replays_without_runs_and_with_its_text_trimmed() -> None:
    turn = TurnMessages(prompt=Message(role="user", content="current request"))
    padded = ScopeSummary(text=f"\n{_SUMMARY.text}  ", updated_at=_SUMMARY.updated_at)

    request = build_request(system=[_system()], summary=padded, history=[], turn=turn)

    assert _contents(request) == ["Be precise.", _rendered_summary(_SUMMARY.text), "current request"]


def test_summary_follows_the_prompt_a_mid_turn_compaction_placed_it_after() -> None:
    folded_prompt = Message(role="user", content="long task", provider_data=_SUMMARY_FOLLOWS)
    runs: list[AnyRunRecord] = [
        _agent_run("folded", messages=[folded_prompt, Message(role="assistant", content="done")]),
    ]
    turn = TurnMessages(prompt=Message(role="user", content="next request"))

    request = build_request(system=[_system()], summary=_SUMMARY, history=_replay(runs), turn=turn)

    assert _contents(request) == [
        "Be precise.",
        "long task",
        _rendered_summary(_SUMMARY.text),
        "done",
        "next request",
    ]


def test_a_replayed_run_local_summary_does_not_replace_the_scope_summary() -> None:
    run_local = Message(
        role="user",
        content=_rendered_summary("local work"),
        provider_data={"mindroom_compaction_summary": "run"},
    )
    runs: list[AnyRunRecord] = [_agent_run("run", messages=[Message(role="user", content="task"), run_local])]
    turn = TurnMessages(prompt=Message(role="user", content="next request"))

    request = build_request(system=[_system()], summary=_SUMMARY, history=_replay(runs), turn=turn)

    assert _contents(request) == [
        "Be precise.",
        _rendered_summary(_SUMMARY.text),
        "task",
        _rendered_summary("local work"),
        "next request",
    ]


def _tool_call(call_id: str) -> Message:
    return Message(
        role="assistant",
        tool_calls=[{"id": call_id, "type": "function", "function": {"name": "multiply", "arguments": "{}"}}],
    )


def test_resumed_request_repeats_the_request_that_paused() -> None:
    """A resume sends the paused request again, so its prefix and the paused call's reasoning stay valid."""
    runs: list[AnyRunRecord] = [_agent_run("earlier")]
    turn = TurnMessages(
        prompt=Message(role="user", content="multiply"),
        transient=(Message(role="user", content="memories", add_to_agent_memory=False),),
    )
    paused_request = build_request(system=[_system()], summary=_SUMMARY, history=_replay(runs), turn=turn)
    call = _tool_call("call-multiply")
    # A paused run stores its own messages, its system message included, and never history copies or transient context.
    stored = [
        message for message in [*paused_request, call] if not message.from_history and message.add_to_agent_memory
    ]

    resumed = build_resume_request(paused=stored, summary=_SUMMARY, history=_replay(runs))

    assert _shape(resumed) == _shape([message for message in [*paused_request, call] if message.add_to_agent_memory])
    assert resumed[0] is stored[0]


def test_resumed_request_moves_the_stored_system_message_first() -> None:
    paused = [Message(role="user", content="multiply"), _system(), _tool_call("call")]

    resumed = build_resume_request(paused=paused, summary=None, history=_replay([_agent_run("earlier")]))

    assert _contents(resumed) == ["Be precise.", "earlier question", "earlier answer", "multiply", None]


def test_resuming_a_pause_whose_system_message_embeds_the_summary_adds_no_second_summary() -> None:
    legacy_system = Message(
        role="system",
        content=(
            "Be precise.\n\nHere is a brief summary of your previous interactions:\n\n"
            "<summary_of_previous_interactions>\nOLD\n</summary_of_previous_interactions>\n\n"
        ),
    )
    paused = [legacy_system, Message(role="user", content="multiply"), _tool_call("call")]

    resumed = build_resume_request(paused=paused, summary=_SUMMARY, history=[])

    assert resumed == paused


def test_resuming_messages_that_carry_the_scope_summary_keeps_that_one() -> None:
    carried = Message(
        role="user",
        content=_rendered_summary("carried"),
        provider_data={"mindroom_compaction_summary": "scope"},
        from_history=True,
    )
    paused = [_system(), carried, Message(role="user", content="multiply"), _tool_call("call")]

    resumed = build_resume_request(paused=paused, summary=_SUMMARY, history=[])

    assert resumed == paused


def test_resume_places_the_summary_after_a_paused_prompt_marked_for_it() -> None:
    prompt = Message(role="user", content="long task", provider_data=_SUMMARY_FOLLOWS)
    paused = [_system(), prompt, _tool_call("call")]

    resumed = build_resume_request(paused=paused, summary=_SUMMARY, history=[])

    assert _contents(resumed) == ["Be precise.", "long task", _rendered_summary(_SUMMARY.text), None]
