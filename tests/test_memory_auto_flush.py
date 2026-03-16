"""Tests for background file-memory auto-flush state and batching."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.memory.auto_flush import (
    MemoryAutoFlushWorker,
    _build_existing_memory_context,
    _load_agent_session,
    mark_auto_flush_dirty_session,
    reprioritize_auto_flush_sessions,
)
from mindroom.memory.functions import add_agent_memory, append_agent_daily_memory
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    agent_workspace_root_path,
    get_tool_execution_identity,
    private_instance_state_root_path,
    resolve_worker_key,
    tool_execution_identity,
)
from tests.conftest import bind_runtime_paths, runtime_paths_for

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
def config(tmp_path: Path) -> Config:
    """Return a file-memory config with deterministic auto-flush limits."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    cfg = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    role="General assistant",
                    rooms=[],
                ),
            },
        ),
        runtime_paths,
    )
    cfg.memory.backend = "file"
    cfg.memory.auto_flush.enabled = True
    cfg.memory.auto_flush.flush_interval_seconds = 1
    cfg.memory.auto_flush.idle_seconds = 0
    cfg.memory.auto_flush.batch.max_sessions_per_cycle = 1
    cfg.memory.auto_flush.batch.max_sessions_per_agent_per_cycle = 1
    cfg.memory.auto_flush.extractor.max_messages_per_flush = 5
    cfg.memory.auto_flush.extractor.max_chars_per_flush = 1000
    return cfg


