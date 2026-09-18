"""Native subagent results obey the same output-file policy as direct tools."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability, build_agent_toolkit
from mindroom.ai import run_delegated_child_response
from mindroom.config.agent import AgentConfig, AgentPrivateConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig
from mindroom.custom_tools.job import JobTools
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.state import DelegationState
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_jobs.agno_compat_execution import install_tool_job_execution
from mindroom.tool_jobs.runtime import ToolJobRuntime, register_background_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call, _saved_approval_calls

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.delegation.state import DelegationChild
    from mindroom.knowledge.refresh_scheduler import KnowledgeRefreshScheduler


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["explicit", "automatic", "invalid", "resumed", "resumed_invalid"])
@pytest.mark.parametrize("execution", ["inline", "foreground", "detached"])
async def test_native_delegation_obeys_output_file_policy(  # noqa: C901, PLR0912, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    execution: str,
) -> None:
    """Persisted child approvals must not bypass path validation, redirection, or automatic saving."""
    paths = _runtime_paths(tmp_path)
    config = Config(
        background_tool_jobs=execution != "inline",
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["child"], private=AgentPrivateConfig(per="user")),
            "child": AgentConfig(display_name="Child", tools=["calculator"]),
        },
        defaults=DefaultsConfig(
            tools=[],
            learning=False,
            tool_output_auto_save_threshold_bytes=16 if mode == "automatic" else 100_000,
        ),
        memory={"backend": "none"},
        tool_approval={"rules": [{"match": "add", "action": "require_approval"}]} if mode.startswith("resumed") else {},
    )
    entity_ids(config, paths)
    identity = ToolExecutionIdentity(
        "matrix",
        "leader",
        "@alice:example.org",
        "!room:example.org",
        None,
        None,
        "parent",
    )
    runtime = ToolJobRuntime(tmp_path) if execution != "inline" else None
    register_background_runtime(paths, runtime)
    release = asyncio.Event()
    if execution != "detached":
        release.set()

    async def run_child(
        child: DelegationChild,
        *,
        prompt: str,
        config: Config,
        runtime_paths: RuntimePaths,
        refresh_scheduler: KnowledgeRefreshScheduler | None,
        supports_native_tool_approval: bool,
    ) -> str:
        await release.wait()
        return await run_delegated_child_response(
            child,
            prompt=prompt,
            config=config,
            runtime_paths=runtime_paths,
            refresh_scheduler=refresh_scheduler,
            supports_native_tool_approval=supports_native_tool_approval,
        )

    child_model = DelegationModel(
        id="child",
        responses=[ModelResponse(tool_calls=[_call("add", "sum", a=1, b=2)]), ModelResponse(content="Child report")],
    )
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda *_args: child_model)
    toolkit = build_agent_toolkit(
        "delegate",
        agent_name="leader",
        config=config,
        runtime_paths=paths,
        worker_tools=[],
        runtime_overrides=None,
        execution_identity=identity,
    )
    assert toolkit is not None
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    output_args: dict[str, object] = (
        {} if mode == "automatic" else {"mindroom_output_path": "../escape.txt" if mode == "invalid" else "report.txt"}
    )
    if execution == "detached":
        output_args["wait_timeout"] = 0
    model = DelegationModel(
        id="parent",
        responses=[
            ModelResponse(
                tool_calls=[_call("run_subagent", "delegate", agent_name="child", task="Write report", **output_args)],
            ),
            ModelResponse(content="Parent done"),
        ],
    )
    storage = create_session_storage("leader", config, paths, identity)
    if runtime is not None:
        install_tool_job_execution(model)
    parent = Agent(id="leader", name="leader", db=storage, tools=[toolkit], model=model)
    workspace = resolve_agent_runtime("leader", config, paths, identity).tool_base_dir
    assert workspace is not None
    options = {"agent_name": "leader", "config": config, "runtime_paths": paths, "execution_identity": identity}
    result_tool_name = "run_subagent"
    relocated_output = None
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
            result = await drive_delegations(parent, response, run_child=run_child, **options)
            if execution == "detached" and mode != "invalid":
                assert runtime is not None
                first = next(tool for tool in result.tools or [] if tool.tool_name == "run_subagent")
                handle = json.loads(first.result)
                assert "job_id" in handle, "A released wait returns its handle without redirecting it to a file"
                assert not (workspace / "report.txt").exists()
                release.set()
                waited = await asyncio.wait_for(runtime.wait(handle["job_id"], owner=identity, depth=0), 5)
                await runtime.release_wait(handle["job_id"], waited.token)
                if mode == "explicit":
                    relocated_output = workspace / "completed_report.txt"
                    (workspace / "report.txt").rename(relocated_output)
                    (workspace / "report.txt").mkdir()
                if mode.startswith("resumed"):
                    await runtime.shutdown()
                    runtime = ToolJobRuntime(tmp_path)
                    register_background_runtime(paths, runtime)
                    await runtime.recover()
                wait_model = DelegationModel(
                    id="waiter",
                    responses=[
                        ModelResponse(tool_calls=[_call("job", "retrieve", action="wait", job_id=handle["job_id"])]),
                        ModelResponse(content="Parent done"),
                    ],
                )
                install_tool_job_execution(wait_model)
                parent = Agent(id="leader", db=storage, tools=[JobTools(paths, identity)], model=wait_model)
                response = await parent.arun("Retrieve", session_id=identity.session_id, user_id=identity.requester_id)
                result = await drive_delegations(parent, response, run_child=run_child, **options)
                result_tool_name = "job"
            if mode.startswith("resumed"):
                assert result.status == RunStatus.paused
                assert not (workspace / "report.txt").exists()
                stored = storage.get_run(result.run_id)
                assert isinstance(stored, RunOutput)
                state = DelegationState.from_metadata(stored.metadata)
                if mode == "resumed_invalid":
                    (workspace / "report.txt").symlink_to(tmp_path / "escape.txt")
                result = await drive_delegations(
                    Agent(
                        id="leader",
                        name="leader",
                        db=storage,
                        tools=[toolkit],
                        model=DelegationModel(id="parent", responses=[ModelResponse(content="Parent done")]),
                    ),
                    stored,
                    run_child=run_child,
                    **options,
                    decisions={str(tool["tool_call_id"]): True for tool in state.pending_tools},
                    approval_calls=_saved_approval_calls(state),
                    denial_reasons={str(tool["tool_call_id"]): None for tool in state.pending_tools},
                )
        assert result.status == RunStatus.completed
        delegation = next(tool for tool in result.tools or [] if tool.tool_name == result_tool_name)
        receipt = json.loads(delegation.result)["mindroom_tool_output"]
        if mode in {"invalid", "resumed_invalid"}:
            assert receipt["status"] == "error"
            assert not (tmp_path / "escape.txt").exists()
            if mode == "invalid":
                assert len(child_model.responses) == 2
            else:
                assert len(child_model.responses) == 1
        else:
            assert receipt["status"] == "saved_to_file"
            assert "Child report" in (relocated_output or workspace / receipt["path"]).read_text()
            assert receipt.get("auto_saved", False) == (mode == "automatic")
    finally:
        release.set()
        if runtime is not None:
            await runtime.shutdown()
        register_background_runtime(paths, None)
        storage.close()
