"""Tests for background file-memory auto-flush state and batching."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.main import Config
from mindroom.memory.auto_flush import (
    MemoryAutoFlushWorker,
    _build_existing_memory_context,
    mark_auto_flush_dirty_session,
    reprioritize_auto_flush_sessions,
)
from mindroom.memory.functions import add_agent_memory, append_agent_daily_memory
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    resolve_worker_key,
    tool_execution_identity,
    worker_root_path,
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
    assert '"shared:general:s1"' in payload
    assert '"shared:general:s2"' in payload
    assert '"priority_boost_at"' in payload


def test_mark_dirty_uses_per_agent_file_override(tmp_path: Path, config: Config) -> None:
    """Auto-flush should track agents explicitly configured for file memory."""
    storage_path = tmp_path
    config.memory.backend = "mem0"
    config.agents["general"].memory_backend = "file"

    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s1",
        room_id="!room:example",
        thread_id="t1",
    )

    payload = json.loads((storage_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    assert "shared:general:s1" in payload["sessions"]


def test_mark_dirty_skips_per_agent_mem0_override(tmp_path: Path, config: Config) -> None:
    """Auto-flush should not track agents explicitly configured for Mem0."""
    storage_path = tmp_path
    config.memory.backend = "file"
    config.agents["general"].memory_backend = "mem0"

    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s1",
        room_id="!room:example",
        thread_id="t1",
    )

    assert not (storage_path / "memory_flush_state.json").exists()


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
        lambda _storage, _config, _agent, _sid, **_kwargs: fake_session,
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


@pytest.mark.asyncio
async def test_worker_flush_keeps_worker_daily_file_memory_isolated(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker-scoped flushes should keep daily file memory inside the worker root."""
    config.agents["general"].worker_scope = "user"
    config.agents["general"].memory_file_path = "./custom-agent-memory"
    config.memory.file.path = str(tmp_path / "shared-memory")

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )

    with (
        patch("mindroom.constants.CONFIG_PATH", tmp_path / "config.yaml"),
        tool_execution_identity(alice_identity),
    ):
        mark_auto_flush_dirty_session(
            tmp_path,
            config,
            agent_name="general",
            session_id="session-alice",
            room_id="!room:example.org",
            thread_id="$thread",
        )

    fake_session = _FakeSession(
        updated_at=100,
        messages=[_FakeMessage(role="user", content="remember this worker-isolated detail")],
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._load_agent_session",
        lambda _storage, _config, _agent, _sid, **_kwargs: fake_session,
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_extract_memory_summary,
    )

    with patch("mindroom.constants.CONFIG_PATH", tmp_path / "config.yaml"):
        worker = MemoryAutoFlushWorker(storage_path=tmp_path, config_provider=lambda: config)
        await worker._run_cycle(config)

    worker_key = resolve_worker_key("user", alice_identity, agent_name="general")
    assert worker_key is not None
    worker_daily_files = list(
        (worker_root_path(tmp_path, worker_key) / "workspace" / "general" / "custom-agent-memory" / "memory").rglob(
            "*.md",
        ),
    )
    assert len(worker_daily_files) == 1
    assert "important decision" in worker_daily_files[0].read_text(encoding="utf-8")

    assert not list((tmp_path / "shared-memory").rglob("*.md"))
    assert not list((tmp_path / "custom-agent-memory").rglob("*.md"))


