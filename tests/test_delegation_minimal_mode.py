"""Callers can run a subagent in token-efficient minimal mode."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from mindroom import ai, minimal_agent
from mindroom.agent_cli.protocol import ToolCallOperation, ToolListOperation
from mindroom.agent_cli.session import TurnToolRegistry
from mindroom.agent_storage import create_session_storage
from mindroom.agents import apply_tool_approval_capability
from mindroom.ai import run_delegated_child_response
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import DefaultsConfig, ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.delegate import DelegateTools
from mindroom.delegation.execution import drive_delegations
from mindroom.delegation.state import DelegationChild
from mindroom.response_turn import ResponseTurnContext
from mindroom.tool_system.runtime_context import tool_runtime_context
from tests.identity_helpers import entity_ids
from tests.test_delegate_tools import _delegate_runtime_context
from tests.test_delegation_direct_audit import _identity
from tests.test_delegation_execution import DelegationModel, _call
from tests.test_mode_commands import _CLI_DEPLOYMENT_ENV

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_LONG_ROLE = "optional long role " * 200


@dataclass
class _ToolRecordingModel(DelegationModel):
    """Record which functions and system prompt each child request offered the provider."""

    seen_tools: list[list[str]] = field(default_factory=list)

    async def ainvoke(self, *args: object, **kwargs: object) -> ModelResponse:
        tools = kwargs.get("tools") or []
        self.seen_tools.append([tool["function"]["name"] for tool in tools])  # type: ignore[index, union-attr]
        return await super().ainvoke(*args, **kwargs)


def _config(*, helper_tools: list[str], approval: ToolApprovalConfig | None = None) -> Config:
    return Config(
        agents={
            "leader": AgentConfig(display_name="Leader", delegate_to=["helper", "plain"]),
            "helper": AgentConfig(display_name="Helper", role=_LONG_ROLE, tools=helper_tools, memory_backend="file"),
            "plain": AgentConfig(display_name="Plain"),
        },
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
        defaults=DefaultsConfig(tools=[], learning=False),
        memory={"backend": "none"},
        **({"tool_approval": approval} if approval is not None else {}),
    )


def _paths(tmp_path: Path, env: dict[str, str]) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)


def _live_context(config: Config, paths: RuntimePaths) -> object:
    """Bind the managed Matrix response owner a minimal child's CLI worker requires."""
    context = _delegate_runtime_context(config, paths, execution_identity=_identity())
    return replace(
        context,
        # The parent's reply target stays on the child's runtime for its Matrix tools.
        target=replace(context.target, reply_to_event_id="$parent-reply"),
        orchestrator=SimpleNamespace(agent_cli_registry=TurnToolRegistry()),
        membership_turn_id="$turn",
        correlation_id="correlation",
    )


