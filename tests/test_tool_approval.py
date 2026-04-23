"""Tests for tool approval config, Matrix transport, and resolution."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import threading
from typing import TYPE_CHECKING, Literal, Self
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

import mindroom.tool_approval as tool_approval_module
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestrator import MultiAgentOrchestrator
from mindroom.tool_approval import (
    ApprovalDecision,
    ApprovalManager,
    PendingApproval,
    SentApprovalEvent,
    ToolApprovalScriptError,
    evaluate_tool_approval,
    get_approval_store,
    initialize_approval_store,
    recover_unconfirmed_approval_event_deliveries,
    resolve_tool_approval_approver,
    shutdown_approval_store,
    sync_unsynced_approval_event_resolutions,
)
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.fixture(autouse=True)
def reset_approval_store() -> Generator[None, None, None]:
    """Keep the module-level approval manager isolated per test."""
    asyncio.run(shutdown_approval_store())
    yield
    asyncio.run(shutdown_approval_store())


def _base_config_kwargs() -> dict[str, object]:
    return {
        "agents": {
            "code": AgentConfig(
                display_name="Code",
                role="Help with coding",
                rooms=["!room:localhost"],
            ),
            "general": AgentConfig(
                display_name="General",
                role="Help generally",
                rooms=["!room:localhost"],
            ),
        },
        "models": {
            "default": ModelConfig(provider="openai", id="gpt-5.4"),
        },
    }


def _runtime_bound_config(
    runtime_paths: RuntimePaths,
    *,
    tool_approval: ToolApprovalConfig | dict[str, object] | None = None,
) -> Config:
    config = Config(
        **_base_config_kwargs(),
        tool_approval=tool_approval or ToolApprovalConfig(),
    )
    return bind_runtime_paths(config, runtime_paths)


def _cinny_accepts_tool_approval_payload(
    payload: dict[str, object],
    *,
    expected_arguments: dict[str, object] | None = None,
) -> bool:
    """Mirror Cinny's required approval-card field checks."""
    candidates = []
    new_content = payload.get("m.new_content")
    if isinstance(new_content, dict):
        candidates.append(new_content)
    candidates.append(payload)

    def _pick(key: str) -> object | None:
        for candidate in candidates:
            value = candidate.get(key)
            if value is not None:
                return value
        return None

    required_keys = ("approval_id", "tool_name", "agent_name", "requested_at", "expires_at")
    if not all(isinstance(_pick(key), str) and _pick(key).strip() for key in required_keys):
        return False
    if _pick("status") not in {"pending", "approved", "denied", "expired"}:
        return False

    def _matches_expected(arguments: object) -> bool:
        return isinstance(arguments, dict) and (expected_arguments is None or arguments == expected_arguments)

    if not _matches_expected(_pick("arguments")):
        return False
    if not isinstance(new_content, dict):
        return True
    new_content_arguments = new_content.get("arguments")
    outer_arguments = payload.get("arguments")
    outer_matches = not isinstance(outer_arguments, dict) or outer_arguments == new_content_arguments
    return _matches_expected(new_content_arguments) and outer_matches


def _agent_bot(tmp_path: Path, *, config: Config, agent_name: str = "code") -> AgentBot:
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name=agent_name,
            user_id=f"@mindroom_{agent_name}:localhost",
            display_name=agent_name.capitalize(),
            password="test-password",  # noqa: S106
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
    )
    bot.client = make_matrix_client_mock(user_id=f"@mindroom_{agent_name}:localhost")
    _grant_approval_room_access(bot)
    return bot


def _grant_approval_room_access(bot: AgentBot, *, room_id: str = "!room:localhost") -> nio.MatrixRoom:
    assert bot.client is not None
    return _grant_approval_room_access_for_client(
        bot.client,
        room_id=room_id,
        display_name=bot.agent_user.display_name,
    )


def _grant_approval_room_access_for_client(
    client: nio.AsyncClient,
    *,
    room_id: str = "!room:localhost",
    display_name: str | None = None,
    user_level: int = 100,
    redact_level: int = 50,
) -> nio.MatrixRoom:
    room = client.rooms[room_id]
    room.power_levels.defaults.events_default = 0
    room.power_levels.defaults.users_default = 0
    room.power_levels.defaults.redact = redact_level
    room.power_levels.users[client.user_id] = user_level
    room.add_member(client.user_id, display_name, None)
    return room


def _sent_approval_event(
    event_id: str = "$approval",
    *,
    sender_user_id: str = "@mindroom_code:localhost",
) -> SentApprovalEvent:
    del sender_user_id
    return SentApprovalEvent(event_id=event_id)


async def _request_tool_approval(
    runtime_paths: RuntimePaths,
    *,
    sender: AsyncMock | None = None,
    editor: AsyncMock | None = None,
    timeout_seconds: float = 60,
    requester_id: str = "@user:localhost",
    arguments: dict[str, object] | None = None,
    thread_id: str | None = "$thread",
) -> tuple[ApprovalManager, asyncio.Task[ApprovalDecision], PendingApproval | None]:
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments=arguments or {"command": "echo hi"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id=thread_id,
            requester_id=requester_id,
            approver_user_id=requester_id,
            matched_rule="run_shell_*",
            script_path=None,
            timeout_seconds=timeout_seconds,
        ),
    )
    pending: list[PendingApproval] = []
    for _ in range(5):
        await asyncio.sleep(0)
        pending = store.list_pending()
        if pending and pending[0].event_id is not None:
            break
    return store, task, pending[0] if pending else None


def _reply_event(*, event_id: str, body: str, sender: str = "@user:localhost") -> MagicMock:
    event = MagicMock(spec=nio.RoomMessageText)
    event.event_id = event_id
    event.sender = sender
    event.body = body
    event.source = {
        "type": "m.room.message",
        "content": {
            "msgtype": "m.text",
            "body": body,
            "m.relates_to": {"m.in_reply_to": {"event_id": "$approval"}},
        },
    }
    return event


def _approval_history_event(
    *,
    event_id: str,
    approval_id: str,
    sender: str = "@mindroom_router:localhost",
    status: str = "pending",
) -> nio.UnknownEvent:
    return nio.UnknownEvent.from_dict(
        {
            "type": "io.mindroom.tool_approval",
            "event_id": event_id,
            "sender": sender,
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "content": {
                "msgtype": "io.mindroom.tool_approval",
                "body": "Approval required",
                "tool_name": "run_shell_command",
                "agent_name": "code",
                "approval_id": approval_id,
                "tool_call_id": approval_id,
                "arguments": {"command": "echo hi"},
                "requested_at": "2026-04-09T12:00:00+00:00",
                "expires_at": "2026-04-10T12:00:00+00:00",
                "status": status,
                "thread_id": "$thread",
            },
        },
    )


def _approval_room() -> MagicMock:
    room = MagicMock()
    room.room_id = "!room:localhost"
    room.canonical_alias = None
    return room


class _TrackingLock:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._owner: int | None = None

    def __enter__(self) -> Self:
        self._lock.acquire()
        self._owner = threading.get_ident()
        return self

    def __exit__(self, *_args: object) -> None:
        self._owner = None
        self._lock.release()

    def held_by_current_thread(self) -> bool:
        return self._owner == threading.get_ident()


class _LockCheckingCache(dict[tuple[str, int], object]):
    def __init__(self, lock: _TrackingLock) -> None:
        super().__init__()
        self._lock = lock

    def _require_lock(self) -> None:
        assert self._lock.held_by_current_thread()

    def get(self, key: tuple[str, int], default: object = None) -> object:
        self._require_lock()
        return super().get(key, default)

    def __setitem__(self, key: tuple[str, int], value: object) -> None:
        self._require_lock()
        super().__setitem__(key, value)

    def __iter__(self) -> Iterator[tuple[str, int]]:
        self._require_lock()
        return super().__iter__()

    def pop(self, key: tuple[str, int], default: object = None) -> object:
        self._require_lock()
        return super().pop(key, default)

    def clear(self) -> None:
        self._require_lock()
        super().clear()


def _write_approval_script(script_path: Path, *, requires_approval: bool, wave: int) -> None:
    script_path.write_text(
        f"def check(tool_name, arguments, agent_name):\n    return {requires_approval!r}\n",
        encoding="utf-8",
    )
    current_stat = script_path.stat()
    offset_ns = wave * 1_000_000_000
    os.utime(
        script_path,
        ns=(current_stat.st_atime_ns + offset_ns, current_stat.st_mtime_ns + offset_ns),
    )
    importlib.invalidate_caches()


