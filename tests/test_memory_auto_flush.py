"""Tests for background file-memory auto-flush state and batching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom.config import Config
from mindroom.memory.auto_flush import (
    MemoryAutoFlushWorker,
    mark_auto_flush_dirty_session,
    reprioritize_auto_flush_sessions,
)

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _FakeMessage:
    role: str
    content: str


@dataclass
class _FakeSession:
    updated_at: int
    messages: list[_FakeMessage]

    def get_chat_history(self) -> list[_FakeMessage]:
        return self.messages


@pytest.fixture
def config() -> Config:
    """Return a file-memory config with deterministic auto-flush limits."""
    cfg = Config.from_yaml()
    cfg.memory.backend = "file"
    cfg.memory.auto_flush.enabled = True
    cfg.memory.auto_flush.flush_interval_seconds = 1
    cfg.memory.auto_flush.idle_seconds = 0
    cfg.memory.auto_flush.batch.max_sessions_per_cycle = 1
    cfg.memory.auto_flush.batch.max_sessions_per_agent_per_cycle = 1
    cfg.memory.auto_flush.extractor.max_messages_per_flush = 5
    cfg.memory.auto_flush.extractor.max_chars_per_flush = 1000
    return cfg


def test_mark_dirty_and_reprioritize(tmp_path: Path, config: Config) -> None:
    """Dirty-state persistence should track and reprioritize agent sessions."""
    storage_path = tmp_path
    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s1",
        room_id="!room:example",
        thread_id="t1",
    )
    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s2",
        room_id="!room:example",
        thread_id="t2",
    )

    reprioritize_auto_flush_sessions(
        storage_path,
        config,
        agent_name="general",
        active_session_id="s1",
    )

    state_file = storage_path / "memory_flush_state.json"
    payload = state_file.read_text(encoding="utf-8")
    assert '"general:s1"' in payload
    assert '"general:s2"' in payload
    assert '"priority_boost_at"' in payload


@pytest.mark.asyncio
async def test_worker_respects_batch_limits(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cycle should process no more than configured batch size."""
    storage_path = tmp_path
    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s1",
        room_id="!room:example",
        thread_id="t1",
    )
    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s2",
        room_id="!room:example",
        thread_id="t2",
    )

    fake_session = _FakeSession(
        updated_at=100,
        messages=[_FakeMessage(role="user", content="remember this important decision")],
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._load_agent_session",
        lambda _storage, _agent, _sid: fake_session,
    )

    writes: list[str] = []

    async def _fake_add_memory(
        content: str,
        agent_name: str,
        **_: object,
    ) -> None:
        writes.append(f"{agent_name}:{content}")

    monkeypatch.setattr("mindroom.memory.auto_flush.add_agent_memory", _fake_add_memory)

    worker = MemoryAutoFlushWorker(storage_path=storage_path, config_provider=lambda: config)
    await worker._run_cycle(config)

    assert len(writes) == 1
