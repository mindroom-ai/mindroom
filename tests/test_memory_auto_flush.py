"""The auto-flush worker turns the new finished turns of file-memory conversations into daily notes, once each."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import pytest

import mindroom.memory._semantic_file_search as semantic_file_search
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import HOOK_SOURCE_RUN_METADATA_KEY, MATRIX_EVENT_ID_METADATA_KEY, resolve_runtime_paths
from mindroom.hooks import EnrichmentItem, render_enrichment_block
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.memory import MemoryAutoFlushWorker, add_agent_memory, auto_flush
from mindroom.memory.functions import append_agent_daily_memory
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_workspace_root_path,
    build_requester_execution_identity,
    private_instance_scope_root_path,
    resolve_worker_key,
    tool_execution_identity,
)
from mindroom.usage.storage import quote_identifier
from tests.conftest import bind_runtime_paths, runtime_paths_for, seed_session
from tests.fakes.scripted_model import ScriptedModel, answer

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.history.session_runs import RedactedEventIds

T0 = 1_760_000_000
ROOM = "!room:example.org"
ALICE = "@alice:example.org"
BOB = "@bob:example.org"


def _thread(name: str, room: str = ROOM) -> str:
    return f"{room}:${name}"


def _config(tmp_path: Path, *, agents: dict[str, AgentConfig] | None = None, **auto_flush_settings: object) -> Config:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008", "MINDROOM_NAMESPACE": ""},
    )
    return bind_runtime_paths(
        Config(
            agents=agents or {"general": AgentConfig(display_name="General", role="General assistant", rooms=[])},
            memory={"backend": "file", "auto_flush": {"enabled": True, "idle_seconds": 1, **auto_flush_settings}},
        ),
        runtime_paths,
    )


def _private_config(tmp_path: Path, **auto_flush_settings: object) -> Config:
    agents = {
        "mind": AgentConfig(
            display_name="Mind",
            role="Persistent private assistant",
            rooms=[],
            memory_backend="file",
            private=AgentPrivateConfig(per="user", root="mind_data"),
        ),
    }
    return _config(tmp_path, agents=agents, **auto_flush_settings)


def _requester(config: Config, requester_id: str, agent: str = "mind") -> ToolExecutionIdentity:
    return build_requester_execution_identity(
        agent_name=agent,
        runtime_paths=runtime_paths_for(config),
        requester_id=requester_id,
    )


def _run(
    run_id: str,
    session_id: str,
    created_at: int,
    *,
    status: RunStatus = RunStatus.completed,
    hook_source: str | None = None,
    messages: list[Message] | None = None,
) -> RunRecord:
    metadata: dict[str, object] = {
        "room_id": session_id.rsplit(":$", 1)[0],
        "requester_id": ALICE,
        MATRIX_EVENT_ID_METADATA_KEY: f"$event-{run_id}",
    }
    if hook_source is not None:
        metadata[HOOK_SOURCE_RUN_METADATA_KEY] = hook_source
    return RunRecord(
        run_id=run_id,
        session_id=session_id,
        agent_id="general",
        created_at=created_at,
        status=status,
        metadata=metadata,
        messages=messages
        or [
            Message(role="user", content=f"question {run_id}", created_at=created_at),
            Message(role="assistant", content=f"answer {run_id}", created_at=created_at),
        ],
    )


def _seed(
    config: Config,
    session_id: str,
    runs: list[RunRecord],
    *,
    agent: str = "general",
    identity: ToolExecutionIdentity | None = None,
    updated_at: int | None = None,
) -> None:
    """Store a conversation, last updated a second after its newest run unless told otherwise."""
    storage = create_session_storage(agent, config, runtime_paths_for(config), execution_identity=identity)
    for run in runs:
        run.agent_id = agent
    seed_session(storage, ConversationSession(session_id=session_id, kind="agent", owner_id=agent, runs=runs))
    newest = max((run.created_at or 0 for run in runs), default=T0)
    with storage.transaction(write=True) as connection:
        connection.execute(
            f"UPDATE {quote_identifier(storage.names.sessions)} SET updated_at = ? WHERE session_id = ?",  # noqa: S608
            (updated_at if updated_at is not None else newest + 1, session_id),
        )


async def _no_redactions(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
    del agent_name, room_id, event_ids
    return frozenset()


@dataclass
class _Clock:
    now: float = T0

    def __call__(self) -> float:
        return self.now


@dataclass
class _Extractor:
    """Stands in for the extraction model, recording what each call was given."""

    summary: str | None = "important decision"
    error: Exception | None = None
    clock: _Clock | None = None
    # Seconds an extraction takes on the clock.
    takes: float = 0
    calls: list[dict[str, object]] = field(default_factory=list)

    async def __call__(self, **kwargs: object) -> str | None:
        self.calls.append(kwargs)
        if self.clock is not None:
            self.clock.now += self.takes
        if self.error is not None:
            raise self.error
        return self.summary

    def lines(self) -> list[list[str]]:
        return [cast("list[str]", call["lines"]) for call in self.calls]


def _lines(*run_ids: str) -> list[str]:
    return [line for run_id in run_ids for line in (f"{ALICE}: question {run_id}", f"assistant: answer {run_id}")]


def _worker(
    config: Config,
    clock: _Clock,
    redacted_event_ids: RedactedEventIds = _no_redactions,
) -> MemoryAutoFlushWorker:
    return MemoryAutoFlushWorker(
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
        redacted_event_ids=redacted_event_ids,
        clock=clock,
    )


def _notes(root: Path) -> list[str]:
    """Return every daily-note entry under a workspace."""
    return [
        line
        for path in sorted((root / "memory").rglob("*.md"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _general_notes(tmp_path: Path) -> list[str]:
    return _notes(agent_workspace_root_path(tmp_path, "general"))


def _entry(tmp_path: Path, session_id: str) -> dict[str, object]:
    state = json.loads((tmp_path / "tracking" / "memory_auto_flush.json").read_text(encoding="utf-8"))
    return next(conversations[session_id] for conversations in state["scopes"].values() if session_id in conversations)


@pytest.fixture
def extractor(monkeypatch: pytest.MonkeyPatch) -> _Extractor:
    """Replace the extraction model call."""
    fake = _Extractor()
    monkeypatch.setattr(auto_flush, "_extract_memory_summary", fake)
    return fake


async def _started(
    config: Config,
    clock: _Clock | None = None,
    redacted_event_ids: RedactedEventIds = _no_redactions,
) -> MemoryAutoFlushWorker:
    """Return a worker whose first pass ran at T0, which starts auto-flush, with its clock moved past every seeded turn."""
    clock = _Clock() if clock is None else clock
    worker = _worker(config, clock, redacted_event_ids)
    await worker._run_cycle(config)
    clock.now = T0 + 1000
    return worker


@pytest.mark.asyncio
async def test_the_first_pass_removes_the_old_queue_and_flushes_only_later_turns(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """Upgrading deletes the push-model queue, and a conversation's turns from before auto-flush started stay unflushed."""
    config = _config(tmp_path)
    (tmp_path / "memory_flush_state.json").write_text('{"version": 1, "sessions": {}}', encoding="utf-8")
    (tmp_path / "memory_flush_state.json.tmp").write_text("{", encoding="utf-8")
    session = _thread("t1")
    old = _run("old", session, T0 - 50)
    _seed(config, session, [old])

    worker = await _started(config)

    assert not (tmp_path / "memory_flush_state.json").exists()
    assert not (tmp_path / "memory_flush_state.json.tmp").exists()
    assert extractor.calls == []

    _seed(config, session, [old, _run("new", session, T0 + 10)])
    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("new")]
    [note] = _general_notes(tmp_path)
    assert note.startswith("- [id=")
    assert note.endswith(f"] [auto_flush:{session}:new] important decision")