async def _delegate(
    toolkit: DelegateTools,
    config: Config,
    paths: RuntimePaths,
    tool_name: str,
    *,
    native: bool,
    **arguments: object,
) -> str:
    """Call a delegate function directly or through a parent Agent and the native delegation driver."""
    if not native:
        return await toolkit.async_functions[tool_name].entrypoint(**arguments)
    identity = _identity()
    apply_tool_approval_capability(toolkit, config, supports_native_tool_approval=True, registered_tool_name="delegate")
    storage = create_session_storage("leader", config, paths, identity)
    parent = Agent(
        id="leader",
        db=storage,
        tools=[toolkit],
        model=DelegationModel(
            id="parent",
            responses=[
                ModelResponse(tool_calls=[_call(tool_name, f"{tool_name}-call", **arguments)]),
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
        return next(str(tool.result) for tool in response.tools or () if tool.tool_name == tool_name)
    finally:
        storage.close()


def test_minimal_option_is_advertised_only_for_capable_subagents(tmp_path: Path) -> None:
    """The description recommends minimal mode only where this deployment can run it."""
    config = _config(helper_tools=["shell"])
    ready = DelegateTools("leader", ["helper", "plain"], _paths(tmp_path, _CLI_DEPLOYMENT_ENV), config)
    description = ready.async_functions["run_subagent"].description or ""
    assert "Subagents that support minimal mode: helper." in description
    assert "not important in its system prompt" in description

    unready = DelegateTools("leader", ["helper", "plain"], _paths(tmp_path, {}), config)
    assert "minimal mode" not in (unready.async_functions["run_subagent"].description or "")

    openai_caller = replace(_identity(), channel="openai_compat")
    detached = DelegateTools(
        "leader",
        ["helper"],
        _paths(tmp_path, _CLI_DEPLOYMENT_ENV),
        config,
        execution_identity=openai_caller,
    )
    assert "minimal mode" not in (detached.async_functions["run_subagent"].description or "")

    gated = _config(
        helper_tools=["shell"],
        approval=ToolApprovalConfig(rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")]),
    )
    gated_tools = DelegateTools("leader", ["helper"], _paths(tmp_path, _CLI_DEPLOYMENT_ENV), gated)
    assert "minimal mode" not in (gated_tools.async_functions["run_subagent"].description or "")


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["direct", "native"])
@pytest.mark.parametrize(
    ("target", "approval"),
    [
        ("plain", None),
        (
            "helper",
            ToolApprovalConfig(rules=[ApprovalRuleConfig(match="run_shell_command", action="require_approval")]),
        ),
    ],
)
async def test_ineligible_minimal_child_fails_with_a_standard_subagent_hint(
    tmp_path: Path,
    target: str,
    approval: ToolApprovalConfig | None,
    native: bool,
) -> None:
    """A child without usable shell commands fails before any Bash and tells the caller how to recover."""
    config = _config(helper_tools=["shell"], approval=approval)
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    toolkit = DelegateTools("leader", ["helper", "plain"], paths, config, execution_identity=_identity())

    with tool_runtime_context(_live_context(config, paths)):
        result = await _delegate(
            toolkit,
            config,
            paths,
            "run_subagent",
            native=native,
            task="Report",
            agent_name=target,
            minimal=True,
        )

    assert "run, check, and kill shell permissions" in result, result
    assert "Start a new subagent without minimal." in result


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True], ids=["direct", "native"])
async def test_minimal_subagent_requests_only_bash_and_follow_ups_keep_its_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native: bool,
) -> None:
    """A minimal child sees one Bash tool and a short prompt, never gated tools; its follow-up stays minimal."""
    config = _config(
        helper_tools=["shell", "calculator"],
        approval=ToolApprovalConfig(rules=[ApprovalRuleConfig(match="factorial", action="require_approval")]),
    )
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    identity = _identity()
    listings: list[str] = []

    class Worker:
        handle = SimpleNamespace(worker_id="worker")
        owner = None

        async def install_grant(self, owner: object, _grant: object, *, shell: object) -> None:
            del shell
            self.owner = owner

        async def invoke_shell(self, name: str, arguments: dict[str, object]) -> str:
            assert name == "run_shell_command"
            assert arguments["args"] == "mindroom-agent tools list"
            assert self.owner is not None
            listing = await self.owner.operation(ToolListOperation(operation="tools.list"))  # type: ignore[attr-defined]
            listings.append(str(listing))
            return str(listing)

    @asynccontextmanager
    async def worker(_runtime: object):  # noqa: ANN202
        yield Worker()

    child_models: list[_ToolRecordingModel] = []

    def load_model(_config: object, _paths: object, _name: str, *_args: object) -> _ToolRecordingModel:
        model = _ToolRecordingModel(
            id="gpt-6-astra",
            responses=[
                ModelResponse(
                    tool_calls=[_call("bash", f"bash-{len(child_models)}", command="mindroom-agent tools list")],
                ),
                ModelResponse(content=f"Child answer {len(child_models)}"),
            ],
        )
        child_models.append(model)
        return model

    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", worker)
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", load_model)
    toolkit = DelegateTools("leader", ["helper", "plain"], paths, config, execution_identity=identity)

    with tool_runtime_context(_live_context(config, paths)):
        result = await _delegate(
            toolkit,
            config,
            paths,
            "run_subagent",
            native=native,
            task="Report",
            agent_name="helper",
            minimal=True,
        )
        assert "Child answer 0" in result, result

        handles = list((paths.storage_root / "subagent_sessions").glob("*.json"))
        assert len(handles) == 1
        child = json.loads(handles[0].read_text())["child"]
        assert child["agent_mode"] == "minimal"
        follow_up = await _delegate(
            toolkit,
            config,
            paths,
            "continue_subagent",
            native=native,
            subagent_id=child["subagent_id"],
            message="More detail",
        )
        assert "Child answer 1" in follow_up, follow_up

    assert len(child_models) == 2
    for model in child_models:
        assert model.seen_tools == [["bash"], ["bash"]]
        system = str(model.seen_messages[0].content)
        assert "optional long role" not in system
        assert "mindroom-agent" in system
    assert len(listings) == 2
    # Native callers support approval, but a minimal child must never pause for it.
    assert all("is_prime" in listing and "factorial" not in listing for listing in listings), listings