def _run_script_approval_wave(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    expected_requires_approval: bool,
) -> None:
    barrier = threading.Barrier(4)
    results: list[bool] = []
    errors: list[BaseException] = []

    def _worker(
        worker_barrier: threading.Barrier = barrier,
        worker_results: list[bool] = results,
        worker_errors: list[BaseException] = errors,
    ) -> None:
        try:
            worker_barrier.wait(timeout=1)
            worker_results.append(
                asyncio.run(
                    evaluate_tool_approval(
                        config,
                        runtime_paths,
                        "run_shell_command",
                        {"command": "echo hi"},
                        "code",
                    ),
                )[0],
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            worker_errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert errors == []
    assert results == [expected_requires_approval] * 4


def _create_persisted_pending_request(
    storage_dir: Path,
    *,
    event_id: str | None = "$approval-event",
    event_arguments_payload: dict[str, object] | None = None,
    event_arguments_truncated: bool = False,
) -> str:
    request_id = "persisted-pending"
    payload = {
        "id": request_id,
        "tool_name": "run_shell_command",
        "arguments_preview": {"command": "echo hi"},
        "arguments_preview_truncated": False,
        "event_arguments_payload": {"command": "echo hi"}
        if event_arguments_payload is None
        else event_arguments_payload,
        "event_arguments_truncated": event_arguments_truncated,
        "agent_name": "code",
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "requester_id": "@user:localhost",
        "approver_user_id": "@user:localhost",
        "matched_rule": "run_shell_*",
        "script_path": None,
        "requested_at": "2026-04-09T12:00:00+00:00",
        "expires_at": "2026-04-10T12:00:00+00:00",
        "status": "pending",
        "resolution_reason": None,
        "resolved_at": None,
        "resolved_by": None,
        "event_id": event_id,
        "resolution_synced_at": None,
    }
    request_path = storage_dir / f"{request_id}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_id


def _create_persisted_resolved_request(
    storage_dir: Path,
    *,
    request_id: str = "persisted-expired",
    arguments_preview: object | None = None,
    arguments_preview_truncated: bool = False,
    event_arguments_payload: dict[str, object] | None = None,
    event_arguments_truncated: bool = False,
) -> str:
    payload = {
        "id": request_id,
        "tool_name": "run_shell_command",
        "arguments_preview": {"command": "echo hi"} if arguments_preview is None else arguments_preview,
        "arguments_preview_truncated": arguments_preview_truncated,
        "event_arguments_payload": {"command": "echo hi"}
        if event_arguments_payload is None
        else event_arguments_payload,
        "event_arguments_truncated": event_arguments_truncated,
        "agent_name": "code",
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "requester_id": "@user:localhost",
        "approver_user_id": "@user:localhost",
        "matched_rule": "run_shell_*",
        "script_path": None,
        "requested_at": "2026-04-09T12:00:00+00:00",
        "expires_at": "2026-04-10T12:00:00+00:00",
        "status": "expired",
        "resolution_reason": "MindRoom restarted before approval completed.",
        "resolved_at": "2026-04-09T12:30:00+00:00",
        "resolved_by": None,
        "event_id": "$approval-event",
        "resolution_synced_at": None,
    }
    request_path = storage_dir / f"{request_id}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_id


def _custom_response_event(
    *,
    approval_event_id: str,
    approval_id: str = "approval-1",
    status: str,
    reason: str | None = None,
    room_id: str = "!room:localhost",
    sender: str = "@user:localhost",
    thread_event_id: str = "$thread",
) -> nio.UnknownEvent:
    content: dict[str, object] = {
        "approval_id": approval_id,
        "status": status,
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": thread_event_id,
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": approval_event_id},
        },
    }
    if reason is not None:
        content["reason"] = reason
    return nio.UnknownEvent.from_dict(
        {
            "type": "io.mindroom.tool_approval_response",
            "event_id": "$response",
            "sender": sender,
            "origin_server_ts": 1,
            "room_id": room_id,
            "content": content,
        },
    )


def test_config_rejects_invalid_tool_approval_rules() -> None:
    """Config validation should reject malformed tool-approval settings."""
    with pytest.raises(ValidationError, match=r"tool_approval.default must be"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"default": "deny_all"},
        )

    with pytest.raises(ValidationError, match=r"tool_approval.rules\[0\].match must not be empty"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"rules": [{"match": "", "action": "require_approval"}]},
        )

    with pytest.raises(ValidationError, match="must set exactly one of action or script"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"rules": [{"match": "run_*"}]},
        )

    with pytest.raises(ValidationError, match="must set exactly one of action or script"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"rules": [{"match": "run_*", "action": "require_approval", "script": "approve.py"}]},
        )

    with pytest.raises(ValidationError, match=r"tool_approval.timeout_days must be a finite number greater than 0"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"timeout_days": 0},
        )


def test_config_rejects_non_finite_tool_approval_timeout_days() -> None:
    """Config validation should reject NaN and infinite approval timeouts."""
    with pytest.raises(ValidationError, match=r"tool_approval.timeout_days must be a finite number greater than 0"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"timeout_days": "nan"},
        )

    with pytest.raises(
        ValidationError,
        match=r"tool_approval.rules\[0\].timeout_days must be a finite number greater than 0",
    ):
        Config(
            **_base_config_kwargs(),
            tool_approval={
                "rules": [
                    {
                        "match": "run_shell_command",
                        "action": "require_approval",
                        "timeout_days": "inf",
                    },
                ],
            },
        )


def test_config_rejects_unrepresentable_tool_approval_timeout_days() -> None:
    """Config validation should reject approval windows that would overflow datetime arithmetic."""
    with pytest.raises(ValidationError, match=r"tool_approval.timeout_days must be at most 36500"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"timeout_days": 36501},
        )


def test_programmatic_tool_approval_models_reject_invalid_values() -> None:
    """Direct model construction should enforce the same approval validation rules."""
    with pytest.raises(ValidationError, match=r"tool_approval.timeout_days must be a finite number greater than 0"):
        ToolApprovalConfig(timeout_days=float("nan"))

    with pytest.raises(ValidationError, match=r"tool_approval.timeout_days must be at most 36500"):
        ToolApprovalConfig(timeout_days=36501)

    with pytest.raises(ValidationError, match=r"tool_approval.rules\[\]\.match must not be empty"):
        ApprovalRuleConfig(match="", action="require_approval")

    with pytest.raises(ValidationError, match="must set exactly one of action or script"):
        ApprovalRuleConfig(match="run_*", action="require_approval", script="approve.py")


@pytest.mark.asyncio
async def test_evaluate_tool_approval_matches_rules_in_order(tmp_path: Path) -> None:
    """The first matching rule should win and return its timeout override."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            default="auto_approve",
            timeout_days=7,
            rules=[
                ApprovalRuleConfig(match="read_*", action="require_approval", timeout_days=3),
                ApprovalRuleConfig(match="read_file", action="auto_approve", timeout_days=1),
            ],
        ),
    )

    requires_approval, matched_rule, script_path, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "read_file",
        {"path": "notes.txt"},
        "code",
    )

    assert requires_approval is True
    assert matched_rule == "read_*"
    assert script_path is None
    assert timeout_seconds == pytest.approx(3 * 24 * 60 * 60)


@pytest.mark.asyncio
async def test_evaluate_tool_approval_supports_async_script_checks(tmp_path: Path) -> None:
    """Approval scripts should support async check() functions."""
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval_scripts" / "shell_review.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(
        "async def check(tool_name, arguments, agent_name):\n"
        "    return tool_name == 'run_shell_command' and agent_name == 'code'\n",
        encoding="utf-8",
    )
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[
                ApprovalRuleConfig(
                    match="run_shell_command",
                    script="approval_scripts/shell_review.py",
                    timeout_days=3,
                ),
            ],
        ),
    )

    requires_approval, matched_rule, resolved_script_path, timeout_seconds = await evaluate_tool_approval(
        config,
        runtime_paths,
        "run_shell_command",
        {"command": "echo hi"},
        "code",
    )

    assert requires_approval is True
    assert matched_rule == "run_shell_command"
    assert resolved_script_path == str(script_path.resolve())
    assert timeout_seconds == pytest.approx(3 * 24 * 60 * 60)


@pytest.mark.asyncio
async def test_script_cache_invalidates_when_mtime_changes(tmp_path: Path) -> None:
    """Script decisions should hot-reload when the file mtime changes."""
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval_scripts" / "shell_review.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("def check(tool_name, arguments, agent_name):\n    return False\n", encoding="utf-8")
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", script="approval_scripts/shell_review.py")],
        ),
    )

    first_result = await evaluate_tool_approval(
        config,
        runtime_paths,
        "run_shell_command",
        {"command": "echo hi"},
        "code",
    )
    assert first_result[0] is False

    script_path.write_text("def check(tool_name, arguments, agent_name):\n    return True\n", encoding="utf-8")
    current_stat = script_path.stat()
    os.utime(script_path, ns=(current_stat.st_atime_ns + 1_000_000_000, current_stat.st_mtime_ns + 1_000_000_000))

    second_result = await evaluate_tool_approval(
        config,
        runtime_paths,
        "run_shell_command",
        {"command": "echo hi"},
        "code",
    )
    assert second_result[0] is True


def test_script_cache_is_thread_safe_under_concurrent_reloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Concurrent approval checks should synchronize all script-cache mutation and eviction."""
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval_scripts" / "shell_review.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", script="approval_scripts/shell_review.py")],
        ),
    )

    tracking_lock = _TrackingLock()
    monkeypatch.setattr(tool_approval_module, "_SCRIPT_CACHE_LOCK", tracking_lock)
    monkeypatch.setattr(tool_approval_module, "_SCRIPT_CACHE", _LockCheckingCache(tracking_lock))

    for wave, requires_approval in enumerate((False, True, False), start=1):
        _write_approval_script(script_path, requires_approval=requires_approval, wave=wave)
        _run_script_approval_wave(
            config=config,
            runtime_paths=runtime_paths,
            expected_requires_approval=requires_approval,
        )

    assert len(tool_approval_module._SCRIPT_CACHE) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("initialize_manager", [False, True])
async def test_shutdown_approval_store_clears_script_cache_under_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    initialize_manager: bool,
) -> None:
    """Shutdown should clear the shared script cache with the same lock discipline as reloads."""
    runtime_paths = test_runtime_paths(tmp_path)
    tracking_lock = _TrackingLock()
    cache = _LockCheckingCache(tracking_lock)
    monkeypatch.setattr(tool_approval_module, "_SCRIPT_CACHE_LOCK", tracking_lock)
    monkeypatch.setattr(tool_approval_module, "_SCRIPT_CACHE", cache)
    with tracking_lock:
        cache[("approval_scripts/shell_review.py", 123)] = object()

    if initialize_manager:
        initialize_approval_store(runtime_paths)

    await shutdown_approval_store()

    assert cache == {}