@pytest.mark.asyncio
async def test_each_private_instance_flushes_into_its_own_memory(tmp_path: Path, extractor: _Extractor) -> None:
    """Every requester's private instance is its own scope, with its own identity, memory, and per-scope cap."""
    config = _private_config(tmp_path, batch={"max_sessions_per_cycle": 2, "max_sessions_per_agent_per_cycle": 1})
    worker = await _started(config)
    alice, bob = _requester(config, ALICE), _requester(config, BOB)
    # Instances share their agent's start, so one first listed long after it keeps every turn since.
    _seed(config, _thread("a"), [_run("a1", _thread("a"), T0 + 10)], agent="mind", identity=alice)
    _seed(config, _thread("b"), [_run("b1", _thread("b"), T0 + 10)], agent="mind", identity=bob)

    await worker._run_cycle(config)

    assert sorted(
        cast("ToolExecutionIdentity", call["execution_identity"]).requester_id for call in extractor.calls
    ) == [
        ALICE,
        BOB,
    ]
    for identity, run_id in ((alice, "a1"), (bob, "b1")):
        worker_key = resolve_worker_key("user", identity, agent_name="mind")
        assert worker_key is not None
        [note] = _notes(private_instance_scope_root_path(tmp_path, worker_key) / "mind" / "mind_data")
        assert f":{run_id}] important decision" in note


@pytest.mark.asyncio
async def test_an_agent_switched_to_file_memory_flushes_only_turns_since_the_previous_pass(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """An agent first seen after the start begins one pass before it was seen, so its older history is never flushed."""
    agents = {
        "general": AgentConfig(display_name="General", rooms=[], memory_backend="file"),
        "other": AgentConfig(display_name="Other", rooms=[], memory_backend="mem0"),
    }
    config, clock = _config(tmp_path, agents=agents), _Clock()
    worker = await _started(config, clock)
    clock.now = T0 + 90 * 86_400
    interval = config.memory.auto_flush.flush_interval_seconds
    session = _thread("t1")
    older = [_run(f"m{index}", session, T0 + 100 + index) for index in range(3)]
    recent = _run("recent", session, int(clock.now) - interval // 2)
    _seed(config, session, [*older, recent], agent="other", updated_at=int(clock.now) - 10)
    stale = _thread("stale")
    _seed(config, stale, [_run("s1", stale, T0 + 200)], agent="other")
    config.agents["other"].memory_backend = "file"

    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("recent")]
    # A conversation last updated before the scope's start is not even read.
    with pytest.raises(StopIteration):
        _entry(tmp_path, stale)


@pytest.mark.asyncio
async def test_a_conversation_updated_in_the_start_second_is_flushed(tmp_path: Path, extractor: _Extractor) -> None:
    """Update times are whole seconds, so a turn finished just after the start, in the same second, still counts."""
    config, clock = _config(tmp_path), _Clock()
    clock.now = T0 + 0.5
    worker = _worker(config, clock)
    await worker._run_cycle(config)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0)], updated_at=T0)
    clock.now = T0 + 1000

    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("r1")]


