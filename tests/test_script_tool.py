"""Tests for the requester-bound background script control toolkit."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths
from mindroom.custom_tools.script import ScriptTools
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
    requested_resource_profile: str | None = None
    calls: builtins.list[str] = field(default_factory=list)
    run_record: ScriptRunRecord = field(default_factory=_run)

    def resource_profiles(self) -> dict[str, object]:
        return {
            "default_profile": "small",
            "profiles": {
                "small": {
                    "requests": {"cpu": "100m", "memory": "256Mi"},
                    "limits": {"cpu": "500m", "memory": "1Gi"},
                },
                "standard": {
                    "requests": {"cpu": "250m", "memory": "512Mi"},
                    "limits": {"cpu": "1", "memory": "2Gi"},
                },
                "large": {
                    "requests": {"cpu": "500m", "memory": "2Gi"},
                    "limits": {"cpu": "2", "memory": "8Gi"},
                },
            },
        }

    async def run(
        self,
        context: ToolRuntimeContext,
        *,
        source: str | None = None,
        path: str | None = None,
        name: str | None = None,
        resource_profile: str | None = None,
        limits: ScriptRunLimits | None = None,
    ) -> ScriptRunRecord:
        del context, source, path, name
        self.limits = limits
        self.requested_resource_profile = resource_profile
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
    ) -> ScriptRunRecord:
        del context, run_id, force
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
    """The registered toolkit exposes exactly the five script controls."""
    toolkit = ScriptTools()

    assert set(toolkit.functions) == {"script_read", "script_write"}
    from mindroom.tool_system.toolkits import adapt_toolkit  # noqa: PLC0415

    offered = {fn.name: fn for fn in adapt_toolkit(toolkit).functions}
    assert (offered["script_read"].offer_background_after, offered["script_read"].offer_output_path) == (False, True)
    assert (offered["script_write"].offer_background_after, offered["script_write"].offer_output_path) == (True, False)


def test_script_tool_discards_blank_allowed_tool_names() -> None:
    """Whitespace-only config entries should not break toolkit construction."""
    toolkit = ScriptTools(allowed_tools=[" calculator ", " ", "calculator", "matrix_message"])

    assert toolkit.limits.allowed_tools == ("calculator", "matrix_message")


def _serving(context: ToolRuntimeContext, manager: _Manager | None) -> ToolRuntimeContext:
    """Return the context of a primary whose script runtime serves tools through *manager*, or not yet."""
    return replace(context, orchestrator=SimpleNamespace(script_runtime=SimpleNamespace(tool_manager=manager)))


@pytest.mark.asyncio
async def test_script_tool_requires_live_room_context() -> None:
    """Detached construction cannot bypass requester and room ownership."""
    payload = json.loads(await ScriptTools().script_write(action="start", source="print('ok')"))

    assert payload == {
        "message": "Background script controls require an active room context.",
        "status": "error",
        "tool": "script",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("has_script_runtime", [False, True])
async def test_script_tool_waits_for_the_runtime_to_serve_tools(
    script_context: ToolRuntimeContext,
    has_script_runtime: bool,
) -> None:
    """Controls answer only while the primary's script runtime serves tools."""
    context = _serving(script_context, None) if has_script_runtime else script_context
    with tool_runtime_context(context):
        payload = json.loads(await ScriptTools().script_read(action="list"))

    assert payload == {
        "message": "Background script runtime is not ready.",
        "status": "error",
        "tool": "script",
    }


@pytest.mark.asyncio
async def test_script_tool_shows_exact_resources_before_launch(
    script_context: ToolRuntimeContext,
) -> None:
    """The model can compare every bounded profile before reserving a worker."""
    with tool_runtime_context(_serving(script_context, _Manager())):
        payload = json.loads(await ScriptTools().script_read(action="resource_profiles"))

    assert payload == {
        **_Manager().resource_profiles(),
        "action": "resource_profiles",
        "status": "ok",
        "tool": "script",
    }


