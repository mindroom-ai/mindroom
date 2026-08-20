"""Tests for the requester-bound background script control toolkit."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.custom_tools.script import ScriptTools, bind_script_run_manager
from mindroom.message_target import MessageTarget
from mindroom.script_runs.manager import ScriptRunLimits, ScriptRunStatus
from mindroom.script_runs.models import ScriptRunRecord, ScriptRunState
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_relation_lookup

if TYPE_CHECKING:
    import builtins
    from pathlib import Path

    from mindroom.tool_system.runtime_context import ToolRuntimeContext


@pytest.fixture
def script_context(tmp_path: Path) -> ToolRuntimeContext:
    """Provide one ordinary agent-owned room context."""
    runtime_paths = RuntimePaths(
        config_path=tmp_path / "config.yaml",
        config_dir=tmp_path,
        env_path=tmp_path / ".env",
        storage_root=tmp_path / "storage",
        control_state_root=tmp_path / "control",
    )
    return make_test_tool_runtime_context(
        agent_name="watcher",
        requester_id="@alice:example.test",
        target=MessageTarget.resolve(
            room_id="!room:example.test",
            thread_id="$thread:example.test",
            reply_to_event_id=None,
        ),
        config=Config(agents={"watcher": {"display_name": "Watcher"}}, defaults={"tools": []}),
        client=SimpleNamespace(),
        runtime_paths=runtime_paths,
        storage_path=runtime_paths.storage_root,
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )


def _run(*, state: ScriptRunState = ScriptRunState.RUNNING) -> ScriptRunRecord:
    return ScriptRunRecord(
        run_id="script-1",
        agent_name="watcher",
        owner_user_id="@alice:example.test",
        room_id="!room:example.test",
        source_digest="digest",
        grants=(),
        token_hash="must-not-leak",  # noqa: S106
        execution_identity={"requester_id": "must-not-leak"},
        worker_key="v1:default:user_agent:@alice:example.test:watcher",
        worker_id="worker-private",
        snapshot_locator="workers/private/.mindroom/script-runs/script-1",
        state=state,
    )


@dataclass
class _Manager:
    limits: ScriptRunLimits | None = None
    calls: builtins.list[str] = field(default_factory=list)
    run_record: ScriptRunRecord = field(default_factory=_run)

    async def run(
        self,
        context: ToolRuntimeContext,
        *,
        source: str | None = None,
        path: str | None = None,
        name: str | None = None,
        limits: ScriptRunLimits | None = None,
    ) -> ScriptRunRecord:
        del context, source, path, name
        self.limits = limits
        self.calls.append("run")
        return self.run_record

    async def status(self, context: ToolRuntimeContext, *, run_id: str) -> ScriptRunStatus:
        del context, run_id
        self.calls.append("status")
        return ScriptRunStatus(run=self.run_record, output="watching")

    async def cancel(
        self,
        context: ToolRuntimeContext,
        *,
        run_id: str,
        force: bool = False,
        reason: str = "",
    ) -> ScriptRunRecord:
        del context, run_id, force, reason
        self.calls.append("cancel")
        return _run(state=ScriptRunState.CANCELLED)

    async def list(
        self,
        context: ToolRuntimeContext,
        *,
        include_finished: bool = True,
    ) -> builtins.list[ScriptRunRecord]:
        del context, include_finished
        self.calls.append("list")
        return [_run()]


def test_script_tool_interface_exists() -> None:
    """The registered toolkit exposes exactly the four script controls."""
    toolkit = ScriptTools()

    assert set(toolkit.async_functions) == {
        "cancel_script",
        "list_scripts",
        "run_script",
        "status_script",
    }


def test_script_tool_discards_blank_allowed_tool_names() -> None:
    """Whitespace-only config entries should not break toolkit construction."""
    toolkit = ScriptTools(allowed_tools=[" calculator ", " ", "calculator", "matrix_message"])

    assert toolkit.limits.allowed_tools == ("calculator", "matrix_message")


@pytest.mark.asyncio
async def test_script_tool_requires_live_room_context() -> None:
    """Detached construction cannot bypass requester and room ownership."""
    bind_script_run_manager(_Manager())
    try:
        payload = json.loads(await ScriptTools().run_script(source="print('ok')"))
    finally:
        bind_script_run_manager(None)

    assert payload == {
        "message": "Background script controls require an active room context.",
        "status": "error",
        "tool": "script",
    }


@pytest.mark.asyncio
async def test_script_tool_public_run_uses_derived_execution_mode_without_redundant_fields(
    script_context: ToolRuntimeContext,
) -> None:
    """Toolkit config becomes durable launch limits without exposing capability material."""
    manager = _Manager()
    bind_script_run_manager(manager)
    toolkit = ScriptTools(
        max_concurrent_runs=2,
        max_tool_calls_per_minute=7,
        max_runtime_hours=3,
    )
    try:
        with tool_runtime_context(script_context):
            payload = json.loads(await toolkit.run_script(source="print('ok')", name="watcher"))
    finally:
        bind_script_run_manager(None)

    assert payload["status"] == "ok"
    assert payload["run"]["run_id"] == "script-1"
    assert "token_hash" not in payload["run"]
    assert "execution_identity" not in payload["run"]
    assert "worker_key" not in payload["run"]
    assert "worker_id" not in payload["run"]
    assert "worker_backend_generation" not in payload["run"]
    assert "supervisor_handle" not in payload["run"]
    assert "snapshot_locator" not in payload["run"]
    assert payload["run"]["execution_mode"] == "worker"
    assert payload["run"]["local_unsafe"] is False
    assert "entity_kind" not in payload["run"]
    assert "call_count" not in payload["run"]
    assert manager.limits == ScriptRunLimits(
        max_concurrent_runs=2,
        max_tool_calls_per_minute=7,
        max_runtime_hours=3,
    )


@pytest.mark.asyncio
async def test_script_tool_keeps_worker_mode_after_terminal_cleanup(
    script_context: ToolRuntimeContext,
) -> None:
    """Cleared worker ownership must not relabel a completed worker run as unsafe local."""
    terminal = replace(
        _run(state=ScriptRunState.EXITED),
        worker_key=None,
        worker_id=None,
        snapshot_locator=None,
        finished_at=_run().created_at,
    )
    manager = _Manager(run_record=terminal)
    bind_script_run_manager(manager)
    try:
        with tool_runtime_context(script_context):
            payload = json.loads(await ScriptTools().status_script(run_id=terminal.run_id))
    finally:
        bind_script_run_manager(None)

    assert payload["run"]["execution_mode"] == "worker"


@pytest.mark.asyncio
async def test_script_tool_controls_use_bound_manager(script_context: ToolRuntimeContext) -> None:
    """Status, cancellation, and listing stay thin wrappers over the primary manager."""
    manager = _Manager()
    bind_script_run_manager(manager)
    toolkit = ScriptTools()
    try:
        with tool_runtime_context(script_context):
            status = json.loads(await toolkit.status_script(run_id="script-1"))
            cancelled = json.loads(await toolkit.cancel_script(run_id="script-1", force=True))
            listed = json.loads(await toolkit.list_scripts(include_finished=False))
    finally:
        bind_script_run_manager(None)

    assert status["output"] == "watching"
    assert cancelled["run"]["state"] == "cancelled"
    assert len(listed["runs"]) == 1
    assert manager.calls == ["status", "cancel", "list"]