@pytest.mark.asyncio
async def test_conversations_without_matrix_turns_leave_the_pass_to_others(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """A conversation with nothing to flush, such as one through the OpenAI-compatible API, takes no place in a pass."""
    config = _config(tmp_path, batch={"max_sessions_per_agent_per_cycle": 1})
    worker = await _started(config)
    for index in range(3):
        api_session = f"api-{index}"
        api_run = _run(f"x{index}", api_session, T0 + 10 + index)
        cast("dict[str, object]", api_run.metadata).pop("room_id")
        _seed(config, api_session, [api_run])
    automation = _thread("automation")
    _seed(config, automation, [_run("auto", automation, T0 + 20, hook_source="automation/daily")])
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 30)])

    await worker._run_cycle(config)
    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("r1")]
    assert _entry(tmp_path, "api-0")["updated_at"] == T0 + 11


@pytest.mark.asyncio
async def test_an_agent_keeps_the_start_it_was_first_seen_with(tmp_path: Path, extractor: _Extractor) -> None:
    """The start of a newly seen agent is saved, so a late next pass still flushes the turns made since it appeared."""
    agents = {
        "general": AgentConfig(display_name="General", rooms=[], memory_backend="file"),
        "other": AgentConfig(display_name="Other", rooms=[], memory_backend="mem0"),
    }
    config, clock = _config(tmp_path, agents=agents), _Clock()
    worker = await _started(config, clock)
    config.agents["other"].memory_backend = "file"
    await worker._run_cycle(config)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, int(clock.now) + 10)], agent="other")

    clock.now += 3 * config.memory.auto_flush.flush_interval_seconds
    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("r1")]


