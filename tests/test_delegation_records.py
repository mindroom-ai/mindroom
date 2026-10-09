"""Primary-owned delegation records and their workspace audit exports."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import importlib
import io
import json
import os
import shutil
import stat
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.redaction import REDACTED
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType


def _records_module() -> ModuleType:
    return importlib.import_module("mindroom.delegation.records")


async def _start(module: ModuleType, owner: object, **overrides: object) -> object:
    return await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        **overrides,
    )


@pytest.mark.asyncio
async def test_interrupted_event_publication_preserves_committed_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed flush must leave committed events readable for continuation and finish."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    event_path = _record_dir(handle) / "events.jsonl"
    committed = event_path.read_bytes()

    def interrupted_flush(_fd: int) -> None:
        message = "Interrupted durable write"
        raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(os, "fsync", interrupted_flush)
        with pytest.raises(OSError, match="Interrupted durable write"):
            await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "Résumé 🙂"}))
    assert event_path.read_bytes() == committed
    reopened = await owner.reopen(handle.locator)
    await owner.append_event(reopened, module.DelegationEvent(kind="output", data={"content": "Resumed"}))
    await owner.finish(reopened, status="completed", output="Done")
    events = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert [event["data"].get("content") for event in events[:2]] == [None, "Resumed"]
    assert _read_json(_record_dir(handle) / "run.json")["status"] == "completed"


@pytest.mark.asyncio
async def test_an_append_whose_state_never_committed_is_dropped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state file is the commit point, so an event logged before a failed state write never counts."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    commit_state = module.write_json_file_durable

    def interrupted_commit(*_args: object, **_kwargs: object) -> None:
        message = "Interrupted state commit"
        raise OSError(message)

    with monkeypatch.context() as fault:
        fault.setattr(module, "write_json_file_durable", interrupted_commit)
        with pytest.raises(OSError, match="Interrupted state commit"):
            await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "Lost"}))
    assert module.write_json_file_durable is commit_state
    assert len(_read_events(handle.state_dir / "events.jsonl")) == 2

    await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "Kept"}))

    for path in (handle.state_dir / "events.jsonl", _record_dir(handle) / "events.jsonl"):
        assert [(event["sequence"], event["data"]) for event in _read_events(path)] == [
            (1, {}),
            (2, {"content": "Kept"}),
        ]


@pytest.mark.asyncio
async def test_a_record_whose_event_log_filled_up_can_still_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events stop short of the log cap, so a long delegation's terminal event still fits and settles the record."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    event_path = _record_dir(handle) / "events.jsonl"
    monkeypatch.setattr(module, "_MAX_EVENT_LOG_BYTES", event_path.stat().st_size + (4 << 10))

    async def fill_the_log() -> None:
        for _ in range(8):
            await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "z" * 1024}))

    with pytest.raises(module.DelegationRecordLimitError, match="size limit"):
        await fill_the_log()
    await owner.finish(handle, status="completed", output="D" * 2048)

    assert _read_json(_record_dir(handle) / "run.json")["status"] == "completed"
    assert _read_events(event_path)[-1]["kind"] == "delegation_finished"


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["line", "log"])
async def test_event_append_refuses_what_would_exceed_a_record_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    """An event above the line cap, or one that would push the log over its cap, is refused before it is written."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    event_path = _record_dir(handle) / "events.jsonl"
    if limit == "line":
        # Each field stays below the artifact threshold, so together they exceed the line cap.
        data: dict[str, object] = {f"field_{index}": "x" * (60 << 10) for index in range(80)}
    else:
        monkeypatch.setattr(module, "_MAX_EVENT_LOG_BYTES", event_path.stat().st_size + 64)
        data = {"content": "y" * 128}
    committed = event_path.read_bytes()

    with pytest.raises(module.DelegationRecordLimitError, match="size limit"):
        await owner.append_event(handle, module.DelegationEvent(kind="output", data=data))

    assert event_path.read_bytes() == committed
    assert (handle.state_dir / "events.jsonl").read_bytes() == committed
    if limit == "line":
        await owner.finish(handle, status="completed", output="Done")


@pytest.mark.asyncio
async def test_a_record_that_reached_its_event_cap_can_still_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events stop one short of the event cap, so the terminal event always fits."""
    module = _records_module()
    monkeypatch.setattr(module, "_MAX_EVENTS", 4)
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    for index in range(2):
        await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": f"part {index}"}))

    with pytest.raises(module.DelegationRecordLimitError, match="size limit"):
        await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "one too many"}))
    await owner.finish(handle, status="completed", output="Done")

    events = _read_events(_record_dir(handle) / "events.jsonl")
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert _read_json(_record_dir(handle) / "run.json")["status"] == "completed"


@pytest.mark.asyncio
async def test_a_task_too_large_to_finish_is_refused_at_start(tmp_path: Path) -> None:
    """A new run.json keeps room for what finish adds, so a record never starts that could not finish."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    near_cap = (4 << 20) - (64 << 10)

    with pytest.raises(module.DelegationRecordLimitError):
        await owner.start(
            _metadata(module, task="t" * near_cap),
            caller_execution_identity=_identity("caller"),
            child_execution_identity=_identity("child"),
        )

    handle = await owner.start(
        _metadata(module, task="t" * (near_cap - 4 * (64 << 10))),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
    )
    large_inline = "v" * ((64 << 10) - 16)
    await owner.finish(handle, status="failed", output=large_inline, error=large_inline, usage={"note": large_inline})
    run = _read_json(_record_dir(handle) / "run.json")
    assert run["status"] == "failed"
    assert run["output"] == large_inline


