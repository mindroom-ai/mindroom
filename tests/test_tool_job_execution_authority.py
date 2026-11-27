"""Execution policies stay isolated between managed runtime roots and report unavailability cleanly."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from structlog.testing import capture_logs

from mindroom.config.access import ResponderAccessConfig
from mindroom.config.models import ToolConfigEntry
from mindroom.config.plugin import PluginEntryConfig
from mindroom.tool_jobs.authorization import authority_snapshot
from mindroom.tool_jobs.execution_authority import (
    JobCall,
    JobTool,
    authorized_tool_call,
    check_current_execution_authority,
)
from mindroom.tool_jobs.instances import pin_background_tool_jobs, release_background_tool_jobs
from mindroom.tool_jobs.runtime import JobAccessError, register_background_runtime
from mindroom.tool_system import plugins as plugins_module
from mindroom.tool_system.invocation import ToolCallContext
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import ConfigPlugins, config_plugins, prepare_plugins, publish_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import publish_test_plugins, test_runtime_paths
from tests.delegation_helpers import _delegate_runtime_context
from tests.tool_job_helpers import (
    ResolvingMembership,
    completed_delegation_job,
    job_tool,
    managed_team_config,
    team_coordinator,
    tool_job_runtime,
    toolkit_job_tools,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.orchestration.tool_job_runtime import ToolJobRuntimeCoordinator
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity

pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

_GENERIC_REFUSAL = "Tool execution is no longer authorized for this caller."
_PLUGIN_NOT_LOADED_REFUSAL = "The plugin that supplied this tool for this turn did not load in the current runtime."
_RUNTIME_UNAVAILABLE_REFUSAL = "This tool is currently unavailable in the current runtime."


def _noop() -> None:
    """Do nothing."""


def _write_plugin(
    plugin_root: Path,
    *,
    name: str = "demo",
    tool_name: str = "demo_tool",
    api_version: str = "1.0",
    suffix: str = "",
) -> Path:
    plugin_root.mkdir(parents=True, exist_ok=True)
    manifest = {"name": name, "mindroom_plugin_api": api_version, "tools_module": "tools.py", "skills": []}
    (plugin_root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    source = (
        "from mindroom.plugin_api import ToolCategory, ToolFileAccess, Toolkit, register_tool_with_metadata, tool\n"
        f"class DemoTools(Toolkit):\n"
        f"    def __init__(self) -> None:\n"
        f"        super().__init__(name={tool_name!r})\n"
        f"    @tool\n"
        f"    def ping(self, secret: str = '') -> str:\n"
        f"        return secret or 'pong'\n"
        f"@register_tool_with_metadata(name={tool_name!r}, file_access=ToolFileAccess.NONE, display_name='Demo', description='Demo', category=ToolCategory.DEVELOPMENT)\n"
        f"def demo_tools():\n"
        f"    return DemoTools\n"
        f"{suffix}"
    )
    (plugin_root / "tools.py").write_text(source, encoding="utf-8")
    return plugin_root


def _reload_plugins(config: Config, paths: RuntimePaths) -> ConfigPlugins:
    publish_plugins(prepare_plugins(config, paths))
    return config_plugins(config, paths)


def _setup_plugin_turn(
    tmp_path: Path,
    *,
    extra_plugins: tuple[PluginEntryConfig, ...] = (),
) -> tuple[list[Config], ToolJobRuntimeCoordinator, ToolExecutionIdentity, JobTool, ToolRuntimeContext, Path]:
    plugin_root = _write_plugin(tmp_path / "plugins" / "demo")
    config = managed_team_config(tmp_path)
    config.plugins = [PluginEntryConfig(path="./plugins/demo"), *extra_plugins]
    config.agents["lead"].tools = ["demo_tool"]
    config_box = [config]
    coordinator = team_coordinator(tmp_path, config)
    coordinator.config_provider = lambda: config_box[0]
    paths = coordinator.runtime_paths
    turn_plugins = publish_test_plugins(config, paths)
    toolkit = get_tool_by_name("demo_tool", paths, worker_target=None, runs_in_worker=False, tools=turn_plugins.tools)
    tool = toolkit_job_tools(toolkit, authored_name="demo_tool", snapshot=authority_snapshot(config, "lead"))["ping"]
    owner = replace(completed_delegation_job().owner, transport_agent_name="lead")
    context = replace(
        _delegate_runtime_context(config, paths, execution_identity=owner),
        agent_name="lead",
        transport_agent_name="lead",
        plugins=turn_plugins,
    )
    return config_box, coordinator, owner, tool, context, plugin_root


def _assert_refused(
    coordinator: ToolJobRuntimeCoordinator,
    owner: ToolExecutionIdentity,
    tool: JobTool,
    *,
    expected_message: str,
    expected_reason: str,
    arguments: dict[str, Any] | None = None,
    forbidden: tuple[str, ...] = (),
) -> None:
    with capture_logs() as logs, pytest.raises(JobAccessError) as exc_info:
        coordinator._authorize_execution(owner, tool, arguments)
    assert str(exc_info.value) == expected_message
    refusals = [entry for entry in logs if entry.get("event") == "Refused tool execution"]
    assert len(refusals) == 1
    refusal = refusals[0]
    assert refusal["log_level"] == "warning"
    assert refusal["agent_name"] == owner.agent_name
    assert refusal["tool_name"] == tool.function.name
    assert refusal["toolkit_name"] == tool.toolkit_name
    assert refusal["constructed_toolkit"] == tool.authority["construction"]["name"]
    assert refusal["reason"] == expected_reason
    assert "arguments" not in refusal
    for secret in forbidden:
        assert secret not in str(exc_info.value)
        assert secret not in str(refusal)


def test_unbound_tool_authority_does_not_access_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An ordinary call needs no managed runtime lookup or storage filesystem access."""
    paths = test_runtime_paths(tmp_path)
    context = _delegate_runtime_context(managed_team_config(tmp_path), paths)

    def unavailable(_path: Path, *_args: object, **_kwargs: object) -> Path:
        msg = "Unbound calls must not resolve job storage"
        raise AssertionError(msg)

    with tool_runtime_context(context), monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", unavailable)
        check_current_execution_authority()