@pytest.mark.asyncio
async def test_a_retry_after_a_failed_progress_write_never_writes_a_note_twice(
    tmp_path: Path,
    extractor: _Extractor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A note written before its progress could be saved is found again, even when newer turns arrived since."""
    config = _config(tmp_path)
    worker = await _started(config)
    session = _thread("t1")
    first = _run("r1", session, T0 + 10)
    _seed(config, session, [first])
    real_write = auto_flush.write_json_file_durable
    failed = False

    def first_progress_write_fails(path: Path, payload: object, **kwargs: object) -> None:
        nonlocal failed
        if extractor.calls and not failed:
            failed = True
            msg = "disk full"
            raise OSError(msg)
        real_write(path, payload, **kwargs)

    monkeypatch.setattr(auto_flush, "write_json_file_durable", first_progress_write_fails)

    await worker._run_cycle(config)
    assert extractor.lines() == [_lines("r1")]
    with pytest.raises(StopIteration):
        _entry(tmp_path, session)

    _seed(config, session, [first, _run("r2", session, T0 + 20)])
    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("r1"), _lines("r2")]
    notes = _general_notes(tmp_path)
    assert [note.split("] ", 1)[1] for note in notes] == [
        f"[auto_flush:{session}:r1] important decision",
        f"[auto_flush:{session}:r2] important decision",
    ]
    await worker._run_cycle(config)
    assert len(extractor.calls) == 2


@pytest.mark.asyncio
async def test_automation_turns_are_skipped_while_other_turns_in_the_thread_are_flushed(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """An automation's maintenance turn is not memory; a person's turn or another hook's turn in the thread is."""
    config = _config(tmp_path)
    worker = await _started(config)
    session = _thread("t1")
    _seed(
        config,
        session,
        [
            _run("dream", session, T0 + 10, hook_source="automation/dreaming"),
            _run("person", session, T0 + 20),
            _run("digest", session, T0 + 30, hook_source="digest:message:received"),
            _run("curate", session, T0 + 40, hook_source="automation/prompt_curation"),
        ],
    )

    await worker._run_cycle(config)
    assert extractor.lines() == [_lines("person", "digest")]

    # A pass whose new turns are all automation turns moves past them without extracting.
    only_automation = _thread("t2")
    _seed(config, only_automation, [_run("weekly", only_automation, T0 + 50, hook_source="automation/weekly")])
    await worker._run_cycle(config)
    assert len(extractor.calls) == 1
    assert _entry(tmp_path, only_automation)["marker"] == [T0 + 50, "weekly"]
    assert _entry(tmp_path, session)["marker"] == [T0 + 40, "curate"]


@pytest.mark.asyncio
async def test_a_redacted_turn_stays_out_and_an_agent_that_cannot_tell_waits_unbudgeted(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """A turn derived from a redacted event is never flushed, nor are later ones; an agent that cannot tell waits."""
    agents = {
        "general": AgentConfig(display_name="General", rooms=[]),
        "other": AgentConfig(display_name="Other", rooms=[]),
    }
    config = _config(tmp_path, agents=agents, batch={"max_sessions_per_cycle": 1})

    async def redacted(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del room_id, event_ids
        return None if agent_name == "other" else frozenset({"$event-gone"})

    worker = await _started(config, redacted_event_ids=redacted)
    # The second pass starts with the second agent, whose bot cannot tell, so its waiting must leave budget.
    waiting = _thread("w")
    _seed(config, waiting, [_run("w1", waiting, T0 + 5)], agent="other")
    session = _thread("t1")
    _seed(
        config,
        session,
        [_run("kept", session, T0 + 10), _run("gone", session, T0 + 20), _run("later", session, T0 + 30)],
    )

    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("kept")]
    assert _entry(tmp_path, session)["marker"] == [T0 + 10, "kept"]
    with pytest.raises(StopIteration):
        _entry(tmp_path, waiting)


@pytest.mark.asyncio
async def test_a_conversation_whose_redactions_cannot_be_checked_backs_off_without_blocking_the_agent(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """A failing redaction lookup backs off one conversation like a failed read, and the agent's others still flush."""
    config, clock = _config(tmp_path, stale_ttl_seconds=3600), _Clock()
    broken_room = "!broken:example.org"

    async def redacted(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del agent_name, event_ids
        if room_id == broken_room:
            msg = "journal read failed"
            raise RuntimeError(msg)
        return frozenset()

    worker = await _started(config, clock, redacted_event_ids=redacted)
    broken = _thread("broken", broken_room)
    _seed(config, broken, [_run("b1", broken, T0 + 5)])
    cooldown = config.memory.auto_flush.retry_cooldown_seconds

    await worker._run_cycle(config)
    entry = _entry(tmp_path, broken)
    assert (entry["failures"], entry["retry_at"]) == (1, clock.now + cooldown)

    fine = _thread("fine")
    _seed(config, fine, [_run("f1", fine, T0 + 10)])
    clock.now += cooldown
    await worker._run_cycle(config)
    assert extractor.lines() == [_lines("f1")]
    assert _entry(tmp_path, broken)["failures"] == 2

    clock.now += 3600
    await worker._run_cycle(config)
    entry = _entry(tmp_path, broken)
    assert (entry["failures"], entry["updated_at"]) == (0, T0 + 6)


@pytest.mark.asyncio
async def test_a_turn_waiting_for_approval_holds_back_later_turns_until_it_finishes(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """A paused turn finishes in place later, so it and the turns after it are flushed once, after it finishes."""
    config = _config(tmp_path)
    worker = await _started(config)
    session = _thread("t1")
    paused = _run("asks", session, T0 + 10, status=RunStatus.paused)
    after = _run("after", session, T0 + 20)
    _seed(config, session, [paused, after])

    await worker._run_cycle(config)
    assert extractor.calls == []

    paused.status = RunStatus.completed
    _seed(config, session, [paused, after], updated_at=T0 + 30)
    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("asks", "after")]


@pytest.mark.asyncio
async def test_a_turn_whose_approval_expired_releases_the_turns_after_it_without_news(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """Once every approval a paused turn could get has expired, the conversation is read again and moves on."""
    config, clock = _config(tmp_path), _Clock()
    worker = await _started(config, clock)
    session = _thread("t1")
    _seed(config, session, [_run("asks", session, T0 + 10, status=RunStatus.paused), _run("after", session, T0 + 20)])

    await worker._run_cycle(config)
    assert extractor.calls == []

    clock.now = T0 + 10 + config.tool_approval.timeout_days * 86_400 + 3600
    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("after")]


@pytest.mark.asyncio
async def test_delegated_task_sessions_are_not_flushed(tmp_path: Path, extractor: _Extractor) -> None:
    """A task another agent delegated is the delegating agent's conversation, not the delegate's."""
    config = _config(tmp_path)
    worker = await _started(config)
    delegated = "delegate:other:general:abc"
    _seed(config, delegated, [_run("task", f"{ROOM}:$x", T0 + 10)])

    await worker._run_cycle(config)

    assert extractor.calls == []


@pytest.mark.asyncio
async def test_a_conversation_still_active_is_not_read_and_reads_happen_off_the_event_loop(
    tmp_path: Path,
    extractor: _Extractor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only conversations quiet for ``idle_seconds`` are decoded, and decoding never blocks the event loop."""
    config, clock = _config(tmp_path, idle_seconds=120), _Clock()
    worker = await _started(config, clock)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 900)], updated_at=T0 + 990)
    loop_thread = threading.get_ident()
    reads: list[bool] = []
    real_session_runs = auto_flush.session_runs

    def session_runs(storage: object, session_id: str) -> list[RunRecord]:
        reads.append(threading.get_ident() != loop_thread)
        return real_session_runs(storage, session_id)

    monkeypatch.setattr(auto_flush, "session_runs", session_runs)

    await worker._run_cycle(config)
    assert (reads, extractor.calls) == ([], [])

    clock.now = T0 + 990 + 120
    await worker._run_cycle(config)
    assert reads == [True]
    assert extractor.lines() == [_lines("r1")]


@pytest.mark.asyncio
async def test_a_zero_idle_time_still_waits_a_second(tmp_path: Path, extractor: _Extractor) -> None:
    """A turn stored in the same second as a pass could share the update time the pass recorded, so it waits."""
    config, clock = _config(tmp_path, idle_seconds=0), _Clock()
    worker = await _started(config, clock)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)], updated_at=int(clock.now))

    await worker._run_cycle(config)
    assert extractor.calls == []

    clock.now += 1
    await worker._run_cycle(config)
    assert extractor.lines() == [_lines("r1")]


@pytest.mark.asyncio
async def test_a_long_backlog_is_flushed_oldest_first_over_passes(tmp_path: Path, extractor: _Extractor) -> None:
    """Each pass extracts the oldest new turns that fit, so a backlog bigger than one extraction loses nothing."""
    config = _config(tmp_path, extractor={"max_messages_per_flush": 4})
    worker = await _started(config)
    session = _thread("t1")
    _seed(config, session, [_run(f"r{index}", session, T0 + index) for index in range(1, 6)])

    for _ in range(4):
        await worker._run_cycle(config)

    assert extractor.lines() == [_lines("r1", "r2"), _lines("r3", "r4"), _lines("r5")]
    assert len(_general_notes(tmp_path)) == 3


def test_a_turn_too_long_for_one_extraction_keeps_its_newest_lines(tmp_path: Path) -> None:
    """A single turn over the limits is sent alone with its newest lines, after any turns with nothing said."""
    settings = _config(tmp_path, extractor={"max_messages_per_flush": 2}).memory.auto_flush
    session = _thread("t1")
    automation = _run("auto", session, T0 + 5, hook_source="automation/dreaming")
    messages = [Message(role="user", content=f"part {index}") for index in range(4)]
    long_turn, following = _run("long", session, T0 + 10, messages=messages), _run("next", session, T0 + 20)

    assert auto_flush._next_batch([automation, long_turn, following], settings) == (
        [automation, long_turn],
        [f"{ALICE}: part 2", f"{ALICE}: part 3"],
    )


def test_each_turn_is_read_as_who_said_what() -> None:
    """Each line names its sender; history replayed into a turn, system and tool messages, and blank text are left out."""
    coalesced = "<messages>" + render_msg_tag(sender=BOB, body="Bob owns Atlas.") + "</messages>"
    messages = [
        Message(role="system", content="system prompt"),
        Message(role="user", content="earlier question", from_history=True),
        Message(role="user", content=coalesced),
        Message(role="user", content="question   with\n spaces"),
        Message(role="tool", content="tool output"),
        Message(role="assistant", content="x" * 600),
        Message(role="assistant", content="   "),
    ]

    lines = auto_flush._run_lines(_run("r1", _thread("t1"), T0 + 10, messages=messages))

    assert lines == [f"{BOB}: Bob owns Atlas.", f"{ALICE}: question with spaces", f"assistant: {'x' * 497}..."]


def test_the_character_limit_bounds_each_extraction(tmp_path: Path) -> None:
    """``max_chars_per_flush`` ends a batch, and a single turn over it keeps only its newest lines."""
    settings = _config(tmp_path, extractor={"max_chars_per_flush": 30}).memory.auto_flush
    session = _thread("t1")
    first, second = _run("r1", session, T0 + 10), _run("r2", session, T0 + 20)

    assert auto_flush._next_batch([first, second], settings) == ([first], ["assistant: answer r1"])


@pytest.mark.asyncio
async def test_failures_back_off_from_when_they_happen_and_are_given_up_after_the_stale_ttl(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """The cooldown doubles from each failure's end, and turns failing for ``stale_ttl_seconds`` are given up."""
    config, clock = _config(tmp_path, stale_ttl_seconds=3600), _Clock()
    worker = await _started(config, clock)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)])
    extractor.error, extractor.clock, extractor.takes = OSError("model unavailable"), clock, 5
    cooldown = config.memory.auto_flush.retry_cooldown_seconds

    await worker._run_cycle(config)
    entry = _entry(tmp_path, session)
    assert (entry["failures"], entry["failing_since"], entry["retry_at"]) == (1, T0 + 1005, T0 + 1005 + cooldown)

    clock.now = T0 + 1005 + cooldown - 1
    await worker._run_cycle(config)
    assert len(extractor.calls) == 1

    clock.now = T0 + 1005 + cooldown
    await worker._run_cycle(config)
    entry = _entry(tmp_path, session)
    assert (entry["failures"], entry["retry_at"]) == (2, clock.now + 2 * cooldown)

    clock.now = T0 + 1005 + 3600 - 5
    await worker._run_cycle(config)
    entry = _entry(tmp_path, session)
    assert (entry["failures"], entry["marker"]) == (0, [T0 + 10, "r1"])
    assert _general_notes(tmp_path) == []

    clock.now += 10_000
    await worker._run_cycle(config)
    assert len(extractor.calls) == 3


@pytest.mark.asyncio
async def test_giving_up_moves_past_the_failing_turns_not_ones_already_written(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """A give-up skips the batch that kept failing, not the turns an unrecorded note already covers."""
    config = _config(tmp_path, stale_ttl_seconds=3600, extractor={"max_messages_per_flush": 2})
    clock = _Clock()
    worker = await _started(config, clock)
    session = _thread("t1")
    _seed(config, session, [_run(f"r{index}", session, T0 + 10 + index) for index in range(1, 4)])
    append_agent_daily_memory(
        f"[auto_flush:{session}:r1] written before its progress was saved",
        "general",
        tmp_path,
        config,
        runtime_paths_for(config),
    )
    extractor.error = OSError("model unavailable")

    await worker._run_cycle(config)
    clock.now += 3600
    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("r2"), _lines("r2")]
    assert _entry(tmp_path, session)["marker"] == [T0 + 12, "r2"]
    extractor.error = None
    await worker._run_cycle(config)
    assert extractor.lines()[-1] == _lines("r3")


@pytest.mark.asyncio
async def test_a_timed_out_extraction_backs_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An extraction over ``max_extraction_seconds`` is abandoned and retried after the cooldown."""
    config = _config(tmp_path, extractor={"max_extraction_seconds": 1})
    worker = await _started(config)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)])

    async def slow(**_: object) -> str:
        await asyncio.sleep(5)
        return "too late"

    monkeypatch.setattr(auto_flush, "_extract_memory_summary", slow)

    await worker._run_cycle(config)

    assert _entry(tmp_path, session)["failures"] == 1
    assert _general_notes(tmp_path) == []


@pytest.mark.asyncio
async def test_an_unreadable_conversation_backs_off_without_blocking_the_others(
    tmp_path: Path,
    extractor: _Extractor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A conversation that cannot be read backs off, gives up until it changes, and the next one is still flushed."""
    config, clock = _config(tmp_path, stale_ttl_seconds=3600), _Clock()
    worker = await _started(config, clock)
    broken, fine = _thread("broken"), _thread("fine")
    _seed(config, broken, [_run("b1", broken, T0 + 10)])
    _seed(config, fine, [_run("f1", fine, T0 + 20)])
    real_session_runs = auto_flush.session_runs

    def session_runs(storage: object, session_id: str) -> list[RunRecord]:
        if session_id == broken:
            msg = "session database is locked"
            raise OSError(msg)
        return real_session_runs(storage, session_id)

    monkeypatch.setattr(auto_flush, "session_runs", session_runs)

    await worker._run_cycle(config)

    assert extractor.lines() == [_lines("f1")]
    entry = _entry(tmp_path, broken)
    cooldown = config.memory.auto_flush.retry_cooldown_seconds
    assert (entry["failures"], entry["retry_at"], entry["updated_at"]) == (1, clock.now + cooldown, 0)

    clock.now += 3600
    await worker._run_cycle(config)
    entry = _entry(tmp_path, broken)
    assert (entry["failures"], entry["updated_at"]) == (0, T0 + 11)


@pytest.mark.asyncio
async def test_budgets_cap_each_pass_and_each_agent_and_passes_take_turns(
    tmp_path: Path,
    extractor: _Extractor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass flushes at most its budget, at most the per-agent cap per agent, and starts with the next agent."""
    agents = {
        "general": AgentConfig(display_name="General", rooms=[]),
        "other": AgentConfig(display_name="Other", rooms=[]),
    }
    config = _config(
        tmp_path,
        agents=agents,
        batch={"max_sessions_per_cycle": 2, "max_sessions_per_agent_per_cycle": 1},
    )
    worker = await _started(config)
    for name in ("g1", "g2", "g3"):
        _seed(config, _thread(name), [_run(name, _thread(name), T0 + 10)])
    for name in ("o1", "o2"):
        _seed(config, _thread(name), [_run(name, _thread(name), T0 + 10)], agent="other")

    await worker._run_cycle(config)
    first = {call["agent_name"] for call in extractor.calls}
    assert first == {"general", "other"}
    assert len(extractor.calls) == 2

    config.memory.auto_flush.batch.max_sessions_per_cycle = 1
    config.memory.auto_flush.batch.max_sessions_per_agent_per_cycle = 3
    listed: list[str] = []
    real_updated_sessions = auto_flush.updated_sessions

    def updated_sessions(storage: object, agent_name: str, *, since: int) -> list[tuple[str, int]]:
        listed.append(agent_name)
        return real_updated_sessions(storage, agent_name, since=since)

    monkeypatch.setattr(auto_flush, "updated_sessions", updated_sessions)
    await worker._run_cycle(config)
    await worker._run_cycle(config)
    assert [call["agent_name"] for call in extractor.calls[2:]] == ["general", "other"]
    # Once a pass has spent its budget, the agents after it are not even listed.
    assert listed == ["general", "other"]


@pytest.mark.asyncio
async def test_no_reply_moves_on_without_extracting_the_same_turns_again(tmp_path: Path, extractor: _Extractor) -> None:
    """When the model finds nothing durable, nothing is written and only later turns are read next time."""
    config = _config(tmp_path)
    worker = await _started(config)
    session = _thread("t1")
    first = _run("r1", session, T0 + 10)
    _seed(config, session, [first])
    extractor.summary = None

    await worker._run_cycle(config)
    await worker._run_cycle(config)
    assert extractor.lines() == [_lines("r1")]
    assert _general_notes(tmp_path) == []

    _seed(config, session, [first, _run("r2", session, T0 + 20)])
    await worker._run_cycle(config)
    assert extractor.lines() == [_lines("r1"), _lines("r2")]


@pytest.mark.asyncio
async def test_cancelling_the_worker_mid_extraction_loses_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shutdown cancels the worker; the conversation it was flushing is flushed by the next start."""
    config, clock = _config(tmp_path), _Clock()
    first_extractor = _Extractor()
    monkeypatch.setattr(auto_flush, "_extract_memory_summary", first_extractor)
    worker = await _started(config, clock)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)])
    entered = asyncio.Event()

    async def hangs(**_: object) -> str:
        entered.set()
        await asyncio.Event().wait()
        return "never"

    monkeypatch.setattr(auto_flush, "_extract_memory_summary", hangs)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert _general_notes(tmp_path) == []

    monkeypatch.setattr(auto_flush, "_extract_memory_summary", first_extractor)
    await _worker(config, clock)._run_cycle(config)
    assert first_extractor.lines() == [_lines("r1")]
    assert len(_general_notes(tmp_path)) == 1