@pytest.mark.asyncio
async def test_evaluate_tool_approval_rejects_bad_scripts(tmp_path: Path) -> None:
    """Script load and execution failures should raise a clear approval error."""
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval_scripts" / "broken.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("def check(tool_name, arguments, agent_name):\n    return 'yes'\n", encoding="utf-8")
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", script="approval_scripts/broken.py")],
        ),
    )

    with pytest.raises(ToolApprovalScriptError, match="non-bool"):
        await evaluate_tool_approval(
            config,
            runtime_paths,
            "run_shell_command",
            {"command": "echo hi"},
            "code",
        )


@pytest.mark.asyncio
async def test_evaluate_tool_approval_sanitizes_import_failures(tmp_path: Path) -> None:
    """Import-time script failures should expose only the exception type."""
    runtime_paths = test_runtime_paths(tmp_path)
    script_path = tmp_path / "approval_scripts" / "broken_import.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("raise RuntimeError('token sk-secret-123')\n", encoding="utf-8")
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", script="approval_scripts/broken_import.py")],
        ),
    )

    with pytest.raises(ToolApprovalScriptError) as exc_info:
        await evaluate_tool_approval(
            config,
            runtime_paths,
            "run_shell_command",
            {"command": "echo hi"},
            "code",
        )

    assert str(exc_info.value) == (f"Approval script '{script_path}' failed to import with RuntimeError")
    assert "sk-secret-123" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_request_approval_approves_and_edits_matrix_event(tmp_path: Path) -> None:
    """Approvals should send a pending card, wait, then edit it on approval."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    approval_store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    assert pending.event_id == "$approval"
    assert "original_event_sender_user_id" not in pending.to_dict()
    assert sender.await_args.args[:2] == ("!room:localhost", "$thread")
    assert sender.await_args.args[2]["msgtype"] == "io.mindroom.tool_approval"
    assert sender.await_args.args[2]["status"] == "pending"

    resolved = await approval_store.approve(pending.id, resolved_by="@user:localhost")
    decision = await task

    assert resolved.status == "approved"
    assert decision.status == "approved"
    assert decision.resolved_by == "@user:localhost"
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    assert editor.await_args.args[2]["status"] == "approved"
    assert editor.await_args.args[2]["thread_id"] == "$thread"
    assert approval_store.list_pending() == []


@pytest.mark.asyncio
async def test_request_approval_persists_event_id_without_original_sender(tmp_path: Path) -> None:
    """Persisted approvals should keep only the event id, not sender-owned state."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    assert pending.event_id == "$approval"
    assert "original_event_sender_user_id" not in pending.to_dict()

    await store.approve(pending.id, resolved_by="@user:localhost")
    await task


@pytest.mark.asyncio
async def test_request_approval_sanitizes_arguments_in_matrix_event_and_persistence(tmp_path: Path) -> None:
    """Approval cards and persisted records should only expose bounded sanitized previews."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    secret_arguments = {
        "command": "curl https://user:super-secret@example.com",
        "headers": {"Authorization": "Bearer sk-live-secret-token"},
        "prompt": "x" * 5000,
    }
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments=secret_arguments,
    )

    assert pending is not None
    assert pending.arguments == secret_arguments
    event_payload = sender.await_args.args[2]
    event_payload_text = json.dumps(event_payload, sort_keys=True)
    persisted_text = (runtime_paths.storage_root / "approvals" / f"{pending.id}.json").read_text(encoding="utf-8")

    assert "super-secret" not in event_payload_text
    assert "sk-live-secret-token" not in event_payload_text
    assert "super-secret" not in persisted_text
    assert "sk-live-secret-token" not in persisted_text
    assert "original_event_sender_user_id" not in persisted_text
    assert "***redacted***" in event_payload_text
    assert "***redacted***" in persisted_text
    assert isinstance(event_payload["arguments"], dict)
    assert _cinny_accepts_tool_approval_payload(event_payload, expected_arguments=event_payload["arguments"])
    assert len(json.dumps(event_payload["arguments"], sort_keys=True)) <= 1200
    assert event_payload["arguments_truncated"] is True

    await store.deny(pending.id, reason="cleanup", resolved_by="@user:localhost")
    await task


@pytest.mark.asyncio
async def test_request_approval_direct_approve_denies_truncated_preview(tmp_path: Path) -> None:
    """Direct approval by id should still fail closed when the approval preview is truncated."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments={"command": "echo hi", "prompt": "x" * 5000},
    )

    assert pending is not None

    resolved = await store.approve(pending.id, resolved_by="@user:localhost")
    decision = await task

    assert resolved.status == "denied"
    assert resolved.resolution_reason == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )
    assert decision.status == "denied"
    assert decision.reason == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )
    assert editor.await_args.args[2]["status"] == "denied"
    assert editor.await_args.args[2]["resolution_reason"] == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )


@pytest.mark.asyncio
async def test_request_approval_caps_key_heavy_arguments_in_matrix_event(tmp_path: Path) -> None:
    """Key-heavy argument payloads should still stay within the event preview budget."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    key_heavy_arguments = {f"key_{index:03d}_{'x' * 16}": "v" for index in range(200)}
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments=key_heavy_arguments,
    )

    assert pending is not None
    event_payload = sender.await_args.args[2]
    assert isinstance(event_payload["arguments"], dict)
    assert len(json.dumps(event_payload["arguments"], sort_keys=True)) <= 1200

    await store.approve(pending.id, resolved_by="@user:localhost")
    await task


@pytest.mark.asyncio
async def test_request_approval_denies_with_reason(tmp_path: Path) -> None:
    """Denials should unblock the waiting tool call and include the denial reason."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    resolved = await store.deny(
        pending.id,
        reason="Too dangerous",
        resolved_by="@user:localhost",
    )
    decision = await task

    assert resolved.status == "denied"
    assert resolved.resolution_reason == "Too dangerous"
    assert decision.status == "denied"
    assert decision.reason == "Too dangerous"
    assert editor.await_args.args[2]["resolution_reason"] == "Too dangerous"


@pytest.mark.asyncio
async def test_request_approval_resolves_from_different_event_loop(tmp_path: Path) -> None:
    """Approval resolution from another thread and loop should wake the waiter."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    result: ApprovalDecision | None = None
    error: BaseException | None = None

    def worker() -> None:
        nonlocal result, error
        try:
            result = asyncio.run(
                store.request_approval(
                    tool_name="run_shell_command",
                    arguments={"command": "echo hi"},
                    agent_name="code",
                    room_id="!room:localhost",
                    thread_id="$thread",
                    requester_id="@user:localhost",
                    approver_user_id="@user:localhost",
                    matched_rule="run_shell_*",
                    script_path=None,
                    timeout_seconds=60,
                ),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            error = exc

    thread = threading.Thread(target=worker)
    thread.start()

    async with asyncio.timeout(1):
        while True:
            pending = store.list_pending()
            if pending:
                break
            await asyncio.sleep(0)

    handled = await store.handle_approval_resolution(
        approval_id=pending[0].id,
        status="approved",
        reason=None,
        resolved_by="@user:localhost",
    )
    thread.join(timeout=1)

    assert handled is True
    assert error is None
    assert not thread.is_alive()
    assert result is not None
    assert result.status == "approved"
    assert result.resolved_by == "@user:localhost"
    assert editor.await_args.args[2]["status"] == "approved"
    assert store.list_pending() == []


@pytest.mark.asyncio
async def test_request_approval_resolution_is_thread_safe_under_concurrent_resolvers(tmp_path: Path) -> None:
    """Concurrent approval resolutions from different threads should only succeed once."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    barrier = threading.Barrier(3)
    results: list[bool] = []
    errors: list[BaseException] = []

    def worker(status: Literal["approved", "denied"]) -> None:
        try:
            barrier.wait(timeout=1)
            handled = asyncio.run(
                store.handle_approval_resolution(
                    approval_id=pending.id,
                    status=status,
                    reason=f"{status} by worker",
                    resolved_by="@user:localhost",
                ),
            )
            results.append(handled)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    approved_thread = threading.Thread(target=worker, args=("approved",))
    denied_thread = threading.Thread(target=worker, args=("denied",))
    approved_thread.start()
    denied_thread.start()
    barrier.wait(timeout=1)
    approved_thread.join(timeout=1)
    denied_thread.join(timeout=1)

    assert errors == []
    assert sorted(results) == [False, True]
    decision = await task
    assert decision.status in {"approved", "denied"}
    assert decision.resolved_by == "@user:localhost"
    assert editor.await_args.args[2]["status"] == decision.status


@pytest.mark.asyncio
async def test_request_approval_times_out_and_edits_card(tmp_path: Path) -> None:
    """Timeouts should expire the request and edit the approval event."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        timeout_seconds=0,
    )

    del pending
    decision = await task

    assert decision.status == "expired"
    assert decision.reason == "Tool approval request timed out."
    assert editor.await_args.args[2]["status"] == "expired"
    assert store.list_pending() == []


@pytest.mark.asyncio
async def test_request_approval_uses_absolute_expiry_after_delayed_send(tmp_path: Path) -> None:
    """The approval deadline should be enforced from the advertised expires_at, not from delivery completion."""
    runtime_paths = test_runtime_paths(tmp_path)
    send_started = asyncio.Event()

    async def delayed_sender(*_args: object) -> SentApprovalEvent:
        send_started.set()
        await asyncio.sleep(0.2)
        return _sent_approval_event()

    sender = AsyncMock(side_effect=delayed_sender)
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments={"command": "echo hi"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            matched_rule="run_shell_*",
            script_path=None,
            timeout_seconds=0.05,
        ),
    )
    await send_started.wait()

    pending = store.list_pending()
    assert len(pending) == 1

    decision = await task

    assert decision.status == "expired"
    assert decision.reason == "Tool approval request timed out."
    assert editor.await_args.args[2]["status"] == "expired"
    with pytest.raises(LookupError, match=pending[0].id):
        await store.approve(pending[0].id, resolved_by="@user:localhost")


@pytest.mark.asyncio
async def test_request_approval_cancellation_marks_request_expired(tmp_path: Path) -> None:
    """Cancelling the waiting tool call should mark the approval as expired."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["resolution_reason"] == "Tool approval request was cancelled."


