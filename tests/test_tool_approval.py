"""Tests for tool approval config, Matrix transport, and resolution."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import TYPE_CHECKING, Literal
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

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
    ToolApprovalScriptError,
    evaluate_tool_approval,
    get_approval_store,
    initialize_approval_store,
    resolve_tool_approval_approver,
    shutdown_approval_store,
    sync_unsynced_approval_event_resolutions,
)
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator
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


def _cinny_accepts_tool_approval_payload(payload: dict[str, object]) -> bool:
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
    arguments = _pick("arguments")
    return isinstance(arguments, dict)


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
    return bot


async def _request_tool_approval(
    runtime_paths: RuntimePaths,
    *,
    sender: AsyncMock | None = None,
    editor: AsyncMock | None = None,
    timeout_seconds: float = 60,
    requester_id: str = "@user:localhost",
    arguments: dict[str, object] | None = None,
) -> tuple[ApprovalManager, asyncio.Task[ApprovalDecision], PendingApproval | None]:
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments=arguments or {"command": "echo hi"},
            agent_name="code",
            transport_agent_name="code",
            room_id="!room:localhost",
            thread_id="$thread",
            requester_id=requester_id,
            approver_user_id=requester_id,
            matched_rule="run_shell_*",
            script_path=None,
            timeout_seconds=timeout_seconds,
        ),
    )
    await asyncio.sleep(0)
    pending = store.list_pending()
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


def _approval_room() -> MagicMock:
    room = MagicMock()
    room.room_id = "!room:localhost"
    room.canonical_alias = None
    return room


def _create_persisted_pending_request(storage_dir: Path, *, event_id: str | None = "$approval-event") -> str:
    request_id = "persisted-pending"
    payload = {
        "id": request_id,
        "tool_name": "run_shell_command",
        "arguments_preview": {"command": "echo hi"},
        "arguments_preview_truncated": False,
        "agent_name": "code",
        "transport_agent_name": "code",
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
) -> str:
    payload = {
        "id": request_id,
        "tool_name": "run_shell_command",
        "arguments_preview": {"command": "echo hi"} if arguments_preview is None else arguments_preview,
        "arguments_preview_truncated": arguments_preview_truncated,
        "agent_name": "code",
        "transport_agent_name": "code",
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
    status: str,
    reason: str | None = None,
    room_id: str = "!room:localhost",
    sender: str = "@user:localhost",
    thread_event_id: str = "$thread",
) -> nio.UnknownEvent:
    content: dict[str, object] = {
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
    with pytest.raises(ValidationError, match="tool_approval.default must be"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"default": "deny_all"},
        )

    with pytest.raises(ValidationError, match="tool_approval.rules\\[0\\].match must not be empty"):
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

    with pytest.raises(ValidationError, match="tool_approval.timeout_days must be a finite number greater than 0"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"timeout_days": 0},
        )


def test_config_rejects_non_finite_tool_approval_timeout_days() -> None:
    """Config validation should reject NaN and infinite approval timeouts."""
    with pytest.raises(ValidationError, match="tool_approval.timeout_days must be a finite number greater than 0"):
        Config(
            **_base_config_kwargs(),
            tool_approval={"timeout_days": "nan"},
        )

    with pytest.raises(
        ValidationError,
        match="tool_approval.rules\\[0\\].timeout_days must be a finite number greater than 0",
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


def test_programmatic_tool_approval_models_reject_invalid_values() -> None:
    """Direct model construction should enforce the same approval validation rules."""
    with pytest.raises(ValidationError, match="tool_approval.timeout_days must be a finite number greater than 0"):
        ToolApprovalConfig(timeout_days=float("nan"))

    with pytest.raises(ValidationError, match="tool_approval.rules\\[\\]\\.match must not be empty"):
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
async def test_request_approval_approves_and_edits_matrix_event(tmp_path: Path) -> None:
    """Approvals should send a pending card, wait, then edit it on approval."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    assert pending.event_id == "$approval"
    assert sender.await_args.args[:3] == ("!room:localhost", "$thread", "code")
    assert sender.await_args.args[3]["msgtype"] == "io.mindroom.tool_approval"
    assert sender.await_args.args[3]["status"] == "pending"

    resolved = await store.approve(pending.id, resolved_by="@user:localhost")
    decision = await task

    assert resolved.status == "approved"
    assert decision.status == "approved"
    assert decision.resolved_by == "@user:localhost"
    assert editor.await_args.args[:3] == ("!room:localhost", "$approval", "code")
    assert editor.await_args.args[3]["status"] == "approved"
    assert editor.await_args.args[3]["thread_id"] == "$thread"
    assert store.list_pending() == []