@pytest.mark.asyncio
async def test_a_failed_pass_does_not_end_the_worker(
    tmp_path: Path,
    extractor: _Extractor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pass that fails, for example on an unreadable private-instance listing, is logged and the next one runs."""
    config, clock = _config(tmp_path), _Clock()
    config.memory.auto_flush.flush_interval_seconds = 0
    worker = _worker(config, clock)
    real_scopes = auto_flush._flush_scopes
    passes = 0
    second_pass = asyncio.Event()

    def scopes(*args: object) -> object:
        nonlocal passes
        passes += 1
        if passes == 1:
            msg = "private instances could not be listed"
            raise OSError(msg)
        second_pass.set()
        return real_scopes(*args)

    monkeypatch.setattr(auto_flush, "_flush_scopes", scopes)
    task = asyncio.create_task(worker.run())
    await asyncio.wait_for(second_pass.wait(), timeout=10)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert passes >= 2
    assert extractor.calls == []


@pytest.mark.asyncio
async def test_an_unreadable_progress_file_starts_over_without_flushing_history(
    tmp_path: Path,
    extractor: _Extractor,
) -> None:
    """A corrupt progress file starts again from the current pass, so no earlier conversation is flushed."""
    config, clock = _config(tmp_path), _Clock()
    worker = await _started(config, clock)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)])
    (tmp_path / "tracking" / "memory_auto_flush.json").write_text("{", encoding="utf-8")

    await worker._run_cycle(config)

    assert extractor.calls == []
    state = json.loads((tmp_path / "tracking" / "memory_auto_flush.json").read_text(encoding="utf-8"))
    assert state["started_at"] == clock.now


@pytest.mark.asyncio
async def test_only_agents_whose_effective_backend_is_file_are_flushed(tmp_path: Path, extractor: _Extractor) -> None:
    """A per-agent file backend is flushed under a global Mem0 default, and a per-agent Mem0 override is not."""
    agents = {
        "general": AgentConfig(display_name="General", rooms=[], memory_backend="file"),
        "other": AgentConfig(display_name="Other", rooms=[], memory_backend="mem0"),
    }
    config = _config(tmp_path, agents=agents)
    config.memory.backend = "mem0"
    worker = await _started(config)
    _seed(config, _thread("g"), [_run("g1", _thread("g"), T0 + 10)])
    _seed(config, _thread("o"), [_run("o1", _thread("o"), T0 + 10)], agent="other")

    await worker._run_cycle(config)

    assert [call["agent_name"] for call in extractor.calls] == ["general"]
    with pytest.raises(StopIteration):
        _entry(tmp_path, _thread("o"))


@pytest.mark.asyncio
async def test_a_shared_agent_note_lands_in_its_canonical_workspace(tmp_path: Path, extractor: _Extractor) -> None:
    """A shared agent's notes go to its workspace's daily file whatever its worker scope or memory.file.path."""
    config = _config(tmp_path)
    config.agents["general"].worker_scope = "user"
    config.memory.file.path = str(tmp_path / "shared-memory")
    worker = await _started(config)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)])

    await worker._run_cycle(config)

    assert [call["execution_identity"] for call in extractor.calls] == [None]
    assert len(_general_notes(tmp_path)) == 1
    assert not list((tmp_path / "shared-memory").rglob("*.md"))


@pytest.mark.asyncio
async def test_a_note_warms_the_semantic_index(
    tmp_path: Path,
    extractor: _Extractor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A written note schedules a background refresh of the agent's semantic file-memory index."""
    config = _config(tmp_path)
    config.memory.search.mode = "semantic"
    scheduled: list[tuple[str, Config]] = []

    class FakeScheduler:
        def schedule_refresh(self, base_id: str, **kwargs: object) -> None:
            scheduled.append((base_id, cast("Config", kwargs["config"])))

    monkeypatch.setattr(semantic_file_search, "_memory_refresh_scheduler", FakeScheduler())
    worker = await _started(config)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)])

    await worker._run_cycle(config)

    [(base_id, scheduled_config)] = scheduled
    workspace = agent_workspace_root_path(tmp_path, "general")
    assert scheduled_config.knowledge_bases[base_id].path == str(workspace.resolve())
    assert extractor.calls