@pytest.mark.asyncio
async def test_event_append_never_writes_through_a_hard_link(tmp_path: Path) -> None:
    """An event log hard-linked to a primary file by an older worker is replaced instead of appended to."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    event_path = _record_dir(handle) / "events.jsonl"
    outside = tmp_path / "primary-owned.db"
    outside.write_bytes(event_path.read_bytes())
    event_path.unlink()
    os.link(outside, event_path)

    await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "More"}))

    assert outside.read_bytes().count(b"\n") == 1
    assert event_path.stat().st_nlink == 1
    assert event_path.read_bytes() == (handle.state_dir / "events.jsonl").read_bytes()


@pytest.mark.asyncio
async def test_a_lock_worker_code_holds_in_the_record_directory_never_stalls_record_writes(tmp_path: Path) -> None:
    """Worker code shares the record directory, so the primary never waits on a lock taken there."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    failures: list[BaseException] = []

    def append() -> None:
        try:
            owner._append_event(handle, module.DelegationEvent(kind="output", data={"content": "More"}))
        except BaseException as exc:
            failures.append(exc)

    with (_record_dir(handle) / ".record.lock").open("a") as planted_lock:
        fcntl.flock(planted_lock.fileno(), fcntl.LOCK_EX)
        writer = threading.Thread(target=append, daemon=True)
        writer.start()
        writer.join(timeout=5)
        stalled = writer.is_alive()
    writer.join(timeout=5)

    assert not stalled
    assert failures == []
    assert [event["sequence"] for event in _read_events(_record_dir(handle) / "events.jsonl")] == [1, 2]


@pytest.mark.asyncio
async def test_concurrent_appends_to_one_record_keep_one_gapless_sequence(tmp_path: Path) -> None:
    """Record writers still exclude each other without a lock file in the workspace."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)

    await asyncio.gather(
        *(
            owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": f"part {index}"}))
            for index in range(8)
        ),
    )

    events = _read_events(_record_dir(handle) / "events.jsonl")
    assert [event["sequence"] for event in events] == list(range(1, 10))
    assert _read_json(_record_dir(handle) / "run.json")["event_count"] == 9
    assert not (_record_dir(handle) / ".record.lock").exists()


def _replace_with(path: Path, content: bytes) -> None:
    """Swap a different file in at the path, as an editor that writes a new file and renames it would."""
    replacement = path.with_name(f"{path.name}.edited")
    replacement.write_bytes(content)
    replacement.replace(path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "append",
        "rewrite",
        "truncate",
        "replace",
        "garbage",
        "delete",
        "delete_record",
        "edit_run",
        "delete_run",
        "edit_transcript",
    ],
)
async def test_an_edited_or_deleted_export_is_rewritten_from_the_primary_copy(tmp_path: Path, change: str) -> None:
    """Exports are only written, so any edit or deletion is replaced by the next write and the delegation settles."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    await owner.append_event(
        handle,
        module.DelegationEvent(kind="tool_call", data={"tool_name": "lookup"}, event_id="call"),
    )
    record_dir = _record_dir(handle)
    event_path = record_dir / "events.jsonl"
    planted = json.dumps({"sequence": 3, "kind": "output", "timestamp": "t", "data": {"content": "planted"}}) + "\n"
    if change == "append":
        with event_path.open("a", encoding="utf-8") as events:
            events.write(planted)
    elif change == "rewrite":
        event_path.write_text(planted, encoding="utf-8")
    elif change == "truncate":
        os.truncate(event_path, 0)
    elif change == "replace":
        _replace_with(event_path, planted.encode())
    elif change == "garbage":
        event_path.write_bytes(b"[" * 100_000 + b"\xff{\n")
    elif change == "delete":
        event_path.unlink()
    elif change == "delete_record":
        shutil.rmtree(record_dir)
    elif change == "edit_run":
        (record_dir / "run.json").write_text('{"status": "completed", "event_count": "many"}', encoding="utf-8")
    elif change == "delete_run":
        (record_dir / "run.json").unlink()
    else:
        (record_dir / "transcript.md").write_text("forged transcript\n", encoding="utf-8")

    await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "Next"}, event_id="next"))

    primary_log = handle.state_dir / "events.jsonl"
    assert event_path.read_bytes() == primary_log.read_bytes()
    assert [event["event_id"] for event in _read_events(event_path)] == ["delegation_started", "call", "next"]
    assert _read_json(record_dir / "run.json")["event_count"] == 3
    assert _read_json(record_dir / "run.json")["status"] == "running"

    await owner.finish(handle, status="completed", output="Done")

    assert event_path.read_bytes() == primary_log.read_bytes()
    assert [event["sequence"] for event in _read_events(event_path)] == [1, 2, 3, 4]
    run = _read_json(record_dir / "run.json")
    assert (run["status"], run["output"], run["event_count"]) == ("completed", "Done", 4)
    transcript = (record_dir / "transcript.md").read_text(encoding="utf-8")
    assert "forged" not in transcript
    assert "planted" not in transcript
    assert (
        transcript.index("delegation_started") < transcript.index("tool_call") < transcript.index("delegation_finished")
    )
    assert _read_json(_receipt_path(handle))["status"] == "completed"


