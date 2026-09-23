"""Reserved job controls use the same approval and plugin boundaries as other tools."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from mindroom.agents import create_agent
from mindroom.approval_tools import required_approval_tool_names, validate_approval_tool_owners
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import BackgroundToolJobsConfig
from mindroom.config.plugin import PluginEntryConfig
from mindroom.event_journal import ApprovalCall
from mindroom.hooks import EVENT_TOOL_AFTER_CALL, EVENT_TOOL_BEFORE_CALL, HookRegistry, hook
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import build_execution_identity_from_runtime_context, tool_runtime_context
from tests.delegation_helpers import DelegationModel, _call, _delegate_runtime_context, _runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.hooks import ToolAfterCallContext, ToolBeforeCallContext


@pytest.mark.asyncio
@pytest.mark.parametrize("approval", [False, True])
@pytest.mark.parametrize("action", ["list", "inspect", "wait", "cancel"])
async def test_reserved_controls_obey_approval_and_plugin_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    approval: bool,
    action: str,
) -> None:
    """Real actor construction must gate every action and emit its before/after pair."""
    config = Config(
        agents={"leader": AgentConfig(display_name="Leader", tools=[], learning=False)},
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
                entry_config=PluginEntryConfig(path="./policy"),
                plugin_order=0,
            ),
        ],
    )
    context = replace(_delegate_runtime_context(config, paths), hook_registry=registry)
    owner = build_execution_identity_from_runtime_context(context)
    runtime = ToolJobRuntime(paths.storage_root)
    register_background_runtime(paths, runtime)
    model = DelegationModel(
        id="test",
        responses=[
            ModelResponse(tool_calls=[_call("job", "control", action=action, job_id="missing")]),
            ModelResponse(content="done"),
        ],
    )
    monkeypatch.setattr("mindroom.model_loading.get_model_instance", lambda *_args, **_kwargs: model)
    try:
        with tool_runtime_context(context):
            actor = create_agent(
                "leader",
                config,
                paths,
                owner,
                hook_registry=registry,
                supports_native_tool_approval=True,
                persist_runtime_state=False,
            )
            response = await actor.arun("Check the job", session_id=owner.session_id)
            if approval:
                assert response.status == RunStatus.paused
                assert phases == []
                for requirement in response.requirements:
                    requirement.confirm()
                calls = (ApprovalCall("control", "job", "leader", 2**62, toolkit_name="job"),)
                restored_tools = await required_approval_tool_names(
                    "leader",
                    calls,
                    config=config,
                    runtime_paths=paths,
                    execution_identity=owner,
                )
                actor = create_agent(
                    "leader",
                    config,
                    paths,
                    owner,
                    hook_registry=registry,
                    supports_native_tool_approval=True,
                    persist_runtime_state=False,
                    required_tool_names=restored_tools,
                )
                validate_approval_tool_owners([actor], calls, response.requirements)
                response = await actor.acontinue_run(run_response=response)
            assert response.status == RunStatus.completed
            assert phases == [("before", "job", action), ("after", "job", action)]
    finally:
        register_background_runtime(paths, None)
        await runtime.shutdown()
