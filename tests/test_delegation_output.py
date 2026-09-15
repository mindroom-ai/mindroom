"""Native subagent results obey the same output-file policy as direct tools."""

from __future__ import annotations

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
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.state import DelegationState
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tool_system.runtime_context import tool_runtime_context
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["explicit", "automatic", "invalid", "resumed", "resumed_invalid"])
async def test_native_delegation_obeys_output_file_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Persisted child approvals must not bypass path validation, redirection, or automatic saving."""
    paths = _runtime_paths(tmp_path)
    config = Config(
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
    output_args = (
        {} if mode == "automatic" else {"mindroom_output_path": "../escape.txt" if mode == "invalid" else "report.txt"}
    )
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
    parent = Agent(id="leader", name="leader", db=storage, tools=[toolkit], model=model)
    workspace = resolve_agent_runtime("leader", config, paths, identity).tool_base_dir
    assert workspace is not None
    options = {"agent_name": "leader", "config": config, "runtime_paths": paths, "execution_identity": identity}
    try:
        with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
            response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
            result = await drive_delegations(parent, response, run_child=run_delegated_child_response, **options)
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
                    run_child=run_delegated_child_response,
                    **options,
                    decisions={str(tool["tool_call_id"]): True for tool in state.pending_tools},
                    denial_reasons={str(tool["tool_call_id"]): None for tool in state.pending_tools},
                )
        assert result.status == RunStatus.completed
        delegation = next(tool for tool in result.tools or [] if tool.tool_name == "run_subagent")
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
            assert "Child report" in (workspace / receipt["path"]).read_text()
            assert receipt.get("auto_saved", False) == (mode == "automatic")
    finally:
        storage.close()
