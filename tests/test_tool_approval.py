"""Tests for tool-call approval config, persistence, and resolution."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.tool_approval import (
    ToolApprovalScriptError,
    evaluate_tool_approval,
    get_approval_store,
    initialize_approval_store,
    shutdown_approval_store,
)
from tests.conftest import bind_runtime_paths, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


@pytest.fixture(autouse=True)
def reset_approval_store() -> Generator[None, None, None]:
    """Keep the module-level approval store isolated per test."""
    asyncio.run(shutdown_approval_store())
    yield
    asyncio.run(shutdown_approval_store())


def _base_config_kwargs() -> dict[str, object]:
    return {
        "agents": {
            "code": AgentConfig(
                display_name="Code",
                role="Help with coding",
                rooms=[],
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
    raw_config = Config(
        **_base_config_kwargs(),
        tool_approval=tool_approval or ToolApprovalConfig(),
    )
    return bind_runtime_paths(raw_config, runtime_paths)


def _create_persisted_pending_request(storage_dir: Path) -> str:
    request_id = "persisted-pending"
    payload = {
        "id": request_id,
        "status": "pending",
        "tool_name": "run_shell_command",
        "arguments": {"command": "echo hi"},
        "agent_name": "code",
        "room_id": "!room:localhost",
        "thread_id": "$thread",
        "requester_id": "@user:localhost",
        "session_id": "session-1",
        "channel": "matrix",
        "tenant_id": "tenant-1",
        "account_id": "account-1",
        "matched_rule": "run_shell_*",
        "script_path": None,
        "created_at": "2026-04-09T12:00:00+00:00",
        "expires_at": "2026-04-10T12:00:00+00:00",
        "resolution_reason": None,
        "resolved_at": None,
        "resolved_by": None,
    }
    request_path = storage_dir / f"{request_id}.json"
    request_path.parent.mkdir(parents=True, exist_ok=True)
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_id


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
        ApprovalRuleConfig(
            match="run_*",
            action="require_approval",
            script="approve.py",
        )


@pytest.mark.asyncio
async def test_evaluate_tool_approval_matches_rules_in_order(tmp_path: Path) -> None:
    """The first matching rule should win and return its timeout."""
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
            rules=[
                ApprovalRuleConfig(
                    match="run_shell_command",
                    script="approval_scripts/shell_review.py",
                ),
            ],
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
            rules=[
                ApprovalRuleConfig(match="run_shell_command", script="approval_scripts/broken.py"),
            ],
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
async def test_create_request_persists_one_json_file(tmp_path: Path) -> None:
    """Creating a request should persist the API-visible record to disk."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(runtime_paths)

    request = await store.create_request(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread",
        requester_id="@user:localhost",
        session_id="session-1",
        channel="matrix",
        tenant_id="tenant-1",
        account_id="account-1",
        matched_rule="run_shell_*",
        script_path=None,
        timeout_seconds=60,
    )

    persisted_path = runtime_paths.storage_root / "approvals" / f"{request.id}.json"
    payload = json.loads(persisted_path.read_text(encoding="utf-8"))

    assert payload["id"] == request.id
    assert payload["status"] == "pending"
    assert payload["arguments"] == {"command": "echo hi"}
    assert payload["matched_rule"] == "run_shell_*"


def test_initialize_approval_store_expires_persisted_pending_requests(tmp_path: Path) -> None:
    """Startup should expire any persisted pending approval requests."""
    runtime_paths = test_runtime_paths(tmp_path)
    request_id = _create_persisted_pending_request(runtime_paths.storage_root / "approvals")

    store = initialize_approval_store(runtime_paths)
    request = store.get_request(request_id)

    assert request is not None
    assert request.status == "expired"
    assert request.resolution_reason == "MindRoom restarted before approval completed."
    payload = json.loads((runtime_paths.storage_root / "approvals" / f"{request_id}.json").read_text(encoding="utf-8"))
    assert payload["status"] == "expired"
    assert payload["resolution_reason"] == "MindRoom restarted before approval completed."