@pytest.mark.asyncio
async def test_the_primary_never_reads_a_workspace_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every workspace file a record operation opens, it opens to write, whatever worker code left there."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    workspace = str(handle.child_workspace)
    reads: list[str] = []
    os_open = os.open
    io_open = io.open

    def spy_os_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        descriptor = os_open(path, flags, *args, **kwargs)
        if flags & os.O_ACCMODE == os.O_RDONLY and stat.S_ISREG(os.fstat(descriptor).st_mode):
            reads.append(str(path))
        return descriptor

    def spy_io_open(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        if "r" in mode and "+" not in mode and str(file).startswith(workspace):
            reads.append(str(file))
        return io_open(file, mode, *args, **kwargs)

    # A large value adds an artifact, and the replays take the deduplication and repeated-finish paths.
    big = module.DelegationEvent(kind="output", data={"content": "x" * 100_000}, event_id="big")
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(io, "open", spy_io_open)
    await owner.reopen(handle.locator)
    for _ in range(2):
        await owner.append_event(handle, big)
    for _ in range(2):
        await owner.finish(handle, status="completed", output="Done")
    monkeypatch.undo()

    assert reads == []
    assert [event["event_id"] for event in _read_events(_record_dir(handle) / "events.jsonl")] == [
        "delegation_started",
        "big",
        "delegation_finished",
    ]


@pytest.mark.asyncio
async def test_dedup_and_sequence_survive_a_restart_without_the_workspace_record(tmp_path: Path) -> None:
    """A restarted primary continues from its own copy, even after the child deleted every export."""
    module = _records_module()
    runtime_paths = test_runtime_paths(tmp_path)
    owner = module.DelegationRecordOwner(_config(), runtime_paths)
    handle = await _start(module, owner, delegation_id="restarted")
    replayed = module.DelegationEvent(kind="tool_result", data={"result": "kept once"}, event_id="tool:call-1:result")
    await owner.append_event(handle, replayed)
    shutil.rmtree(_record_dir(handle))
    _receipt_path(handle).unlink()

    restarted = module.DelegationRecordOwner(_config(), runtime_paths)
    reopened = await restarted.reopen(module.DelegationRecordLocator.from_dict(handle.locator.to_dict()))
    await restarted.append_event(reopened, replayed)
    await restarted.append_event(reopened, module.DelegationEvent(kind="output", data={"content": "after restart"}))
    await restarted.finish(reopened, status="completed", output="Done")

    events = _read_events(_record_dir(reopened) / "events.jsonl")
    assert [(event["sequence"], event["kind"]) for event in events] == [
        (1, "delegation_started"),
        (2, "tool_result"),
        (3, "output"),
        (4, "delegation_finished"),
    ]
    assert _read_json(_record_dir(reopened) / "run.json")["event_count"] == 4
    assert _read_json(_receipt_path(reopened))["status"] == "completed"
    assert "after restart" in (_record_dir(reopened) / "transcript.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_record_started_before_primary_state_is_left_as_it_was(tmp_path: Path) -> None:
    """A record with only workspace files settles without touching them; a record found nowhere stays missing."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner, delegation_id="before-upgrade")
    await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "Before"}, status="paused"))
    shutil.rmtree(handle.state_dir)
    exported = {path: path.read_bytes() for path in _record_dir(handle).rglob("*") if path.is_file()}
    receipt = _receipt_path(handle).read_bytes()

    reopened = await owner.reopen(handle.locator)
    await owner.append_event(
        reopened,
        module.DelegationEvent(kind="output", data={"content": "After"}, event_id="after"),
    )
    await owner.finish(reopened, status="completed", output="Done")

    assert {path: path.read_bytes() for path in _record_dir(handle).rglob("*") if path.is_file()} == exported
    assert _receipt_path(handle).read_bytes() == receipt
    assert not handle.state_dir.exists()
    shutil.rmtree(_record_dir(handle))
    with pytest.raises(FileNotFoundError):
        await owner.reopen(handle.locator)


@pytest.mark.asyncio
async def test_a_log_planted_where_a_new_record_starts_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new record's exports come from its primary copy, so files planted where it will live are replaced."""
    module = _records_module()
    monkeypatch.setattr(module, "_utc_timestamp", lambda: "2026-09-28T00:00:00Z")
    runtime_paths = test_runtime_paths(tmp_path)
    owner = module.DelegationRecordOwner(_config(), runtime_paths)
    record_dir = (
        runtime_paths.storage_root / "agents" / "child" / "workspace" / ".mindroom/delegations/2026-09-28/planted"
    )
    record_dir.mkdir(parents=True)
    planted = json.dumps({"sequence": 1, "kind": "output", "timestamp": "t", "data": {}}) + "\n"
    (record_dir / "events.jsonl").write_text(planted * 3, encoding="utf-8")
    (record_dir / "run.json").write_text('{"status": "completed"}', encoding="utf-8")

    handle = await _start(module, owner, delegation_id="planted")

    assert [event["kind"] for event in _read_events(record_dir / "events.jsonl")] == ["delegation_started"]
    assert (record_dir / "events.jsonl").read_bytes() == (handle.state_dir / "events.jsonl").read_bytes()
    assert _read_json(record_dir / "run.json")["status"] == "running"


def _config(*, private_child: bool = False) -> Config:
    child = AgentConfig(display_name="Child")
    if private_child:
        child.private = AgentPrivateConfig(per="user", root="child_data")
    return Config(
        agents={
            "caller": AgentConfig(display_name="Caller"),
            "child": child,
        },
        models={"default": ModelConfig(provider="test", id="test-model")},
    )


def _identity(agent_name: str, requester_id: str = "@alice:localhost") -> ToolExecutionIdentity:
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:localhost",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id=f"session-{agent_name}",
    )


def _metadata(module: ModuleType, **overrides: object) -> object:
    values = {
        "caller_agent_name": "caller",
        "child_agent_name": "child",
        "requester_id": "@alice:localhost",
        "parent_run_id": "parent-run",
        "parent_tool_call_id": "parent-call",
        "source_room_id": "!room:localhost",
        "source_thread_id": "$thread",
        "model_name": "test-model",
        "task": "Investigate the failure",
        "parent_delegation_id": None,
        "subagent_id": None,
        "previous_delegation_id": None,
    }
    values.update(overrides)
    return module.DelegationMetadata(**values)


def _record_dir(handle: object) -> Path:
    return handle.child_workspace / handle.scoped_path


def _receipt_path(handle: object) -> Path:
    return handle.caller_workspace / _records_module()._receipt_relative_path(handle.locator)


def _read_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _read_events(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "separator",
    ["\u0085", "\u2028", "\u2029"],
    ids=["next-line", "line-separator", "paragraph-separator"],
)
async def test_unicode_separators_preserve_delegation_event_boundaries(tmp_path: Path, separator: str) -> None:
    """Unicode separators inside JSON strings must not split durable events."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
    )
    content = f"First{separator}second"
    await owner.append_event(handle, module.DelegationEvent(kind="tool_result", data={"result": content}))
    event_path = _record_dir(handle) / "events.jsonl"
    committed = event_path.read_bytes()
    assert separator.encode("utf-8") in committed

    reopened = await owner.reopen(handle.locator)
    await owner.finish(reopened, status="completed", output=content)

    assert event_path.read_bytes().startswith(committed)
    events = _read_events(event_path)
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert events[1]["data"]["result"] == content
    assert events[2]["data"]["output"] == content
    run = _read_json(_record_dir(handle) / "run.json")
    assert run["status"] == "completed"
    assert run["output"] == content
    assert content in (_record_dir(handle) / "transcript.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_start_writes_initial_record_and_restart_safe_parent_receipt(tmp_path: Path) -> None:
    """Removing any initial export or persisted locator identity must fail this test."""
    module = _records_module()
    runtime_paths = test_runtime_paths(tmp_path)
    owner = module.DelegationRecordOwner(_config(), runtime_paths)

    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="delegation-123",
    )

    expected_scoped_path = ".mindroom/delegations"
    assert handle.locator.delegation_id == "delegation-123"
    assert handle.scoped_path.startswith(f"{expected_scoped_path}/")
    assert _record_dir(handle) == (runtime_paths.storage_root / "agents" / "child" / "workspace" / handle.scoped_path)
    # The working state lives in primary-only storage, and the workspace log is an exact export of it.
    assert handle.state_dir == (
        runtime_paths.storage_root
        / "tracking"
        / "agents"
        / "child"
        / "delegations"
        / handle.scoped_path.split("/", 2)[2]
    )
    assert (_record_dir(handle) / "events.jsonl").read_bytes() == (handle.state_dir / "events.jsonl").read_bytes()
    run = _read_json(_record_dir(handle) / "run.json")
    assert run == {
        "schema_version": 1,
        "delegation_id": "delegation-123",
        "caller_agent_name": "caller",
        "child_agent_name": "child",
        "requester_id": "@alice:localhost",
        "parent_run_id": "parent-run",
        "parent_tool_call_id": "parent-call",
        "parent_delegation_id": None,
        "subagent_id": None,
        "previous_delegation_id": None,
        "source_room_id": "!room:localhost",
        "source_thread_id": "$thread",
        "model_name": "test-model",
        "task": "Investigate the failure",
        "status": "running",
        "started_at": run["started_at"],
        "updated_at": run["updated_at"],
        "finished_at": None,
        "output": None,
        "error": None,
        "usage": None,
        "event_count": 1,
        "record_reference": f"{handle.locator.child_agent_name}:{handle.scoped_path}",
    }
    assert isinstance(run["started_at"], str)
    assert str(run["started_at"]).endswith("Z")
    events = _read_events(_record_dir(handle) / "events.jsonl")
    assert [(event["sequence"], event["kind"]) for event in events] == [(1, "delegation_started")]
    # Rendering reads the whole log, so the transcript waits for the terminal event.
    assert not (_record_dir(handle) / "transcript.md").exists()

    receipt = _read_json(_receipt_path(handle))
    assert receipt["delegation_id"] == "delegation-123"
    expected_reference = f"{handle.locator.child_agent_name}:{handle.scoped_path}"
    assert receipt["record_reference"] == expected_reference
    assert receipt["status"] == "running"
    assert "delegation-123" in handle.to_receipt()
    assert expected_reference in handle.to_receipt()

    serialized = handle.locator.to_dict()
    reopened = await owner.reopen(module.DelegationRecordLocator.from_dict(serialized))

    assert _record_dir(reopened) == _record_dir(handle)
    assert _receipt_path(reopened) == _receipt_path(handle)


@pytest.mark.asyncio
async def test_append_event_preserves_order_and_actual_tool_payloads(tmp_path: Path) -> None:
    """Replacing full tool payloads with previews or reordering events must fail this test."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="ordered-events",
    )

    await owner.append_event(
        handle,
        module.DelegationEvent(
            kind="tool_call",
            data={
                "tool_call_id": "call-1",
                "tool_name": "lookup",
                "arguments": {"query": "full search terms", "limit": 25},
            },
        ),
    )
    await owner.append_event(
        handle,
        module.DelegationEvent(
            kind="tool_result",
            data={
                "tool_call_id": "call-1",
                "result": {"items": [{"name": "first"}, {"name": "second"}]},
            },
        ),
    )

    events = _read_events(_record_dir(handle) / "events.jsonl")
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert events[1]["data"] == {
        "tool_call_id": "call-1",
        "tool_name": "lookup",
        "arguments": {"query": "full search terms", "limit": 25},
    }
    assert events[2]["data"] == {
        "tool_call_id": "call-1",
        "result": {"items": [{"name": "first"}, {"name": "second"}]},
    }
    assert _read_json(_record_dir(handle) / "run.json")["event_count"] == 3
    await owner.finish(handle, status="completed", output="Done")
    transcript = (_record_dir(handle) / "transcript.md").read_text(encoding="utf-8")
    assert "Investigate the failure" in transcript
    assert transcript.index("tool_call") < transcript.index("tool_result") < transcript.index("delegation_finished")


