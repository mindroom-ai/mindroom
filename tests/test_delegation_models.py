"""Subagent model overrides select a configured model without changing identity."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.ai import run_delegated_child_response
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.execution import drive_delegations
from mindroom.thread_models import set_thread_model_override
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context, _runtime_paths
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import DelegationModel, _call

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["direct", "native"])
@pytest.mark.parametrize("agent_name", [None, "worker"], ids=["self", "named"])
@pytest.mark.parametrize("model", [None, "alternate", "missing", "", "   "])
async def test_subagent_model_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
    agent_name: str | None,
    model: str | None,
) -> None:
    """Valid aliases select the provider; invalid aliases cannot create child state."""
    config = Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["leader", "worker"]),
            "worker": AgentConfig(display_name="Worker"),
        },
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra"),
            "alternate": ModelConfig(provider="anthropic", id="claude-sonnet-5"),
        },
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    set_thread_model_override(
        paths,
        thread_id="$thread",
        model_name="default",
        room_id="!room:example.org",
        set_by="@alice:example.org",
    )
    toolkit = DelegateTools("leader", ["leader", "worker"], paths, config, execution_identity=identity)
    models = {
        name: DelegationModel(id=name, responses=[ModelResponse(content=f"Answer from {name}")])
        for name in ("default", "alternate")
    }
    monkeypatch.setattr(
        "mindroom.agents._load_agent_model_instance",
        lambda _config, _paths, name, *_args: models[name],
    )

    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        if native:
            apply_tool_approval_capability(
                toolkit,
                config,
                supports_native_tool_approval=True,
                registered_tool_name="delegate",
            )
            storage = create_session_storage("leader", config, paths, identity)
            parent = Agent(
                id="leader",
                db=storage,
                tools=[toolkit],
                model=DelegationModel(
                    id="parent",
                    responses=[
                        ModelResponse(
                            tool_calls=[
                                _call("run_subagent", "delegate", task="Report", agent_name=agent_name, model=model),
                            ],
                        ),
                        ModelResponse(content="Parent done"),
                    ],
                ),
            )
            try:
                response = await parent.arun("Delegate", session_id=identity.session_id, user_id=identity.requester_id)
                response = await drive_delegations(
                    parent,
                    response,
                    agent_name="leader",
                    run_child=run_delegated_child_response,
                    config=config,
                    runtime_paths=paths,
                    execution_identity=identity,
                )
                assert response.status == RunStatus.completed
                result = next(str(tool.result) for tool in response.tools or () if tool.tool_name == "run_subagent")
            finally:
                storage.close()
        else:
            result = await toolkit.run_subagent(task="Report", agent_name=agent_name, model=model)

    records = list(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
    handles = list((paths.storage_root / "subagent_sessions").glob("*.json"))
    if model in (None, "alternate"):
        expected_model = model or "default"
        assert f"Answer from {expected_model}" in result
        assert len(records) == len(handles) == 1
        record = json.loads(records[0].read_text())
        assert record["model_name"] == expected_model
        assert record["child_agent_name"] == (agent_name or "leader")
        assert record["caller_agent_name"] == "leader"
        assert json.loads(handles[0].read_text())["child"]["model_name"] == expected_model
    else:
        assert "Unknown model" in result
        assert "alternate" in result
        assert "default" in result
        assert not records
        assert not handles
    assert config.agents["leader"].model == config.agents["worker"].model == "default"