def _private_auto_flush_config(tmp_path: Path) -> Config:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )
    cfg = bind_runtime_paths(
        Config(
            agents={
                "mind": AgentConfig(
                    display_name="Mind",
                    role="Persistent private assistant",
                    rooms=[],
                    memory_backend="file",
                    private=AgentPrivateConfig(per="user", root="mind_data"),
                ),
            },
        ),
        runtime_paths,
    )
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
    )
    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s2",
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
    assert '"room_id"' not in payload
    assert '"thread_id"' not in payload


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
    )

    payload = json.loads((storage_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    assert "general:s1" in payload["sessions"]


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
    )
    mark_auto_flush_dirty_session(
        storage_path,
        config,
        agent_name="general",
        session_id="s2",
    )

    fake_session = _FakeSession(
        updated_at=100,
        messages=[_FakeMessage(role="user", content="remember this important decision")],
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._load_agent_session",
        lambda _config, _storage, _agent, _sid, **_kwargs: fake_session,
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

    worker = MemoryAutoFlushWorker(
        storage_path=storage_path,
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
    )
    await worker._run_cycle(config)

    assert len(writes) == 1


@pytest.mark.asyncio
async def test_worker_flush_writes_daily_file_memory_into_canonical_agent_root(
    tmp_path: Path,
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Worker-scoped flushes should reuse the canonical agent-owned memory path."""
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

    with tool_execution_identity(alice_identity):
        mark_auto_flush_dirty_session(
            tmp_path,
            config,
            agent_name="general",
            session_id="session-alice",
        )

    fake_session = _FakeSession(
        updated_at=100,
        messages=[_FakeMessage(role="user", content="remember this shared agent detail")],
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._load_agent_session",
        lambda _config, _storage, _agent, _sid, **_kwargs: fake_session,
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_extract_memory_summary,
    )

    worker = MemoryAutoFlushWorker(
        storage_path=tmp_path,
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
    )
    await worker._run_cycle(config)

    worker_daily_files = list(
        (agent_workspace_root_path(tmp_path, "general") / "custom-agent-memory" / "memory").rglob("*.md"),
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
        lambda _config, _storage, _agent, _sid, **_kwargs: fake_session,
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_extract_memory_summary,
    )

    worker = MemoryAutoFlushWorker(
        storage_path=tmp_path,
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
    )
    wrote_memory = await worker._flush_session(
        config,
        agent_name="general",
        session_id="session-general",
    )

    assert wrote_memory is True
    custom_daily_files = list(
        (agent_workspace_root_path(tmp_path, "general") / "custom-agent-memory" / "memory").rglob("*.md"),
    )
    assert len(custom_daily_files) == 1
    assert "important decision" in custom_daily_files[0].read_text(encoding="utf-8")
    assert not list((tmp_path / "shared-memory").rglob("*.md"))
    assert not list((tmp_path / "memory_files").rglob("*.md"))


@pytest.mark.asyncio
async def test_existing_memory_context_resolves_to_canonical_agent_memory_path(
    tmp_path: Path,
    config: Config,
) -> None:
    """Duplicate-avoidance context should reuse the canonical agent-owned memory path."""
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
    with tool_execution_identity(alice_identity):
        await add_agent_memory("Alice-authored shared memory", "general", tmp_path, config, runtime_paths_for(config))

    append_agent_daily_memory("Shared daily memory", "general", tmp_path, config, runtime_paths_for(config))

    worker_context = await _build_existing_memory_context(
        agent_name="general",
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )

    assert "Alice-authored shared memory" in worker_context
    assert "Shared daily memory" in worker_context


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
    )

    def _load_session(_config: object, _storage: Path, _agent: str, _sid: str, **_kwargs: object) -> _FakeSession:
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

    worker = MemoryAutoFlushWorker(
        storage_path=storage_path,
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
    )

    async def _fake_flush(
        config: Config,
        *,
        agent_name: str,
        session_id: str,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> bool:
        nonlocal session_updated_at
        _ = execution_identity
        session_updated_at = 200
        mark_auto_flush_dirty_session(
            storage_path,
            config,
            agent_name=agent_name,
            session_id=session_id,
        )
        return True

    monkeypatch.setattr(worker, "_flush_session", _fake_flush)
    await worker._run_cycle(config)

    payload = json.loads((storage_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    session_state = payload["sessions"]["general:s1"]
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
    )

    def _load_session(_config: object, _storage: Path, _agent: str, _sid: str, **_kwargs: object) -> _FakeSession:
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

    worker = MemoryAutoFlushWorker(
        storage_path=storage_path,
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
    )
    await worker._run_cycle(config)

    payload = json.loads((storage_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    session_state = payload["sessions"]["general:s1"]
    assert session_state["dirty"] is False
    assert session_state["in_flight"] is False
    assert session_state["last_flushed_session_updated_at"] == 100
    assert session_state["last_session_updated_at"] == 200
    assert append_calls == []


def test_mark_dirty_coalesces_shared_agent_sessions(tmp_path: Path, config: Config) -> None:
    """Two runtimes touching the same agent session should share one auto-flush entry."""
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
        )
    with tool_execution_identity(bob_identity):
        mark_auto_flush_dirty_session(
            tmp_path,
            config,
            agent_name="general",
            session_id="shared-session-id",
        )

    payload = json.loads((tmp_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    assert list(payload["sessions"]) == ["general:shared-session-id"]


def test_mark_dirty_separates_private_agent_sessions_by_requester_scope(tmp_path: Path) -> None:
    """Private agents should keep one auto-flush entry per private requester scope."""
    config = _private_auto_flush_config(tmp_path)
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-bob",
    )

    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="!room:example.org:$thread",
        execution_identity=alice_identity,
    )
    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="!room:example.org:$thread",
        execution_identity=bob_identity,
    )

    payload = json.loads((tmp_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    sessions = payload["sessions"]
    assert len(sessions) == 2
    worker_keys = {entry["worker_key"] for entry in sessions.values()}
    assert worker_keys == {
        resolve_worker_key("user", alice_identity, agent_name="mind"),
        resolve_worker_key("user", bob_identity, agent_name="mind"),
    }
    requester_ids = {entry["execution_identity"]["requester_id"] for entry in sessions.values()}
    assert requester_ids == {"@alice:example.org", "@bob:example.org"}


@pytest.mark.asyncio
async def test_worker_batch_limits_are_scoped_per_private_requester(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different private requester scopes should not compete for one per-agent batch slot."""
    config = _private_auto_flush_config(tmp_path)
    config.memory.auto_flush.batch.max_sessions_per_cycle = 2
    config.memory.auto_flush.batch.max_sessions_per_agent_per_cycle = 1
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-bob",
    )
    fake_session = _FakeSession(
        updated_at=100,
        messages=[_FakeMessage(role="user", content="remember this important decision")],
    )

    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="session-alice",
        execution_identity=alice_identity,
    )
    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="session-bob",
        execution_identity=bob_identity,
    )

    monkeypatch.setattr(
        "mindroom.memory.auto_flush._load_agent_session",
        lambda _config, _storage, _agent, _sid, **_kwargs: fake_session,
    )
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_extract_memory_summary,
    )

    writes: list[tuple[str, str | None]] = []

    def _fake_append_daily_memory(
        content: str,
        agent_name: str,
        **_: object,
    ) -> dict[str, str]:
        identity = get_tool_execution_identity()
        writes.append((agent_name, identity.requester_id if identity is not None else None))
        return {"id": "m_test", "memory": content, "user_id": f"agent_{agent_name}"}

    monkeypatch.setattr("mindroom.memory.auto_flush.append_agent_daily_memory", _fake_append_daily_memory)

    worker = MemoryAutoFlushWorker(
        storage_path=tmp_path,
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
    )
    await worker._run_cycle(config)

    assert len(writes) == 2
    assert set(writes) == {
        ("mind", "@alice:example.org"),
        ("mind", "@bob:example.org"),
    }