@pytest.mark.asyncio
async def test_append_event_updates_paused_status_and_reopened_receipt(tmp_path: Path) -> None:
    """Losing an approval pause or its caller-visible state after reopen must fail this test."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="pause-resume",
    )
    reopened = await owner.reopen(module.DelegationRecordLocator.from_dict(handle.locator.to_dict()))

    await owner.append_event(
        reopened,
        module.DelegationEvent(
            kind="approval_requested",
            data={"tool_call_id": "pending-1", "tool_name": "shell", "arguments": {"command": "pwd"}},
            status="paused",
        ),
    )

    assert _read_json(_record_dir(handle) / "run.json")["status"] == "paused"
    assert _read_json(_receipt_path(handle))["status"] == "paused"


@pytest.mark.asyncio
async def test_append_event_deduplicates_stable_event_id_after_reopen(tmp_path: Path) -> None:
    """Replaying an observed stream event after restart must not duplicate its audit entry."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="event-replay",
    )
    event = module.DelegationEvent(
        kind="tool_result",
        data={"tool_call_id": "call-1", "result": "kept once"},
        event_id="tool:call-1:result",
    )

    await owner.append_event(handle, event)
    reopened = await owner.reopen(module.DelegationRecordLocator.from_dict(handle.locator.to_dict()))
    await owner.append_event(reopened, event)

    events = _read_events(_record_dir(handle) / "events.jsonl")
    assert [entry["event_id"] for entry in events] == [
        "delegation_started",
        "tool:call-1:result",
    ]
    assert _read_json(_record_dir(handle) / "run.json")["event_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_event", [False, True])