@pytest.mark.asyncio
async def test_request_approval_sanitizes_arguments_in_matrix_event_and_persistence(tmp_path: Path) -> None:
    """Approval cards and persisted records should only expose bounded sanitized previews."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
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
    event_payload = sender.await_args.args[3]
    event_payload_text = json.dumps(event_payload, sort_keys=True)
    persisted_text = (runtime_paths.storage_root / "approvals" / f"{pending.id}.json").read_text(encoding="utf-8")

    assert "super-secret" not in event_payload_text
    assert "sk-live-secret-token" not in event_payload_text
    assert "super-secret" not in persisted_text
    assert "sk-live-secret-token" not in persisted_text
    assert "***redacted***" in event_payload_text
    assert "***redacted***" in persisted_text
    assert isinstance(event_payload["arguments"], dict)
    assert _cinny_accepts_tool_approval_payload(event_payload)
    assert len(json.dumps(event_payload["arguments"], sort_keys=True)) <= 1200
    assert event_payload["arguments_truncated"] is True

    await store.approve(pending.id, resolved_by="@user:localhost")
    await task


@pytest.mark.asyncio
async def test_request_approval_caps_key_heavy_arguments_in_matrix_event(tmp_path: Path) -> None:
    """Key-heavy argument payloads should still stay within the event preview budget."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    key_heavy_arguments = {f"key_{index:03d}_{'x' * 16}": "v" for index in range(200)}
    store, task, pending = await _request_tool_approval(
        runtime_paths,
        sender=sender,
        editor=editor,
        arguments=key_heavy_arguments,
    )

    assert pending is not None
    event_payload = sender.await_args.args[3]
    assert isinstance(event_payload["arguments"], dict)
    assert len(json.dumps(event_payload["arguments"], sort_keys=True)) <= 1200

    await store.approve(pending.id, resolved_by="@user:localhost")
    await task


@pytest.mark.asyncio
async def test_request_approval_denies_with_reason(tmp_path: Path) -> None:
    """Denials should unblock the waiting tool call and include the denial reason."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
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
    assert editor.await_args.args[3]["denial_reason"] == "Too dangerous"


@pytest.mark.asyncio
async def test_request_approval_resolves_from_different_event_loop(tmp_path: Path) -> None:
    """Approval resolution from another thread and loop should wake the waiter."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
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
                    transport_agent_name="code",
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
    assert editor.await_args.args[3]["status"] == "approved"
    assert store.list_pending() == []


@pytest.mark.asyncio
async def test_request_approval_resolution_is_thread_safe_under_concurrent_resolvers(tmp_path: Path) -> None:
    """Concurrent approval resolutions from different threads should only succeed once."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
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
    assert editor.await_args.args[3]["status"] == decision.status


@pytest.mark.asyncio
async def test_request_approval_times_out_and_edits_card(tmp_path: Path) -> None:
    """Timeouts should expire the request and edit the approval event."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
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
    assert editor.await_args.args[3]["status"] == "expired"
    assert store.list_pending() == []


@pytest.mark.asyncio
async def test_request_approval_uses_absolute_expiry_after_delayed_send(tmp_path: Path) -> None:
    """The approval deadline should be enforced from the advertised expires_at, not from delivery completion."""
    runtime_paths = test_runtime_paths(tmp_path)
    send_started = asyncio.Event()

    async def delayed_sender(*_args: object) -> str:
        send_started.set()
        await asyncio.sleep(0.2)
        return "$approval"

    sender = AsyncMock(side_effect=delayed_sender)
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments={"command": "echo hi"},
            agent_name="code",
            transport_agent_name="code",
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
    assert editor.await_args.args[3]["status"] == "expired"
    with pytest.raises(LookupError, match=pending[0].id):
        await store.approve(pending[0].id, resolved_by="@user:localhost")


