"""Tests for voice-call transcripts and their memory reference."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.calls import CallAgentConfig, CallsConfig
from mindroom.config.main import Config
from mindroom.matrix_rtc.transcript import CallTranscript
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, build_tool_execution_identity
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

AGENT = "helper"
ROOM_ID = "!room:example.org"


def _config(
    *,
    memory_backend: Literal["file", "mem0", "none"] = "mem0",
    private_scope: Literal["user", "user_agent"] | None = None,
) -> Config:
    private = AgentPrivateConfig(per=private_scope) if private_scope is not None else None
    agent = AgentConfig(display_name="Helper", memory_backend=memory_backend, private=private)
    return Config(
        agents={AGENT: agent},
        models={},
        calls=CallsConfig(enabled=True, agents={AGENT: CallAgentConfig()}),
    )


def _execution_identity(
    runtime_paths: RuntimePaths,
    *,
    requester_id: str = "@alice:example.org",
) -> ToolExecutionIdentity:
    return build_tool_execution_identity(
        channel="matrix",
        agent_name=AGENT,
        runtime_paths=runtime_paths,
        requester_id=requester_id,
        room_id=ROOM_ID,
        thread_id=None,
        resolved_thread_id=None,
        session_id=ROOM_ID,
    )


def _transcript(
    tmp_path: Path,
    config: Config | None = None,
    *,
    execution_identity: ToolExecutionIdentity | None = None,
) -> CallTranscript:
    config = config or _config()
    return CallTranscript.start(
        agent_name=AGENT,
        config=config,
        runtime_paths=test_runtime_paths(tmp_path),
        execution_identity=execution_identity,
        room_id=ROOM_ID,
        room_display_name="Lobby",
    )


@pytest.mark.asyncio
async def test_transcript_writes_turns_incrementally(tmp_path: Path) -> None:
    """Turns are flushed to the markdown file as they happen."""
    transcript = _transcript(tmp_path)
    transcript.record("user", "Hello agent")
    flush_task = transcript._flush_task
    transcript.record("assistant", "Hi! How can I help?")
    assert transcript._flush_task is flush_task
    assert flush_task is not None
    await flush_task

    content = transcript.path.read_text()
    assert "# Voice call in Lobby" in content
    assert "**user**: Hello agent" in content
    assert "**assistant**: Hi! How can I help?" in content
    assert transcript._turns == 2


@pytest.mark.asyncio
async def test_finalize_stores_relative_transcript_memory_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ending a call stores a portable reference through the configured backend."""
    add_memory = AsyncMock()
    monkeypatch.setattr("mindroom.matrix_rtc.transcript.add_agent_memory", add_memory)
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    transcript = _transcript(tmp_path)
    transcript.record("user", "Ping")

    await transcript.finalize(config=config, runtime_paths=runtime_paths)

    add_memory.assert_awaited_once()
    memory_content = add_memory.await_args.args[0]
    assert "voice call in Lobby" in memory_content
    assert f"Transcript: calls/{AGENT}/{transcript.path.name}" in memory_content
    assert "**user**: Ping" in memory_content
    assert str(tmp_path) not in memory_content


@pytest.mark.asyncio
async def test_file_memory_stores_workspace_relative_reference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File memory points at the transcript without duplicating its contents."""
    add_memory = AsyncMock()
    monkeypatch.setattr("mindroom.matrix_rtc.transcript.add_agent_memory", add_memory)
    config = _config(memory_backend="file")
    transcript = _transcript(tmp_path, config)
    transcript.record("user", "Ping")

    await transcript.finalize(config=config, runtime_paths=test_runtime_paths(tmp_path))

    memory_content = add_memory.await_args.args[0]
    assert f"Transcript: calls/{transcript.path.name}" in memory_content
    assert "**user**: Ping" not in memory_content


@pytest.mark.asyncio
async def test_finalize_without_turns_skips_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call where nothing was said leaves no memory entry or file."""
    add_memory = AsyncMock()
    monkeypatch.setattr("mindroom.matrix_rtc.transcript.add_agent_memory", add_memory)
    config = _config()
    transcript = _transcript(tmp_path, config)

    await transcript.finalize(config=config, runtime_paths=test_runtime_paths(tmp_path))

    add_memory.assert_not_awaited()
    assert not transcript.path.exists()


