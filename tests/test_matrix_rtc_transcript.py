"""Tests for voice-call transcripts and the daily-memory entry."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.matrix_rtc.transcript import CallTranscript, _call_transcript_path
from mindroom.tool_system.worker_routing import agent_workspace_root_path
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

AGENT = "helper"
ROOM_ID = "!room:example.org"


def _config(*, private: bool = False) -> Config:
    agent = AgentConfig(
        display_name="Helper",
        private=AgentPrivateConfig(per="user") if private else None,
    )
    return Config(agents={AGENT: agent}, models={})


def _transcript(tmp_path: Path, config: Config | None = None) -> CallTranscript:
    return CallTranscript.start(
        agent_name=AGENT,
        config=config or _config(),
        storage_path=tmp_path,
        room_id=ROOM_ID,
        room_display_name="Lobby",
    )


@pytest.mark.asyncio
async def test_transcript_writes_turns_incrementally(tmp_path: Path) -> None:
    """Turns are flushed to the markdown file as they happen."""
    transcript = _transcript(tmp_path)
    transcript.record("user", "Hello agent")
    transcript.record("assistant", "Hi! How can I help?")
    transcript.record_tool_use(["shell"])
    await asyncio.sleep(0.05)

    content = transcript.path.read_text()
    assert "# Voice call in Lobby" in content
    assert "**user**: Hello agent" in content
    assert "**assistant**: Hi! How can I help?" in content
    assert "tools used: shell" in content
    assert transcript._turns == 2


@pytest.mark.asyncio
async def test_finalize_appends_daily_memory_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ending a call with turns records a daily-memory summary."""
    entries: list[str] = []
    monkeypatch.setattr(
        "mindroom.matrix_rtc.transcript.append_agent_daily_memory",
        lambda content, *_args, **_kwargs: entries.append(content),
    )
    config = _config()
    runtime_paths = test_runtime_paths(tmp_path)
    transcript = _transcript(tmp_path, config)
    transcript.record("user", "Ping")

    await transcript.finalize(config=config, runtime_paths=runtime_paths, storage_path=tmp_path)

    assert len(entries) == 1
    assert "voice call in Lobby" in entries[0]
    assert str(transcript.path) in entries[0]


@pytest.mark.asyncio
async def test_finalize_without_turns_skips_daily_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A call where nothing was said leaves no memory entry or file."""
    entries: list[str] = []
    monkeypatch.setattr(
        "mindroom.matrix_rtc.transcript.append_agent_daily_memory",
        lambda content, *_args, **_kwargs: entries.append(content),
    )
    config = _config()
    transcript = _transcript(tmp_path, config)

    await transcript.finalize(config=config, runtime_paths=test_runtime_paths(tmp_path), storage_path=tmp_path)

    assert entries == []
    assert not transcript.path.exists()


def test_transcript_path_prefers_agent_workspace(tmp_path: Path) -> None:
    """Private agents keep transcripts inside their workspace."""
    from datetime import UTC, datetime  # noqa: PLC0415

    started = datetime.now(tz=UTC)
    private_path = _call_transcript_path(
        agent_name=AGENT,
        config=_config(private=True),
        storage_path=tmp_path,
        room_id=ROOM_ID,
        started_at=started,
    )
    assert private_path.is_relative_to(agent_workspace_root_path(tmp_path, AGENT))
    shared_path = _call_transcript_path(
        agent_name=AGENT,
        config=_config(),
        storage_path=tmp_path,
        room_id=ROOM_ID,
        started_at=started,
    )
    assert shared_path.is_relative_to(tmp_path / "calls" / AGENT)