async def test_event_append_repairs_views_after_interrupted_write(tmp_path: Path, fresh_event: bool) -> None:
    """A replayed or fresh event must repair views left stale by interruption."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="event-repair",
    )
    run_path = _record_dir(handle) / "run.json"
    stale_run = run_path.read_bytes()
    stale_receipt = _receipt_path(handle).read_bytes()
    event = module.DelegationEvent(
        kind="approval_requested",
        data={"tool_call_id": "pending", "arguments": {"command": "pwd"}},
        status="paused",
        event_id="approval:pending",
    )
    await owner.append_event(handle, event)
    run_path.write_bytes(stale_run)
    _receipt_path(handle).write_bytes(stale_receipt)

    if fresh_event:
        event = module.DelegationEvent(kind="output", data={"content": "Still waiting"}, event_id="fresh-output")
    await owner.append_event(handle, event)

    assert _read_json(run_path)["event_count"] == 2 + fresh_event
    assert _read_json(run_path)["status"] == "paused"
    assert _read_json(_receipt_path(handle))["status"] == "paused"
    assert not (_record_dir(handle) / "transcript.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_fresh_event", [False, True])
async def test_repeated_finish_repairs_stale_terminal_views(tmp_path: Path, reject_fresh_event: bool) -> None:
    """Replaying finish must rebuild stale run, transcript, and receipt without a duplicate event."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="finish-repair",
    )
    run_path = _record_dir(handle) / "run.json"
    transcript_path = _record_dir(handle) / "transcript.md"
    stale_run = run_path.read_bytes()
    stale_receipt = _receipt_path(handle).read_bytes()
    await owner.finish(handle, status="completed", output="durable result", usage={"output_tokens": 4})
    run_path.write_bytes(stale_run)
    transcript_path.write_text("stale\n", encoding="utf-8")
    _receipt_path(handle).write_bytes(stale_receipt)

    if reject_fresh_event:
        with pytest.raises(ValueError, match="already terminal"):
            await owner.append_event(
                handle,
                module.DelegationEvent(kind="output", data={"content": "late output"}, event_id="late-output"),
            )
    await owner.finish(handle, status="completed", output="durable result", usage={"output_tokens": 4})

    run = _read_json(run_path)
    assert run["status"] == "completed"
    assert run["output"] == "durable result"
    assert run["usage"] == {"output_tokens": 4}
    assert _read_json(_receipt_path(handle))["status"] == "completed"
    assert "Status: completed" in transcript_path.read_text(encoding="utf-8")
    assert [event["kind"] for event in _read_events(_record_dir(handle) / "events.jsonl")].count(
        "delegation_finished",
    ) == 1