@pytest.mark.asyncio
async def test_request_approval_cancellation_during_send_expires_persisted_pending_request(tmp_path: Path) -> None:
    """Cancelling while the approval card is still sending should clean up the persisted pending request."""
    runtime_paths = test_runtime_paths(tmp_path)

    async def _blocked_send(*_args: object) -> SentApprovalEvent:
        await asyncio.sleep(60)
        return _sent_approval_event()

    sender = AsyncMock(side_effect=_blocked_send)
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments={"command": "echo hi"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            matched_rule="run_shell_*",
            script_path=None,
            timeout_seconds=60,
        ),
    )
    await asyncio.sleep(0)

    pending = store.list_pending()
    assert len(pending) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert store._pending_by_id == {}
    assert (runtime_paths.storage_root / "approvals" / f"{pending[0].id}.json").exists() is False
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_approval_requires_matrix_context(tmp_path: Path) -> None:
    """Requests without a Matrix room should fail closed."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(
        runtime_paths,
        sender=AsyncMock(return_value=_sent_approval_event()),
        editor=AsyncMock(),
    )

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id=None,
        thread_id=None,
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=60,
    )

    assert decision.status == "denied"
    assert decision.reason == "Tool approval requires a Matrix room."


@pytest.mark.asyncio
async def test_request_approval_supports_room_mode_without_thread_id(tmp_path: Path) -> None:
    """Room-mode approvals should anchor to the room even without a thread."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments={"command": "echo hi"},
            agent_name="code",
            room_id="!room:localhost",
            thread_id=None,
            requester_id="@user:localhost",
            approver_user_id="@user:localhost",
            matched_rule="run_shell_*",
            script_path=None,
            timeout_seconds=60,
        ),
    )
    await asyncio.sleep(0)
    pending = store.list_pending()

    assert len(pending) == 1
    assert sender.await_args.args[:2] == ("!room:localhost", None)
    assert sender.await_args.args[2]["thread_id"] is None

    await store.approve(pending[0].id, resolved_by="@user:localhost")
    decision = await task

    assert decision.status == "approved"
    assert editor.await_args.args[2]["thread_id"] is None


@pytest.mark.asyncio
async def test_request_approval_requires_human_requester(tmp_path: Path) -> None:
    """Agent-authored approval requests should fail closed without sending a card."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@mindroom_code:localhost",
        approver_user_id=None,
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=0,
    )

    assert decision.status == "denied"
    assert decision.reason == "Tool approval requires a human requester."
    sender.assert_not_awaited()
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_approval_rejects_configured_bot_account_requester(tmp_path: Path) -> None:
    """Configured bridge bots should not own human approval requests."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            **_base_config_kwargs(),
            bot_accounts=["@bridgebot:localhost"],
        ),
        runtime_paths,
    )
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@bridgebot:localhost",
        approver_user_id=resolve_tool_approval_approver(config, runtime_paths, "@bridgebot:localhost"),
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=0,
    )

    assert decision.status == "denied"
    assert decision.reason == "Tool approval requires a human requester."
    sender.assert_not_awaited()
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_approval_expires_when_matrix_send_fails(tmp_path: Path) -> None:
    """Requests should fail closed when the Matrix approval card cannot be delivered."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(runtime_paths, sender=AsyncMock(return_value=None), editor=AsyncMock())

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=60,
    )

    assert decision.status == "expired"
    assert decision.reason == "Tool approval request could not be delivered to Matrix."
    assert store.list_pending() == []


@pytest.mark.asyncio
async def test_request_approval_returns_specific_reason_when_router_transport_is_unavailable(tmp_path: Path) -> None:
    """Requests in rooms without the router should fail with an explicit limitation message."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$approval-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    orchestrator.agent_bots = {"router": router_bot}

    store = initialize_approval_store(
        runtime_paths,
        sender=orchestrator._send_approval_event,
        editor=AsyncMock(),
    )

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=60,
    )

    assert decision.status == "expired"
    assert decision.reason == (
        "Tool approval requires the router to be joined to the Matrix room. "
        "In ad-hoc invited rooms accepted via accept_invites, approval only works if the router "
        "is already joined there; otherwise retry from a managed room."
    )
    router_client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_approval_discards_pending_request_when_matrix_send_returns_none(tmp_path: Path) -> None:
    """Send failures that return no event ID should not leak approval requests in memory or on disk."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(runtime_paths, sender=AsyncMock(return_value=None), editor=AsyncMock())

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=60,
    )

    assert decision.status == "expired"
    assert store._pending_by_id == {}
    assert list((runtime_paths.storage_root / "approvals").glob("*.json")) == []


@pytest.mark.asyncio
async def test_request_approval_discards_pending_request_when_matrix_send_raises(tmp_path: Path) -> None:
    """Exceptions during send should not leak approval requests in memory or on disk."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(
        runtime_paths,
        sender=AsyncMock(side_effect=RuntimeError("send failed")),
        editor=AsyncMock(),
    )

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=60,
    )

    assert decision.status == "expired"
    assert store._pending_by_id == {}
    assert list((runtime_paths.storage_root / "approvals").glob("*.json")) == []


@pytest.mark.asyncio
async def test_request_approval_persists_request_before_matrix_send(tmp_path: Path) -> None:
    """Approval state should be durable before the Matrix transport is attempted."""
    runtime_paths = test_runtime_paths(tmp_path)
    editor = AsyncMock()

    async def sender(
        room_id: str,
        thread_id: str,
        content: dict[str, object],
    ) -> SentApprovalEvent | None:
        del room_id, thread_id
        request_path = runtime_paths.storage_root / "approvals" / f"{content['approval_id']}.json"
        assert request_path.exists()
        persisted_payload = json.loads(request_path.read_text(encoding="utf-8"))
        assert persisted_payload["status"] == "pending"
        assert persisted_payload["event_id"] is None
        return None

    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        approver_user_id="@user:localhost",
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=60,
    )

    assert decision.status == "expired"
    assert decision.reason == "Tool approval request could not be delivered to Matrix."
    persisted_requests = list((runtime_paths.storage_root / "approvals").glob("*.json"))
    assert persisted_requests == []


@pytest.mark.asyncio
async def test_handle_approval_resolution_updates_future_and_card(tmp_path: Path) -> None:
    """Direct resolution by approval ID should resolve the pending request exactly once."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    handled = await store.handle_approval_resolution(
        approval_id=pending.id,
        status="approved",
        reason=None,
        resolved_by="@user:localhost",
    )
    decision = await task

    assert handled is True
    assert decision.status == "approved"
    assert store.get_request(pending.id) is None
    handled_again = await store.handle_approval_resolution(
        approval_id=pending.id,
        status="approved",
        reason=None,
        resolved_by="@user:localhost",
    )
    assert handled_again is False


@pytest.mark.asyncio
async def test_handle_approval_resolution_denies_truncated_preview_on_approve(tmp_path: Path) -> None:
    """Approval-by-id should deny truncated approval previews instead of bypassing the guard."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments={"command": "echo hi", "prompt": "x" * 5000},
    )

    assert pending is not None

    handled = await store.handle_approval_resolution(
        approval_id=pending.id,
        status="approved",
        reason=None,
        resolved_by="@user:localhost",
    )
    decision = await task

    assert handled is True
    assert decision.status == "denied"
    assert decision.reason == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )
    assert editor.await_args.args[2]["status"] == "denied"
    assert editor.await_args.args[2]["resolution_reason"] == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )


@pytest.mark.asyncio
async def test_synced_resolved_approval_is_evicted_from_live_indexes(tmp_path: Path) -> None:
    """Resolved approvals should clear raw arguments and disappear from live lookup indexes after sync."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock(return_value=True)
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None

    await store.approve(pending.id, resolved_by="@user:localhost")
    decision = await task

    assert decision.status == "approved"
    assert pending.arguments == {}
    assert pending.id not in store._requests_by_id
    assert store.anchored_request_for_event(approval_event_id="$approval", room_id="!room:localhost") is None


@pytest.mark.asyncio
async def test_synced_resolved_approval_deletes_persisted_file(tmp_path: Path) -> None:
    """Synced approval resolutions should remove their persisted JSON record from disk."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock(return_value=True)
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    request_path = runtime_paths.storage_root / "approvals" / f"{pending.id}.json"
    assert request_path.exists()

    await store.approve(pending.id, resolved_by="@user:localhost")
    decision = await task

    assert decision.status == "approved"
    assert request_path.exists() is False


@pytest.mark.asyncio
async def test_programmatic_approve_requires_original_requester(tmp_path: Path) -> None:
    """Programmatic approval should reject non-requester actors."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None

    with pytest.raises(PermissionError, match="original requester"):
        await store.approve(pending.id, resolved_by="@other:localhost")

    resolved = await store.approve(pending.id, resolved_by="@user:localhost")
    decision = await task

    assert resolved.status == "approved"
    assert decision.status == "approved"
    assert editor.await_args.args[2]["resolved_by"] == "@user:localhost"


@pytest.mark.asyncio
async def test_handle_reaction_approves_by_event_id(tmp_path: Path) -> None:
    """Reaction approval should resolve the pending request by Matrix event ID."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, _pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    handled = await store.handle_reaction(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reaction_key="✅",
        resolved_by="@user:localhost",
    )
    decision = await task

    assert handled.handled is True
    assert decision.status == "approved"