@pytest.mark.asyncio
async def test_disabled_memory_keeps_transcript_without_memory_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Memory-disabled agents retain the audit file without creating recall state."""
    add_memory = AsyncMock()
    monkeypatch.setattr("mindroom.matrix_rtc.transcript.add_agent_memory", add_memory)
    config = _config(memory_backend="none")
    transcript = _transcript(tmp_path, config)
    transcript.record("user", "Ping")

    await transcript.finalize(config=config, runtime_paths=test_runtime_paths(tmp_path))

    add_memory.assert_not_awaited()
    assert transcript.path.exists()


@pytest.mark.asyncio
async def test_failed_flush_preserves_pending_lines_for_retry(tmp_path: Path) -> None:
    """A filesystem failure cannot discard transcript turns before a later retry."""
    transcript = _transcript(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block")
    transcript.path = blocker / "transcript.md"
    transcript._pending.append("- preserved\n")

    with pytest.raises(FileExistsError):
        await transcript._flush()

    assert transcript._pending == ["- preserved\n"]
    transcript.path = tmp_path / "calls" / "retry.md"
    await transcript._flush()
    assert "preserved" in transcript.path.read_text(encoding="utf-8")
    assert transcript._pending == []


def test_sync_record_contains_flush_failure(tmp_path: Path) -> None:
    """A synchronous media callback survives local transcript storage failure."""
    transcript = _transcript(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block")
    transcript.path = blocker / "transcript.md"

    transcript.record("user", "keep me")

    assert transcript._pending


@pytest.mark.asyncio
async def test_background_flush_observes_and_logs_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scheduled flush errors are retrieved and reported instead of leaking task warnings."""
    transcript = _transcript(tmp_path)
    logged = MagicMock()

    async def fail_flush() -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(transcript, "_flush", fail_flush)
    monkeypatch.setattr("mindroom.matrix_rtc.transcript.logger.warning", logged)

    transcript.record("user", "keep me")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    logged.assert_called_once()
    assert transcript._pending
    assert transcript._flush_task is None


@pytest.mark.asyncio
async def test_finalize_contains_transcript_io_failure(tmp_path: Path) -> None:
    """Transcript storage errors remain local to call teardown."""
    transcript = _transcript(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block")
    transcript.path = blocker / "transcript.md"
    transcript._turns = 1
    transcript._pending.append("- preserved\n")

    await transcript.finalize(
        config=_config(),
        runtime_paths=test_runtime_paths(tmp_path),
    )

    assert transcript._pending == ["- preserved\n"]


def test_transcript_path_routes_by_memory_backend(tmp_path: Path) -> None:
    """File agents use their workspace; other agents use the call archive."""
    runtime_paths = test_runtime_paths(tmp_path)
    workspace_path = CallTranscript.start(
        agent_name=AGENT,
        config=_config(memory_backend="file"),
        runtime_paths=runtime_paths,
        execution_identity=None,
        room_id=ROOM_ID,
        room_display_name="Lobby",
    ).path
    workspace_runtime = resolve_agent_runtime(
        AGENT,
        _config(memory_backend="file"),
        runtime_paths,
        execution_identity=None,
    )
    assert workspace_runtime.workspace is not None
    assert workspace_path.is_relative_to(workspace_runtime.workspace.root)
    archive_path = CallTranscript.start(
        agent_name=AGENT,
        config=_config(),
        runtime_paths=runtime_paths,
        execution_identity=None,
        room_id=ROOM_ID,
        room_display_name="Lobby",
    ).path
    assert archive_path.is_relative_to(runtime_paths.storage_root / "calls" / AGENT)
    second_archive_path = CallTranscript.start(
        agent_name=AGENT,
        config=_config(),
        runtime_paths=runtime_paths,
        execution_identity=None,
        room_id=ROOM_ID,
        room_display_name="Lobby",
    ).path
    assert second_archive_path != archive_path


def test_private_transcript_requires_requester_identity(tmp_path: Path) -> None:
    """A private call cannot fall back to shared transcript storage."""
    with pytest.raises(ValueError, match="requires an active execution identity"):
        _transcript(tmp_path, _config(private_scope="user_agent"))


@pytest.mark.parametrize("private_scope", ["user", "user_agent"])
@pytest.mark.parametrize("memory_backend", ["file", "mem0"])
@pytest.mark.asyncio
async def test_private_transcripts_and_memory_are_requester_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    private_scope: Literal["user", "user_agent"],
    memory_backend: Literal["file", "mem0"],
) -> None:
    """Transcript storage and memory finalization use the verified caller scope."""
    add_memory = AsyncMock()
    monkeypatch.setattr("mindroom.matrix_rtc.transcript.add_agent_memory", add_memory)
    config = _config(memory_backend=memory_backend, private_scope=private_scope)
    runtime_paths = test_runtime_paths(tmp_path)
    alice_identity = _execution_identity(runtime_paths)
    bob_identity = _execution_identity(runtime_paths, requester_id="@bob:example.org")

    alice = _transcript(tmp_path, config, execution_identity=alice_identity)
    bob = _transcript(tmp_path, config, execution_identity=bob_identity)
    alice_runtime = resolve_agent_runtime(AGENT, config, runtime_paths, execution_identity=alice_identity)
    bob_runtime = resolve_agent_runtime(AGENT, config, runtime_paths, execution_identity=bob_identity)

    assert alice.path.parent != bob.path.parent
    assert alice.path.is_relative_to(alice_runtime.state_root)
    assert bob.path.is_relative_to(bob_runtime.state_root)
    alice.record("user", "private ping")
    await alice.finalize(config=config, runtime_paths=runtime_paths)

    assert add_memory.await_args.kwargs["execution_identity"] is alice_identity
    assert add_memory.await_args.args[2] == runtime_paths.storage_root
    assert "Transcript: calls/" in add_memory.await_args.args[0]