@pytest.mark.asyncio
async def test_execution_authorizers_are_scoped_to_runtime(tmp_path: Path) -> None:
    """Publishing or releasing a second runtime cannot bypass the first runtime's policy."""
    first = test_runtime_paths(tmp_path / "first")
    second = test_runtime_paths(tmp_path / "second")
    owner = completed_delegation_job().owner
    config = managed_team_config(tmp_path)
    context = _delegate_runtime_context(config, first, execution_identity=owner)
    call = JobCall(
        tool=job_tool(_noop),
        arguments={},
        context=ToolCallContext(run_id="run", session_id="session", user_id=None),
        team=False,
    )

    def denied(*_args: object) -> None:
        message = "Revoked"
        raise JobAccessError(message)

    first_runtime = await tool_job_runtime(first.storage_root, authorize_execution=denied)
    second_runtime = await tool_job_runtime(second.storage_root)
    pin_background_tool_jobs(config, first)
    register_background_runtime(first, first_runtime)
    second_instance = pin_background_tool_jobs(config, second)
    register_background_runtime(second, second_runtime)
    try:
        with tool_runtime_context(context), authorized_tool_call(owner, call):
            with pytest.raises(JobAccessError, match="Revoked"):
                check_current_execution_authority()
            release_background_tool_jobs(second, second_instance)
            with pytest.raises(JobAccessError, match="Revoked"):
                check_current_execution_authority()
        with (
            tool_runtime_context(replace(context, runtime_paths=second)),
            authorized_tool_call(owner, call),
        ):
            check_current_execution_authority()
    finally:
        await first_runtime.shutdown()
        await second_runtime.shutdown()


