"""Subagent model overrides select a configured model without changing identity."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.thread_models import set_thread_model_override
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.delegation_helpers import _delegate_runtime_context, _runtime_paths, respond
from tests.fakes.scripted_model import ScriptedModel, answer, calls, tool_call
from tests.identity_helpers import entity_ids
from tests.test_delegation_direct_audit import _identity

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.llm.provider import ModelProvider
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


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
        defaults=DefaultsConfig(tools=[]),
        memory={"backend": "none"},
    )
    paths = _runtime_paths(tmp_path)
    entity_ids(config, paths)
    identity = _identity()
    set_thread_model_override(
        paths,
        thread_id="$thread",
        model_name="default",
        entity_names=("leader", "worker"),
        config=config,
    )
    parent = ScriptedModel(
        [
            calls(tool_call("run_subagent", "delegate", task="Report", agent_name=agent_name, model=model)),
            answer("Parent done"),
        ],
    )
    built_models: list[str] = []

    def build(
        _config: Config,
        _paths: RuntimePaths,
        model_name: str,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> ModelProvider:
        if execution_identity is not None and execution_identity.session_id == identity.session_id:
            return parent
        built_models.append(model_name)
        return ScriptedModel([answer(f"Answer from {model_name}")])

    monkeypatch.setattr("mindroom.providers.registry.build_model_provider", build)

    with tool_runtime_context(_delegate_runtime_context(config, paths, execution_identity=identity)):
        if native:
            text = await respond(
                "leader",
                "Delegate",
                config=config,
                runtime_paths=paths,
                identity=identity,
                run_id="parent-run",
            )
            assert isinstance(text, str)
            assert text.startswith("Parent done")
            stored = create_session_storage("leader", config, paths, identity).get_run("parent-run")
            assert isinstance(stored, RunRecord)
            assert stored.status is RunStatus.completed
            result = next(str(tool.result) for tool in stored.tools or () if tool.tool_name == "run_subagent")
        else:
            toolkit = DelegateTools("leader", ["leader", "worker"], paths, config, execution_identity=identity)
            result = await toolkit.run_subagent(task="Report", agent_name=agent_name, model=model)

    records = list(tmp_path.glob("agents/*/workspace/.mindroom/delegations/*/*/run.json"))
    handles = list((paths.storage_root / "subagent_sessions").glob("*.json"))
    if model in (None, "alternate"):
        expected_model = model or "default"
        assert f"Answer from {expected_model}" in result
        assert built_models == [expected_model]
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
        assert built_models == []
        assert not records
        assert not handles
    assert config.agents["leader"].model == config.agents["worker"].model == "default"
