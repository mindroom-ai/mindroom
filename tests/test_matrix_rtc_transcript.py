"""Tests for voice-call transcripts and their memory reference."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.calls import CallsConfig
from mindroom.config.main import Config
from mindroom.matrix_rtc.transcript import CallTranscript, _call_transcript_path
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

AGENT = "helper"
ROOM_ID = "!room:example.org"


def _config(*, memory_backend: Literal["file", "mem0", "none"] = "mem0") -> Config:
    agent = AgentConfig(display_name="Helper", memory_backend=memory_backend)
    return Config(
        agents={AGENT: agent},
        models={},
        calls=CallsConfig(enabled=True, agents=[AGENT]),
    )


def _transcript(tmp_path: Path, config: Config | None = None) -> CallTranscript:
    config = config or _config()
    return CallTranscript.start(
        agent_name=AGENT,
        config=config,
        storage_path=tmp_path,
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

    await transcript.finalize(config=config, runtime_paths=runtime_paths, storage_path=tmp_path)

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

    await transcript.finalize(config=config, runtime_paths=test_runtime_paths(tmp_path), storage_path=tmp_path)

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

    await transcript.finalize(config=config, runtime_paths=test_runtime_paths(tmp_path), storage_path=tmp_path)

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

    await transcript.finalize(config=config, runtime_paths=test_runtime_paths(tmp_path), storage_path=tmp_path)

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
        storage_path=tmp_path,
    )

    assert transcript._pending == ["- preserved\n"]


def test_transcript_path_routes_by_memory_backend(tmp_path: Path) -> None:
    """File agents use their workspace; other agents use the call archive."""
    from datetime import UTC, datetime  # noqa: PLC0415

    started = datetime.now(tz=UTC)
    workspace_path = _call_transcript_path(
        agent_name=AGENT,
        config=_config(memory_backend="file"),
        storage_path=tmp_path,
        room_id=ROOM_ID,
        started_at=started,
    )
    assert workspace_path.is_relative_to(agent_workspace_root_path(tmp_path, AGENT))
    archive_path = _call_transcript_path(
        agent_name=AGENT,
        config=_config(),
        storage_path=tmp_path,
        room_id=ROOM_ID,
        started_at=started,
    )
    assert archive_path.is_relative_to(tmp_path / "calls" / AGENT)