@pytest.mark.asyncio
async def test_handle_reaction_requires_original_requester(tmp_path: Path) -> None:
    """Only the original requester should be able to resolve a pending approval."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None

    handled = await store.handle_reaction(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reaction_key="✅",
        resolved_by="@other:localhost",
    )

    assert handled.handled is False
    assert task.done() is False

    handled = await store.handle_reaction(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reaction_key="✅",
        resolved_by="@user:localhost",
    )
    decision = await task

    assert handled.handled is True
    assert decision.status == "approved"


@pytest.mark.asyncio
async def test_handle_reaction_refuses_truncated_approval_preview(tmp_path: Path) -> None:
    """Approve actions should fail closed when the approval card preview is truncated."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    long_arguments = {"command": "echo hi", "prompt": "x" * 5000}
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments=long_arguments,
    )

    assert pending is not None

    handled = await store.handle_reaction(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reaction_key="✅",
        resolved_by="@user:localhost",
    )

    assert handled.handled is True
    assert handled.error_reason == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )
    assert task.done() is False

    await store.deny(pending.id, reason="cleanup", resolved_by="@user:localhost")
    decision = await task
    assert decision.status == "denied"


@pytest.mark.asyncio
async def test_handle_reply_denies_by_event_id(tmp_path: Path) -> None:
    """Reply denial should resolve the pending request by Matrix event ID."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, _pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    handled = await store.handle_reply(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reason="No destructive commands",
        resolved_by="@user:localhost",
    )
    decision = await task

    assert handled.handled is True
    assert decision.status == "denied"
    assert decision.reason == "No destructive commands"


@pytest.mark.asyncio
async def test_shutdown_expires_pending_requests(tmp_path: Path) -> None:
    """Shutdown should expire any live approvals and unblock waiting tasks."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    await shutdown_approval_store()
    decision = await task

    assert get_approval_store() is None
    assert decision.status == "expired"
    assert decision.reason == "MindRoom shut down before approval completed."
    assert editor.await_args.args[2]["status"] == "expired"


def test_initialize_approval_store_expires_persisted_pending_requests(tmp_path: Path) -> None:
    """Startup should expire persisted pending approvals from a prior process."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_pending_request(runtime_paths.storage_root / "approvals")

    store = initialize_approval_store(runtime_paths)

    assert store.list_pending() == []
    payload = json.loads((runtime_paths.storage_root / "approvals" / f"{request_id}.json").read_text(encoding="utf-8"))
    assert payload["status"] == "expired"
    assert payload["resolution_reason"] == "MindRoom restarted before approval completed."
    assert payload["resolution_synced_at"] is None


def test_initialize_approval_store_expires_undelivered_pending_requests(tmp_path: Path) -> None:
    """Startup should fail closed but keep unresolved delivery state recoverable across restart."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_pending_request(
        runtime_paths.storage_root / "approvals",
        event_id=None,
    )
    request_path = runtime_paths.storage_root / "approvals" / f"{request_id}.json"

    store = initialize_approval_store(runtime_paths)

    assert store.list_pending() == []
    assert request_path.exists() is True
    assert request_id in store._requests_by_id
    pending = store._requests_by_id[request_id]
    assert pending.status == "expired"
    assert pending.resolution_reason == "MindRoom restarted before approval delivery could be confirmed."
    assert pending.event_id is None


def test_initialize_approval_store_reindexes_persisted_approval_event_ids(tmp_path: Path) -> None:
    """Restarted stores should still anchor stale approval cards by their original event id."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_resolved_request(runtime_paths.storage_root / "approvals")

    store = initialize_approval_store(runtime_paths)
    pending = store.anchored_request_for_event(
        approval_event_id="$approval-event",
        room_id="!room:localhost",
    )

    assert pending is not None
    assert pending.id == request_id
    assert pending.status == "expired"
    assert pending.resolution_synced_at is None


@pytest.mark.asyncio
async def test_recover_unconfirmed_approval_event_deliveries_replays_expired_card(tmp_path: Path) -> None:
    """Restart recovery should recover missing event ids before replaying the expired-card edit."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_pending_request(
        runtime_paths.storage_root / "approvals",
        event_id=None,
    )
    recoverer = AsyncMock(return_value="$approval-event")
    editor = AsyncMock(return_value=True)
    initialize_approval_store(runtime_paths, editor=editor, recoverer=recoverer)

    recovered_requests = await recover_unconfirmed_approval_event_deliveries()
    synced_requests = await sync_unsynced_approval_event_resolutions()

    assert [request.id for request in recovered_requests] == [request_id]
    assert [request.id for request in synced_requests] == [request_id]
    recoverer.assert_awaited_once()
    editor.assert_awaited_once()
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval-event")
    assert (runtime_paths.storage_root / "approvals" / f"{request_id}.json").exists() is False


@pytest.mark.asyncio
async def test_recover_unconfirmed_approval_event_deliveries_discards_missing_cards(tmp_path: Path) -> None:
    """Restart recovery should discard unconfirmed approvals when no Matrix card can be found."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_pending_request(
        runtime_paths.storage_root / "approvals",
        event_id=None,
    )
    recoverer = AsyncMock(return_value=None)
    editor = AsyncMock(return_value=True)
    store = initialize_approval_store(runtime_paths, editor=editor, recoverer=recoverer)

    recovered_requests = await recover_unconfirmed_approval_event_deliveries()

    assert recovered_requests == []
    recoverer.assert_awaited_once()
    editor.assert_not_awaited()
    assert request_id not in store._requests_by_id
    assert (runtime_paths.storage_root / "approvals" / f"{request_id}.json").exists() is False


@pytest.mark.asyncio
async def test_sync_unsynced_approval_event_resolutions_replays_persisted_expired_requests(tmp_path: Path) -> None:
    """Startup reconciliation should edit stale approval cards back to expired."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_resolved_request(runtime_paths.storage_root / "approvals")
    editor = AsyncMock(return_value=True)
    initialize_approval_store(runtime_paths, editor=editor)

    synced_requests = await sync_unsynced_approval_event_resolutions()

    assert [request.id for request in synced_requests] == [request_id]
    editor.assert_awaited_once()
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval-event")
    assert editor.await_args.args[2]["status"] == "expired"
    assert editor.await_args.args[2]["thread_id"] == "$thread"
    assert (runtime_paths.storage_root / "approvals" / f"{request_id}.json").exists() is False


@pytest.mark.asyncio
async def test_sync_unsynced_approval_event_resolutions_keep_original_argument_shape_for_cinny(
    tmp_path: Path,
) -> None:
    """Replay edits should keep a full Cinny-readable replacement payload."""
    runtime_paths = test_runtime_paths(tmp_path)
    original_event_arguments = {"command": "echo hi", "prompt": "[truncated]"}
    request_id = _create_persisted_resolved_request(
        runtime_paths.storage_root / "approvals",
        arguments_preview='{"command":"echo hi","prompt":"[truncated]"}',
        arguments_preview_truncated=True,
        event_arguments_payload=original_event_arguments,
        event_arguments_truncated=True,
    )
    editor = AsyncMock(return_value=True)
    initialize_approval_store(runtime_paths, editor=editor)

    synced_requests = await sync_unsynced_approval_event_resolutions()

    assert [request.id for request in synced_requests] == [request_id]
    edited_payload = editor.await_args.args[2]
    assert edited_payload["approval_id"] == request_id
    assert edited_payload["tool_name"] == "run_shell_command"
    assert edited_payload["agent_name"] == "code"
    assert edited_payload["arguments"] == original_event_arguments
    assert edited_payload["arguments_truncated"] is True
    assert edited_payload["status"] == "expired"
    assert edited_payload["requested_at"] == "2026-04-09T12:00:00+00:00"
    assert edited_payload["expires_at"] == "2026-04-10T12:00:00+00:00"
    assert _cinny_accepts_tool_approval_payload(edited_payload, expected_arguments=original_event_arguments)
    edited_event = {
        **edited_payload,
        "m.new_content": edited_payload,
        "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval-event"},
    }
    cinny_content = edited_event["m.new_content"]
    assert isinstance(cinny_content, dict)
    assert _cinny_accepts_tool_approval_payload(cinny_content, expected_arguments=original_event_arguments)


@pytest.mark.asyncio
async def test_sync_unsynced_approval_event_resolutions_retry_when_editor_sends_no_edit(tmp_path: Path) -> None:
    """Resolved approvals should stay unsynced when the editor reports a no-op."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_resolved_request(runtime_paths.storage_root / "approvals")
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    initialize_approval_store(runtime_paths, editor=orchestrator._edit_approval_event)

    synced_requests = await sync_unsynced_approval_event_resolutions()

    assert synced_requests == []
    payload = json.loads((runtime_paths.storage_root / "approvals" / f"{request_id}.json").read_text(encoding="utf-8"))
    assert payload["resolution_synced_at"] is None


@pytest.mark.asyncio
async def test_sync_unsynced_approval_event_resolutions_claim_one_replay_under_concurrency(tmp_path: Path) -> None:
    """Concurrent replay workers should claim each approval once before editing."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_resolved_request(runtime_paths.storage_root / "approvals")
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_editor(*_args: object) -> bool:
        started.set()
        await release.wait()
        return True

    editor = AsyncMock(side_effect=delayed_editor)
    initialize_approval_store(runtime_paths, editor=editor)

    tasks = [asyncio.create_task(sync_unsynced_approval_event_resolutions()) for _ in range(3)]
    await started.wait()
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert editor.await_count == 1
    assert sum(len(result) for result in results) == 1
    assert (runtime_paths.storage_root / "approvals" / f"{request_id}.json").exists() is False