@pytest.mark.asyncio
async def test_shutdown_approval_store_expires_pending_requests(tmp_path: Path) -> None:
    """Shutdown should expire all live pending requests and clear the store."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(runtime_paths)
    request = await store.create_request(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id=None,
        thread_id=None,
        requester_id=None,
        session_id=None,
        channel="matrix",
        tenant_id=None,
        account_id=None,
        matched_rule="run_shell_command",
        script_path=None,
        timeout_seconds=60,
    )

    await shutdown_approval_store()

    assert get_approval_store() is None
    payload = json.loads((runtime_paths.storage_root / "approvals" / f"{request.id}.json").read_text(encoding="utf-8"))
    assert payload["status"] == "expired"
    assert payload["resolution_reason"] == "MindRoom shut down before approval completed."


@pytest.mark.asyncio
async def test_initialize_approval_store_expires_pending_requests_before_rebinding(tmp_path: Path) -> None:
    """Rebinding the module-level store should expire the old store's pending requests."""
    first_runtime_paths = test_runtime_paths(tmp_path / "runtime-a")
    first_store = initialize_approval_store(first_runtime_paths)
    request = await first_store.create_request(
        tool_name="run_shell_command",
        arguments={"command": "echo hi"},
        agent_name="code",
        room_id=None,
        thread_id=None,
        requester_id=None,
        session_id=None,
        channel="matrix",
        tenant_id=None,
        account_id=None,
        matched_rule="run_shell_command",
        script_path=None,
        timeout_seconds=60,
    )

    second_runtime_paths = test_runtime_paths(tmp_path / "runtime-b")
    second_store = initialize_approval_store(second_runtime_paths)
    await asyncio.sleep(0)

    assert second_store is not first_store
    assert get_approval_store() is second_store
    assert second_store.list_pending() == []

    expired_request = first_store.get_request(request.id)
    assert expired_request is not None
    assert expired_request.status == "expired"
    assert expired_request.resolution_reason == "MindRoom reinitialized before approval completed."
    assert first_store.list_pending() == []
    assert request._future is not None
    assert request._future.done()
    assert request._future.result() == "expired"

    payload = json.loads(
        (first_runtime_paths.storage_root / "approvals" / f"{request.id}.json").read_text(encoding="utf-8"),
    )
    assert payload["status"] == "expired"
    assert payload["resolution_reason"] == "MindRoom reinitialized before approval completed."


def test_cross_thread_resolution_wakes_waiter(tmp_path: Path) -> None:
    """Resolving from another loop should wake the waiting future via call_soon_threadsafe()."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(runtime_paths)
    request_id: dict[str, str] = {}
    result_holder: dict[str, str] = {}
    created = threading.Event()

    async def _wait_for_resolution() -> None:
        request = await store.create_request(
            tool_name="run_shell_command",
            arguments={"command": "echo hi"},
            agent_name="code",
            room_id=None,
            thread_id=None,
            requester_id=None,
            session_id=None,
            channel="matrix",
            tenant_id=None,
            account_id=None,
            matched_rule="run_shell_command",
            script_path=None,
            timeout_seconds=60,
        )
        request_id["value"] = request.id
        created.set()
        assert request._future is not None
        result_holder["status"] = await request._future

    thread = threading.Thread(target=lambda: asyncio.run(_wait_for_resolution()))
    thread.start()
    assert created.wait(timeout=5)

    asyncio.run(store.approve(request_id["value"], resolved_by="dashboard-user"))

    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result_holder["status"] == "approved"


def test_approval_store_is_thread_safe_under_concurrent_access(tmp_path: Path) -> None:
    """Concurrent request creation and reads should not race."""
    runtime_paths = test_runtime_paths(tmp_path)
    store = initialize_approval_store(runtime_paths)
    created_count = 200
    start_barrier = threading.Barrier(2)
    stop = threading.Event()
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _record_error(exc: BaseException) -> None:
        with errors_lock:
            errors.append(exc)
        stop.set()

    async def _create_requests() -> None:
        try:
            for index in range(created_count):
                await store.create_request(
                    tool_name="run_shell_command",
                    arguments={"command": f"echo hi {index}"},
                    agent_name="code",
                    room_id=None,
                    thread_id=None,
                    requester_id=None,
                    session_id=None,
                    channel="matrix",
                    tenant_id=None,
                    account_id=None,
                    matched_rule="run_shell_command",
                    script_path=None,
                    timeout_seconds=60,
                )
                await asyncio.sleep(0)
        finally:
            stop.set()

    def _creator_thread() -> None:
        try:
            start_barrier.wait()
            asyncio.run(_create_requests())
        except BaseException as exc:
            _record_error(exc)

    def _reader_thread() -> None:
        try:
            start_barrier.wait()
            while not stop.is_set():
                store.list_pending()
        except BaseException as exc:
            _record_error(exc)

    creator_thread = threading.Thread(target=_creator_thread)
    reader_thread = threading.Thread(target=_reader_thread)
    creator_thread.start()
    reader_thread.start()

    creator_thread.join(timeout=10)
    stop.set()
    reader_thread.join(timeout=10)

    assert not creator_thread.is_alive()
    assert not reader_thread.is_alive()
    assert errors == []
    assert len(store.list_pending()) == created_count
