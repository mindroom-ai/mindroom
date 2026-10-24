"""Reserved job controls use the same approval and plugin boundaries as other tools."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.hooks import EVENT_TOOL_AFTER_CALL, EVENT_TOOL_BEFORE_CALL, HookRegistry, hook
from mindroom.llm.runs import RunStatus
from mindroom.tool_jobs.instances import pin_background_tool_jobs
from mindroom.tool_jobs.runtime import register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import ScriptedAgents, _delegate_runtime_context, _runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import persist_entity_accounts
from tests.tool_job_helpers import run_built_turn, tool_job_runtime

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.hooks import ToolAfterCallContext, ToolBeforeCallContext


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("action", ["list", "collect", "wait", "cancel"])
async def test_reserved_controls_run_without_approval_and_emit_plugin_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approval: bool,
    action: str,
) -> None:
    """Every action runs at once, even when every call needs approval, and emits its before/after pair."""
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", tools=[])},
        background_tool_jobs=BackgroundToolJobsConfig(enabled=True),
    )
    config.memory.backend = "none"
    config.defaults.tools = []
    config.tool_approval.default = "require_approval" if approval else "auto_approve"
    paths = _runtime_paths(tmp_path)
    persist_entity_accounts(config, paths)
    phases = []

    @hook(EVENT_TOOL_BEFORE_CALL)
    async def before(context: ToolBeforeCallContext) -> None:
        phases.append(("before", context.tool_name, context.arguments["action"]))

    @hook(EVENT_TOOL_AFTER_CALL)
    async def after(context: ToolAfterCallContext) -> None:
        phases.append(("after", context.tool_name, context.arguments["action"]))

    registry = HookRegistry.from_plugins(
        [
            SimpleNamespace(
                name="policy",
                discovered_hooks=(before, after),
                discovered_automations=(),
                entry_config=PluginEntryConfig(path="./policy"),
                plugin_order=0,
            ),
        ],
    )
    context = replace(_delegate_runtime_context(config, paths), hook_registry=registry)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = await tool_job_runtime(paths.storage_root)
    pin_background_tool_jobs(context.config, paths)
    register_background_runtime(paths, runtime)
    model = ScriptedModel(
        [calls(tool_call("job", "control", action=action, job_id="missing")), answer("done")],
    )
    ScriptedAgents({"leader": model}).install(monkeypatch)
    try:
        with tool_runtime_context(context):
            build = create_agent(
                "leader",
                config,
                paths,
                owner,
                hook_registry=registry,
                supports_native_tool_approval=True,
                persist_runtime_state=False,
            )
            response = await run_built_turn(
                build,
                config=config,
                runtime_paths=paths,
                prompt="Check the job",
                session_id=owner.session_id or "session",
            )
            assert response.status is RunStatus.completed
            assert phases == [("before", "job", action), ("after", "job", action)]
    finally:
        await runtime.shutdown()
