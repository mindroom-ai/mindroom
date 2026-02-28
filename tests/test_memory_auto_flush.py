"""Tests for background file-memory auto-flush state and batching."""

from __future__ import annotations

import json
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
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_extract_memory_summary,
    )

    writes: list[str] = []

    def _fake_append_daily_memory(
        content: str,
        agent_name: str,
        **_: object,
    ) -> dict[str, str]:
        writes.append(f"{agent_name}:{content}")
        return {"id": "m_test", "memory": content, "user_id": f"agent_{agent_name}"}

    monkeypatch.setattr("mindroom.memory.auto_flush.append_agent_daily_memory", _fake_append_daily_memory)

    worker = MemoryAutoFlushWorker(storage_path=storage_path, config_provider=lambda: config)
    await worker._run_cycle(config)

    assert len(writes) == 1


async def _fake_extract_memory_summary(**_: object) -> str:
    return "important decision"


@pytest.mark.asyncio
async def test_worker_keeps_session_dirty_when_new_activity_arrives_mid_flush(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New activity during a flush should keep the session dirty for a later pass."""
    storage_path = tmp_path
    session_updated_at = 100

    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s1",
        room_id="!room:example",
        thread_id="t1",
    )

    def _load_session(_storage: Path, _agent: str, _sid: str) -> _FakeSession:
        return _FakeSession(
            updated_at=session_updated_at,
            messages=[_FakeMessage(role="user", content="important detail")],
        )

    monkeypatch.setattr("mindroom.memory.auto_flush._load_agent_session", _load_session)
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_extract_memory_summary,
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush.append_agent_daily_memory",
        lambda *_args, **_kwargs: {
            "id": "m_test",
            "memory": "important detail",
            "user_id": "agent_general",
        },
    )

    worker = MemoryAutoFlushWorker(storage_path=storage_path, config_provider=lambda: config)

    async def _fake_flush(config: Config, *, agent_name: str, session_id: str) -> bool:
        nonlocal session_updated_at
        session_updated_at = 200
        mark_auto_flush_dirty_session(
            storage_path,
            config,
            agent_name=agent_name,
            session_id=session_id,
            room_id="!room:example",
            thread_id="t1",
        )
        return True

    monkeypatch.setattr(worker, "_flush_session", _fake_flush)
    await worker._run_cycle(config)

    payload = json.loads((storage_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    session_state = payload["sessions"]["general:s1"]
    assert session_state["dirty"] is True
    assert session_state["in_flight"] is False
    assert session_state["last_flushed_session_updated_at"] == 100