@pytest.mark.asyncio
async def test_a_settled_record_keeps_its_first_terminal_outcome(tmp_path: Path) -> None:
    """A later finish with another terminal status, such as a stale cancellation, keeps the first outcome."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner)
    await owner.finish(handle, status="failed", error="Child failed")

    await owner.finish(handle, status="cancelled", error="Delegation cancelled.")

    run = _read_json(_record_dir(handle) / "run.json")
    assert (run["status"], run["error"]) == ("failed", "Child failed")
    assert _read_json(_receipt_path(handle))["status"] == "failed"
    assert [event["kind"] for event in _read_events(handle.state_dir / "events.jsonl")] == [
        "delegation_started",
        "delegation_finished",
    ]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "denied"])
@pytest.mark.asyncio
async def test_finish_persists_each_terminal_outcome(status: str, tmp_path: Path) -> None:
    """Dropping or conflating any terminal outcome must fail this test."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id=f"terminal-{status}",
    )
    output = "Final delegated answer" if status == "completed" else None
    error = None if status == "completed" else f"Delegation {status}"

    await owner.finish(
        handle,
        status=status,
        output=output,
        error=error,
        usage={"input_tokens": 11, "output_tokens": 7},
    )

    run = _read_json(_record_dir(handle) / "run.json")
    assert run["status"] == status
    assert run["output"] == output
    assert run["error"] == error
    assert run["usage"] == {"input_tokens": 11, "output_tokens": 7}
    assert isinstance(run["finished_at"], str)
    assert _read_json(_receipt_path(handle))["status"] == status
    assert _read_events(_record_dir(handle) / "events.jsonl")[-1]["kind"] == "delegation_finished"
    assert f"Status: {status}" in (_record_dir(handle) / "transcript.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_all_record_surfaces_redact_credentials(tmp_path: Path) -> None:
    """Writing a secret from metadata, arguments, results, or errors must fail this test."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module, task="Use api_key=sk-example-task-secret"),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="redacted",
    )
    await owner.append_event(
        handle,
        module.DelegationEvent(
            kind="tool_result",
            data={
                "arguments": {"api_key": "sk-example-argument-secret"},
                "result": "Authorization: Bearer example-result-secret",
            },
        ),
    )
    await owner.finish(
        handle,
        status="failed",
        error="password=example-error-secret",
    )

    stored = [*_record_dir(handle).rglob("*"), *handle.state_dir.rglob("*"), _receipt_path(handle)]
    exported = "\n".join(path.read_text(encoding="utf-8") for path in stored if path.is_file())
    for secret in (
        "sk-example-task-secret",
        "sk-example-argument-secret",
        "example-result-secret",
        "example-error-secret",
    ):
        assert secret not in exported
    assert REDACTED in exported


@pytest.mark.asyncio
async def test_oversized_tool_output_is_preserved_as_redacted_artifact(tmp_path: Path) -> None:
    """Discarding or silently truncating a large tool result must fail this test."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="large-output",
    )
    large_output = "large-result-" + ("é" * 100_000)

    await owner.append_event(
        handle,
        module.DelegationEvent(
            kind="tool_result",
            data={"tool_call_id": "large-call", "result": large_output},
        ),
    )

    event = _read_events(_record_dir(handle) / "events.jsonl")[-1]
    data = event["data"]
    assert isinstance(data, dict)
    artifact_reference = data["result"]
    assert isinstance(artifact_reference, dict)
    assert artifact_reference["oversized"] is True
    assert artifact_reference["redacted"] is True
    artifact_path = _record_dir(handle) / str(artifact_reference["artifact_path"])
    assert artifact_path.is_file()
    artifact_bytes = artifact_path.read_bytes()
    assert json.loads(artifact_bytes) == large_output
    assert artifact_reference["byte_count"] == len(artifact_bytes)
    assert artifact_reference["sha256"] == hashlib.sha256(artifact_bytes).hexdigest()