def test_mid_turn_plugin_api_version_mismatch_reports_plugin_not_loaded(tmp_path: Path) -> None:
    """A plugin skipped on reload for an incompatible plugin API reports plugin unavailability and logs once."""
    config_box, coordinator, owner, tool, context, plugin_root = _setup_plugin_turn(tmp_path)
    _write_plugin(plugin_root, api_version="99.0")
    live = _reload_plugins(config_box[0], coordinator.runtime_paths)
    assert live.tools.unresolved_plugin_sources
    assert "demo_tool" not in live.tools.entries
    assert "demo_tool" not in live.tools.unavailable

    with tool_runtime_context(context):
        _assert_refused(
            coordinator,
            owner,
            tool,
            expected_message=_PLUGIN_NOT_LOADED_REFUSAL,
            expected_reason="plugin_not_loaded",
            arguments={"secret": "top-secret-arg"},
            forbidden=("top-secret-arg", "99.0", str(plugin_root)),
        )


def test_mid_turn_plugin_tools_module_failure_reports_plugin_not_loaded(tmp_path: Path) -> None:
    """A plugin whose tools module fails on reload reports plugin unavailability without leaking the error."""
    config_box, coordinator, owner, tool, context, plugin_root = _setup_plugin_turn(tmp_path)
    _write_plugin(plugin_root, suffix="raise RuntimeError('secret-import-boom')\n")
    live = _reload_plugins(config_box[0], coordinator.runtime_paths)
    assert "demo_tool" in live.tools.unavailable
    assert "demo_tool" not in live.tools.entries

    with tool_runtime_context(context):
        _assert_refused(
            coordinator,
            owner,
            tool,
            expected_message=_PLUGIN_NOT_LOADED_REFUSAL,
            expected_reason="plugin_not_loaded",
            arguments={"secret": "top-secret-arg"},
            forbidden=("secret-import-boom", "top-secret-arg", str(plugin_root)),
        )