@pytest.mark.asyncio
async def test_sync_unsynced_approval_event_resolutions_wait_for_router_transport_bot(tmp_path: Path) -> None:
    """Replay should wait for the router transport instead of falling back to other live bots."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_resolved_request(runtime_paths.storage_root / "approvals")
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()

    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$edit-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = False
    router_bot.client = router_client
    router_bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest-thread-event")

    other_client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    other_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$wrong-edit", room_id="!room:localhost"),
    )
    other_bot = MagicMock()
    other_bot.agent_name = "general"
    other_bot.running = True
    other_bot.client = other_client
    other_bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$other-latest")

    orchestrator.agent_bots = {"router": router_bot, "general": other_bot}
    _grant_approval_room_access_for_client(router_client)
    _grant_approval_room_access_for_client(other_client)
    store = initialize_approval_store(runtime_paths, editor=orchestrator._edit_approval_event)

    synced_requests = await sync_unsynced_approval_event_resolutions()

    assert synced_requests == []
    router_client.room_send.assert_not_awaited()
    other_client.room_send.assert_not_awaited()
    request = store.anchored_request_for_event(
        approval_event_id="$approval-event",
        room_id="!room:localhost",
    )
    assert request is not None
    assert request.id == request_id
    assert request.resolution_synced_at is None

    router_bot.running = True

    synced_requests = await sync_unsynced_approval_event_resolutions()

    assert [request.id for request in synced_requests] == [request_id]
    router_client.room_send.assert_awaited_once()
    other_client.room_send.assert_not_awaited()
    assert (runtime_paths.storage_root / "approvals" / f"{request_id}.json").exists() is False


@pytest.mark.asyncio
async def test_orchestrator_recover_approval_event_id_scans_room_history(tmp_path: Path) -> None:
    """Delivery recovery should recover missing approval event ids from router room history."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_messages = AsyncMock(
        return_value=nio.RoomMessagesResponse(
            room_id="!room:localhost",
            chunk=[_approval_history_event(event_id="$approval-event", approval_id="approval-1")],
            start="",
            end=None,
        ),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    _grant_approval_room_access_for_client(router_client)
    orchestrator.agent_bots = {"router": router_bot}
    pending = PendingApproval.from_dict(
        {
            "id": "approval-1",
            "tool_name": "run_shell_command",
            "arguments_preview": {"command": "echo hi"},
            "arguments_preview_truncated": False,
            "event_arguments_payload": {"command": "echo hi"},
            "event_arguments_truncated": False,
            "agent_name": "code",
            "room_id": "!room:localhost",
            "thread_id": "$thread",
            "requester_id": "@user:localhost",
            "approver_user_id": "@user:localhost",
            "matched_rule": "run_shell_*",
            "script_path": None,
            "requested_at": "2026-04-09T12:00:00+00:00",
            "expires_at": "2026-04-10T12:00:00+00:00",
            "status": "expired",
            "resolution_reason": "MindRoom restarted before approval delivery could be confirmed.",
            "resolved_at": "2026-04-09T12:30:00+00:00",
            "resolved_by": None,
            "event_id": None,
            "resolution_synced_at": None,
        },
    )

    event_id = await orchestrator._recover_approval_event_id(pending)

    assert event_id == "$approval-event"
    router_client.room_messages.assert_awaited_once_with(
        "!room:localhost",
        start=None,
        limit=100,
        message_filter={"types": ["io.mindroom.tool_approval"]},
        direction=nio.MessageDirection.back,
    )


@pytest.mark.asyncio
async def test_orchestrator_send_approval_event_requires_runtime_loop(tmp_path: Path) -> None:
    """Approval transport should fail fast without a captured runtime loop."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    client = MagicMock()
    client.user_id = "@mindroom_code:localhost"
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$approval-event", room_id="!room:localhost"),
    )
    bot = MagicMock()
    bot.client = client
    orchestrator.agent_bots = {"code": bot}

    with pytest.raises(RuntimeError, match="Approval runtime loop is not available"):
        await orchestrator._send_approval_event(
            "!room:localhost",
            "$thread",
            {
                "approval_id": "approval-1",
                "tool_name": "run_shell_command",
                "arguments": {"command": "echo hi"},
                "agent_name": "code",
                "status": "pending",
                "msgtype": "io.mindroom.tool_approval",
                "body": "🔒 Approval required: run_shell_command",
            },
        )


@pytest.mark.asyncio
async def test_orchestrator_send_approval_event_uses_expected_room_send_payload(tmp_path: Path) -> None:
    """The orchestrator helper should emit the Matrix approval card payload."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$approval-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    router_bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest-thread-event")
    _grant_approval_room_access_for_client(router_client)
    code_client = make_matrix_client_mock(user_id="@mindroom_code:localhost")
    code_client.room_send = AsyncMock()
    code_bot = MagicMock()
    code_bot.agent_name = "code"
    code_bot.running = True
    code_bot.client = code_client
    code_bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$code-thread-event")
    orchestrator.agent_bots = {"router": router_bot, "code": code_bot}

    event_id = await orchestrator._send_approval_event(
        "!room:localhost",
        "$thread",
        {
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "pending",
            "msgtype": "io.mindroom.tool_approval",
            "body": "🔒 Approval required: run_shell_command",
        },
    )

    assert event_id == SentApprovalEvent(event_id="$approval-event")
    router_client.room_send.assert_awaited_once_with(
        room_id="!room:localhost",
        message_type="io.mindroom.tool_approval",
        content={
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "pending",
            "msgtype": "io.mindroom.tool_approval",
            "body": "🔒 Approval required: run_shell_command",
            "m.relates_to": {
                "rel_type": "m.thread",
                "event_id": "$thread",
                "is_falling_back": True,
                "m.in_reply_to": {"event_id": "$latest-thread-event"},
            },
        },
    )
    code_client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrator_send_approval_event_raises_when_router_not_joined(tmp_path: Path) -> None:
    """Approval transport should surface the router-managed-room limitation explicitly."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$approval-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    orchestrator.agent_bots = {"router": router_bot}

    with pytest.raises(
        RuntimeError,
        match=(
            r"Tool approval requires the router to be joined to the Matrix room\. "
            r"In ad-hoc invited rooms accepted via accept_invites, approval only works if "
            r"the router is already joined there; otherwise retry from a managed room\."
        ),
    ):
        await orchestrator._send_approval_event(
            "!room:localhost",
            "$thread",
            {
                "approval_id": "approval-1",
                "tool_name": "run_shell_command",
                "arguments": {"command": "echo hi"},
                "agent_name": "code",
                "status": "pending",
                "msgtype": "io.mindroom.tool_approval",
                "body": "🔒 Approval required: run_shell_command",
            },
        )

    router_client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_orchestrator_send_approval_event_supports_room_mode_without_thread_id(tmp_path: Path) -> None:
    """Room-mode approval cards should send without a thread relation."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$approval-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    _grant_approval_room_access_for_client(router_client)
    orchestrator.agent_bots = {"router": router_bot}

    event_id = await orchestrator._send_approval_event(
        "!room:localhost",
        None,
        {
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "pending",
            "msgtype": "io.mindroom.tool_approval",
            "body": "🔒 Approval required: run_shell_command",
            "thread_id": None,
        },
    )

    assert event_id == SentApprovalEvent(event_id="$approval-event")
    router_client.room_send.assert_awaited_once_with(
        room_id="!room:localhost",
        message_type="io.mindroom.tool_approval",
        content={
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "pending",
            "msgtype": "io.mindroom.tool_approval",
            "body": "🔒 Approval required: run_shell_command",
            "thread_id": None,
        },
    )


@pytest.mark.asyncio
async def test_orchestrator_edit_approval_event_uses_expected_room_send_payload(tmp_path: Path) -> None:
    """The orchestrator helper should edit approval cards via m.replace."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$edit-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    router_bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest-thread-event")
    orchestrator.agent_bots = {"router": router_bot}
    _grant_approval_room_access_for_client(router_client)

    edited = await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        {
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "tool_call_id": "approval-1",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "denied",
            "msgtype": "io.mindroom.tool_approval",
            "body": "Denied: run_shell_command",
            "requested_at": "2026-04-11T00:00:00+00:00",
            "expires_at": "2026-04-12T00:00:00+00:00",
            "thread_id": "$thread",
            "resolved_at": "2026-04-12T00:00:00+00:00",
            "resolved_by": "@bas:localhost",
            "resolution_reason": "Too dangerous",
        },
    )

    assert edited is True
    new_content = {
        "approval_id": "approval-1",
        "tool_name": "run_shell_command",
        "tool_call_id": "approval-1",
        "arguments": {"command": "echo hi"},
        "agent_name": "code",
        "status": "denied",
        "msgtype": "io.mindroom.tool_approval",
        "body": "Denied: run_shell_command",
        "requested_at": "2026-04-11T00:00:00+00:00",
        "expires_at": "2026-04-12T00:00:00+00:00",
        "resolved_at": "2026-04-12T00:00:00+00:00",
        "resolved_by": "@bas:localhost",
        "resolution_reason": "Too dangerous",
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": "$thread",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$latest-thread-event"},
        },
    }
    router_client.room_send.assert_awaited_once_with(
        room_id="!room:localhost",
        message_type="io.mindroom.tool_approval",
        content={
            **new_content,
            "m.new_content": new_content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval-event"},
        },
    )


@pytest.mark.asyncio
async def test_orchestrator_edit_approval_event_supports_room_mode_without_thread_id(tmp_path: Path) -> None:
    """Room-mode approval edits should not inject a thread relation."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$edit-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    orchestrator.agent_bots = {"router": router_bot}
    _grant_approval_room_access_for_client(router_client)

    edited = await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        {
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "tool_call_id": "approval-1",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "approved",
            "msgtype": "io.mindroom.tool_approval",
            "body": "Approved: run_shell_command",
            "requested_at": "2026-04-11T00:00:00+00:00",
            "expires_at": "2026-04-12T00:00:00+00:00",
            "thread_id": None,
            "resolved_at": "2026-04-12T00:00:00+00:00",
            "resolved_by": "@bas:localhost",
        },
    )

    assert edited is True
    new_content = {
        "approval_id": "approval-1",
        "tool_name": "run_shell_command",
        "tool_call_id": "approval-1",
        "arguments": {"command": "echo hi"},
        "agent_name": "code",
        "status": "approved",
        "msgtype": "io.mindroom.tool_approval",
        "body": "Approved: run_shell_command",
        "requested_at": "2026-04-11T00:00:00+00:00",
        "expires_at": "2026-04-12T00:00:00+00:00",
        "resolved_at": "2026-04-12T00:00:00+00:00",
        "resolved_by": "@bas:localhost",
    }
    router_client.room_send.assert_awaited_once_with(
        room_id="!room:localhost",
        message_type="io.mindroom.tool_approval",
        content={
            **new_content,
            "m.new_content": new_content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval-event"},
        },
    )