@pytest.mark.asyncio
async def test_request_approval_cancellation_marks_request_expired(tmp_path: Path) -> None:
    """Cancelling the waiting tool call should mark the approval as expired."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert editor.await_args.args[3]["status"] == "expired"
    assert editor.await_args.args[3]["resolution_reason"] == "Tool approval request was cancelled."


@pytest.mark.asyncio
async def test_request_approval_cancellation_during_send_expires_persisted_pending_request(tmp_path: Path) -> None:
    """Cancelling while the approval card is still sending should clean up the persisted pending request."""
    runtime_paths = test_runtime_paths(tmp_path)

    async def _blocked_send(*_args: object) -> str:
        await asyncio.sleep(60)
        return "$approval"

    sender = AsyncMock(side_effect=_blocked_send)
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments={"command": "echo hi"},
            agent_name="code",
            transport_agent_name="code",
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
    payload = json.loads(
        (runtime_paths.storage_root / "approvals" / f"{pending[0].id}.json").read_text(encoding="utf-8"),
    )
    assert payload["status"] == "expired"
    assert payload["resolution_reason"] == "Tool approval request was cancelled."
    assert payload["event_id"] is None
    editor.assert_not_awaited()


@pytest.mark.asyncio
async def test_request_approval_requires_matrix_context(tmp_path: Path) -> None:
    """Requests without a Matrix room should fail closed."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(runtime_paths, sender=AsyncMock(return_value="$approval"), editor=AsyncMock())

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        transport_agent_name="code",
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
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)
    task = asyncio.create_task(
        store.request_approval(
            tool_name="run_shell_command",
            arguments={"command": "echo hi"},
            agent_name="code",
            transport_agent_name="code",
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
    assert sender.await_args.args[:3] == ("!room:localhost", None, "code")
    assert sender.await_args.args[3]["thread_id"] is None

    await store.approve(pending[0].id, resolved_by="@user:localhost")
    decision = await task

    assert decision.status == "approved"
    assert editor.await_args.args[3]["thread_id"] is None


@pytest.mark.asyncio
async def test_request_approval_requires_human_requester(tmp_path: Path) -> None:
    """Agent-authored approval requests should fail closed without sending a card."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        transport_agent_name="code",
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
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store = initialize_approval_store(runtime_paths, sender=sender, editor=editor)

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        transport_agent_name="code",
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
        transport_agent_name="code",
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
async def test_request_approval_discards_pending_request_when_matrix_send_returns_none(tmp_path: Path) -> None:
    """Send failures that return no event ID should not leak pending requests in memory."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(runtime_paths, sender=AsyncMock(return_value=None), editor=AsyncMock())

    decision = await store.request_approval(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        transport_agent_name="code",
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


@pytest.mark.asyncio
async def test_request_approval_discards_pending_request_when_matrix_send_raises(tmp_path: Path) -> None:
    """Exceptions during send should not leak pending requests in memory."""
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
        transport_agent_name="code",
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


@pytest.mark.asyncio
async def test_request_approval_persists_request_before_matrix_send(tmp_path: Path) -> None:
    """Approval state should be durable before the Matrix transport is attempted."""
    runtime_paths = test_runtime_paths(tmp_path)
    editor = AsyncMock()

    async def sender(room_id: str, thread_id: str, agent_name: str, content: dict[str, object]) -> str | None:
        del room_id, thread_id, agent_name
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
        transport_agent_name="code",
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
    assert len(persisted_requests) == 1
    persisted_payload = json.loads(persisted_requests[0].read_text(encoding="utf-8"))
    assert persisted_payload["status"] == "expired"
    assert persisted_payload["event_id"] is None


@pytest.mark.asyncio
async def test_handle_approval_resolution_updates_future_and_card(tmp_path: Path) -> None:
    """Direct resolution by approval ID should resolve the pending request exactly once."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
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
async def test_programmatic_approve_requires_original_requester(tmp_path: Path) -> None:
    """Programmatic approval should reject non-requester actors."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None

    with pytest.raises(PermissionError, match="original requester"):
        await store.approve(pending.id, resolved_by="@other:localhost")

    resolved = await store.approve(pending.id, resolved_by="@user:localhost")
    decision = await task

    assert resolved.status == "approved"
    assert decision.status == "approved"
    assert editor.await_args.args[3]["resolved_by"] == "@user:localhost"


@pytest.mark.asyncio
async def test_handle_reaction_approves_by_event_id(tmp_path: Path) -> None:
    """Reaction approval should resolve the pending request by Matrix event ID."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store, task, _pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    handled = await store.handle_reaction(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reaction_key="✅",
        resolved_by="@user:localhost",
        transport_agent_name="code",
    )
    decision = await task

    assert handled is True
    assert decision.status == "approved"


@pytest.mark.asyncio
async def test_handle_reaction_requires_original_requester(tmp_path: Path) -> None:
    """Only the original requester should be able to resolve a pending approval."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None

    handled = await store.handle_reaction(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reaction_key="✅",
        resolved_by="@other:localhost",
        transport_agent_name="code",
    )

    assert handled is False
    assert task.done() is False

    handled = await store.handle_reaction(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reaction_key="✅",
        resolved_by="@user:localhost",
        transport_agent_name="code",
    )
    decision = await task

    assert handled is True
    assert decision.status == "approved"


@pytest.mark.asyncio
async def test_handle_reply_denies_by_event_id(tmp_path: Path) -> None:
    """Reply denial should resolve the pending request by Matrix event ID."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store, task, _pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    handled = await store.handle_reply(
        approval_event_id="$approval",
        room_id="!room:localhost",
        reason="No destructive commands",
        resolved_by="@user:localhost",
        transport_agent_name="code",
    )
    decision = await task

    assert handled is True
    assert decision.status == "denied"
    assert decision.reason == "No destructive commands"


@pytest.mark.asyncio
async def test_shutdown_expires_pending_requests(tmp_path: Path) -> None:
    """Shutdown should expire any live approvals and unblock waiting tasks."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    _store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    await shutdown_approval_store()
    decision = await task

    assert get_approval_store() is None
    assert decision.status == "expired"
    assert decision.reason == "MindRoom shut down before approval completed."
    assert editor.await_args.args[3]["status"] == "expired"


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
    """Startup should fail closed for persisted approvals that never recorded an event ID."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_pending_request(runtime_paths.storage_root / "approvals", event_id=None)

    store = initialize_approval_store(runtime_paths)

    assert store.list_pending() == []
    payload = json.loads((runtime_paths.storage_root / "approvals" / f"{request_id}.json").read_text(encoding="utf-8"))
    assert payload["status"] == "expired"
    assert payload["resolution_reason"] == "MindRoom restarted before approval request could be delivered to Matrix."
    assert payload["event_id"] is None


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
    assert editor.await_args.args[:3] == ("!room:localhost", "$approval-event", "code")
    assert editor.await_args.args[3]["status"] == "expired"
    assert editor.await_args.args[3]["thread_id"] == "$thread"
    payload = json.loads((runtime_paths.storage_root / "approvals" / f"{request_id}.json").read_text(encoding="utf-8"))
    assert payload["resolution_synced_at"] is not None


@pytest.mark.asyncio
async def test_sync_unsynced_approval_event_resolutions_keep_original_argument_shape_for_cinny(
    tmp_path: Path,
) -> None:
    """Replay edits should rely on the original approval payload for unchanged argument fields."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_resolved_request(
        runtime_paths.storage_root / "approvals",
        arguments_preview='{"command":"echo hi","prompt":"[truncated]"}',
        arguments_preview_truncated=True,
    )
    editor = AsyncMock(return_value=True)
    initialize_approval_store(runtime_paths, editor=editor)

    synced_requests = await sync_unsynced_approval_event_resolutions()

    assert [request.id for request in synced_requests] == [request_id]
    edited_payload = editor.await_args.args[3]
    assert "arguments" not in edited_payload
    render_payload = {
        "approval_id": request_id,
        "tool_name": "run_shell_command",
        "arguments": {"command": "echo hi", "prompt": "[truncated]"},
        "agent_name": "code",
        "status": "pending",
        "requested_at": "2026-04-09T12:00:00+00:00",
        "expires_at": "2026-04-10T12:00:00+00:00",
        "thread_id": "$thread",
        "msgtype": "io.mindroom.tool_approval",
        "body": "🔒 Approval required: run_shell_command",
        "m.new_content": {key: value for key, value in edited_payload.items() if key != "thread_id"},
    }
    assert _cinny_accepts_tool_approval_payload(render_payload)


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
async def test_orchestrator_send_approval_event_requires_runtime_loop(tmp_path: Path) -> None:
    """Approval transport should fail fast without a captured runtime loop."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    client = MagicMock()
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
            "code",
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
    client = MagicMock()
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$approval-event", room_id="!room:localhost"),
    )
    bot = MagicMock()
    bot.client = client
    bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest-thread-event")
    orchestrator.agent_bots = {"code": bot}

    event_id = await orchestrator._send_approval_event(
        "!room:localhost",
        "$thread",
        "code",
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

    assert event_id == "$approval-event"
    client.room_send.assert_awaited_once_with(
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


@pytest.mark.asyncio
async def test_orchestrator_send_approval_event_supports_room_mode_without_thread_id(tmp_path: Path) -> None:
    """Room-mode approval cards should send without a thread relation."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()
    client = MagicMock()
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$approval-event", room_id="!room:localhost"),
    )
    bot = MagicMock()
    bot.client = client
    orchestrator.agent_bots = {"code": bot}

    event_id = await orchestrator._send_approval_event(
        "!room:localhost",
        None,
        "code",
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

    assert event_id == "$approval-event"
    client.room_send.assert_awaited_once_with(
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
    client = MagicMock()
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$edit-event", room_id="!room:localhost"),
    )
    bot = MagicMock()
    bot.client = client
    bot._conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest-thread-event")
    orchestrator.agent_bots = {"code": bot}

    edited = await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        "code",
        {
            "status": "denied",
            "msgtype": "io.mindroom.tool_approval",
            "body": "Denied: run_shell_command",
            "thread_id": "$thread",
            "resolved_at": "2026-04-12T00:00:00+00:00",
            "resolved_by": "@bas:localhost",
            "denial_reason": "Too dangerous",
            "resolution_reason": "Too dangerous",
        },
    )

    assert edited is True
    new_content = {
        "status": "denied",
        "msgtype": "io.mindroom.tool_approval",
        "body": "Denied: run_shell_command",
        "resolved_at": "2026-04-12T00:00:00+00:00",
        "resolved_by": "@bas:localhost",
        "denial_reason": "Too dangerous",
        "resolution_reason": "Too dangerous",
        "m.relates_to": {
            "rel_type": "m.thread",
            "event_id": "$thread",
            "is_falling_back": True,
            "m.in_reply_to": {"event_id": "$latest-thread-event"},
        },
    }
    client.room_send.assert_awaited_once_with(
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
    client = MagicMock()
    client.room_send = AsyncMock(
        return_value=nio.RoomSendResponse(event_id="$edit-event", room_id="!room:localhost"),
    )
    bot = MagicMock()
    bot.client = client
    orchestrator.agent_bots = {"code": bot}

    edited = await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        "code",
        {
            "status": "approved",
            "msgtype": "io.mindroom.tool_approval",
            "body": "Approved: run_shell_command",
            "thread_id": None,
            "resolved_at": "2026-04-12T00:00:00+00:00",
            "resolved_by": "@bas:localhost",
        },
    )

    assert edited is True
    new_content = {
        "status": "approved",
        "msgtype": "io.mindroom.tool_approval",
        "body": "Approved: run_shell_command",
        "resolved_at": "2026-04-12T00:00:00+00:00",
        "resolved_by": "@bas:localhost",
    }
    client.room_send.assert_awaited_once_with(
        room_id="!room:localhost",
        message_type="io.mindroom.tool_approval",
        content={
            **new_content,
            "m.new_content": new_content,
            "m.relates_to": {"rel_type": "m.replace", "event_id": "$approval-event"},
        },
    )


@pytest.mark.asyncio
async def test_orchestrator_edit_approval_event_returns_false_without_transport_bot(tmp_path: Path) -> None:
    """The orchestrator helper should report a no-op when the transport bot is unavailable."""
    runtime_paths = test_runtime_paths(tmp_path)
    orchestrator = MultiAgentOrchestrator(runtime_paths=runtime_paths)
    orchestrator._capture_runtime_loop()

    edited = await orchestrator._edit_approval_event(
        "!room:localhost",
        "$approval-event",
        "code",
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
    sender = AsyncMock(return_value="$approval")
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
    assert editor.await_args.args[3]["status"] == "approved"


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
    sender = AsyncMock(return_value="$approval")
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
    sender = AsyncMock(return_value="$approval")
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
    sender = AsyncMock(return_value="$approval")
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
    assert editor.await_args.args[3]["resolution_reason"] == "Do not run this"


@pytest.mark.asyncio
async def test_bot_reply_from_wrong_user_falls_through_to_normal_handling(tmp_path: Path) -> None:
    """Replies from other users should not be swallowed when they cannot resolve the approval."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    bot = _agent_bot(tmp_path, config=config)
    bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value="$approval")
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
    assert bot._turn_controller.handle_text_event.await_count == 1

    await store.deny(pending.id, reason="cleanup", resolved_by="@user:localhost")
    decision = await task
    assert decision.status == "denied"


@pytest.mark.asyncio
async def test_other_bot_skips_tool_approval_reply_instead_of_treating_it_as_chat(tmp_path: Path) -> None:
    """Replies to approval cards should not reach non-transport bots as normal chat."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    other_bot = _agent_bot(tmp_path, config=config, agent_name="general")
    other_bot._turn_controller.handle_text_event = AsyncMock()
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None
    room = _approval_room()
    event = _reply_event(event_id="$reply", body="approve")

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(other_bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await other_bot._on_message(room, event)

    assert task.done() is False
    assert other_bot._turn_controller.handle_text_event.await_count == 0

    await store.deny(pending.id, reason="cleanup", resolved_by="@user:localhost")
    decision = await task
    assert decision.status == "denied"


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
    sender = AsyncMock(return_value="$approval")
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
    assert editor.await_args.args[3]["resolved_by"] == "@user:localhost"


@pytest.mark.asyncio
async def test_only_transport_bot_can_resolve_pending_tool_call(tmp_path: Path) -> None:
    """Only the transport bot that sent the card may resolve it in a multi-bot room."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _runtime_bound_config(
        runtime_paths,
        tool_approval=ToolApprovalConfig(
            rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")],
        ),
    )
    transport_bot = _agent_bot(tmp_path, config=config, agent_name="code")
    other_bot = _agent_bot(tmp_path, config=config, agent_name="general")
    sender = AsyncMock(return_value="$approval")
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

    assert task.done() is False

    with (
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(type(transport_bot._turn_policy), "can_reply_to_sender", return_value=True),
    ):
        await transport_bot._handle_reaction_inner(room, reaction)

    decision = await task
    assert decision.status == "approved"
    assert editor.await_args.args[:3] == ("!room:localhost", "$approval", "code")
    assert editor.await_args.args[3]["resolved_by"] == "@user:localhost"


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
    sender = AsyncMock(return_value="$approval")
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
async def test_handle_custom_response_requires_matching_room_and_event_id(tmp_path: Path) -> None:
    """Custom approval responses should anchor to the original approval card in the original room."""
    runtime_paths = test_runtime_paths(tmp_path)
    sender = AsyncMock(return_value="$approval")
    editor = AsyncMock()
    store, task, pending = await _request_tool_approval(runtime_paths, sender=sender, editor=editor)

    assert pending is not None

    handled_wrong_room = await store.handle_custom_response(
        approval_event_id="$approval",
        room_id="!other:localhost",
        status="denied",
        reason="Wrong room",
        resolved_by="@user:localhost",
        transport_agent_name="code",
    )
    handled_wrong_event = await store.handle_custom_response(
        approval_event_id="$other-approval",
        room_id="!room:localhost",
        status="denied",
        reason="Wrong event",
        resolved_by="@user:localhost",
        transport_agent_name="code",
    )

    assert handled_wrong_room is False
    assert handled_wrong_event is False
    assert task.done() is False

    await store.handle_approval_resolution(
        approval_id=pending.id,
        status="denied",
        reason="cleanup",
        resolved_by="@user:localhost",
    )
    decision = await task
    assert decision.status == "denied"