@pytest.mark.asyncio
async def test_oversized_dict_artifact_hashes_exact_written_bytes(tmp_path: Path) -> None:
    """Artifact metadata must hash the serialized bytes on disk for mappings too."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="large-mapping",
    )
    large_output = {"z-last": "z" * 40_000, "a-first": "a" * 40_000}

    await owner.append_event(
        handle,
        module.DelegationEvent(kind="tool_result", data={"result": large_output}),
    )

    reference = _read_events(_record_dir(handle) / "events.jsonl")[-1]["data"]["result"]
    artifact_bytes = (_record_dir(handle) / reference["artifact_path"]).read_bytes()
    assert reference["byte_count"] == len(artifact_bytes)
    assert reference["sha256"] == hashlib.sha256(artifact_bytes).hexdigest()


@pytest.mark.asyncio
async def test_private_child_record_uses_requester_scope_and_caller_receipt(tmp_path: Path) -> None:
    """Routing a private child's record into shared state or another requester must fail this test."""
    module = _records_module()
    runtime_paths = test_runtime_paths(tmp_path)
    owner = module.DelegationRecordOwner(_config(private_child=True), runtime_paths)
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="private-child",
    )

    assert _record_dir(handle).is_relative_to(runtime_paths.storage_root / "private_instances")
    assert handle.state_dir.is_relative_to(runtime_paths.storage_root / "tracking" / "private_instances")
    assert _receipt_path(handle).is_relative_to(
        runtime_paths.storage_root / "agents" / "caller" / "workspace",
    )
    assert not (runtime_paths.storage_root / "agents" / "child" / "workspace" / ".mindroom" / "delegations").exists()

    wrong_scope = handle.locator.to_dict()
    wrong_identity = dict(wrong_scope["child_execution_identity"])
    wrong_identity["requester_id"] = "@bob:localhost"
    wrong_scope["child_execution_identity"] = wrong_identity
    with pytest.raises(FileNotFoundError):
        await owner.reopen(module.DelegationRecordLocator.from_dict(wrong_scope))


def test_locator_rejects_malformed_present_execution_identity() -> None:
    """A malformed retained identity must never fall back to shared scope."""
    module = _records_module()
    locator = {
        "delegation_id": "strict-identity",
        "started_date": "2026-09-14",
        "caller_agent_name": "caller",
        "child_agent_name": "child",
        "caller_execution_identity": None,
        "child_execution_identity": {"channel": "invalid", "agent_name": "child"},
    }

    with pytest.raises(TypeError, match="channel"):
        module.DelegationRecordLocator.from_dict(locator)