@pytest.mark.asyncio
async def test_worker_flush_unscoped_preserves_custom_agent_memory_path(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unscoped flushes should still honor per-agent custom file-memory paths."""
    config.agents["general"].memory_file_path = "./custom-agent-memory"
    config.memory.file.path = str(tmp_path / "shared-memory")

    fake_session = _FakeSession(
        updated_at=100,
        messages=[_FakeMessage(role="user", content="remember this important decision")],
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._load_agent_session",
        lambda _storage, _config, _agent, _sid, **_kwargs: fake_session,
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_extract_memory_summary,
    )

    with patch("mindroom.constants.CONFIG_PATH", tmp_path / "config.yaml"):
        worker = MemoryAutoFlushWorker(storage_path=tmp_path, config_provider=lambda: config)
        wrote_memory = await worker._flush_session(
            config,
            agent_name="general",
            session_id="session-general",
            worker_key=None,
        )

    assert wrote_memory is True
    custom_daily_files = list((tmp_path / "custom-agent-memory" / "memory").rglob("*.md"))
    assert len(custom_daily_files) == 1
    assert "important decision" in custom_daily_files[0].read_text(encoding="utf-8")
    assert not list((tmp_path / "shared-memory").rglob("*.md"))
    assert not list((tmp_path / "memory_files").rglob("*.md"))


@pytest.mark.asyncio
async def test_existing_memory_context_reads_worker_root_instead_of_shared_custom_path(
    tmp_path: Path,
    config: Config,
) -> None:
    """Duplicate-avoidance context should read worker-local file memory, not shared custom-path memory."""
    config.agents["general"].worker_scope = "user"
    config.agents["general"].memory_file_path = "./custom-agent-memory"
    config.memory.auto_flush.extractor.include_memory_context.memory_snippets = 5
    config.memory.auto_flush.extractor.include_memory_context.snippet_max_chars = 200

    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    worker_key = resolve_worker_key("user", alice_identity, agent_name="general")
    assert worker_key is not None

    with patch("mindroom.constants.CONFIG_PATH", tmp_path / "config.yaml"):
        with tool_execution_identity(alice_identity):
            await add_agent_memory("Alice isolated memory", "general", tmp_path, config)

        append_agent_daily_memory("Shared leaked memory", "general", tmp_path, config)

        worker_context = await _build_existing_memory_context(
            agent_name="general",
            storage_path=worker_root_path(tmp_path, worker_key),
            config=config,
            preserve_resolved_storage_path=True,
        )

    assert "Alice isolated memory" in worker_context
    assert "Shared leaked memory" not in worker_context


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

    def _load_session(_storage: Path, _config: Config, _agent: str, _sid: str, **_kwargs: object) -> _FakeSession:
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

    async def _fake_flush(config: Config, *, agent_name: str, session_id: str, worker_key: str | None) -> bool:
        assert worker_key is None
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
    session_state = payload["sessions"]["shared:general:s1"]
    assert session_state["dirty"] is True
    assert session_state["in_flight"] is False
    assert session_state["last_flushed_session_updated_at"] == 100


@pytest.mark.asyncio
async def test_worker_no_reply_does_not_requeue_without_new_dirty_mark(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NO_REPLY flushes should clear dirty state unless new activity marked it dirty again."""
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

    def _load_session(_storage: Path, _config: Config, _agent: str, _sid: str, **_kwargs: object) -> _FakeSession:
        return _FakeSession(
            updated_at=session_updated_at,
            messages=[_FakeMessage(role="user", content="no durable memory here")],
        )

    async def _fake_no_reply(**_: object) -> None:
        nonlocal session_updated_at
        # Simulate unrelated session timestamp movement during extractor execution.
        session_updated_at = 200

    monkeypatch.setattr("mindroom.memory.auto_flush._load_agent_session", _load_session)
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_no_reply,
    )
    append_calls: list[str] = []
    monkeypatch.setattr(
        "mindroom.memory.auto_flush.append_agent_daily_memory",
        lambda *_args, **_kwargs: append_calls.append("called"),
    )

    worker = MemoryAutoFlushWorker(storage_path=storage_path, config_provider=lambda: config)
    await worker._run_cycle(config)

    payload = json.loads((storage_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    session_state = payload["sessions"]["shared:general:s1"]
    assert session_state["dirty"] is False
    assert session_state["in_flight"] is False
    assert session_state["last_flushed_session_updated_at"] == 100
    assert session_state["last_session_updated_at"] == 200
    assert append_calls == []


def test_mark_dirty_keeps_worker_scopes_separate(tmp_path: Path, config: Config) -> None:
    """Two users with the same session ID should get separate auto-flush entries under worker scope."""
    config.agents["general"].worker_scope = "user"
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-bob",
    )

    with tool_execution_identity(alice_identity):
        mark_auto_flush_dirty_session(
            tmp_path,
            config,
            agent_name="general",
            session_id="shared-session-id",
            room_id="!room:example.org",
            thread_id="$thread",
        )
    with tool_execution_identity(bob_identity):
        mark_auto_flush_dirty_session(
            tmp_path,
            config,
            agent_name="general",
            session_id="shared-session-id",
            room_id="!room:example.org",
            thread_id="$thread",
        )

    payload = json.loads((tmp_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    assert len(payload["sessions"]) == 2