@pytest.mark.asyncio
async def test_minimal_parent_runs_a_minimal_child_through_its_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child started inside a parent's CLI call captures its own provider batch and runs its own Bash."""
    config = _config(helper_tools=["shell"])
    config.agents["leader"] = AgentConfig(
        display_name="Leader",
        model="lead",
        tools=["shell"],
        memory_backend="file",
        delegate_to=["helper"],
    )
    config.models["lead"] = ModelConfig(provider="openai", id="gpt-6-sol")
    paths = _paths(tmp_path, _CLI_DEPLOYMENT_ENV)
    entity_ids(config, paths)
    identity = _identity()
    child_listings: list[str] = []

    class Worker:
        handle = SimpleNamespace(worker_id="worker")
        owner = None

        def __init__(self, agent_name: str) -> None:
            self.agent_name = agent_name

        async def install_grant(self, owner: object, _grant: object, *, shell: object) -> None:
            del shell
            self.owner = owner

        async def invoke_shell(self, name: str, _arguments: dict[str, object]) -> str:
            assert name == "run_shell_command"
            owner = self.owner
            assert owner is not None
            if self.agent_name == "helper":
                listing = str(await owner.operation(ToolListOperation(operation="tools.list")))  # type: ignore[attr-defined]
                child_listings.append(listing)
                return listing
            call_id = uuid4()
            await owner.operation(  # type: ignore[attr-defined]
                ToolCallOperation(
                    operation="tools.call",
                    toolkit="delegate",
                    function="run_subagent",
                    arguments={"agent_name": "helper", "task": "List your tools", "minimal": True},
                    call_id=call_id,
                ),
            )
            while (receipt := await owner.get_call(str(call_id)))["status"] in {"queued", "running", "waiting"}:  # type: ignore[attr-defined]  # noqa: ASYNC110 - actual CLI receipt protocol
                await asyncio.sleep(0.01)
            assert receipt["status"] == "completed", json.dumps(receipt, default=str)
            return str(receipt["outcome"])

    @asynccontextmanager
    async def worker(runtime: object):  # noqa: ANN202
        yield Worker(runtime.agent_name)  # type: ignore[attr-defined]

    models = {
        "lead": DelegationModel(
            id="gpt-6-sol",
            responses=[
                ModelResponse(tool_calls=[_call("bash", "lead-bash", command="mindroom-agent tools call ...")]),
                ModelResponse(content="Leader done"),
            ],
        ),
        "default": _ToolRecordingModel(
            id="gpt-6-astra",
            responses=[
                ModelResponse(tool_calls=[_call("bash", "child-bash", command="mindroom-agent tools list")]),
                ModelResponse(content="Child answer"),
            ],
        ),
    }
    monkeypatch.setattr(minimal_agent, "open_configured_cli_worker", worker)
    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", lambda _c, _p, name, *_a: models[name])
    turn = ResponseTurnContext(
        agent_mode="minimal",
        entity_label="leader",
        session_id=identity.session_id,
        run_id=None,
        correlation_id="correlation",
        reply_to_event_id="$parent-reply",
        room_id=identity.room_id,
        thread_id=identity.resolved_thread_id,
        requester_id=identity.requester_id,
        matrix_run_metadata=None,
    )

    with tool_runtime_context(_live_context(config, paths)):
        result = await ai.ai_response(
            turn,
            prompt="Delegate",
            runtime_paths=paths,
            config=config,
            execution_identity=identity,
            supports_native_tool_approval=True,
        )

    assert "Leader done" in result, result
    assert len(child_listings) == 1
    assert models["default"].seen_tools == [["bash"], ["bash"]]
    assert "Child answer" in str(models["lead"].seen_messages)


@pytest.mark.asyncio
async def test_child_snapshot_without_mode_continues_in_standard_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subagent saved before modes existed continues in standard mode with its full tool surface."""
    config = _config(helper_tools=["shell"])
    paths = _paths(tmp_path, {})
    entity_ids(config, paths)
    models: list[_ToolRecordingModel] = []

    def load_model(_config: object, _paths: object, _name: str, *_args: object) -> _ToolRecordingModel:
        model = _ToolRecordingModel(id="gpt-6-astra", responses=[ModelResponse(content=f"Answer {len(models)}")])
        models.append(model)
        return model

    monkeypatch.setattr("mindroom.agents._load_agent_model_instance", load_model)
    toolkit = DelegateTools("leader", ["helper"], paths, config, execution_identity=_identity())

    with tool_runtime_context(_live_context(config, paths)):
        assert "Answer 0" in await toolkit.run_subagent(task="Report", agent_name="helper")
        handle = next((paths.storage_root / "subagent_sessions").glob("*.json"))
        payload = json.loads(handle.read_text())
        del payload["child"]["agent_mode"]
        handle.write_text(json.dumps(payload))
        assert DelegationChild(**payload["child"]).agent_mode == "standard"
        assert "Answer 1" in await toolkit.continue_subagent(payload["child"]["subagent_id"], "More")

    assert "bash" not in models[1].seen_tools[0]
    assert "run_shell_command" in models[1].seen_tools[0]