@pytest.mark.parametrize("leaf_name", ["run.json", "events.jsonl", "transcript.md", "receipt"])
@pytest.mark.asyncio
async def test_record_exports_never_follow_a_symlinked_leaf(leaf_name: str, tmp_path: Path) -> None:
    """An export replaced by a link is replaced in turn, never read or written through."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id=f"symlink-{leaf_name.replace('.', 'dot')}",
    )
    target = _receipt_path(handle) if leaf_name == "receipt" else _record_dir(handle) / leaf_name
    target.unlink(missing_ok=leaf_name == "transcript.md")
    outside = tmp_path / f"outside-{leaf_name.replace('.', 'dot')}"
    outside.write_text('{"victim": "victim-only note"}\n', encoding="utf-8")
    target.symlink_to(outside)

    if leaf_name == "transcript.md":
        await owner.finish(handle, status="completed", output="written")
    else:
        await owner.append_event(
            handle,
            module.DelegationEvent(kind="output", event_id=f"symlink:{leaf_name}", data={"content": "written"}),
        )

    assert not target.is_symlink()
    if leaf_name in {"run.json", "receipt"}:
        assert _read_json(target)["delegation_id"] == handle.locator.delegation_id
    else:
        assert "written" in target.read_text(encoding="utf-8")
    assert outside.read_text(encoding="utf-8") == '{"victim": "victim-only note"}\n'
    record_bytes = b"".join(path.read_bytes() for path in _record_dir(handle).rglob("*") if path.is_file())
    assert b"victim-only note" not in record_bytes


@pytest.mark.parametrize("swapped", ["record_dir", "delegations_root", "events_fifo"])
@pytest.mark.asyncio
async def test_record_mutation_refuses_swapped_record_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swapped: str,
) -> None:
    """A record directory or ancestor swapped for a link is never written through, and a FIFO log is replaced."""
    module = _records_module()
    runtime_paths = test_runtime_paths(tmp_path)
    owner = module.DelegationRecordOwner(_config(), runtime_paths)
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id=f"swap-{swapped.replace('_', '-')}",
    )
    victim = tmp_path / "victim-workspace" / "record"
    victim.mkdir(parents=True)
    (victim / "events.jsonl").write_text('{"victim": "victim-only note"}\n', encoding="utf-8")
    victim_tree = victim.parent
    before = {path: path.read_bytes() if path.is_file() else None for path in victim_tree.rglob("*")}
    validated_handle = module.DelegationRecordOwner._validated_handle

    def swap_after_validation(self: object, record_handle: object) -> object:
        # Worker code races the primary between path validation and the record I/O.
        validated = validated_handle(self, record_handle)
        if swapped == "record_dir":
            _record_dir(handle).rename(_record_dir(handle).with_name("moved"))
            _record_dir(handle).symlink_to(victim, target_is_directory=True)
        elif swapped == "delegations_root":
            delegations = _record_dir(handle).parents[1]
            delegations.rename(delegations.with_name("moved"))
            delegations.symlink_to(victim.parent, target_is_directory=True)
        else:
            (_record_dir(handle) / "events.jsonl").unlink()
            os.mkfifo(_record_dir(handle) / "events.jsonl")
        return validated

    monkeypatch.setattr(module.DelegationRecordOwner, "_validated_handle", swap_after_validation)
    await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "after"}))

    if swapped == "events_fifo":
        assert (_record_dir(handle) / "events.jsonl").read_bytes() == (handle.state_dir / "events.jsonl").read_bytes()
    assert _read_events(handle.state_dir / "events.jsonl")[-1]["data"] == {"content": "after"}
    assert {path: path.read_bytes() if path.is_file() else None for path in victim_tree.rglob("*")} == before


@pytest.mark.parametrize("planted", ["run.json", "events.jsonl", "transcript.md", "receipt", "record_dir", "artifacts"])
@pytest.mark.asyncio
async def test_an_export_worker_code_made_unwritable_never_fails_the_record(planted: str, tmp_path: Path) -> None:
    """A directory or link planted where an export goes skips that export, and events and finish still commit."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await _start(module, owner, delegation_id=f"planted-{planted.replace('.', '-').replace('_', '-')}")
    record_dir = _record_dir(handle)
    outside = tmp_path / "outside"
    outside.mkdir()
    if planted == "record_dir":
        shutil.rmtree(record_dir)
        record_dir.symlink_to(outside, target_is_directory=True)
    elif planted == "artifacts":
        (record_dir / "artifacts").symlink_to(outside, target_is_directory=True)
    else:
        target = _receipt_path(handle) if planted == "receipt" else record_dir / planted
        target.unlink(missing_ok=True)
        target.mkdir()
    decision = module.DelegationEvent(
        kind="approval_decision",
        data={"tool_call_id": "gated", "approved": True, "reason": "x" * 100_000},
        status="running",
        event_id="decision",
    )

    await owner.append_event(handle, decision)
    await owner.append_event(handle, decision)
    await owner.finish(handle, status="completed", output="Done")

    events = _read_events(handle.state_dir / "events.jsonl")
    assert [event["kind"] for event in events] == ["delegation_started", "approval_decision", "delegation_finished"]
    assert _read_json(handle.state_dir / "state.json")["run"]["status"] == "completed"
    assert list(outside.iterdir()) == []
    if planted != "receipt":
        assert _read_json(_receipt_path(handle))["status"] == "completed"


@pytest.mark.asyncio
async def test_self_delegation_creates_one_transcript_and_minimal_receipt(tmp_path: Path) -> None:
    """Creating a second transcript for a self-call must fail this test."""
    module = _records_module()
    config = Config(
        agents={"caller": AgentConfig(display_name="Caller")},
        models={"default": ModelConfig(provider="test", id="test-model")},
    )
    runtime_paths = test_runtime_paths(tmp_path)
    owner = module.DelegationRecordOwner(config, runtime_paths)
    identity = _identity("caller")
    handle = await owner.start(
        _metadata(module, child_agent_name="caller"),
        caller_execution_identity=identity,
        child_execution_identity=identity,
        delegation_id="self-call",
    )
    await owner.finish(handle, status="completed", output="Done")

    workspace = runtime_paths.storage_root / "agents" / "caller" / "workspace"
    assert list(workspace.rglob("transcript.md")) == [_record_dir(handle) / "transcript.md"]
    receipt = _read_json(_receipt_path(handle))
    assert set(receipt) == {
        "schema_version",
        "delegation_id",
        "caller_agent_name",
        "child_agent_name",
        "status",
        "record_reference",
        "started_at",
        "updated_at",
        "finished_at",
    }