def test_load_agent_session_passes_execution_identity_for_private_agents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private auto-flush session loads must reopen the scoped session storage explicitly."""
    config = _private_auto_flush_config(tmp_path)
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    captured: dict[str, object] = {}

    class _DummyStorage:
        def get_session(self, session_id: str, _session_type: object) -> None:
            captured["session_id"] = session_id

    def _fake_create_session_storage(
        agent_name: str,
        storage_path: Path,
        config: Config,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> _DummyStorage:
        captured["agent_name"] = agent_name
        captured["storage_path"] = storage_path
        captured["config"] = config
        captured["execution_identity"] = execution_identity
        return _DummyStorage()

    monkeypatch.setattr("mindroom.memory.auto_flush.create_session_storage", _fake_create_session_storage)

    assert (
        _load_agent_session(
            config,
            tmp_path,
            "mind",
            "session-alice",
            execution_identity=alice_identity,
        )
        is None
    )
    assert captured["execution_identity"] == alice_identity
    assert captured["agent_name"] == "mind"
    assert captured["session_id"] == "session-alice"


def test_reprioritize_private_sessions_stays_within_private_scope(tmp_path: Path) -> None:
    """Private auto-flush reprioritization must not cross requester boundaries."""
    config = _private_auto_flush_config(tmp_path)
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    bob_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@bob:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-bob",
    )

    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="alice-active",
        execution_identity=alice_identity,
    )
    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="alice-other",
        execution_identity=alice_identity,
    )
    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="bob-other",
        execution_identity=bob_identity,
    )

    reprioritize_auto_flush_sessions(
        tmp_path,
        config,
        agent_name="mind",
        active_session_id="alice-active",
        execution_identity=alice_identity,
    )

    payload = json.loads((tmp_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    sessions = payload["sessions"]
    boosted_requesters = {
        entry["execution_identity"]["requester_id"]
        for entry in sessions.values()
        if entry.get("priority_boost_at") is not None
    }
    assert boosted_requesters == {"@alice:example.org"}


@pytest.mark.asyncio
async def test_worker_removes_stale_private_entries_when_agent_becomes_shared(tmp_path: Path) -> None:
    """Config reloads should drop persisted private dirty entries once the agent is no longer private."""
    config = _private_auto_flush_config(tmp_path)
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="!room:example.org:$thread",
        execution_identity=alice_identity,
    )
    config.agents["mind"].private = None

    worker = MemoryAutoFlushWorker(
        storage_path=tmp_path,
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
    )
    await worker._run_cycle(config)

    payload = json.loads((tmp_path / "memory_flush_state.json").read_text(encoding="utf-8"))
    assert payload["sessions"] == {}


@pytest.mark.asyncio
async def test_worker_flush_private_agent_uses_persisted_private_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Private auto-flush should rebind the persisted requester scope for memory writes."""
    config = _private_auto_flush_config(tmp_path)
    alice_identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="mind",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-alice",
    )
    mark_auto_flush_dirty_session(
        tmp_path,
        config,
        agent_name="mind",
        session_id="!room:example.org:$thread",
        execution_identity=alice_identity,
    )

    seen_execution_identities: list[ToolExecutionIdentity | None] = []
    fake_session = _FakeSession(
        updated_at=100,
        messages=[_FakeMessage(role="user", content="remember this private detail")],
    )

    def _fake_load_session(
        _config: Config,
        _storage: Path,
        _agent: str,
        _sid: str,
        *,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> _FakeSession:
        seen_execution_identities.append(execution_identity)
        return fake_session

    monkeypatch.setattr("mindroom.memory.auto_flush._load_agent_session", _fake_load_session)
    monkeypatch.setattr(
        "mindroom.memory.auto_flush._extract_memory_summary",
        _fake_extract_memory_summary,
    )

    worker = MemoryAutoFlushWorker(
        storage_path=tmp_path,
        runtime_paths=runtime_paths_for(config),
        config_provider=lambda: config,
    )
    await worker._run_cycle(config)

    assert seen_execution_identities
    assert all(identity == alice_identity for identity in seen_execution_identities)

    worker_key = resolve_worker_key("user", alice_identity, agent_name="mind")
    assert worker_key is not None
    private_daily_files = list(
        (
            private_instance_state_root_path(
                tmp_path,
                worker_key=worker_key,
                agent_name="mind",
            )
            / "mind_data"
            / "memory"
        ).rglob("*.md"),
    )
    assert len(private_daily_files) == 1
    assert "important decision" in private_daily_files[0].read_text(encoding="utf-8")