@pytest.mark.asyncio
async def test_orchestrator_edit_approval_event_keeps_full_payload_in_new_content(tmp_path: Path) -> None:
    """Cinny-style getContent() reads should still see a complete resolved approval payload."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$edit-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    router_bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest-thread-event")
    orchestrator.agent_bots = {"router": router_bot}
    _grant_approval_room_access_for_client(router_client)

    await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        {
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "tool_call_id": "approval-1",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "approved",
            "msgtype": "io.mindroom.tool_approval",
            "body": "Approved: run_shell_command",
            "requested_at": "2026-04-11T00:00:00+00:00",
            "expires_at": "2026-04-12T00:00:00+00:00",
            "thread_id": "$thread",
            "resolved_at": "2026-04-12T00:00:00+00:00",
            "resolved_by": "@bas:localhost",
        },
    )

    sent_content = router_client.room_send.await_args.kwargs["content"]
    cinny_content = sent_content["m.new_content"]
    assert isinstance(cinny_content, dict)
    assert cinny_content["approval_id"] == "approval-1"
    assert cinny_content["tool_name"] == "run_shell_command"
    assert cinny_content["agent_name"] == "code"
    assert cinny_content["status"] == "approved"
    assert _cinny_accepts_tool_approval_payload(cinny_content, expected_arguments={"command": "echo hi"})


@pytest.mark.asyncio
async def test_orchestrator_edit_approval_event_does_not_require_redact_power(tmp_path: Path) -> None:
    """Approval edits should still send when the original sender lacks redact permission."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$edit-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    orchestrator.agent_bots = {"router": router_bot}
    _grant_approval_room_access_for_client(router_client, user_level=0, redact_level=100)

    edited = await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        {
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "tool_call_id": "approval-1",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "approved",
            "msgtype": "io.mindroom.tool_approval",
            "body": "Approved: run_shell_command",
            "requested_at": "2026-04-11T00:00:00+00:00",
            "expires_at": "2026-04-12T00:00:00+00:00",
            "thread_id": None,
            "resolved_at": "2026-04-12T00:00:00+00:00",
            "resolved_by": "@bas:localhost",
        },
    )

    assert edited is True
    router_client.room_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_orchestrator_edit_approval_event_returns_false_without_live_room_bot(tmp_path: Path) -> None:
    """The orchestrator helper should report a no-op when no live room bot can service the card."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()

    edited = await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        {
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "approved",
            "msgtype": "io.mindroom.tool_approval",
            "body": "Approved: run_shell_command",
            "thread_id": "$thread",
            "resolved_at": "2026-04-12T00:00:00+00:00",
            "resolved_by": "@bas:localhost",
        },
    )

    assert edited is False


@pytest.mark.asyncio
async def test_orchestrator_edit_approval_event_returns_false_when_router_not_joined(tmp_path: Path) -> None:
    """Approval edits should fail when the router is not joined to the room."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    router_client = make_matrix_client_mock(user_id="@mindroom_router:localhost")
    router_client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$edit-event", room_id="!room:localhost"),
    )
    router_bot = MagicMock()
    router_bot.agent_name = "router"
    router_bot.running = True
    router_bot.client = router_client
    orchestrator.agent_bots = {"router": router_bot}
    _grant_approval_room_access_for_client(router_client, room_id="!other-room:localhost")

    edited = await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        {
            "approval_id": "approval-1",
            "tool_name": "run_shell_command",
            "arguments": {"command": "echo hi"},
            "agent_name": "code",
            "status": "approved",
            "msgtype": "io.mindroom.tool_approval",
            "body": "Approved: run_shell_command",
            "thread_id": "$thread",
            "resolved_at": "2026-04-12T00:00:00+00:00",
            "resolved_by": "@bas:localhost",
        },
    )

    assert edited is False
    router_client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_reaction_approves_pending_tool_call(tmp_path: Path) -> None:
    """Reactions on approval cards should resolve the pending approval from the bot handler."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    reaction = nio.ReactionEvent.from_dict(
        {
            "type": "m.reaction",
            "event_id": "$reaction",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$approval",
                    "key": "✅",
                },
            },
        },
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._handle_reaction_inner(room, reaction)

    decision = await task
    assert decision.status == "approved"
    assert editor.await_args.args[2]["status"] == "approved"


@pytest.mark.asyncio
async def test_bot_reaction_rejects_resolution_after_sender_loses_access(tmp_path: Path) -> None:
    """Approval resolution should fail when the original requester is no longer authorized."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    reaction = nio.ReactionEvent.from_dict(
        {
            "type": "m.reaction",
            "event_id": "$reaction",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$approval",
                    "key": "✅",
                },
            },
        },
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=False),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._handle_reaction_inner(room, reaction)

    assert task.done() is False

    await store.approve(pending.id, resolved_by="@user:localhost")
    decision = await task
    assert decision.status == "approved"


@pytest.mark.asyncio
async def test_bot_reply_denies_pending_tool_call(tmp_path: Path) -> None:
    """Replies to approval cards should deny the tool call and not reach the turn controller."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    event = _reply_event(event_id="$reply", body="Do not run this")

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._on_message(room, event)

    decision = await task
    assert decision.status == "denied"
    assert decision.reason == "Do not run this"
    assert bot._turn_controller.handle_text_event.await_count == 0


@pytest.mark.asyncio
async def test_bot_reply_denial_strips_matrix_rich_reply_fallback(tmp_path: Path) -> None:
    """Reply denials should persist only the user's text, not the quoted Matrix fallback."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    event = _reply_event(
        event_id="$reply",
        body=(
            "> <@mindroom_code:localhost> Approval required: run_shell_command\n"
            '> {"command": "echo hi"}\n'
            "\n"
            "Do not run this"
        ),
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._on_message(room, event)

    decision = await task
    assert decision.status == "denied"
    assert decision.reason == "Do not run this"
    assert editor.await_args.args[2]["resolution_reason"] == "Do not run this"


@pytest.mark.asyncio
async def test_bot_reply_to_unsynced_resolved_approval_does_not_fall_through_to_chat(tmp_path: Path) -> None:
    """Replies on a visually stale approval card should still be swallowed until the resolved edit syncs."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock(return_value=False)
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    first_event = _reply_event(event_id="$reply-1", body="Do not run this")

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._on_message(room, first_event)

    decision = await task

    assert decision.status == "denied"
    assert store.anchored_request_for_event(approval_event_id="$approval", room_id="!room:localhost") is not None

    second_event = _reply_event(event_id="$reply-2", body="Still no")
    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._on_message(room, second_event)

    assert editor.await_count == 1
    assert bot._turn_controller.handle_text_event.await_count == 0


@pytest.mark.asyncio
async def test_other_user_reply_to_unsynced_resolved_approval_is_swallowed(tmp_path: Path) -> None:
    """Replies on a stale approval card should stay on the approval path for every room member."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock(return_value=False)
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    first_event = _reply_event(event_id="$reply-1", body="Do not run this")

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._on_message(room, first_event)

    decision = await task

    assert decision.status == "denied"
    assert store.anchored_request_for_event(approval_event_id="$approval", room_id="!room:localhost") is not None

    second_event = _reply_event(event_id="$reply-2", body="This should become chat", sender="@other:localhost")
    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._on_message(room, second_event)

    assert editor.await_count == 1
    assert bot._turn_controller.handle_text_event.await_count == 0


@pytest.mark.asyncio
async def test_bot_reply_from_wrong_user_is_swallowed_as_approval_action(tmp_path: Path) -> None:
    """Replies from other users should not leak into normal chat when anchored to an approval card."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    event = _reply_event(event_id="$reply", body="I cannot approve this either", sender="@other:localhost")

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._on_message(room, event)

    assert task.done() is False
    assert bot._turn_controller.handle_text_event.await_count == 0

    await store.deny(pending.id, reason="cleanup", resolved_by="@user:localhost")
    decision = await task
    assert decision.status == "denied"


@pytest.mark.asyncio
async def test_resolved_approval_edit_preserves_original_event_arguments_shape(tmp_path: Path) -> None:
    """Live approval edits should reuse the exact arguments payload from the original event."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock(return_value=True)
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments={"command": "echo hi", "prompt": "x" * 5000},
    )

    assert pending is not None
    original_event_arguments = json.loads(json.dumps(sender.await_args.args[2]["arguments"]))

    await store.deny(pending.id, reason="Too large", resolved_by="@user:localhost")
    decision = await task

    assert decision.status == "denied"
    edited_payload = editor.await_args.args[2]
    assert edited_payload["arguments"] == original_event_arguments
    assert edited_payload["arguments_truncated"] is True