@pytest.mark.asyncio
async def test_existing_memories_reach_the_extractor(tmp_path: Path, extractor: _Extractor) -> None:
    """The extractor sees the agent's canonical memories, from MEMORY.md and daily notes, to avoid repeating them."""
    config = _config(tmp_path)
    config.agents["general"].worker_scope = "user"
    config.memory.auto_flush.extractor.include_memory_context.snippet_max_chars = 200
    alice = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id=ALICE,
        room_id=ROOM,
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    with tool_execution_identity(alice):
        await add_agent_memory("Alice-authored shared memory", "general", tmp_path, config, runtime_paths_for(config))
    append_agent_daily_memory("Shared daily memory", "general", tmp_path, config, runtime_paths_for(config))
    worker = await _started(config)
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)])

    await worker._run_cycle(config)

    [call] = extractor.calls
    context = cast("str", call["existing_context"])
    assert "Alice-authored shared memory" in context
    assert "Shared daily memory" in context


@pytest.mark.asyncio
@pytest.mark.parametrize(("extractor_model", "used_model"), [(None, "default"), ("fast", "fast")])
async def test_extraction_runs_on_the_extractor_model_when_one_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extractor_model: str | None,
    used_model: str,
) -> None:
    """An agent on a slow model can leave extraction to a faster one, and otherwise extracts with its own."""
    config = _config(tmp_path)
    config.memory.auto_flush.extractor.model = extractor_model
    session = _thread("t1")
    _seed(config, session, [_run("r1", session, T0 + 10)])
    requested: list[str] = []

    def model_provider(_config: object, _runtime_paths: object, model_name: str) -> ScriptedModel:
        requested.append(model_name)
        return ScriptedModel([answer("NO_REPLY")])

    monkeypatch.setattr(auto_flush, "build_model_provider", model_provider)

    await auto_flush._extract_memory_summary(
        config=config,
        runtime_paths=runtime_paths_for(config),
        agent_name="general",
        session_id=session,
        lines=_lines("r1"),
        existing_context="",
        execution_identity=None,
    )

    assert requested == [used_model]