def test_mid_turn_runtime_unavailable_reports_runtime_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool that becomes runtime_unavailable mid-turn reports runtime unavailability and logs once."""
    config = managed_team_config(tmp_path)
    config.agents["lead"].tools = ["calculator"]
    config_box = [config]
    coordinator = team_coordinator(tmp_path, config)
    coordinator.config_provider = lambda: config_box[0]
    paths = coordinator.runtime_paths
    toolkit = get_tool_by_name(
        "calculator",
        paths,
        worker_target=None,
        runs_in_worker=False,
        tools=config_plugins(config, paths).tools,
    )
    tool = toolkit_job_tools(toolkit, authored_name="calculator", snapshot=authority_snapshot(config, "lead"))[
        "calculate"
    ]
    owner = replace(completed_delegation_job().owner, transport_agent_name="lead")
    coordinator._authorize_execution(owner, tool)

    monkeypatch.setattr(plugins_module, "egress_tool_unavailable", lambda name, _paths: name == "calculator")
    config_box[0] = config.model_copy()
    assert "calculator" in config_plugins(config_box[0], paths).tools.runtime_unavailable

    _assert_refused(
        coordinator,
        owner,
        tool,
        expected_message=_RUNTIME_UNAVAILABLE_REFUSAL,
        expected_reason="runtime_unavailable",
        arguments={"a": 1, "b": 2},
    )


@pytest.mark.parametrize(
    ("revocation", "expected_reason"),
    [
        ("tool_removed", "not_locally_allowed"),
        ("requester_denied", "requester_denied"),
        ("requester_pending", "not_locally_allowed"),
        ("scope_changed", "not_locally_allowed"),
        ("file_access_changed", "not_locally_allowed"),
        ("invalid_transport", "invalid_transport"),
    ],
)
def test_simultaneous_revocation_and_plugin_load_failure_keeps_generic_denial(
    tmp_path: Path,
    revocation: str,
    expected_reason: str,
) -> None:
    """Caller, membership, or agent-policy revocation never discloses plugin load failure."""
    config_box, coordinator, owner, tool, context, plugin_root = _setup_plugin_turn(tmp_path)
    _write_plugin(plugin_root, api_version="99.0")
    config = config_box[0]
    if revocation == "tool_removed":
        config.agents["lead"].tools = []
    elif revocation == "requester_denied":
        config.agents["lead"].access = ResponderAccessConfig(users=["@other:localhost"], current_room_members=False)
    elif revocation == "requester_pending":
        config.agents["lead"].access = ResponderAccessConfig(current_room_members=True)
        coordinator.agent_reply_memberships = ResolvingMembership()
        assert owner.requester_id is not None
        assert coordinator._requester_access(config, owner.requester_id, owner.room_id, {"lead"}) == "pending"
    elif revocation == "scope_changed":
        config.agents["lead"].worker_scope = "user"
    elif revocation == "file_access_changed":
        config.agents["lead"].file_access = "unrestricted"
    elif revocation == "invalid_transport":
        owner = replace(owner, transport_agent_name="removed_team")
    _reload_plugins(config, coordinator.runtime_paths)

    with tool_runtime_context(context):
        _assert_refused(
            coordinator,
            owner,
            tool,
            expected_message=_GENERIC_REFUSAL,
            expected_reason=expected_reason,
            forbidden=("plugin", str(plugin_root)),
        )


@pytest.mark.parametrize("mode", ["removed", "disabled", "healthy_without_toolkit"])
def test_removed_disabled_or_healthy_plugin_keeps_generic_denial_with_unrelated_failure(
    tmp_path: Path,
    mode: str,
) -> None:
    """Unrelated plugin failures never misclassify removed, disabled, or changed-toolkit plugins."""
    broken_root = _write_plugin(tmp_path / "plugins" / "broken", name="broken", tool_name="broken_tool")
    config_box, coordinator, owner, tool, context, plugin_root = _setup_plugin_turn(
        tmp_path,
        extra_plugins=(PluginEntryConfig(path="./plugins/broken"),),
    )
    _write_plugin(broken_root, name="broken", tool_name="broken_tool", api_version="99.0")
    if mode == "removed":
        config_box[0] = config_box[0].model_copy(update={"plugins": [PluginEntryConfig(path="./plugins/broken")]})
    elif mode == "disabled":
        config_box[0] = config_box[0].model_copy(
            update={
                "plugins": [
                    PluginEntryConfig(path="./plugins/demo", enabled=False),
                    PluginEntryConfig(path="./plugins/broken"),
                ],
            },
        )
    elif mode == "healthy_without_toolkit":
        _write_plugin(plugin_root, tool_name="other_tool")
    _reload_plugins(config_box[0], coordinator.runtime_paths)

    with tool_runtime_context(context):
        _assert_refused(
            coordinator,
            owner,
            tool,
            expected_message=_GENERIC_REFUSAL,
            expected_reason="not_locally_allowed",
            forbidden=("plugin", str(plugin_root), str(broken_root)),
        )


def test_incompatible_override_with_unloaded_plugin_fails_closed(tmp_path: Path) -> None:
    """An incompatible tool override added while the plugin is unloaded fails closed to generic denial."""
    config_box, coordinator, owner, tool, context, plugin_root = _setup_plugin_turn(tmp_path)
    _write_plugin(plugin_root, api_version="99.0")
    config_box[0].agents["lead"].tools = [ToolConfigEntry(name="demo_tool", overrides={"unknown_override": "x"})]
    _reload_plugins(config_box[0], coordinator.runtime_paths)

    with tool_runtime_context(context):
        _assert_refused(
            coordinator,
            owner,
            tool,
            expected_message=_GENERIC_REFUSAL,
            expected_reason="not_locally_allowed",
            forbidden=("plugin", "unknown_override"),
        )


def test_pending_requester_membership_still_allows_live_tool_execution(tmp_path: Path) -> None:
    """Pending requester room membership keeps permitting execution of a still-allowed live tool."""
    config_box, coordinator, owner, tool, context, _plugin_root = _setup_plugin_turn(tmp_path)
    config_box[0].agents["lead"].access = ResponderAccessConfig(current_room_members=True)
    coordinator.agent_reply_memberships = ResolvingMembership()
    assert owner.requester_id is not None
    assert coordinator._requester_access(config_box[0], owner.requester_id, owner.room_id, {"lead"}) == "pending"

    with tool_runtime_context(context), capture_logs() as logs:
        coordinator._authorize_execution(owner, tool)
    assert not [entry for entry in logs if entry.get("event") == "Refused tool execution"]
