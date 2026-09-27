"""Workspace audit records for delegated agent runs."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
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


@pytest.mark.asyncio
async def test_interrupted_event_publication_preserves_committed_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed flush must leave committed events readable for continuation and finish."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
    )
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
    assert _read_json(_record_dir(handle) / "run.json")["status"] == "completed"


@pytest.mark.asyncio
async def test_event_log_is_streamed_and_appended_without_a_capped_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event log is never loaded into one capped buffer, so a long delegation has no log size ceiling."""
    module = _records_module()
    read = module.read_regular_file_within_root
    capped_reads: list[str] = []

    def spy(root: object, relative_path: object, **kwargs: object) -> bytes:
        capped_reads.append(str(relative_path))
        return read(root, relative_path, **kwargs)

    monkeypatch.setattr(module, "read_regular_file_within_root", spy)
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
    )
    await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "More"}))
    await owner.finish(handle, status="completed", output="Done")

    assert "events.jsonl" not in capped_reads
    assert [event["sequence"] for event in _read_events(_record_dir(handle) / "events.jsonl")] == [1, 2, 3]


@pytest.mark.asyncio
async def test_event_log_refuses_an_oversized_event_line(tmp_path: Path) -> None:
    """A planted multi-megabyte event line is refused instead of loaded, since large values move to artifacts."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
    )
    event_path = _record_dir(handle) / "events.jsonl"
    huge = {"sequence": 2, "kind": "output", "timestamp": "2026-01-01T00:00:00Z", "data": {"content": "x" * (5 << 20)}}
    with event_path.open("a", encoding="utf-8") as events:
        events.write(json.dumps(huge) + "\n")

    with pytest.raises(ValueError, match="unreadable"):
        await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "More"}))


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["line", "log"])
async def test_event_append_refuses_what_would_make_the_log_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    """An event above the line cap, or one that would push the log over its cap, is refused before it is written."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
    )
    event_path = _record_dir(handle) / "events.jsonl"
    if limit == "line":
        # Each field stays below the artifact threshold, so together they exceed the line cap.
        data: dict[str, object] = {f"field_{index}": "x" * (60 << 10) for index in range(80)}
    else:
        monkeypatch.setattr(module, "_MAX_EVENT_LOG_BYTES", event_path.stat().st_size + 64)
        data = {"content": "y" * 128}
    committed = event_path.read_bytes()

    with pytest.raises(ValueError, match="size limit"):
        await owner.append_event(handle, module.DelegationEvent(kind="output", data=data))

    assert event_path.read_bytes() == committed
    if limit == "line":
        await owner.finish(handle, status="completed", output="Done")


@pytest.mark.asyncio
async def test_event_append_never_writes_through_a_hard_link(tmp_path: Path) -> None:
    """An event log hard-linked to a primary file by an older worker is refused instead of appended to."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
    )
    event_path = _record_dir(handle) / "events.jsonl"
    outside = tmp_path / "primary-owned.db"
    outside.write_bytes(event_path.read_bytes())
    event_path.unlink()
    os.link(outside, event_path)

    with pytest.raises(ValueError, match="hard link"):
        await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "More"}))

    assert outside.read_bytes().count(b"\n") == 1


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
@pytest.mark.parametrize(
    ("contents", "error"),
    [(b"\n", "unreadable"), (b"{", "unreadable"), (b"[]\n", "malformed"), (b"\xff\n", "unreadable")],
    ids=["blank-line", "invalid-json", "non-object", "invalid-utf8"],
)
async def test_invalid_event_stream_blocks_finish_without_mutation(tmp_path: Path, contents: bytes, error: str) -> None:
    """Invalid records must fail closed instead of being skipped during settlement."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
    )
    event_path = _record_dir(handle) / "events.jsonl"
    event_path.write_bytes(contents)
    run_path = _record_dir(handle) / "run.json"
    original_run = run_path.read_bytes()

    with pytest.raises(ValueError, match=f"Delegation event stream is {error}"):
        await owner.finish(handle, status="completed", output="Done")

    assert event_path.read_bytes() == contents
    assert run_path.read_bytes() == original_run


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
    assert "Investigate the failure" in (_record_dir(handle) / "transcript.md").read_text(encoding="utf-8")

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
    transcript = (_record_dir(handle) / "transcript.md").read_text(encoding="utf-8")
    assert transcript.index("tool_call") < transcript.index("tool_result")


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
    transcript_path = _record_dir(handle) / "transcript.md"
    stale_run = run_path.read_bytes()
    stale_transcript = transcript_path.read_bytes()
    stale_receipt = _receipt_path(handle).read_bytes()
    event = module.DelegationEvent(
        kind="approval_requested",
        data={"tool_call_id": "pending", "arguments": {"command": "pwd"}},
        status="paused",
        event_id="approval:pending",
    )
    await owner.append_event(handle, event)
    run_path.write_bytes(stale_run)
    transcript_path.write_bytes(stale_transcript)
    _receipt_path(handle).write_bytes(stale_receipt)

    if fresh_event:
        event = module.DelegationEvent(kind="output", data={"content": "Still waiting"}, event_id="fresh-output")
    await owner.append_event(handle, event)

    assert _read_json(run_path)["event_count"] == 2 + fresh_event
    assert _read_json(run_path)["status"] == "paused"
    assert _read_json(_receipt_path(handle))["status"] == "paused"
    transcript = transcript_path.read_text(encoding="utf-8")
    assert "Status: paused" in transcript
    assert "approval_requested" in transcript


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
    stale_transcript = transcript_path.read_bytes()
    stale_receipt = _receipt_path(handle).read_bytes()
    await owner.finish(handle, status="completed", output="durable result", usage={"output_tokens": 4})
    run_path.write_bytes(stale_run)
    transcript_path.write_bytes(stale_transcript)
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

    exported = "\n".join(path.read_text(encoding="utf-8") for path in _record_dir(handle).rglob("*") if path.is_file())
    exported += _receipt_path(handle).read_text(encoding="utf-8")
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