@pytest.mark.asyncio
async def test_other_bot_can_resolve_tool_approval_reply_instead_of_treating_it_as_chat(tmp_path: Path) -> None:
    """Replies to approval cards should be consumable by any live bot in the room."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    other_bot = _agent_bot(tmp_path, config=config, agent_name="general")
    other_bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    event = _reply_event(event_id="$reply", body="approve")

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(other_bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await other_bot._on_message(room, event)

    assert other_bot._turn_controller.handle_text_event.await_count == 0

    decision = await task
    assert decision.status == "denied"
    assert decision.reason == "approve"


@pytest.mark.asyncio
async def test_other_bot_resolves_tool_approval_reply_when_stale_bot_registry_entry_remains(tmp_path: Path) -> None:
    """Replies should still resolve through a live room bot when the original bot is stale in the registry."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    other_bot = _agent_bot(tmp_path, config=config, agent_name="general")
    stale_bot = MagicMock()
    stale_bot.client = None
    stale_bot.running = False
    other_bot.orchestrator = MagicMock(agent_bots={"code": stale_bot, "general": other_bot})
    other_bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    event = _reply_event(event_id="$reply", body="deny this")

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(other_bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await other_bot._on_message(room, event)

    assert other_bot._turn_controller.handle_text_event.await_count == 0
    decision = await task
    assert decision.status == "denied"
    assert decision.reason == "deny this"


@pytest.mark.asyncio
async def test_requester_can_resolve_approval_when_local_reply_policy_denies(tmp_path: Path) -> None:
    """Approval resolution should still work for the stored requester despite reply-policy changes."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config, agent_name="code")
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    reaction = nio.ReactionEvent.from_dict(
        {
            "type": "m.reaction",
            "event_id": "$reaction",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$approval",
                    "key": "✅",
                },
            },
        },
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=False),
    ):
        await bot._handle_reaction_inner(room, reaction)

    decision = await task
    assert decision.status == "approved"
    assert editor.await_args.args[2]["resolved_by"] == "@user:localhost"


@pytest.mark.asyncio
async def test_any_live_room_bot_can_resolve_pending_tool_call(tmp_path: Path) -> None:
    """Any live bot in the room may resolve the pending approval card."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    _agent_bot(tmp_path, config=config, agent_name="code")
    other_bot = _agent_bot(tmp_path, config=config, agent_name="general")
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    reaction = nio.ReactionEvent.from_dict(
        {
            "type": "m.reaction",
            "event_id": "$reaction",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$approval",
                    "key": "✅",
                },
            },
        },
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(other_bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await other_bot._handle_reaction_inner(room, reaction)

    decision = await task
    assert decision.status == "approved"
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    assert editor.await_args.args[2]["resolved_by"] == "@user:localhost"


@pytest.mark.asyncio
async def test_multiple_live_bots_racing_same_reaction_resolve_once(tmp_path: Path) -> None:
    """Concurrent same-room bot handlers should race safely and produce one resolution edit."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    first_bot = _agent_bot(tmp_path, config=config, agent_name="code")
    second_bot = _agent_bot(tmp_path, config=config, agent_name="general")
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    reaction = nio.ReactionEvent.from_dict(
        {
            "type": "m.reaction",
            "event_id": "$reaction",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$approval",
                    "key": "✅",
                },
            },
        },
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(first_bot._turn_policy), "can_reply_to_sender", return_value=True),
        patch.object(type(second_bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await asyncio.gather(
            first_bot._handle_reaction_inner(room, reaction),
            second_bot._handle_reaction_inner(room, reaction),
        )

    decision = await task
    assert decision.status == "approved"
    assert editor.await_count == 1


@pytest.mark.asyncio
async def test_bot_custom_approval_response_event_resolves_pending_call(tmp_path: Path) -> None:
    """Cinny's custom approval-response event should resolve the pending approval."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    event = _custom_response_event(
        approval_event_id="$approval",
        status="denied",
        reason="Use a safer command",
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await bot._on_unknown_event(room, event)

    decision = await task
    assert decision.status == "denied"
    assert decision.reason == "Use a safer command"


@pytest.mark.asyncio
async def test_bot_custom_approval_response_event_denies_truncated_approval_and_edits_card(
    tmp_path: Path,
) -> None:
    """Approve clicks on truncated approval cards should fail closed through the normal edit contract."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    assert bot.client is not None
    bot.client.room_send = AsyncMock(return_value=nio.RoomSendResponse(event_id="$error", room_id="!room:localhost"))
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments={"command": "echo hi", "prompt": "x" * 5000},
    )

    assert pending is not None
    room = _approval_room()

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        handled = await bot._handle_tool_approval_action(
            room=room,
            sender_id="@user:localhost",
            approval_event_id="$approval",
            action=lambda approval_manager: approval_manager.handle_custom_response(
                approval_event_id="$approval",
                room_id=room.room_id,
                status="approved",
                reason=None,
                resolved_by="@user:localhost",
            ),
        )

    assert handled is True
    decision = await task
    assert decision.status == "denied"
    assert decision.reason == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )
    assert store.list_pending() == []
    editor.assert_awaited_once()
    assert editor.await_args.args[:2] == ("!room:localhost", "$approval")
    assert editor.await_args.args[2]["status"] == "denied"
    assert editor.await_args.args[2]["resolution_reason"] == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )
    bot.client.room_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_truncated_approval_notice_is_sent_once_by_router_bot(tmp_path: Path) -> None:
    """Concurrent bot handlers should emit one truncated-preview notice through the router transport."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    router_bot = _agent_bot(tmp_path, config=config, agent_name="router")
    general_bot = _agent_bot(tmp_path, config=config, agent_name="general")
    router_bot.running = True
    general_bot.running = True
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.agent_bots = {"router": router_bot, "general": general_bot}
    router_bot.orchestrator = orchestrator
    general_bot.orchestrator = orchestrator
    assert router_bot.client is not None
    assert general_bot.client is not None
    router_bot.client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$notice", room_id="!room:localhost"),
    )
    general_bot.client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$notice-2", room_id="!room:localhost"),
    )
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments={"command": "echo hi", "prompt": "x" * 5000},
    )

    assert pending is not None
    room = _approval_room()
    reaction = nio.ReactionEvent.from_dict(
        {
            "type": "m.reaction",
            "event_id": "$reaction",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$approval",
                    "key": "✅",
                },
            },
        },
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(router_bot._turn_policy), "can_reply_to_sender", return_value=True),
        patch.object(type(general_bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await asyncio.gather(
            router_bot._handle_reaction_inner(room, reaction),
            general_bot._handle_reaction_inner(room, reaction),
        )

    assert task.done() is False
    assert store.list_pending() == [pending]
    router_bot.client.room_send.assert_awaited_once()
    general_bot.client.room_send.assert_not_awaited()
    notice_content = router_bot.client.room_send.await_args.kwargs["content"]
    assert notice_content["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$thread",
        "is_falling_back": False,
        "m.in_reply_to": {"event_id": "$approval"},
    }

    await store.deny(pending.id, reason="cleanup", resolved_by="@user:localhost")
    decision = await task
    assert decision.status == "denied"


@pytest.mark.asyncio
async def test_truncated_approval_notice_is_plain_room_message_in_room_mode(tmp_path: Path) -> None:
    """Room-mode truncated approval notices should be plain room messages, not threaded replies."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General",
                    role="Help generally",
                    rooms=["!room:localhost"],
                    thread_mode="room",
                ),
            },
            models={"default": ModelConfig(provider="openai", id="gpt-5.4")},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
            ),
        ),
        runtime_paths,
    )
    router_bot = _agent_bot(tmp_path, config=config, agent_name="router")
    general_bot = _agent_bot(tmp_path, config=config, agent_name="general")
    router_bot.running = True
    general_bot.running = True
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator.agent_bots = {"router": router_bot, "general": general_bot}
    router_bot.orchestrator = orchestrator
    general_bot.orchestrator = orchestrator
    assert router_bot.client is not None
    router_bot.client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$notice", room_id="!room:localhost"),
    )
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments={"command": "echo hi", "prompt": "x" * 5000},
        thread_id=None,
    )

    assert pending is not None
    room = _approval_room()
    reaction = nio.ReactionEvent.from_dict(
        {
            "type": "m.reaction",
            "event_id": "$reaction",
            "sender": "@user:localhost",
            "origin_server_ts": 1,
            "room_id": "!room:localhost",
            "content": {
                "m.relates_to": {
                    "rel_type": "m.annotation",
                    "event_id": "$approval",
                    "key": "✅",
                },
            },
        },
    )

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(router_bot._turn_policy), "can_reply_to_sender", return_value=True),
        patch.object(type(general_bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await asyncio.gather(
            router_bot._handle_reaction_inner(room, reaction),
            general_bot._handle_reaction_inner(room, reaction),
        )

    assert task.done() is False
    assert store.list_pending() == [pending]
    router_bot.client.room_send.assert_awaited_once()
    notice_content = router_bot.client.room_send.await_args.kwargs["content"]
    assert notice_content["msgtype"] == "m.notice"
    assert notice_content["body"] == (
        "Cannot approve: the displayed arguments are truncated. "
        "Ask the agent to retry with a smaller payload, or approve via the script-based approval rule."
    )
    assert "m.relates_to" not in notice_content

    await store.deny(pending.id, reason="cleanup", resolved_by="@user:localhost")
    decision = await task
    assert decision.status == "denied"


@pytest.mark.asyncio
async def test_handle_custom_response_requires_matching_room_and_event_id(tmp_path: Path) -> None:
    """Custom approval responses should anchor to the original approval card in the original room."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value=_sent_approval_event())
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None

    handled_wrong_room = await store.handle_custom_response(
        approval_event_id="$approval",
        room_id="!other:localhost",
        status="denied",
        reason="Wrong room",
        resolved_by="@user:localhost",
    )
    handled_wrong_event = await store.handle_custom_response(
        approval_event_id="$other-approval",
        room_id="!room:localhost",
        status="denied",
        reason="Wrong event",
        resolved_by="@user:localhost",
    )

    assert handled_wrong_room.handled is False
    assert handled_wrong_event.handled is False
    assert task.done() is False

    await store.handle_approval_resolution(
        approval_id=pending.id,
        status="denied",
        reason="cleanup",
        resolved_by="@user:localhost",
    )
    decision = await task
    assert decision.status == "denied"