def test_credentials_are_removed_before_the_extractor() -> None:
    """A token pasted into a conversation stays out of the extraction prompt and so out of daily memory."""
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    content = f"the deploy token is {token}\npassword: hunter2\nremember the deploy is Friday"

    text = "\n".join(
        auto_flush._run_lines(_run("r1", _thread("t1"), T0 + 10, messages=[Message(role="user", content=content)])),
    )

    assert token not in text
    assert "hunter2" not in text
    # A secret on one line does not swallow the lines after it.
    assert "remember the deploy is Friday" in text


def test_context_that_hooks_attach_to_a_message_stays_out_of_the_extractor() -> None:
    """A location a hook added to the prompt is MindRoom's, so it never reaches daily memory."""
    block = render_enrichment_block([EnrichmentItem(key="location", text="latitude: 47.6")])
    messages = [
        Message(role="user", content=render_msg_tag(sender=ALICE, body=f"When does Atlas ship?\n\n{block}")),
        Message(role="user", content=render_msg_tag(sender=ALICE, body=block)),
    ]

    lines = auto_flush._run_lines(_run("r1", _thread("t1"), T0 + 10, messages=messages))

    assert lines == [f"{ALICE}: When does Atlas ship?"]


def test_extractor_output_becomes_one_line_of_distinct_facts() -> None:
    """List markers and repeats are dropped, and an answer of only the no-reply token means nothing to note."""
    raw = "\n- Atlas ships Friday.\n* atlas ships friday.\n2. Priya owns QA.\nNO_REPLY\n\n"

    assert auto_flush._sanitize_extractor_output(raw, "NO_REPLY") == "Atlas ships Friday. | Priya owns QA."
    assert auto_flush._sanitize_extractor_output("  no_reply \n", "NO_REPLY") is None