@pytest.mark.parametrize(
    ("leaf_name", "refusal"),
    [
        ("run.json", ValueError),
        ("events.jsonl", ValueError),
        ("transcript.md", None),
        (".record.lock", OSError),
        ("receipt", None),
    ],
)
@pytest.mark.asyncio
async def test_record_mutation_never_follows_a_symlinked_leaf(
    leaf_name: str,
    refusal: type[Exception] | None,
    tmp_path: Path,
) -> None:
    """A record leaf replaced by a link is refused or replaced, never read or written through."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id=f"symlink-{leaf_name.replace('.', 'dot')}",
    )
    target = _receipt_path(handle) if leaf_name == "receipt" else _record_dir(handle) / leaf_name
    target.unlink()
    outside = tmp_path / f"outside-{leaf_name.replace('.', 'dot')}"
    outside.write_text('{"victim": "victim-only note"}\n', encoding="utf-8")
    target.symlink_to(outside)

    async def mutate_record() -> None:
        if leaf_name == "run.json":
            await owner.reopen(handle.locator)
            return
        await owner.append_event(
            handle,
            module.DelegationEvent(
                kind="output",
                event_id=f"symlink:{leaf_name}",
                data={"content": "blocked"},
            ),
        )

    if refusal is None:
        await mutate_record()
        assert not target.is_symlink()
        assert "blocked" in target.read_text(encoding="utf-8") or leaf_name == "receipt"
    else:
        with pytest.raises(refusal):
            await mutate_record()
    assert outside.read_text(encoding="utf-8") == '{"victim": "victim-only note"}\n'
    record_bytes = b"".join(
        path.read_bytes() for path in _record_dir(handle).rglob("*") if path.is_file() and not path.is_symlink()
    )
    assert b"victim-only note" not in record_bytes


@pytest.mark.parametrize("swapped", ["record_dir", "delegations_root", "events_fifo"])
@pytest.mark.asyncio
async def test_record_mutation_refuses_swapped_record_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swapped: str,
) -> None:
    """A record directory or ancestor swapped for a link after validation, or a FIFO log, is refused."""
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
    with pytest.raises((OSError, ValueError)):
        await owner.append_event(handle, module.DelegationEvent(kind="output", data={"content": "attacker"}))

    assert {path: path.read_bytes() if path.is_file() else None for path in victim_tree.rglob("*")} == before


@pytest.mark.asyncio
async def test_oversized_artifact_rejects_symlinked_directory(tmp_path: Path) -> None:
    """An oversized output artifact must remain under its resolved record directory."""
    module = _records_module()
    owner = module.DelegationRecordOwner(_config(), test_runtime_paths(tmp_path))
    handle = await owner.start(
        _metadata(module),
        caller_execution_identity=_identity("caller"),
        child_execution_identity=_identity("child"),
        delegation_id="symlink-artifacts",
    )
    outside = tmp_path / "outside-artifacts"
    outside.mkdir()
    (_record_dir(handle) / "artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        await owner.append_event(
            handle,
            module.DelegationEvent(
                kind="tool_result",
                event_id="large-symlink",
                data={"result": "x" * 100_000},
            ),
        )
    assert list(outside.iterdir()) == []


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