@pytest.mark.asyncio
async def test_script_tool_launch_applies_toolkit_limits_and_reports_only_public_run_fields(
    script_context: ToolRuntimeContext,
) -> None:
    """Toolkit config becomes durable launch limits, and the run payload exposes no capability or worker material."""
    manager = _Manager()
    toolkit = ScriptTools(
        max_concurrent_runs=2,
        max_tool_calls_per_minute=7,
        max_runtime_hours=3,
    )
    with tool_runtime_context(_serving(script_context, manager)):
        payload = json.loads(await toolkit.script_write(action="start", source="print('ok')", name="watcher"))

    assert payload["status"] == "ok"
    assert set(payload["run"]) == {
        "run_id",
        "agent_name",
        "name",
        "state",
        "execution_mode",
        "local_unsafe",
        "resource_profile",
        "resource_requests",
        "resource_limits",
        "created_at",
        "started_at",
        "finished_at",
        "exit_code",
        "error",
        "cancel_requested_at",
        "cancellation_reason",
        "max_tool_calls_per_minute",
        "max_runtime_seconds",
        "grants",
    }
    assert payload["run"]["run_id"] == "script-1"
    assert payload["run"]["execution_mode"] == "worker"
    assert payload["run"]["local_unsafe"] is False
    assert manager.limits == ScriptRunLimits(
        max_concurrent_runs=2,
        max_tool_calls_per_minute=7,
        max_runtime_hours=3,
    )


@pytest.mark.asyncio
async def test_script_tool_selects_and_reports_exact_resource_profile(
    script_context: ToolRuntimeContext,
) -> None:
    """The selected bounded profile and its exact reservation remain visible after launch."""
    run = replace(
        _run(),
        resource_profile="large",
        resource_requests={"cpu": "500m", "memory": "2Gi"},
        resource_limits={"cpu": "2", "memory": "8Gi"},
    )
    manager = _Manager(run_record=run)
    with tool_runtime_context(_serving(script_context, manager)):
        payload = json.loads(
            await ScriptTools().script_write(
                action="start",
                source="print('ok')",
                resource_profile="large",
            ),
        )

    assert manager.requested_resource_profile == "large"
    assert payload["run"]["resource_profile"] == "large"
    assert payload["run"]["resource_requests"] == {"cpu": "500m", "memory": "2Gi"}
    assert payload["run"]["resource_limits"] == {"cpu": "2", "memory": "8Gi"}


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
    with tool_runtime_context(_serving(script_context, manager)):
        payload = json.loads(await ScriptTools().script_read(action="get", run_id=terminal.run_id))

    assert payload["run"]["execution_mode"] == "worker"


@pytest.mark.asyncio
async def test_script_tool_controls_use_bound_manager(script_context: ToolRuntimeContext) -> None:
    """Status, cancellation, and listing stay thin wrappers over the primary manager."""
    manager = _Manager()
    toolkit = ScriptTools()
    with tool_runtime_context(_serving(script_context, manager)):
        status = json.loads(await toolkit.script_read(action="get", run_id="script-1"))
        cancelled = json.loads(await toolkit.script_write(action="cancel", run_id="script-1", force=True))
        listed = json.loads(await toolkit.script_read(action="list", include_finished=False))

    assert status["output"] == "watching"
    assert cancelled["run"]["state"] == "cancelled"
    assert len(listed["runs"]) == 1
    assert manager.calls == ["status", "cancel", "list"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "kwargs", "message_part"),
    [
        ("script_read", {"action": "nope"}, "Unsupported script_read action"),
        ("script_read", {"action": "list", "run_id": "s1"}, "'run_id'"),
        ("script_read", {"action": "get"}, "run_id is required"),
        ("script_read", {"action": "get", "run_id": "   "}, "run_id is required"),
        ("script_read", {"action": "get", "run_id": "s1", "include_finished": False}, "'include_finished'"),
        ("script_read", {"action": "resource_profiles", "include_finished": True}, "'include_finished'"),
        ("script_write", {"action": "nope"}, "Unsupported script_write action"),
        ("script_write", {"action": "start", "run_id": "s1"}, "'run_id'"),
        ("script_write", {"action": "start", "force": False}, "'force'"),
        ("script_write", {"action": "cancel"}, "run_id is required"),
        ("script_write", {"action": "cancel", "run_id": " "}, "run_id is required"),
        ("script_write", {"action": "cancel", "run_id": "s1", "source": "print(1)"}, "'source'"),
    ],
)
async def test_script_tool_validates_action_and_cross_action_params_before_side_effects(
    script_context: ToolRuntimeContext,
    method: str,
    kwargs: dict[str, object],
    message_part: str,
) -> None:
    """Invalid actions, missing required args, and unexpected cross-action params fail before touching the manager."""
    manager = _Manager()
    toolkit = ScriptTools()
    with tool_runtime_context(_serving(script_context, manager)):
        payload = json.loads(await getattr(toolkit, method)(**kwargs))

    assert payload["status"] == "error"
    assert message_part in payload["message"]
    assert manager.calls == []
