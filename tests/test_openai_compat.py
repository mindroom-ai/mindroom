"""Tests for the OpenAI-compatible chat completions API."""

from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import re
import tempfile
from concurrent.futures import CancelledError as FutureCancelledError
from contextlib import contextmanager, suppress
from contextvars import Context
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from mindroom.tool_system.plugins import config_plugins
from tests.conftest import completed_ai_response_mock, completing, install_model_providers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator, Sequence

    from agno.run import RunContext as AgnoRunContext
    from httpx import Response

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.agent_loop.spec import RunContext
    from mindroom.knowledge.registry import PublishedIndexResolution
    from mindroom.response_turn import ResponseTurnContext
    from mindroom.teams.members import BuiltTeam, ResolvedExactTeamMembers
    from mindroom.tool_system.tool_function import AdaptedToolkit

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from structlog.testing import capture_logs

from mindroom import constants
from mindroom.agent_loop.output_limit import CUT_OFF_TOOL_CALL_RESULT
from mindroom.agents import create_agent
from mindroom.ai import TurnResult
from mindroom.ai_run_metadata import build_ai_run_metadata_content, build_prepared_history_metadata_content
from mindroom.api import config_lifecycle, openai_compat
from mindroom.api.main import initialize_api_app
from mindroom.api.openai_request_parsing import ChatMessage, _extract_content_text, convert_messages, derive_session_id
from mindroom.approval.tool_approval import shutdown_approval_runtime
from mindroom.budgets.monitor import BudgetMonitor
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.agent import AgentConfig, AgentPrivateConfig, TeamConfig
from mindroom.config.judgment import TypeSafeJudgmentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig, ToolConfigEntry
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.execution_preparation import _PreparedExecutionContext
from mindroom.helper_usage import HelperUsageOwner, get_helper_usage_owner
from mindroom.history.request_messages import TurnMessages
from mindroom.history.session_context import (
    create_scope_session_storage,
    load_bound_scope_session,
)
from mindroom.history.types import HistoryScope, PreparedHistoryState, ResolvedReplayPlan
from mindroom.judgment.client import JudgmentClient
from mindroom.judgment.typesafe import _PINNED_MODEL
from mindroom.knowledge.availability import KnowledgeAvailability
from mindroom.knowledge.search import AgentKnowledge
from mindroom.knowledge.utils import KnowledgeAvailabilityDetail, _KnowledgeResolution
from mindroom.llm.errors import MISSING_API_KEY, AuthenticationFailed, ProviderError
from mindroom.llm.events import RunFinished, ToolCompleted, ToolStarted
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity, ModelRequest, StreamEvent, TextDelta, TurnCompleted
from mindroom.llm.request_context import current_llm_request_log_context
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.memory import MemoryPromptParts
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.response_turn import CompletedAttempt
from mindroom.routing import ResponderSelection
from mindroom.teams import leader as leader_module
from mindroom.teams.envelope import PreparedTeamExecution
from mindroom.teams.runtime import MemberEvent
from mindroom.teams.spec import TeamMode, TeamSpec
from mindroom.tool_system.agno_toolkits import Toolkit, adapt_toolkit
from mindroom.tool_system.events import FailedReply
from mindroom.tool_system.invocation import current_tool_call
from mindroom.tool_system.runtime_context import current_execution_identity
from mindroom.tool_system.tool_calls import ToolCallTiming, record_tool_success
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    build_tool_execution_identity,
)
from mindroom.turn_model import ModelScope, TeamTurnModelSelection, resolve_team_turn_models
from mindroom.usage.stats import collect_admin_usage
from tests.agent_loop.helpers import toolkit
from tests.api.conftest import publish_api_config
from tests.fakes.knowledge import RecordingRefreshScheduler, published_index_lookup
from tests.fakes.scripted_model import SYNTHETIC, Await, ScriptedModel, ScriptStep, answer, calls, tool_call, usage
from tests.identity_helpers import persist_entity_accounts
from tests.runtime_fakes import bind_test_embedded_runtime, openai_compat_app
from tests.teams.squad import built_team, resolved_members, team_spec
from tests.teams.squad import member as team_member
from tests.turn_model_helpers import turn_model


@contextmanager
def _openai_client(config: Config | None, runtime_paths: RuntimePaths) -> Iterator[TestClient]:
    """Serve the OpenAI-compatible routes for `runtime_paths`, with `config` as their committed config when given."""
    app = openai_compat_app(runtime_paths)
    if config is not None:
        publish_api_config(app, config)
    with TestClient(app, base_url="http://localhost") as client:
        yield client


def _runtime_paths(process_env: dict[str, str] | None = None) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=Path(__file__),
        storage_path=Path(tempfile.mkdtemp()),
        process_env=process_env or {},
    )


def _runtime_paths_for_config(config: Config, process_env: dict[str, str] | None = None) -> RuntimePaths:
    runtime_paths = _runtime_paths(process_env)
    persist_entity_accounts(config, runtime_paths)
    return runtime_paths


def _team_models(team_name: str, config: Config, runtime_paths: RuntimePaths) -> TeamTurnModelSelection:
    """Return the models a completion of ``team_name`` freezes when the requester has no budget to spend."""
    return resolve_team_turn_models(
        config,
        runtime_paths,
        ModelScope(entity=team_name),
        config.teams[team_name].agents,
        None,
    )


def _team_identity(
    team_name: str,
    runtime_paths: RuntimePaths,
    *,
    session_id: str = "session-1",
) -> ToolExecutionIdentity:
    """Return the identity the endpoint builds for an anonymous request to ``team/<team_name>`` in ``session_id``."""
    return build_tool_execution_identity(
        channel="openai_compat",
        agent_name=f"team/{team_name}",
        session_id=session_id,
        runtime_paths=runtime_paths,
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
    )


def _knowledge_indexes(knowledge: object) -> list[object]:
    """Return the published index readers behind the knowledge handle an agent received."""
    assert isinstance(knowledge, AgentKnowledge)
    return [source.index for source in knowledge.sources]


def _prepared_team_execution_context(
    *,
    final_prompt: str,
    run_metadata: dict[str, object] | None = None,
    replay_plan: ResolvedReplayPlan | None = None,
    replays_persisted_history: bool = False,
    messages: list[Message] | None = None,
    history: Sequence[Message] = (),
    prepared_context_tokens: int | None = None,
) -> PreparedTeamExecution:
    """Return a prepared team turn as ``prepare_materialized_team_execution`` builds one: its turn, replayed history, and run metadata."""
    turn_messages = list(messages or [Message(role="user", content=final_prompt)])
    turn = TurnMessages(prompt=turn_messages[-1], context=tuple(turn_messages[:-1]))
    prepared_history = PreparedHistoryState(
        replay_plan=replay_plan,
        replays_persisted_history=replays_persisted_history,
        prepared_context_tokens=prepared_context_tokens,
        native_history=None,
    )
    return PreparedTeamExecution(
        turn=turn,
        history=tuple(history),
        summary=None,
        run_metadata=cast("dict[str, Any] | None", run_metadata)
        or build_prepared_history_metadata_content(prepared_history),
        prepared_history=prepared_history,
        runtime_model_name="default",
    )


@pytest.fixture(autouse=True)
def reset_approval_store() -> Iterator[None]:
    """Keep the module-level approval store isolated per test."""
    asyncio.run(shutdown_approval_runtime())
    yield
    asyncio.run(shutdown_approval_runtime())


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _tool_calls_path(runtime_paths: RuntimePaths) -> Path:
    return constants.tracking_dir(runtime_paths) / "tool_calls.jsonl"


@pytest.fixture
def test_config() -> Config:
    """Create a minimal test config with a few agents."""
    return Config(
        agents={
            "general": AgentConfig(
                display_name="GeneralAgent",
                role="General-purpose assistant",
                rooms=[],
            ),
            "code": AgentConfig(
                display_name="CodeAgent",
                role="Generate code and manage files",
                tools=["file", "shell"],
                rooms=[],
            ),
            "research": AgentConfig(
                display_name="ResearchAgent",
                role="",
                rooms=[],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )


@pytest.fixture
def app_client(test_config: Config) -> Iterator[TestClient]:
    """Create a FastAPI test client with mocked config."""
    runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})
    persist_entity_accounts(test_config, runtime_paths)

    with _openai_client(test_config, runtime_paths) as client:
        yield client


@pytest.fixture
def authed_client(test_config: Config) -> Iterator[TestClient]:
    """Create a test client with API key auth enabled."""
    runtime_paths = _runtime_paths({"OPENAI_COMPAT_API_KEYS": "test-key-1,test-key-2"})
    persist_entity_accounts(test_config, runtime_paths)

    with _openai_client(test_config, runtime_paths) as client:
        yield client


def test_list_models_uses_committed_snapshot_until_reload(tmp_path: Path) -> None:
    """OpenAI-compatible routes should ignore newer on-disk edits until reload publishes a new snapshot."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-6-astra\n"
        "router:\n"
        "  model: default\n"
        "agents:\n"
        "  general:\n"
        "    display_name: General\n"
        "    role: helper\n"
        "    rooms: []\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
    )

    with _openai_client(None, runtime_paths) as client:
        assert config_lifecycle.load_config_into_app(runtime_paths, client.app).loaded
        config_path.write_text(
            "models:\n"
            "  default:\n"
            "    provider: openai\n"
            "    id: gpt-6-astra\n"
            "router:\n"
            "  model: default\n"
            "agents:\n"
            "  changed:\n"
            "    display_name: Changed\n"
            "    role: helper\n"
            "    rooms: []\n",
            encoding="utf-8",
        )
        response = client.get("/v1/models")

    assert response.status_code == 200
    model_ids = {model["id"] for model in response.json()["data"]}
    assert "general" in model_ids
    assert "changed" not in model_ids


def test_list_models_keeps_auth_runtime_bound_across_runtime_swap(test_config: Config) -> None:
    """Model listing should load config from the same runtime that authenticated the request."""
    runtime_a = _runtime_paths({"OPENAI_COMPAT_API_KEYS": "old-key"})
    runtime_b = _runtime_paths({"OPENAI_COMPAT_API_KEYS": "new-key"})

    def _authenticate_and_swap(
        _request: Request,
        authorization: str | None,
        runtime_paths: RuntimePaths,
    ) -> openai_compat._AuthenticatedCaller:
        assert authorization == "Bearer old-key"
        assert runtime_paths == runtime_a
        # The swapped-in runtime publishes no config, so a read from it would answer 500.
        initialize_api_app(client.app, runtime_b)
        return openai_compat._AuthenticatedCaller(session_namespace="old-key-namespace", requester_id=None)

    with (
        patch("mindroom.api.openai_compat._authenticate_request", side_effect=_authenticate_and_swap),
        _openai_client(test_config, runtime_a) as client,
    ):
        response = client.get("/v1/models", headers={"authorization": "Bearer old-key"})

    assert response.status_code == 200
    assert "general" in {model["id"] for model in response.json()["data"]}


def _offered_function_names(build: AgentBuild) -> set[str]:
    """Return the names of every function the built agent offers its model."""
    return {function.name for toolkit in build.spec.toolkits for function in toolkit.functions}


def test_openai_compatible_agent_hides_approval_gated_tools(
    test_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI-compatible agent construction should hide tools that require approval."""
    runtime_paths = constants.resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    config = Config.validate_with_runtime(
        {
            **test_config.authored_model_dump(),
            "tool_approval": {
                "rules": [{"match": "run_shell_command", "action": "require_approval"}],
            },
        },
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths)
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name="code",
        runtime_paths=runtime_paths,
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="openai-session",
    )

    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    build = create_agent(
        "code",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        include_openai_compat_guidance=True,
        knowledge=None,
        model=turn_model(config, runtime_paths, "code"),
        plugins=config_plugins(config, runtime_paths),
    )

    exposed_tool_names = _offered_function_names(build)
    assert "run_shell_command" not in exposed_tool_names
    assert "read_file" in exposed_tool_names


def test_openai_compatible_agent_hides_context_bound_todo_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI-compatible agent construction should hide Matrix-context-bound todo tools."""
    runtime_paths = constants.resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    config = Config(
        agents={
            "code": AgentConfig(
                display_name="CodeAgent",
                role="Generate code and manage files",
                tools=["todo"],
                rooms=[],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )
    persist_entity_accounts(config, runtime_paths)
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name="code",
        runtime_paths=runtime_paths,
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="openai-session",
    )

    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    build = create_agent(
        "code",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        include_openai_compat_guidance=True,
        knowledge=None,
        model=turn_model(config, runtime_paths, "code"),
        plugins=config_plugins(config, runtime_paths),
    )

    exposed_tool_names = _offered_function_names(build)
    assert "todo" not in {toolkit.name for toolkit in build.spec.toolkits}
    assert (
        not {
            "add_todo",
            "apply_template",
            "list_templates",
            "list_todos",
            "plan",
            "update_todo",
        }
        & exposed_tool_names
    )


def test_openai_compatible_dynamic_tools_hide_deferred_context_bound_todo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI-compatible tool search should neither offer nor find context-bound todo."""
    runtime_paths = constants.resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    config = Config(
        agents={
            "code": AgentConfig(
                display_name="CodeAgent",
                role="Generate code and manage files",
                tools=[
                    ToolConfigEntry(name="todo", defer=True),
                    ToolConfigEntry(name="sleep", defer=True),
                ],
                rooms=[],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )
    persist_entity_accounts(config, runtime_paths)
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name="code",
        runtime_paths=runtime_paths,
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="openai-session",
    )

    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    build = create_agent(
        "code",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        include_openai_compat_guidance=True,
        knowledge=None,
        model=turn_model(config, runtime_paths, "code"),
        plugins=config_plugins(config, runtime_paths),
    )

    toolkits = {toolkit.name: toolkit for toolkit in build.spec.toolkits}
    assert "todo" not in toolkits
    [tool_search] = [function for function in toolkits["dynamic_tools"].functions if function.name == "tool_search"]
    search_result = json.loads(tool_search.entrypoint("todo"))

    assert search_result["matches"] == []
    assert build.request_template.deferred_tools.names == frozenset({"sleep"})


def test_openai_compatible_agent_hides_script_gated_tools(
    test_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Script-based approval rules should also hide matching /v1 tools."""
    runtime_paths = constants.resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    approval_script = tmp_path / "approval_scripts" / "shell_review.py"
    approval_script.parent.mkdir(parents=True)
    approval_script.write_text(
        "def check(tool_name, arguments, agent_name):\n    return tool_name == 'run_shell_command'\n",
        encoding="utf-8",
    )
    config = Config.validate_with_runtime(
        {
            **test_config.authored_model_dump(),
            "tool_approval": {
                "rules": [{"match": "run_shell_command", "script": "approval_scripts/shell_review.py"}],
            },
        },
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths)
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name="code",
        runtime_paths=runtime_paths,
        requester_id=None,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="openai-session",
    )

    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    build = create_agent(
        "code",
        config=config,
        runtime_paths=runtime_paths,
        execution_identity=execution_identity,
        include_openai_compat_guidance=True,
        knowledge=None,
        model=turn_model(config, runtime_paths, "code"),
        plugins=config_plugins(config, runtime_paths),
    )

    exposed_tool_names = _offered_function_names(build)
    assert "run_shell_command" not in exposed_tool_names


def test_chat_completions_keeps_auth_runtime_bound_across_runtime_swap(test_config: Config) -> None:
    """Chat completions should parse against the same runtime that authenticated the request."""
    runtime_a = _runtime_paths({"OPENAI_COMPAT_API_KEYS": "old-key"})
    runtime_b = _runtime_paths({"OPENAI_COMPAT_API_KEYS": "new-key"})

    def _authenticate_and_swap(
        _request: Request,
        authorization: str | None,
        runtime_paths: RuntimePaths,
    ) -> openai_compat._AuthenticatedCaller:
        assert authorization == "Bearer old-key"
        assert runtime_paths == runtime_a
        # The swapped-in runtime publishes no config, so a read from it would answer 500.
        initialize_api_app(client.app, runtime_b)
        return openai_compat._AuthenticatedCaller(session_namespace="old-key-namespace", requester_id=None)

    completion = AsyncMock(return_value=openai_compat.OpenAIJSONResponse({"ok": True}))
    with (
        patch("mindroom.api.openai_compat._authenticate_request", side_effect=_authenticate_and_swap),
        patch("mindroom.api.openai_compat._non_stream_completion", new=completion),
        _openai_client(test_config, runtime_a) as client,
    ):
        response = client.post(
            "/v1/chat/completions",
            headers={"authorization": "Bearer old-key"},
            json={"model": "general", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    _turn, config, runtime_paths, _cost = completion.await_args.args
    assert (config, runtime_paths) == (test_config, runtime_a)


def test_list_models_tolerate_missing_plugin_path(tmp_path: Path) -> None:
    """OpenAI-compatible reads should keep working when plugin loading degrades."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-6-astra\n"
        "router:\n"
        "  model: default\n"
        "agents: {}\n"
        "plugins:\n"
        "  - ./plugins/missing\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
    )

    with _openai_client(None, runtime_paths) as client:
        assert config_lifecycle.load_config_into_app(runtime_paths, client.app).loaded
        response = client.get("/v1/models")

    assert response.status_code == 200
    assert response.json()["data"] == []


def test_list_models_returns_malformed_yaml_errors(tmp_path: Path) -> None:
    """OpenAI-compatible routes should surface malformed YAML as 422."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents:\n  bad: [\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
    )

    with _openai_client(None, runtime_paths) as client:
        assert not config_lifecycle.load_config_into_app(runtime_paths, client.app).loaded
        response = client.get("/v1/models")

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]


def test_chat_completions_tolerate_missing_plugin_path_during_model_validation(tmp_path: Path) -> None:
    """Chat completions should reach normal request validation when plugin loading degrades."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "models:\n"
        "  default:\n"
        "    provider: openai\n"
        "    id: gpt-6-astra\n"
        "router:\n"
        "  model: default\n"
        "agents:\n"
        "  general:\n"
        "    display_name: General\n"
        "    role: helper\n"
        "    rooms: []\n"
        "plugins:\n"
        "  - ./plugins/missing\n",
        encoding="utf-8",
    )
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
    )

    with _openai_client(None, runtime_paths) as client:
        assert config_lifecycle.load_config_into_app(runtime_paths, client.app).loaded
        response = client.post(
            "/v1/chat/completions",
            json={"model": "missing-model", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 404
    assert "Model 'missing-model' not found" in response.json()["error"]["message"]


def test_chat_completions_returns_malformed_yaml_errors(tmp_path: Path) -> None:
    """Chat completions should surface malformed YAML as 422."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents:\n  bad: [\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(
        config_path=config_path,
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
    )

    with _openai_client(None, runtime_paths) as client:
        assert not config_lifecycle.load_config_into_app(runtime_paths, client.app).loaded
        response = client.post(
            "/v1/chat/completions",
            json={"model": "general", "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 422
    assert "Could not parse configuration YAML" in response.json()["detail"][0]["msg"]


def test_openai_incompatible_agents_is_order_independent_for_cycles() -> None:
    """Cyclic delegation should not change which /v1 agents are rejected."""
    config = Config(
        agents={
            "a": AgentConfig(
                display_name="A",
                role="Private agent",
                rooms=[],
                private={"per": "user"},
                delegate_to=["b"],
            ),
            "b": AgentConfig(
                display_name="B",
                role="Delegating agent",
                rooms=[],
                delegate_to=["a"],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )

    assert openai_compat.openai_incompatible_agents(["a", "b"], config) == ["a", "b"]
    assert openai_compat.openai_incompatible_agents(["b", "a"], config) == ["b", "a"]


# ---------------------------------------------------------------------------
# GET /v1/models
# ---------------------------------------------------------------------------


class TestListModels:
    """Tests for GET /v1/models."""

    def test_lists_agents(self, app_client: TestClient) -> None:
        """Lists all configured agents as models, plus auto."""
        response = app_client.get("/v1/models")
        assert response.status_code == 200

        data = response.json()
        assert data["object"] == "list"
        model_ids = [m["id"] for m in data["data"]]
        assert "auto" in model_ids
        assert "general" in model_ids
        assert "code" in model_ids
        assert "research" in model_ids

    def test_includes_name_and_description(self, app_client: TestClient) -> None:
        """Models include display name and role description."""
        response = app_client.get("/v1/models")
        data = response.json()

        general = next(m for m in data["data"] if m["id"] == "general")
        assert general["name"] == "GeneralAgent"
        assert general["description"] == "General-purpose assistant"

    def test_an_agent_named_auto_is_not_listed(self, test_config: Config, app_client: TestClient) -> None:
        """The `auto` model id always means automatic routing, so an agent of that name is not offered."""
        test_config.agents["auto"] = AgentConfig(display_name="Auto Agent")

        response = app_client.get("/v1/models")

        assert response.status_code == 200
        assert [(m["id"], m["name"]) for m in response.json()["data"] if m["id"] == "auto"] == [("auto", "Auto")]

    def test_hides_models_with_non_shared_worker_scopes(self, test_config: Config) -> None:
        """Agents and teams that require non-shared worker scopes should not be advertised on /v1."""
        test_config.agents["code"].worker_scope = "user"
        test_config.teams = {
            "dev-team": TeamConfig(
                display_name="Dev Team",
                role="Engineering helpers",
                agents=["general", "code"],
                mode="coordinate",
            ),
        }

        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        with _openai_client(test_config, runtime_paths) as client:
            response = client.get("/v1/models")

        assert response.status_code == 200
        model_ids = {model["id"] for model in response.json()["data"]}
        assert "general" in model_ids
        assert "code" not in model_ids
        assert "team/dev-team" not in model_ids

    def test_hides_agents_that_delegate_to_private_agents(self, test_config: Config) -> None:
        """Shared agents should not be advertised on /v1 when delegation reaches private agents."""
        test_config.agents["research"].private = AgentPrivateConfig(per="user", root="research_data")
        test_config.agents["general"].delegate_to = ["research"]

        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        with _openai_client(test_config, runtime_paths) as client:
            response = client.get("/v1/models")

        assert response.status_code == 200
        model_ids = {model["id"] for model in response.json()["data"]}
        assert "general" not in model_ids
        assert "research" not in model_ids

    def test_hides_auto_model_when_no_openai_compatible_agents(self, test_config: Config) -> None:
        """Auto should not be advertised when no compatible agents can satisfy auto-routing."""
        test_config.agents["general"].worker_scope = "user"
        test_config.agents["code"].worker_scope = "user_agent"
        test_config.agents["research"].worker_scope = "user"

        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        with _openai_client(test_config, runtime_paths) as client:
            response = client.get("/v1/models")

        assert response.status_code == 200
        model_ids = {model["id"] for model in response.json()["data"]}
        assert "auto" not in model_ids

    def test_empty_role_is_none(self, app_client: TestClient) -> None:
        """Agents with empty role have description=None."""
        response = app_client.get("/v1/models")
        data = response.json()

        research = next(m for m in data["data"] if m["id"] == "research")
        assert research["description"] is None

    def test_excludes_router(self, app_client: TestClient) -> None:
        """Router agent is not listed."""
        response = app_client.get("/v1/models")
        data = response.json()
        model_ids = [m["id"] for m in data["data"]]
        assert "router" not in model_ids

    def test_auto_model_listed_first(self, app_client: TestClient) -> None:
        """Auto model is listed first with description."""
        response = app_client.get("/v1/models")
        data = response.json()
        first = data["data"][0]
        assert first["id"] == "auto"
        assert first["name"] == "Auto"
        assert "routes" in first["description"].lower() or "auto" in first["description"].lower()

    def test_empty_agents_list_is_empty(self) -> None:
        """With no agents configured, /v1/models should not advertise auto-routing."""
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        empty_config = Config(
            agents={},
            models={"default": ModelConfig(provider="ollama", id="test")},
            router=RouterConfig(model="default"),
        )
        with _openai_client(empty_config, runtime_paths) as client:
            response = client.get("/v1/models")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data == []


class TestChatCompletions:
    """Tests for POST /v1/chat/completions (non-streaming)."""

    def test_basic_completion(self, app_client: TestClient) -> None:
        """Basic non-streaming completion returns correct shape."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Hello! How can I help?"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "chat.completion"
        assert data["model"] == "general"
        assert data["id"].startswith("chatcmpl-")
        assert len(data["choices"]) == 1
        assert data["choices"][0]["message"]["role"] == "assistant"
        assert data["choices"][0]["message"]["content"] == "Hello! How can I help?"
        assert data["choices"][0]["finish_reason"] == "stop"
        assert data["usage"]["prompt_tokens"] == 0
        assert mock_ai.call_args.args[0].show_tool_calls is False

    def test_non_stream_completion_reports_run_token_usage(self, app_client: TestClient) -> None:
        """Non-streaming usage comes from the run's own token metrics instead of zeros."""
        result = TurnResult(
            text="Hello!",
            completed=CompletedAttempt(
                build_run_metadata=lambda: {
                    constants.AI_RUN_METADATA_KEY: {
                        "usage": {"input_tokens": 1200, "output_tokens": 34, "total_tokens": 1234},
                    },
                },
            ),
        )

        with patch("mindroom.api.openai_compat.ai_response", new=AsyncMock(return_value=result)):
            response = app_client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hello"}]},
            )

        assert response.status_code == 200
        assert response.json()["usage"] == {"prompt_tokens": 1200, "completion_tokens": 34, "total_tokens": 1234}

    def test_non_stream_usage_counts_cached_prompt_tokens_for_anthropic(self, app_client: TestClient) -> None:
        """Anthropic reports cache reads and writes outside input_tokens; prompt_tokens must include them."""
        result = TurnResult(
            text="Hello!",
            completed=CompletedAttempt(
                build_run_metadata=partial(
                    build_ai_run_metadata_content,
                    prices={},
                    config=Config(
                        agents={},
                        models={"default": ModelConfig(provider="anthropic", id="claude-sonnet-5-5")},
                        router=RouterConfig(model="default"),
                    ),
                    model_name="default",
                    run_id="run-1",
                    session_id="session-1",
                    status=RunStatus.completed,
                    model="claude-sonnet-5-5",
                    model_provider="Anthropic",
                    tool_count=0,
                    metrics=RunMetrics(
                        tokens=TokenCounts(
                            input_tokens=40,
                            output_tokens=200,
                            total_tokens=240,
                            cache_read_tokens=30000,
                            cache_write_tokens=500,
                        ),
                    ),
                ),
            ),
        )

        with patch("mindroom.api.openai_compat.ai_response", new=AsyncMock(return_value=result)):
            response = app_client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hello"}]},
            )

        assert response.status_code == 200
        assert response.json()["usage"] == {"prompt_tokens": 30540, "completion_tokens": 200, "total_tokens": 30740}

    def test_completion_lock_releases_when_request_is_cancelled(self, app_client: TestClient) -> None:
        """Cancellation after lock acquisition must not leave the OpenAI session locked."""
        completion_lock = asyncio.Lock()

        async def cancelled_response(*_args: object, **_kwargs: object) -> str:
            raise asyncio.CancelledError

        with (
            patch("mindroom.api.openai_compat._openai_completion_lock", return_value=completion_lock),
            patch("mindroom.api.openai_compat.ai_response", side_effect=completing(cancelled_response)),
            pytest.raises((asyncio.CancelledError, FutureCancelledError)),
        ):
            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert not completion_lock.locked()

    def test_does_not_pass_include_default_tools_flag(self, app_client: TestClient) -> None:
        """Default tool behavior is now resolved from agent config, not a runtime flag."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

            assert "include_default_tools" not in mock_ai.call_args.kwargs
            assert mock_ai.call_args.args[0].include_interactive_questions is False
            assert mock_ai.call_args.args[0].active_event_ids == frozenset()

    def test_agent_completion_mints_fresh_uuid_correlation_id(self, app_client: TestClient) -> None:
        """Each OpenAI-compatible agent completion mints its own uuid-hex correlation id."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Response"

            for _ in range(2):
                app_client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "general",
                        "messages": [{"role": "user", "content": "Hi"}],
                    },
                )

            first_ctx, second_ctx = (call.args[0] for call in mock_ai.call_args_list)
            assert re.fullmatch(r"[0-9a-f]{32}", first_ctx.correlation_id)
            assert re.fullmatch(r"[0-9a-f]{32}", second_ctx.correlation_id)
            assert first_ctx.correlation_id != second_ctx.correlation_id

    def test_passes_knowledge_none(self, app_client: TestClient) -> None:
        """Passes knowledge=None when agent has no knowledge_bases."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

            assert mock_ai.call_args.args[0].knowledge is None

    def test_request_body_user_is_not_passed_as_ai_requester(self, app_client: TestClient) -> None:
        """OpenAI user must not be written as Matrix requester metadata."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "user": "user-123",
                },
            )

            assert mock_ai.call_args.args[0].requester_id is None

    def test_request_body_user_is_not_used_for_openai_execution_identity(self, app_client: TestClient) -> None:
        """The OpenAI user field should not become credential-routing identity."""
        seen_requester_ids: list[str | None] = []

        async def _capture(*args: object, **kwargs: object) -> str:  # noqa: ARG001
            identity = current_execution_identity()
            seen_requester_ids.append(identity.requester_id if identity is not None else None)
            return "Response"

        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = completing(_capture)

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "user": "spoofed-user",
                },
            )

        assert response.status_code == 200
        assert seen_requester_ids == [None]

    def test_non_stream_completion_keeps_execution_identity_for_ai_response(self, app_client: TestClient) -> None:
        """Non-stream agent responses must keep execution identity active through ai_response."""
        observed_agent_names: list[str | None] = []
        observed_session_ids: list[str | None] = []

        async def _capture(*args: object, **kwargs: object) -> str:  # noqa: ARG001
            identity = current_execution_identity()
            observed_agent_names.append(identity.agent_name if identity is not None else None)
            observed_session_ids.append(identity.session_id if identity is not None else None)
            return "Response"

        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = completing(_capture)

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert response.status_code == 200
        assert observed_agent_names == ["general"]
        assert len(observed_session_ids) == 1
        assert observed_session_ids[0] is not None

    def test_agent_completion_does_not_require_persisted_matrix_accounts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OpenAI-compatible agent completions should not prepare or require Matrix accounts."""
        config = Config(
            agents={
                "general": AgentConfig(
                    display_name="GeneralAgent",
                    role="General-purpose assistant",
                    rooms=[],
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "data",
            process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
        )

        install_model_providers(monkeypatch, {"default": ScriptedModel([answer("Agent reply")])})
        with (
            patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
            _openai_client(config, runtime_paths) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "Agent reply"
        assert not constants.matrix_state_file(runtime_paths=runtime_paths).exists()

    @pytest.mark.parametrize("stream", [False, True])
    def test_a_completion_that_fails_after_its_answer_began_reports_the_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stream: bool,
    ) -> None:
        """A turn that fails after streaming part of its answer reports the failure in either mode.

        A blocking completion answers with an error, since it has sent nothing yet; a streamed one already sent its
        status, so it sends the partial answer and then the failure.
        """
        config = Config(
            agents={"general": AgentConfig(display_name="GeneralAgent", role="General-purpose assistant", rooms=[])},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "data",
            process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
        )

        async def lose_the_provider() -> None:
            msg = "provider connection lost"
            raise ProviderError(msg, provider="Synthetic")

        model = ScriptedModel([[TextDelta("Checking."), Await(lose_the_provider)]])
        install_model_providers(monkeypatch, {"default": model})
        with (
            patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
            _openai_client(config, runtime_paths) as client,
            capture_logs() as logs,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hi"}], "stream": stream},
            )

        if not stream:
            assert response.status_code == 500
            assert response.json()["error"]["message"] == "Agent execution failed"
            assert response.json()["error"]["type"] == "server_error"
            # The warning names why the turn failed, never the answer it had started, which is conversation content.
            [warning] = [log for log in logs if log["event"] == "AI response failed"]
            assert warning["error"] == "provider connection lost"
            assert "Checking." not in str(warning)
            return
        assert response.status_code == 200
        content = "".join(
            chunk["choices"][0]["delta"].get("content", "")
            for line in response.text.splitlines()
            if line.startswith("data: {") and (chunk := json.loads(line.removeprefix("data: ")))
        )
        assert content.startswith("Checking.")
        assert "provider connection lost" in content

    @pytest.mark.parametrize("stream", [False, True])
    def test_a_completion_without_a_provider_key_answers_with_the_setup_guidance(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        stream: bool,
    ) -> None:
        """A turn that fails for want of a provider key answers with how to set one up, in either mode.

        That reply is what the user has to read, so a blocking completion sends it as its answer, not a bare error.
        """
        config = Config(
            agents={"general": AgentConfig(display_name="GeneralAgent", role="General-purpose assistant", rooms=[])},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "data",
            process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
        )

        async def no_key() -> None:
            msg = "no API key"
            raise AuthenticationFailed(msg, provider="openai", status_code=401, code=MISSING_API_KEY)

        install_model_providers(monkeypatch, {"default": ScriptedModel([[Await(no_key)]])})
        with (
            patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
            _openai_client(config, runtime_paths) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hi"}], "stream": stream},
            )

        assert response.status_code == 200
        if stream:
            content = "".join(
                chunk["choices"][0]["delta"].get("content", "")
                for line in response.text.splitlines()
                if line.startswith("data: {") and (chunk := json.loads(line.removeprefix("data: ")))
            )
        else:
            content = response.json()["choices"][0]["message"]["content"]
        assert "No AI provider key is set up yet" in content
        assert "Connect your AI provider" in content

    def test_openai_execution_identity_ignores_request_user(self) -> None:
        """OpenAI-compatible execution identity should not trust the request-body user."""
        identity = build_tool_execution_identity(
            channel="openai_compat",
            agent_name="general",
            session_id="session-123",
            runtime_paths=resolve_runtime_paths(process_env={}),
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
        )
        assert identity.requester_id is None

    def test_requester_header_is_not_used_for_execution_identity(self, authed_client: TestClient) -> None:
        """Caller-supplied requester headers must not become `/v1` execution identity."""
        seen_requester_ids: list[str | None] = []

        async def _capture(*args: object, **kwargs: object) -> str:  # noqa: ARG001
            identity = current_execution_identity()
            seen_requester_ids.append(identity.requester_id if identity is not None else None)
            return "Response"

        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = completing(_capture)

            response = authed_client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test-key-1",
                    "X-Mindroom-Requester-Id": "@alice:example.com",
                },
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert response.status_code == 200
        assert seen_requester_ids == [None]

    def test_rejects_non_shared_worker_scope_agent(self, test_config: Config) -> None:
        """Explicit agent requests should fail on /v1 when worker_scope is not shared."""
        test_config.agents["general"].worker_scope = "user"
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        with _openai_client(test_config, runtime_paths) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "unsupported_worker_scope"
        assert "unscoped or configured with worker_scope=shared" in error["message"]
        assert "general" in error["message"]

    def test_rejects_non_shared_worker_scope_team(self, test_config: Config) -> None:
        """Team requests should fail on /v1 when any member requires a non-shared worker scope."""
        test_config.agents["code"].worker_scope = "user_agent"
        test_config.teams = {
            "dev-team": TeamConfig(
                display_name="Dev Team",
                role="Engineering helpers",
                agents=["general", "code"],
                mode="coordinate",
            ),
        }
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        with _openai_client(test_config, runtime_paths) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/dev-team",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "unsupported_worker_scope"
        assert "unscoped or configured with worker_scope=shared" in error["message"]
        assert "code" in error["message"]

    def test_rejects_agent_that_delegates_to_private_agent(self, test_config: Config) -> None:
        """Shared agents should fail on /v1 when delegation reaches a private agent."""
        test_config.agents["research"].private = AgentPrivateConfig(per="user", root="research_data")
        test_config.agents["general"].delegate_to = ["research"]

        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        with _openai_client(test_config, runtime_paths) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert response.status_code == 400
        error = response.json()["error"]
        assert error["code"] == "unsupported_worker_scope"
        assert "Requester-private agents configured with private.per are not yet supported on /v1." in error["message"]
        assert "Delegation reaches unsupported agents: research" in error["message"]

    def test_auto_route_errors_when_no_openai_compatible_agents(self, test_config: Config) -> None:
        """Auto-routing should fail when all agents require unsupported worker scopes."""
        test_config.agents["general"].worker_scope = "user"
        test_config.agents["code"].worker_scope = "user_agent"
        test_config.agents["research"].worker_scope = "user"
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        with _openai_client(test_config, runtime_paths) as client:
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Route me"}],
                },
            )

        assert response.status_code == 500
        assert response.json()["error"]["message"] == "No OpenAI-compatible agents configured for auto-routing"

    def test_explicit_session_id_header_is_stable_across_turns(self, app_client: TestClient) -> None:
        """Repeated requests with the same X-Session-Id should reuse one derived session ID."""
        observed_session_ids: list[str] = []

        async def _capture(*args: object, **kwargs: object) -> str:  # noqa: ARG001
            observed_session_ids.append(args[0].session_id)
            return "Response"

        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = completing(_capture)

            app_client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "shared-session"},
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Turn one"}],
                },
            )
            app_client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "shared-session"},
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Turn two"}],
                },
            )

        assert len(observed_session_ids) == 2
        assert observed_session_ids[0] == observed_session_ids[1]
        assert observed_session_ids[0].endswith(":shared-session")

    def test_explicit_session_id_header_is_namespaced_by_api_key(self, authed_client: TestClient) -> None:
        """Same X-Session-Id with different API keys should map to different derived IDs."""
        observed_session_ids: list[str] = []

        async def _capture(*args: object, **kwargs: object) -> str:  # noqa: ARG001
            observed_session_ids.append(args[0].session_id)
            return "Response"

        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.side_effect = completing(_capture)

            authed_client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test-key-1",
                    "X-Session-Id": "shared-session",
                },
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Turn one"}],
                },
            )
            authed_client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer test-key-2",
                    "X-Session-Id": "shared-session",
                },
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Turn two"}],
                },
            )

        assert len(observed_session_ids) == 2
        assert observed_session_ids[0] != observed_session_ids[1]
        assert observed_session_ids[0].endswith(":shared-session")
        assert observed_session_ids[1].endswith(":shared-session")

    def test_unknown_model_404(self, app_client: TestClient) -> None:
        """Unknown model returns 404 with OpenAI error format."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "nonexistent",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 404
        data = response.json()
        assert data["error"]["code"] == "model_not_found"
        assert data["error"]["param"] == "model"
        assert "nonexistent" in data["error"]["message"]

    def test_router_model_404(self, app_client: TestClient) -> None:
        """Router agent cannot be used as a model."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "router",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 404

    def test_unknown_team_404(self, app_client: TestClient) -> None:
        """Unknown team models return 404 (no teams in test_config)."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "team/nonexistent",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )

        assert response.status_code == 404
        assert "nonexistent" in response.json()["error"]["message"]
        assert response.json()["error"]["code"] == "model_not_found"

    def test_empty_messages_400(self, app_client: TestClient) -> None:
        """Empty messages array returns 400."""
        response = app_client.post(
            "/v1/chat/completions",
            json={
                "model": "general",
                "messages": [],
            },
        )

        assert response.status_code == 400

    def test_extra_fields_ignored(self, app_client: TestClient) -> None:
        """Extra/unknown fields don't cause 422."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Response"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "temperature": 0.7,
                    "max_tokens": 100,
                    "logit_bias": {"42": 10},
                    "seed": 42,
                    "unknown_field": "should be ignored",
                },
            )

        assert response.status_code == 200

    def test_error_response_detection(self, app_client: TestClient) -> None:
        """A turn that completes no attempt failed, so it answers HTTP 500 with a sanitized message."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=AsyncMock) as mock_ai:
            mock_ai.return_value = TurnResult("❌ Authentication failed (openai): Invalid API key")

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500
        error = response.json()["error"]
        assert error["type"] == "server_error"
        # Error message is sanitized — raw backend details are not exposed
        assert error["message"] == "Agent execution failed"

    def test_a_completed_answer_that_starts_like_an_error_is_an_answer(self, app_client: TestClient) -> None:
        """A blocking completion reads failure from the turn's outcome, not from its text."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "⚠️ Error: the log line you asked about means the disk is full."

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"].startswith("⚠️ Error: the log line")


async def _provider_fails() -> None:
    msg = "Connection reset by peer"
    raise ConnectionError(msg)


def _post_to_a_real_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    script: Sequence[ScriptStep],
    *,
    stream: bool,
) -> Response:
    """Post one completion that runs the real agent turn driver over ``script``."""
    config = Config(
        agents={"general": AgentConfig(display_name="GeneralAgent", role="General-purpose assistant", rooms=[])},
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "data",
        process_env={"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"},
    )
    install_model_providers(monkeypatch, {"default": ScriptedModel([list(script)])})
    with (
        patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        _openai_client(config, runtime_paths) as client,
    ):
        return client.post(
            "/v1/chat/completions",
            json={"model": "general", "messages": [{"role": "user", "content": "Hi"}], "stream": stream},
        )


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "whole"])
def test_a_turn_whose_provider_fails_before_any_text_is_a_500(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """A provider failure before the first token fails the request, streamed or not."""
    response = _post_to_a_real_turn(tmp_path, monkeypatch, [Await(_provider_fails)], stream=stream)

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Agent execution failed"


def test_a_whole_turn_that_fails_after_partial_text_is_a_500(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Narration before a failure does not make the collected reply a successful completion."""
    response = _post_to_a_real_turn(
        tmp_path,
        monkeypatch,
        [TextDelta("Checking..."), Await(_provider_fails)],
        stream=False,
    )

    assert response.status_code == 500
    assert response.json()["error"]["message"] == "Agent execution failed"


def test_a_streamed_turn_that_fails_after_partial_text_ends_with_its_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once text streamed, a failure is the stream's last content chunk, and nothing follows it as an answer."""
    response = _post_to_a_real_turn(
        tmp_path,
        monkeypatch,
        [TextDelta("Checking..."), Await(_provider_fails)],
        stream=True,
    )

    assert response.status_code == 200
    contents = [
        delta["content"]
        for line in response.text.strip().split("\n\n")
        if line.startswith("data: {")
        for delta in [json.loads(line.removeprefix("data: "))["choices"][0]["delta"]]
        if "content" in delta
    ]
    assert contents[0] == "Checking..."
    assert contents[-1] == "\n\n[general] ⚠️ Error: Connection reset by peer"


@dataclass
class _ClosingModel(ScriptedModel):
    """A scripted route adapter that records when its stream closes."""

    closed: bool = False

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        """Yield the scripted turn, noting the stream's close."""
        try:
            async for event in super().stream(request):
                yield event
        finally:
            self.closed = True


class TestStreamingCompletion:
    """Tests for POST /v1/chat/completions with stream=true."""

    def test_streaming_sse_format(self, app_client: TestClient) -> None:
        """Streaming returns valid SSE format."""

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
            yield TextDelta("Hello ")
            yield TextDelta("world!")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # Parse SSE lines
        lines = response.text.strip().split("\n\n")
        assert len(lines) >= 4  # role + 2 content + finish + [DONE]

        # First chunk: role announcement
        first = json.loads(lines[0].removeprefix("data: "))
        assert first["choices"][0]["delta"] == {"role": "assistant"}
        assert first["object"] == "chat.completion.chunk"

        # Content chunks
        second = json.loads(lines[1].removeprefix("data: "))
        assert second["choices"][0]["delta"]["content"] == "Hello "

        third = json.loads(lines[2].removeprefix("data: "))
        assert third["choices"][0]["delta"]["content"] == "world!"

        # Finish chunk
        fourth = json.loads(lines[3].removeprefix("data: "))
        assert fourth["choices"][0]["finish_reason"] == "stop"
        assert fourth["choices"][0]["delta"] == {}

        # [DONE] terminator
        assert lines[4] == "data: [DONE]"

    @pytest.mark.parametrize(
        ("deltas", "expected"),
        [((), ["Whole answer"]), (("Whole ", "answer"), ["Whole ", "answer"])],
        ids=["no-deltas", "streamed"],
    )
    def test_streaming_sends_a_finished_reply_that_streamed_no_text_once(
        self,
        app_client: TestClient,
        deltas: tuple[str, ...],
        expected: list[str],
    ) -> None:
        """A run that streamed no text sends its finished reply as one chunk; a streamed reply is not repeated."""

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta | RunFinished]:
            for delta in deltas:
                yield TextDelta(delta)
            yield RunFinished(RunRecord(run_id="run", status=RunStatus.completed, content="Whole answer"))

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={"model": "general", "messages": [{"role": "user", "content": "Hello"}], "stream": True},
            )

        assert response.status_code == 200
        assert _sse_contents(response.text) == expected

    def test_streaming_passes_include_interactive_questions_false(self, app_client: TestClient) -> None:
        """Streaming disables interactive question prompting for OpenAI compatibility."""

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
            yield TextDelta("ok")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream) as mock_stream_fn:
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "include_default_tools" not in mock_stream_fn.call_args.kwargs
        assert mock_stream_fn.call_args.args[0].include_interactive_questions is False
        assert mock_stream_fn.call_args.args[0].active_event_ids == frozenset()

    def test_streaming_consistent_id(self, app_client: TestClient) -> None:
        """All streaming chunks have the same completion ID."""

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
            yield TextDelta("test")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        lines = response.text.strip().split("\n\n")
        ids = []
        for line in lines:
            text = line.removeprefix("data: ")
            if text == "[DONE]":
                continue
            chunk = json.loads(text)
            ids.append(chunk["id"])

        assert len(set(ids)) == 1  # All same ID
        assert ids[0].startswith("chatcmpl-")

    def test_streaming_keeps_execution_identity_for_full_stream(self, app_client: TestClient) -> None:
        """Worker-routing identity must stay active after the first streamed event."""
        observed_session_ids: list[str | None] = []

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
            identity = current_execution_identity()
            observed_session_ids.append(identity.session_id if identity is not None else None)
            yield TextDelta("Hello ")

            identity = current_execution_identity()
            observed_session_ids.append(identity.session_id if identity is not None else None)
            yield TextDelta("world!")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert len(observed_session_ids) == 2
        assert all(session_id is not None for session_id in observed_session_ids)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
    async def test_prefetched_provider_stream_completes_under_asgi(
        self,
        test_config: Config,
        spec_version: str,
    ) -> None:
        """The API's prefetch and the ASGI response may advance the agent's provider stream in different tasks."""
        model = _ClosingModel(
            [
                [
                    TextDelta("Hello"),
                    TextDelta(" world"),
                    TurnCompleted(message=Message(role="assistant", content="Hello world"), stop_reason="end"),
                ],
            ],
        )

        runtime_paths = _runtime_paths()
        with (
            patch("mindroom.providers.registry._build_adapter", return_value=model),
            patch("mindroom.ai.build_memory_prompt_parts", new=AsyncMock(return_value=MemoryPromptParts())),
        ):
            execution_identity = build_tool_execution_identity(
                channel="openai_compat",
                agent_name="general",
                session_id="session-123",
                runtime_paths=runtime_paths,
                requester_id=None,
                room_id=None,
                thread_id=None,
                resolved_thread_id=None,
            )
            response = await openai_compat._stream_completion(
                openai_compat._openai_turn_context(
                    "general",
                    session_id="session-123",
                    model_name="default",
                    execution_identity=execution_identity,
                    prompt="Hello",
                ),
                test_config,
                runtime_paths,
                openai_compat._CompletionCost(),
            )

        assert isinstance(response, openai_compat.OpenAIStreamingResponse)
        sent: list[bytes] = []

        async def receive() -> dict[str, object]:
            await asyncio.Event().wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.body":
                sent.append(cast("bytes", message["body"]))

        await response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send)

        assert model.closed
        assert response.completion_predicate is not None
        assert response.completion_predicate()
        # The response, not only its body, closes the agent stream, so a body that never started cannot leave it open.
        assert response.close_stream is not None
        body = b"".join(sent).decode()
        assert '"content":"Hello"' in body
        assert '"content":" world"' in body
        assert body.endswith("data: [DONE]\n\n")

    @pytest.mark.asyncio
    async def test_streaming_close_from_other_task_keeps_execution_identity(self, test_config: Config) -> None:
        """Closing the SSE body from another task should still clean up inside the execution identity."""
        runtime_paths = _runtime_paths()
        execution_identity = build_tool_execution_identity(
            channel="openai_compat",
            agent_name="general",
            session_id="session-123",
            runtime_paths=runtime_paths,
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
        )
        observed_final_identities: list[ToolExecutionIdentity | None] = []

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
            try:
                assert current_execution_identity() == execution_identity
                yield TextDelta("Hello")
                await asyncio.Future()
            finally:
                observed_final_identities.append(current_execution_identity())

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = await openai_compat._stream_completion(
                openai_compat._openai_turn_context(
                    "general",
                    session_id="session-123",
                    model_name="default",
                    execution_identity=execution_identity,
                    prompt="Hello",
                ),
                test_config,
                runtime_paths,
                openai_compat._CompletionCost(),
            )

        assert isinstance(response, StreamingResponse)
        body_iterator = response.body_iterator
        await asyncio.create_task(anext(body_iterator), context=Context())
        await asyncio.create_task(body_iterator.aclose(), context=Context())
        assert observed_final_identities == [execution_identity]

    def test_streaming_first_event_failure_returns_500(self, app_client: TestClient) -> None:
        """A stream that starts with a failed turn's reply is HTTP 500 instead of SSE, whatever the reply's wording."""

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[FailedReply]:
            yield FailedReply("The run could not finish.", RuntimeError("boom"))

        with (
            patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream),
            capture_logs() as logs,
        ):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 500
        error = response.json()["error"]
        assert error["type"] == "server_error"
        assert error["message"] == "Agent execution failed"
        # The warning names why the turn failed, as a blocking completion's does.
        [warning] = [log for log in logs if log["event"] == "Stream returned error"]
        assert warning["error"] == "boom"

    def test_a_streamed_answer_that_starts_like_an_error_is_the_answer(self, app_client: TestClient) -> None:
        """An answer's wording never makes it a failure: only a failed turn's reply is one."""

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
            yield TextDelta("⚠️ Error: the log line you asked about means the disk is full.")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        lines = response.text.strip().split("\n\n")
        content_chunk = json.loads(lines[1].removeprefix("data: "))
        assert content_chunk["choices"][0]["delta"]["content"].startswith("⚠️ Error: the log line")

    @pytest.mark.asyncio
    async def test_a_failure_after_streamed_text_ends_the_stream_uncompleted(self, test_config: Config) -> None:
        """A failed turn's reply after streamed text is the last content chunk, and the stream does not count as done.

        Its warning names why the turn failed, never the reply, as a blocking completion's does.
        """

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta | FailedReply]:
            yield TextDelta("Checking.")
            yield FailedReply("\n\n⚠️ Error: provider connection lost", RuntimeError("provider connection lost"))

        runtime_paths = _runtime_paths()
        execution_identity = build_tool_execution_identity(
            channel="openai_compat",
            agent_name="general",
            session_id="session-123",
            runtime_paths=runtime_paths,
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
        )
        turn = openai_compat._openai_turn_context(
            "general",
            session_id="session-123",
            execution_identity=execution_identity,
        )
        with (
            patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream),
            capture_logs() as logs,
        ):
            response = await openai_compat._stream_completion(
                turn,
                test_config,
                runtime_paths,
                openai_compat._CompletionCost(),
            )
            assert isinstance(response, openai_compat.OpenAIStreamingResponse)
            body = [cast("str", chunk) async for chunk in response.body_iterator]

        contents = [
            delta["content"]
            for line in body
            if line.startswith("data: {")
            for delta in [json.loads(line.removeprefix("data: "))["choices"][0]["delta"]]
            if "content" in delta
        ]
        assert contents == ["Checking.", "\n\n⚠️ Error: provider connection lost"]
        assert response.completion_predicate is not None
        assert not response.completion_predicate()
        [warning] = [log for log in logs if log["event"] == "Stream emitted terminal failure"]
        assert warning["error"] == "provider connection lost"

    def test_streaming_tool_events(self, app_client: TestClient) -> None:
        """Streaming emits start/done tool blocks with a stable per-stream tool id."""
        tool_started = ToolExecution(
            tool_name="search",
            tool_args={"query": "X"},
            tool_call_id="tc-stream-1",
        )
        tool_completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "X"},
            tool_call_id="tc-stream-1",
            result="3 results",
        )

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[object]:
            yield TextDelta("Let me search. ")
            yield ToolStarted(tool_started.call)
            yield ToolCompleted(tool_completed.call)
            yield TextDelta("Found it!")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Search for X"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        full_content = "".join(_sse_contents(response.text))
        assert "Let me search. " in full_content
        assert '<tool id="1" state="start">search(query=X)</tool>' in full_content
        assert '<tool id="1" state="done">search(query=X)\n3 results</tool>' in full_content
        assert "Result:" not in full_content

    def test_streaming_tool_events_escape_payload_and_truncate_result(self, app_client: TestClient) -> None:
        """Tool SSE payloads escape XML and keep large results bounded."""
        tool_started = ToolExecution(
            tool_name="search",
            tool_args={"query": "</tool><b>pwn</b>"},
            tool_call_id="tc-stream-escape-1",
        )
        tool_completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "</tool><b>pwn</b>"},
            tool_call_id="tc-stream-escape-1",
            result="</tool><i>boom</i>" + ("x" * 600),
        )

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[object]:
            yield ToolStarted(tool_started.call)
            yield ToolCompleted(tool_completed.call)

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Search"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        full_content = "".join(_sse_contents(response.text))
        assert full_content.count('<tool id="1" state="start">') == 1
        assert full_content.count('<tool id="1" state="done">') == 1
        assert "query=&lt;/tool&gt;&lt;b&gt;pwn&lt;/b&gt;" in full_content
        assert "&lt;/tool&gt;&lt;i&gt;boom&lt;/i&gt;" in full_content
        assert "</tool><b>pwn</b>" not in full_content
        assert "</tool><i>boom</i>" not in full_content
        assert "…" in full_content
        assert ("x" * 550) not in full_content

    def test_streaming_tool_ids_increment_for_multiple_calls(self, app_client: TestClient) -> None:
        """Tool ids start at 1 and increment for each new started tool call in a stream."""
        first_started = ToolExecution(
            tool_name="search",
            tool_args={"query": "one"},
            tool_call_id="tc-stream-1",
        )
        first_completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "one"},
            tool_call_id="tc-stream-1",
            result="one-result",
        )
        second_started = ToolExecution(
            tool_name="search",
            tool_args={"query": "two"},
            tool_call_id="tc-stream-2",
        )
        second_completed = ToolExecution(
            tool_name="search",
            tool_args={"query": "two"},
            tool_call_id="tc-stream-2",
            result="two-result",
        )

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[object]:
            yield TextDelta("Start ")
            yield ToolStarted(first_started.call)
            yield ToolCompleted(first_completed.call)
            yield ToolStarted(second_started.call)
            yield ToolCompleted(second_completed.call)
            yield TextDelta("End")

        with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Run two searches"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        full_content = "".join(_sse_contents(response.text))
        assert '<tool id="1" state="start">search(query=one)</tool>' in full_content
        assert '<tool id="1" state="done">search(query=one)\none-result</tool>' in full_content
        assert '<tool id="2" state="start">search(query=two)</tool>' in full_content
        assert '<tool id="2" state="done">search(query=two)\ntwo-result</tool>' in full_content


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Tests for bearer token authentication."""

    def test_valid_key_accepted(self, authed_client: TestClient) -> None:
        """Valid API key allows access."""
        response = authed_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-key-1"},
        )
        assert response.status_code == 200

    def test_second_key_accepted(self, authed_client: TestClient) -> None:
        """Second key from comma-separated list works."""
        response = authed_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer test-key-2"},
        )
        assert response.status_code == 200

    def test_missing_key_401(self, authed_client: TestClient) -> None:
        """Missing key returns 401."""
        response = authed_client.get("/v1/models")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_wrong_key_401(self, authed_client: TestClient) -> None:
        """Wrong key returns 401."""
        response = authed_client.get(
            "/v1/models",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    def test_auth_required_when_keys_unset(self) -> None:
        """Auth is required by default when OPENAI_COMPAT_API_KEYS is unset."""
        runtime_paths = _runtime_paths(
            {
                "OPENAI_COMPAT_API_KEYS": "",
                "OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "false",
            },
        )
        with _openai_client(None, runtime_paths) as client:
            response = client.get("/v1/models")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_missing_key_401_does_not_load_config_first(self) -> None:
        """Missing auth should fail before config loading."""
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_API_KEYS": "test-key-1"})

        # No config is published, so a route that read config first would answer 500.
        with _openai_client(None, runtime_paths) as client:
            response = client.get("/v1/models")

        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"

    def test_no_auth_when_explicitly_allowed(self, app_client: TestClient) -> None:
        """Unauthenticated mode works when explicitly opted in."""
        response = app_client.get("/v1/models")
        assert response.status_code == 200

    def test_auth_on_completions(self, authed_client: TestClient) -> None:
        """Auth is checked on completions endpoint too."""
        response = authed_client.post(
            "/v1/chat/completions",
            json={
                "model": "general",
                "messages": [{"role": "user", "content": "Hi"}],
            },
        )
        assert response.status_code == 401

    def test_unauthenticated_api_refuses_rebound_hosts_and_other_sites(self, app_client: TestClient) -> None:
        """Without a key a completion runs an agent for any caller, so no rebound or cross-site page may start one."""
        body = json.dumps({"model": "general", "messages": [{"role": "user", "content": "Hello"}]})
        # A text/plain POST is a simple request, which a browser sends cross-site without a preflight.
        simple = {"Content-Type": "text/plain"}
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Hello!"
            rebound = app_client.post(
                "/v1/chat/completions",
                content=body,
                headers={**simple, "Host": "attacker.example"},
            )
            cross_origin = app_client.post(
                "/v1/chat/completions",
                content=body,
                headers={**simple, "Origin": "https://attacker.example"},
            )
            cross_site = app_client.post(
                "/v1/chat/completions",
                content=body,
                headers={**simple, "Sec-Fetch-Site": "cross-site"},
            )
            rebound_models = app_client.get("/v1/models", headers={"Host": "attacker.example"})

        statuses = [response.status_code for response in (rebound, cross_origin, cross_site, rebound_models)]
        assert statuses == [400, 403, 403, 400]
        assert "MINDROOM_DASHBOARD_ALLOWED_HOSTS" in rebound.json()["error"]["message"]
        mock_ai.assert_not_awaited()

    def test_unauthenticated_api_serves_local_callers(self, app_client: TestClient) -> None:
        """API clients, the local UI, and callers on an address keep reaching an unauthenticated `/v1`."""
        payload = {"model": "general", "messages": [{"role": "user", "content": "Hello"}]}
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Hello!"
            responses = [
                app_client.post("/v1/chat/completions", json=payload, headers=headers)
                for headers in ({}, {"Origin": "http://localhost"}, {"Host": "192.168.1.20:8765"})
            ]

        assert [response.status_code for response in responses] == [200, 200, 200]

    def test_keyed_api_serves_any_host_and_origin(self, authed_client: TestClient) -> None:
        """A key authorizes a `/v1` request, so the host and origin it names do not matter."""
        response = authed_client.get(
            "/v1/models",
            headers={
                "Authorization": "Bearer test-key-1",
                "Host": "mindroom.internal",
                "Origin": "https://ui.example.org",
            },
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# Message conversion
# ---------------------------------------------------------------------------


class TestMessageConversion:
    """Tests for convert_messages()."""

    def test_simple_user_message(self) -> None:
        """Single user message becomes prompt with no history."""
        messages = [ChatMessage(role="user", content="Hello")]
        prompt, history = convert_messages(messages)
        assert prompt == "Hello"
        assert history is None

    def test_multi_turn_conversation(self) -> None:
        """Multi-turn conversation splits into history + prompt."""
        messages = [
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Hello!"),
            ChatMessage(role="user", content="How are you?"),
        ]
        prompt, history = convert_messages(messages)
        assert prompt == "How are you?"
        assert history == [
            ResolvedVisibleMessage.synthetic(sender="user", body="Hi", event_id="$openai-1", timestamp=1),
            ResolvedVisibleMessage.synthetic(sender="assistant", body="Hello!", event_id="$openai-2", timestamp=2),
        ]

    def test_system_message_prepended(self) -> None:
        """System message is prepended to prompt."""
        messages = [
            ChatMessage(role="system", content="You are helpful."),
            ChatMessage(role="user", content="Hello"),
        ]
        prompt, history = convert_messages(messages)
        assert "You are helpful." in prompt
        assert "Hello" in prompt
        assert history is None

    def test_developer_role_treated_as_system(self) -> None:
        """Developer role is treated same as system."""
        messages = [
            ChatMessage(role="developer", content="Be concise."),
            ChatMessage(role="user", content="Hello"),
        ]
        prompt, _ = convert_messages(messages)
        assert "Be concise." in prompt
        assert "Hello" in prompt

    def test_tool_messages_skipped(self) -> None:
        """Tool role messages are skipped."""
        messages = [
            ChatMessage(role="user", content="Run search"),
            ChatMessage(role="assistant", content="I'll search for that."),
            ChatMessage(role="tool", content="Search results: ..."),
            ChatMessage(role="user", content="Thanks"),
        ]
        prompt, history = convert_messages(messages)
        assert prompt == "Thanks"
        # tool message should not appear in history
        assert history is not None
        assert all(h.sender != "tool" for h in history)

    def test_multimodal_content(self) -> None:
        """Multimodal content extracts text parts."""
        messages = [
            ChatMessage(
                role="user",
                content=[
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                    {"type": "text", "text": "Describe it."},
                ],
            ),
        ]
        prompt, _ = convert_messages(messages)
        assert "What is this?" in prompt
        assert "Describe it." in prompt

    def test_none_content_skipped(self) -> None:
        """Messages with None content are skipped."""
        messages = [
            ChatMessage(role="assistant", content=None),
            ChatMessage(role="user", content="Hello"),
        ]
        prompt, history = convert_messages(messages)
        assert prompt == "Hello"
        assert history is None

    def test_only_system_messages(self) -> None:
        """Only system messages become the prompt."""
        messages = [
            ChatMessage(role="system", content="Be helpful."),
        ]
        prompt, history = convert_messages(messages)
        assert prompt == "Be helpful."
        assert history is None

    def test_conversation_ending_with_assistant(self) -> None:
        """Prompt uses last user message even when conversation ends with assistant."""
        messages = [
            ChatMessage(role="user", content="Hi"),
            ChatMessage(role="assistant", content="Hello! How can I help?"),
        ]
        prompt, history = convert_messages(messages)
        # Last user message is "Hi", not the trailing assistant message
        assert prompt == "Hi"
        assert history is None

    def test_empty_messages(self) -> None:
        """Empty messages returns empty prompt."""
        prompt, history = convert_messages([])
        assert prompt == ""
        assert history is None


# ---------------------------------------------------------------------------
# Session ID derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_completion_lock_releases_after_response_background() -> None:
    """Response background work must start only after the completion lock finalizer runs."""
    events: list[str] = []
    completion_lock = asyncio.Lock()
    await completion_lock.acquire()

    async def existing_background() -> None:
        assert not completion_lock.locked()
        events.append("background")

    response = openai_compat.OpenAIJSONResponse(
        content={"ok": True},
        background=BackgroundTask(existing_background),
    )

    wrapped = openai_compat._attach_openai_completion_lock_release(
        response,
        completion_lock,
        MagicMock(spec=BudgetMonitor),
        None,
        openai_compat._CompletionCost(),
    )

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(_message: dict[str, object]) -> None:
        return None

    await wrapped(
        {"type": "http"},
        receive,
        send,
    )

    assert events == ["background"]
    assert not completion_lock.locked()


@pytest.mark.asyncio
async def test_openai_completion_lock_is_shared_and_dropped_when_unreferenced(tmp_path: Path) -> None:
    """Same-session requests share one lock; cache entries vanish once no request holds the lock."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data")
    key = (str(runtime_paths.storage_root), "general", "session-1")

    lock = openai_compat._openai_completion_lock(
        runtime_paths=runtime_paths,
        agent_name="general",
        session_id="session-1",
    )
    same_session_lock = openai_compat._openai_completion_lock(
        runtime_paths=runtime_paths,
        agent_name="general",
        session_id="session-1",
    )
    assert same_session_lock is lock
    assert key in openai_compat._OPENAI_COMPLETION_LOCKS

    del lock, same_session_lock
    gc.collect()
    assert key not in openai_compat._OPENAI_COMPLETION_LOCKS


@pytest.mark.asyncio
async def test_openai_stream_response_runs_background_when_client_closes_after_done() -> None:
    """A client closing after [DONE] should not skip the response finalizer."""
    events: list[str] = []
    done_sent = False

    async def body() -> AsyncIterator[str]:
        nonlocal done_sent
        yield "data: [DONE]\n\n"
        done_sent = True

    async def background() -> None:
        events.append("background")

    response = openai_compat.OpenAIStreamingResponse(
        body(),
        media_type="text/event-stream",
        background=BackgroundTask(background),
    )
    response.completion_predicate = lambda: done_sent

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body") == b"":
            error_message = "client closed"
            raise OSError(error_message)

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )

    assert events == ["background"]


@pytest.mark.asyncio
async def test_openai_stream_response_skips_background_when_client_closes_before_done() -> None:
    """Incomplete streams must release the lock without running response background work."""
    events: list[str] = []
    done_sent = False
    completion_lock = asyncio.Lock()
    await completion_lock.acquire()

    async def body() -> AsyncIterator[str]:
        nonlocal done_sent
        yield "data: partial\n\n"
        yield "data: [DONE]\n\n"
        done_sent = True

    async def background() -> None:
        events.append("background")

    streaming_response = openai_compat.OpenAIStreamingResponse(
        body(),
        media_type="text/event-stream",
        background=BackgroundTask(background),
    )
    streaming_response.completion_predicate = lambda: done_sent
    response = openai_compat._attach_openai_completion_lock_release(
        streaming_response,
        completion_lock,
        MagicMock(spec=BudgetMonitor),
        None,
        openai_compat._CompletionCost(),
    )

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body") == b"data: partial\n\n":
            error_message = "client closed"
            raise OSError(error_message)

    with pytest.raises(ClientDisconnect):
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}},
            receive,
            send,
        )

    assert events == []
    assert not completion_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_a_stream_the_client_left_closes_while_its_session_is_still_held(spec_version: str) -> None:
    """Closing a stream the client left stores the turn it cut short, so that close ends before the lock releases.

    Otherwise the session's next completion could store its turn first, and replay would read the two out of order.
    """
    completion_lock = asyncio.Lock()
    await completion_lock.acquire()
    held_at_close: list[bool] = []
    partial_sent = asyncio.Event()

    async def body() -> AsyncIterator[str]:
        try:
            yield "data: partial\n\n"
            await asyncio.Event().wait()
            yield "data: [DONE]\n\n"
        finally:
            held_at_close.append(completion_lock.locked())

    streaming_response = openai_compat.OpenAIStreamingResponse(body(), media_type="text/event-stream")
    streaming_response.completion_predicate = lambda: False
    response = openai_compat._attach_openai_completion_lock_release(
        streaming_response,
        completion_lock,
        MagicMock(spec=BudgetMonitor),
        None,
        openai_compat._CompletionCost(),
    )

    async def receive() -> dict[str, str]:
        await partial_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body") == b"data: partial\n\n":
            partial_sent.set()
            if spec_version == "2.4":
                error_message = "client closed"
                raise OSError(error_message)

    with suppress(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": spec_version}}, receive, send)

    assert held_at_close == [True]
    assert not completion_lock.locked()


@pytest.mark.asyncio
async def test_a_stream_whose_headers_never_reached_the_client_closes_while_its_session_is_still_held() -> None:
    """A body that never started cannot close the stream it reads from, so the response closes that stream itself."""
    completion_lock = asyncio.Lock()
    await completion_lock.acquire()
    held_at_close: list[bool] = []

    async def body() -> AsyncIterator[str]:
        yield "data: [DONE]\n\n"

    async def close_stream() -> None:
        held_at_close.append(completion_lock.locked())

    streaming_response = openai_compat.OpenAIStreamingResponse(body(), media_type="text/event-stream")
    streaming_response.completion_predicate = lambda: False
    streaming_response.close_stream = close_stream
    response = openai_compat._attach_openai_completion_lock_release(
        streaming_response,
        completion_lock,
        MagicMock(spec=BudgetMonitor),
        None,
        openai_compat._CompletionCost(),
    )

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            error_message = "client closed"
            raise OSError(error_message)

    with suppress(ClientDisconnect, OSError):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    assert held_at_close == [True]
    assert not completion_lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_send", ["http.response.start", "http.response.body"])
async def test_an_agent_stream_the_client_left_tells_budgets_its_cost_is_unknown(
    test_config: Config,
    failed_send: str,
) -> None:
    """A stream left before its turn ended, even before its body started, reports no cost, so budgets recount it."""

    async def unfinished_turn(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
        yield TextDelta("Checking.")
        await asyncio.Event().wait()

    runtime_paths = _runtime_paths()
    execution_identity = build_tool_execution_identity(
        channel="openai_compat",
        agent_name="general",
        session_id="session-123",
        runtime_paths=runtime_paths,
        requester_id="@alice:localhost",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
    )
    turn = openai_compat._openai_turn_context(
        "general",
        session_id="session-123",
        execution_identity=execution_identity,
    )
    cost = openai_compat._CompletionCost()
    monitor = MagicMock(spec=BudgetMonitor)
    completion_lock = asyncio.Lock()
    await completion_lock.acquire()
    with patch("mindroom.api.openai_compat.stream_agent_response", side_effect=unfinished_turn):
        response = openai_compat._attach_openai_completion_lock_release(
            await openai_compat._stream_completion(turn, test_config, runtime_paths, cost),
            completion_lock,
            monitor,
            "@alice:localhost",
            cost,
        )

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == failed_send:
            error_message = "client closed"
            raise OSError(error_message)

    with suppress(ClientDisconnect, OSError):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

    monitor.response_finished.assert_called_once_with("@alice:localhost", None, cost.started_at)
    assert not completion_lock.locked()


def test_a_team_completion_without_a_run_record_has_an_unknown_cost(test_config: Config) -> None:
    """A team run that failed before handing over its record leaves stored usage no completion can price."""
    record = TeamRunRecord(run_id="team-run", team_id="test-team", status=RunStatus.completed)

    assert openai_compat._team_cost_usd(None, test_config) is None
    assert openai_compat._team_cost_usd(record, test_config) == 0.0


@pytest.mark.asyncio
async def test_openai_stream_response_skips_background_on_asgi20_disconnect_before_done() -> None:
    """ASGI 2.0 disconnect can return normally but still must skip background work."""
    events: list[str] = []
    partial_sent = asyncio.Event()
    continue_stream = asyncio.Event()
    done_sent = False
    completion_lock = asyncio.Lock()
    await completion_lock.acquire()

    async def body() -> AsyncIterator[str]:
        nonlocal done_sent
        yield "data: partial\n\n"
        await continue_stream.wait()
        yield "data: [DONE]\n\n"
        done_sent = True

    async def background() -> None:
        events.append("background")

    streaming_response = openai_compat.OpenAIStreamingResponse(
        body(),
        media_type="text/event-stream",
        background=BackgroundTask(background),
    )
    streaming_response.completion_predicate = lambda: done_sent
    response = openai_compat._attach_openai_completion_lock_release(
        streaming_response,
        completion_lock,
        MagicMock(spec=BudgetMonitor),
        None,
        openai_compat._CompletionCost(),
    )

    async def receive() -> dict[str, str]:
        await partial_sent.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body" and message.get("body") == b"data: partial\n\n":
            partial_sent.set()

    await response(
        {"type": "http", "asgi": {"spec_version": "2.0"}},
        receive,
        send,
    )

    assert done_sent is False
    assert events == []
    assert not completion_lock.locked()


@pytest.mark.asyncio
async def test_openai_json_response_skips_background_when_send_fails() -> None:
    """JSON send failures should release the lock without finalizing an undelivered response."""
    events: list[str] = []
    completion_lock = asyncio.Lock()
    await completion_lock.acquire()

    async def background() -> None:
        events.append("background")

    response = openai_compat._attach_openai_completion_lock_release(
        openai_compat.OpenAIJSONResponse(
            content={"ok": True},
            background=BackgroundTask(background),
        ),
        completion_lock,
        MagicMock(spec=BudgetMonitor),
        None,
        openai_compat._CompletionCost(),
    )

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            error_message = "client closed"
            raise OSError(error_message)

    with pytest.raises(OSError, match="client closed"):
        await response(
            {"type": "http"},
            receive,
            send,
        )

    assert events == []
    assert not completion_lock.locked()


@pytest.mark.asyncio
async def test_openai_error_response_releases_lock_when_send_fails() -> None:
    """Locked error responses should use the same finalizer-safe JSON path."""
    completion_lock = asyncio.Lock()
    await completion_lock.acquire()
    response = openai_compat._attach_openai_completion_lock_release(
        openai_compat.error_response(500, "failed", error_type="server_error"),
        completion_lock,
        MagicMock(spec=BudgetMonitor),
        None,
        openai_compat._CompletionCost(),
    )

    async def receive() -> dict[str, str]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            error_message = "client closed"
            raise OSError(error_message)

    with pytest.raises(OSError, match="client closed"):
        await response(
            {"type": "http"},
            receive,
            send,
        )

    assert not completion_lock.locked()


def _completion_session_ids(client: TestClient, authorization_headers: list[str]) -> list[str]:
    """Post one completion per Authorization header with the same X-Session-Id and return the session IDs."""
    session_ids: list[str] = []

    async def capture(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> str:
        session_ids.append(ctx.session_id)
        return "Response"

    with patch("mindroom.api.openai_compat.ai_response", side_effect=completing(capture)):
        for authorization in authorization_headers:
            response = client.post(
                "/v1/chat/completions",
                headers={"Authorization": authorization, "X-Session-Id": "shared"},
                json={"model": "general", "messages": [{"role": "user", "content": "Hi"}]},
            )
            assert response.status_code == 200
    return session_ids


def test_session_namespace_is_the_full_digest_of_the_validated_key(authed_client: TestClient) -> None:
    """Accepted spellings of one key share its session; another key never does."""
    first, padded, tabbed, other = _completion_session_ids(
        authed_client,
        ["Bearer test-key-1", "Bearer   test-key-1", "Bearer \ttest-key-1", "Bearer test-key-2"],
    )

    assert first == f"{hashlib.sha256(b'test-key-1').hexdigest()}:shared"
    assert padded == tabbed == first
    assert other == f"{hashlib.sha256(b'test-key-2').hexdigest()}:shared"


def test_unauthenticated_session_namespace_ignores_the_authorization_header(app_client: TestClient) -> None:
    """Without configured keys the header authenticates nothing, so it cannot select a session namespace."""
    session_ids = _completion_session_ids(app_client, ["Bearer anything", "Bearer something-else"])

    assert session_ids == ["noauth:shared", "noauth:shared"]


class TestSessionIdDerivation:
    """Tests for derive_session_id()."""

    @staticmethod
    def _mock_request(headers: dict[str, str] | None = None) -> Request:
        """Create a mock Request with given headers."""
        mock = MagicMock(spec=Request)
        header_dict = headers or {}
        mock.headers = {k.lower(): v for k, v in header_dict.items()}
        return mock

    def test_explicit_session_id_header(self) -> None:
        """X-Session-Id header takes highest priority (namespaced with the caller's key)."""
        request = self._mock_request({"X-Session-Id": "my-session"})
        sid = derive_session_id("general", request, "noauth")
        assert sid == "noauth:my-session"

    def test_explicit_session_id_namespaced_by_key(self) -> None:
        """Different key namespaces produce different session IDs for one X-Session-Id."""
        request = self._mock_request({"X-Session-Id": "sess"})
        sid1 = derive_session_id("general", request, "key-1-digest")
        sid2 = derive_session_id("general", request, "key-2-digest")
        assert sid1 == "key-1-digest:sess"
        assert sid2 == "key-2-digest:sess"

    def test_librechat_conversation_id(self) -> None:
        """X-LibreChat-Conversation-Id header is used when no X-Session-Id."""
        request = self._mock_request({"X-LibreChat-Conversation-Id": "conv-123"})
        sid = derive_session_id("general", request, "noauth")
        assert sid == "noauth:conv-123:general"

    def test_session_id_takes_priority_over_librechat(self) -> None:
        """X-Session-Id takes priority over X-LibreChat-Conversation-Id."""
        request = self._mock_request(
            {
                "X-Session-Id": "explicit",
                "X-LibreChat-Conversation-Id": "libre",
            },
        )
        sid = derive_session_id("general", request, "noauth")
        assert "explicit" in sid
        assert "libre" not in sid

    def test_fallback_generates_ephemeral_session_id(self) -> None:
        """Fallback generates an ephemeral namespaced session ID."""
        request = self._mock_request()
        sid1 = derive_session_id("general", request, "noauth")
        assert sid1.startswith("noauth:ephemeral:")
        assert len(sid1) > len("noauth:ephemeral:")

    def test_fallback_is_not_deterministic(self) -> None:
        """Fallback IDs differ across requests to avoid cross-chat collisions."""
        request = self._mock_request()
        sid1 = derive_session_id("general", request, "noauth")
        sid2 = derive_session_id("general", request, "noauth")
        assert sid1 != sid2

    def test_fallback_ignores_user_message_content(self, app_client: TestClient) -> None:
        """Without explicit conversation IDs, each request gets a distinct session ID."""
        session_ids: list[str] = []

        original_derive = derive_session_id

        def capture_session_id(*args: object, **kwargs: object) -> str:
            sid = original_derive(*args, **kwargs)
            session_ids.append(sid)
            return sid

        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
            patch("mindroom.api.openai_compat.derive_session_id", side_effect=capture_session_id),
        ):
            mock_ai.return_value = "Response"

            # First request
            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )
            # Second request with the same first message and extra history
            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [
                        {"role": "user", "content": "Hello"},
                        {"role": "assistant", "content": "Hi!"},
                        {"role": "user", "content": "Different follow-up"},
                    ],
                },
            )

        # Fallback sessions are intentionally distinct to avoid collisions.
        assert len(session_ids) == 2
        assert session_ids[0] != session_ids[1]


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


class TestContentExtraction:
    """Tests for _extract_content_text()."""

    def test_string_content(self) -> None:
        """String content is returned as-is."""
        assert _extract_content_text("Hello") == "Hello"

    def test_none_content(self) -> None:
        """None content returns empty string."""
        assert _extract_content_text(None) == ""

    def test_multimodal_content(self) -> None:
        """Multimodal content concatenates text parts."""
        content: list[dict] = [
            {"type": "text", "text": "First"},
            {"type": "image_url", "image_url": {"url": "..."}},
            {"type": "text", "text": "Second"},
        ]
        assert _extract_content_text(content) == "First Second"

    def test_empty_list(self) -> None:
        """Empty list returns empty string."""
        assert _extract_content_text([]) == ""

    def test_malformed_content_part(self) -> None:
        """Malformed content parts are skipped."""
        content: list[dict] = [
            {"type": "text"},  # missing "text" key
            {"type": "text", "text": "Valid"},
            "not a dict",
        ]
        assert _extract_content_text(content) == "Valid"

    def test_non_string_text_coerced(self) -> None:
        """Non-string text values are coerced to str."""
        content: list[dict] = [
            {"type": "text", "text": 123},
            {"type": "text", "text": "Hello"},
        ]
        assert _extract_content_text(content) == "123 Hello"


# ---------------------------------------------------------------------------
# Auto-routing (Phase 2)
# ---------------------------------------------------------------------------


class TestAutoRouting:
    """Tests for auto-routing via model='auto'."""

    def test_auto_routes_to_suggested_agent(self, app_client: TestClient) -> None:
        """Auto model routes to the agent suggested by suggest_responder()."""
        observed: list[tuple[str | None, str | None]] = []

        async def respond(*_args: object, **_kwargs: object) -> str:
            (identity,) = config_lifecycle.app_state(app_client.app).openai_responses
            observed.append((identity.responder, identity.requester_id))
            return "Here is your code"

        with (
            patch("mindroom.api.openai_compat.suggest_responder", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", side_effect=completing(respond)),
        ):
            mock_route.return_value = ResponderSelection("code")

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Write Python code"}],
                    "user": "@bob:example.org",
                },
            )

        assert response.status_code == 200
        data = response.json()
        # Response model field shows the resolved agent, not "auto"
        assert data["model"] == "code"
        assert data["choices"][0]["message"]["content"] == "Here is your code"
        assert observed == [("code", None)]

    @pytest.mark.parametrize("stream", [False, True])
    def test_auto_no_fit_does_not_fall_back_to_first_agent(
        self,
        test_config: Config,
        monkeypatch: pytest.MonkeyPatch,
        stream: bool,
    ) -> None:
        """An accepted no-fit returns an error before invoking any response agent."""
        test_config.router.judgment = TypeSafeJudgmentConfig(provider="typesafe")
        runtime_paths = _runtime_paths_for_config(
            test_config,
            {"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true", "TYPESAFE_API_KEY": "synthetic"},
        )
        posted = []

        async def post(_self: JudgmentClient, body: bytes) -> bytes:
            payload = json.loads(body)
            posted.append(payload)
            return json.dumps(
                {
                    "model": _PINNED_MODEL,
                    "answers": {
                        "responder_selection": {
                            "type": "choice",
                            "choice": "no_fit",
                            "confidence": 1,
                            "probabilities": {
                                key: int(key == "no_fit")
                                for key in payload["questions"]["responder_selection"]["criteria"]
                            },
                        },
                    },
                    "usage": {"input_tokens": 42, "output_tokens": 1},
                },
            ).encode()

        monkeypatch.setattr(JudgmentClient, "_post", post)
        with (
            _openai_client(test_config, runtime_paths) as app_client,
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as response_agent,
        ):
            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "An unsupported request"}],
                    "stream": stream,
                },
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "no_suitable_responder"
        assert len(posted) == 1
        response_agent.assert_not_called()

    def test_auto_fallback_when_routing_fails(self, app_client: TestClient) -> None:
        """When suggest_responder returns None, falls back to first agent."""
        with (
            patch("mindroom.api.openai_compat.suggest_responder", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
        ):
            mock_route.return_value = None
            mock_ai.return_value = "Fallback response"

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        # Falls back to first agent in config (dict insertion order)
        assert response.json()["model"] == "general"

    def test_auto_passes_thread_history(self, app_client: TestClient) -> None:
        """Auto-routing passes thread_history to suggest_responder for context."""
        with (
            patch("mindroom.api.openai_compat.suggest_responder", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
        ):
            mock_route.return_value = ResponderSelection("general")
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [
                        {"role": "user", "content": "Hi"},
                        {"role": "assistant", "content": "Hello!"},
                        {"role": "user", "content": "Write code"},
                    ],
                },
            )

            # suggest_responder receives runtime_paths before the optional thread history.
            call_args = mock_route.call_args
            assert call_args[0][0] == "Write code"  # prompt
            thread_history = call_args[0][4]
            assert thread_history == [
                ResolvedVisibleMessage.synthetic(sender="user", body="Hi", event_id="$openai-1", timestamp=1),
                ResolvedVisibleMessage.synthetic(sender="assistant", body="Hello!", event_id="$openai-2", timestamp=2),
            ]

    def test_auto_streaming(self, app_client: TestClient) -> None:
        """Auto model works with streaming, chunks carry resolved agent name."""

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
            yield TextDelta("Streamed!")

        with (
            patch("mindroom.api.openai_compat.suggest_responder", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream),
        ):
            mock_route.return_value = ResponderSelection("research")

            response = app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Research this"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # Verify SSE chunks carry the resolved agent name, not "auto"
        lines = response.text.strip().split("\n\n")
        first_chunk = json.loads(lines[0].removeprefix("data: "))
        assert first_chunk["model"] == "research"

    def test_auto_no_agents_returns_500(self) -> None:
        """Auto with no configured agents returns 500."""
        empty_config = Config(
            agents={},
            models={"default": ModelConfig(provider="ollama", id="test")},
            router=RouterConfig(model="default"),
        )
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})
        with (
            patch("mindroom.api.openai_compat.suggest_responder", new_callable=AsyncMock) as mock_route,
            _openai_client(empty_config, runtime_paths) as client,
        ):
            mock_route.return_value = None
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500
        assert "no openai-compatible agents" in response.json()["error"]["message"].lower()

    def test_auto_session_id_uses_resolved_agent(self, app_client: TestClient) -> None:
        """Session ID derivation uses the resolved agent name, not 'auto'."""
        with (
            patch("mindroom.api.openai_compat.suggest_responder", new_callable=AsyncMock) as mock_route,
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
        ):
            mock_route.return_value = ResponderSelection("code")
            mock_ai.return_value = "Response"

            app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "auto",
                    "messages": [{"role": "user", "content": "Write code"}],
                },
                headers={"X-LibreChat-Conversation-Id": "conv-abc"},
            )

            # ai_response should receive entity_label="code", not "auto"
            assert mock_ai.call_args.args[0].entity_label == "code"
            # Session ID should use the resolved model name with LibreChat IDs.
            session_id = mock_ai.call_args.args[0].session_id
            assert session_id.endswith(":conv-abc:code")
            assert "auto" not in session_id


# ---------------------------------------------------------------------------
# Team completion (Phase 3)
# ---------------------------------------------------------------------------


@pytest.fixture
def team_config() -> Config:
    """Create a test config with agents and a team."""
    return Config(
        agents={
            "general": AgentConfig(
                display_name="GeneralAgent",
                role="General-purpose assistant",
                rooms=[],
            ),
            "code": AgentConfig(
                display_name="CodeAgent",
                role="Generate code",
                rooms=[],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
        teams={
            "super_team": TeamConfig(
                display_name="Super Team",
                role="Collaborative engineering team",
                agents=["general", "code"],
                mode="coordinate",
            ),
        },
    )


@pytest.fixture
def team_app_client(team_config: Config) -> Iterator[TestClient]:
    """Create a FastAPI test client with team-enabled config."""
    runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})
    persist_entity_accounts(team_config, runtime_paths)
    with _openai_client(team_config, runtime_paths) as client:
        yield client


_PREPARED_TEAM_TURN = _prepared_team_execution_context(final_prompt="Build it")


@dataclass(frozen=True)
class _TeamRunCall:
    """One ``run_team`` call the bridge made: the team, its request, and the run's context."""

    spec: TeamSpec
    messages: list[Message]
    ctx: RunContext
    request_template: ModelRequest


@contextmanager
def _recorded_team_runs() -> Iterator[list[_TeamRunCall]]:
    """Run the real team loop while recording what the bridge hands it."""
    recorded: list[_TeamRunCall] = []
    real_run_team = leader_module.run_team

    def recording_run_team(
        spec: TeamSpec,
        messages: Sequence[Message],
        *,
        ctx: RunContext,
        request_template: ModelRequest,
        **kwargs: object,
    ) -> AsyncIterator[object]:
        recorded.append(_TeamRunCall(spec, list(messages), ctx, request_template))
        return real_run_team(spec, messages, ctx=ctx, request_template=request_template, **kwargs)  # type: ignore[arg-type]

    with patch("mindroom.teams.leader.run_team", new=recording_run_team):
        yield recorded


def _scripted_team(
    *leader_turns: Sequence[ScriptStep],
    members: Sequence[tuple[str, str, ScriptedModel]] = (),
    mode: TeamMode = TeamMode.COORDINATE,
    leader_identity: ModelIdentity = SYNTHETIC,
    team_id: str = "test-team",
    member_toolkits: tuple[AdaptedToolkit, ...] = (),
) -> BuiltTeam:
    """Return what ``_build_team`` builds: a leader on a scripted provider and ``(id, name, model)`` members.

    ``team_id`` is the stored ``team_id`` of the team's runs, which a configured team's scope replays by; every
    member offers ``member_toolkits``.
    """
    leader = ScriptedModel([list(turn) for turn in leader_turns], identity=leader_identity)
    spec = team_spec(
        team_id,
        "Test Team",
        leader,
        "You lead the team.",
        [team_member(member_id, name, model, *member_toolkits) for member_id, name, model in members],
        mode=mode,
    )
    return built_team(spec)


def _resolved_members(members: Sequence[tuple[str, str, ScriptedModel]]) -> ResolvedExactTeamMembers:
    """Return the member set ``resolve_exact_team_members`` resolves for the ``(id, name, model)`` members."""
    return resolved_members({member_id: name for member_id, name, _model in members})


def _finished(content: str) -> RunFinished:
    """Return the terminal event of a team run that completed with ``content``."""
    return RunFinished(
        TeamRunRecord(run_id="team-run", team_id="test-team", status=RunStatus.completed, content=content),
    )


def _team_events(*events: object) -> Callable[..., AsyncIterator[object]]:
    """Return a ``run_team`` stand-in that yields ``events`` for whatever team it is asked to run."""

    async def run(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
        for event in events:
            yield event

    return run


def remember(label: str, run_context: AgnoRunContext) -> str:
    """Keep a label in the conversation's shared session state.

    Args:
        label: What to keep.
        run_context: The run, supplied by the loop.

    """
    run_context.session_state["remembered"] = label
    return "remembered"


def _sse_contents(response_text: str) -> list[str]:
    """Return the content deltas of an SSE body, in order."""
    contents: list[str] = []
    for line in response_text.strip().split("\n\n"):
        if line.startswith("data: ") and line != "data: [DONE]":
            chunk = json.loads(line.removeprefix("data: "))
            delta = chunk["choices"][0]["delta"]
            if "content" in delta:
                contents.append(delta["content"])
    return contents


def _post_team_completion_capturing_run_user(
    client: TestClient,
    *,
    stream: bool,
    headers: dict[str, str] | None = None,
) -> RunContext:
    """Post one team completion carrying a spoofed body ``user`` and return the run context the team ran under."""
    with (
        patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team(answer("Team answer", chunk=99))),
        patch(
            "mindroom.api.openai_compat._prepare_openai_team_execution",
            new=AsyncMock(return_value=_PREPARED_TEAM_TURN),
        ),
        _recorded_team_runs() as runs,
    ):
        response = client.post(
            "/v1/chat/completions",
            headers=headers or {},
            json={
                "model": "team/super_team",
                "messages": [{"role": "user", "content": "Build it"}],
                "stream": stream,
                "user": "@victim:localhost",
            },
        )
    assert response.status_code == 200
    assert "Team answer" in response.text
    [run] = runs
    return run.ctx


@pytest.mark.parametrize("stream", [False, True])
def test_team_run_ignores_request_body_user_without_requester(team_app_client: TestClient, stream: bool) -> None:
    """The request body ``user`` must not choose the user scope of a team run."""
    ctx = _post_team_completion_capturing_run_user(team_app_client, stream=stream)

    assert ctx.user_id is None


@pytest.mark.parametrize("stream", [False, True])
def test_team_run_uses_mapped_requester_instead_of_request_body_user(team_config: Config, stream: bool) -> None:
    """A mapped key's canonical requester, never the body ``user``, owns the team run's user scope."""
    team_config.teams["super_team"].access = ResponderAccessConfig(users=["@alice:localhost"])
    runtime_paths = _runtime_paths(
        {
            "OPENAI_COMPAT_API_KEYS": "alice-key",
            "OPENAI_COMPAT_API_KEY_REQUESTERS": json.dumps({"alice-key": "@alice:localhost"}),
        },
    )
    persist_entity_accounts(team_config, runtime_paths)
    with _openai_client(team_config, runtime_paths) as client:
        ctx = _post_team_completion_capturing_run_user(
            client,
            stream=stream,
            headers={"Authorization": "Bearer alice-key"},
        )

    assert ctx.user_id == "@alice:localhost"


@pytest.mark.parametrize("stream", [False, True])
def test_agent_turn_names_the_mapped_keys_requester(test_config: Config, stream: bool) -> None:
    """An agent turn on a key mapped to a requester names that requester in its turn context."""
    test_config.agents["general"].access = ResponderAccessConfig(users=["@alice:localhost"])
    runtime_paths = _runtime_paths(
        {
            "OPENAI_COMPAT_API_KEYS": "alice-key",
            "OPENAI_COMPAT_API_KEY_REQUESTERS": json.dumps({"alice-key": "@alice:localhost"}),
        },
    )
    persist_entity_accounts(test_config, runtime_paths)
    contexts: list[ResponseTurnContext] = []

    async def respond(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> str:
        contexts.append(ctx)
        return "Response"

    async def stream_response(ctx: ResponseTurnContext, *_args: object, **_kwargs: object) -> AsyncIterator[TextDelta]:
        contexts.append(ctx)
        yield TextDelta("Response")

    with (
        patch("mindroom.api.openai_compat.ai_response", new=completing(respond)),
        patch("mindroom.api.openai_compat.stream_agent_response", new=stream_response),
        _openai_client(test_config, runtime_paths) as client,
    ):
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer alice-key"},
            json={"model": "general", "messages": [{"role": "user", "content": "Hi"}], "stream": stream},
        )

    assert response.status_code == 200
    assert [ctx.requester_id for ctx in contexts] == ["@alice:localhost"]


@pytest.mark.parametrize("stream", [False, True])
def test_team_run_records_helper_usage_in_its_conversation(team_app_client: TestClient, stream: bool) -> None:
    """Helper work during an API team run, such as a member's Claude Code session, belongs to the team conversation."""
    owners: list[HelperUsageOwner | None] = []
    session_ids: list[str | None] = []

    async def run(*_args: object, ctx: RunContext, **_kwargs: object) -> AsyncIterator[object]:
        owners.append(get_helper_usage_owner())
        session_ids.append(ctx.session_id)
        yield TextDelta("Team answer")
        yield _finished("Team answer")

    with (
        patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team()),
        patch(
            "mindroom.api.openai_compat._prepare_openai_team_execution",
            new=AsyncMock(return_value=_PREPARED_TEAM_TURN),
        ),
        patch("mindroom.teams.leader.run_team", new=run),
    ):
        response = team_app_client.post(
            "/v1/chat/completions",
            headers={"X-Session-Id": "team-session"},
            json={"model": "team/super_team", "messages": [{"role": "user", "content": "Build it"}], "stream": stream},
        )

    assert response.status_code == 200
    assert len(owners) == 1
    assert owners[0] is not None
    assert owners[0].session_id == session_ids[0]


class TestTeamCompletion:
    """Tests for team model support (Phase 3)."""

    @pytest.mark.parametrize(
        ("prompts", "opening"),
        [
            ({}, ""),
            (
                {"TEAM_LEADER_IDENTITY_CONTEXT_TEMPLATE": "Custom identity of {display_name}.\n"},
                "Custom identity of Super Team.\n\n",
            ),
            (
                {"TEAM_LEADER_IDENTITY_CONTEXT_TEMPLATE": "Custom identity of {display_name}."},
                "Custom identity of Super Team.\n\n",
            ),
        ],
        ids=["default", "custom_matrix_identity", "custom_matrix_identity_without_a_trailing_newline"],
    )
    def test_an_api_team_leader_is_told_it_answers_through_the_api_as_its_members_are(
        self,
        team_config: Config,
        prompts: dict[str, str],
        opening: str,
    ) -> None:
        """The leader gets the API identity and history format, never the Matrix chatroom, history, or mention guide.

        An operator's custom Matrix leader identity, without a custom API one, opens it a blank line apart, as it does
        for an agent, whether or not the authored text ends with a newline.
        """
        config = team_config.model_copy(update={"prompts": prompts})
        runtime_paths = _runtime_paths_for_config(config)
        identity = _team_identity("super_team", runtime_paths)
        scope_context = load_bound_scope_session(
            member_names=[],
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=identity,
            team_name="super_team",
        )

        built = openai_compat._build_team(
            "super_team",
            config,
            runtime_paths,
            identity,
            scope_context,
            {},
            None,
            models=_team_models("super_team", config, runtime_paths),
        )

        leader_prompt = str(built.spec.leader.system_messages[0].content)
        assert leader_prompt.startswith(
            f"<your_role>\n{opening}## Your Identity\nYou are Super Team, the MindRoom team exposed through the"
            " OpenAI-compatible API: you lead its members, hand them work, and write the team's answer.\n"
            "Team purpose: Collaborative engineering team\n"
            "In OpenAI-compatible API contexts, prior turns may instead appear as plain `role: body` lines.",
        )
        for matrix_text in ("Matrix chatroom", "<conversation>", "complete Matrix ID"):
            assert matrix_text not in leader_prompt
        for member in built.spec.members:
            assert "the MindRoom agent exposed through the OpenAI-compatible API" in str(
                member.build().spec.system_messages[0].content,
            )

    @pytest.mark.asyncio
    async def test_prepare_openai_team_execution_uses_non_matrix_team_preparation_defaults(
        self,
        team_config: Config,
        tmp_path: Path,
    ) -> None:
        """OpenAI team turn preparation should preserve non-Matrix request defaults."""
        runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml")
        execution_identity = build_tool_execution_identity(
            channel="openai_compat",
            agent_name="team/super_team",
            session_id="session-openai-team",
            runtime_paths=runtime_paths,
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
        )
        built = _scripted_team(members=[("general", "GeneralAgent", ScriptedModel([]))])

        with patch(
            "mindroom.api.openai_compat.prepare_materialized_team_execution",
            new_callable=AsyncMock,
        ) as mock_prepare:
            mock_prepare.return_value = _prepared_team_execution_context(final_prompt="Build it")

            prepared = await openai_compat._prepare_openai_team_execution(
                scope_context=None,
                team_name="super_team",
                built=built,
                prompt="Build it",
                config=team_config,
                runtime_paths=runtime_paths,
                thread_history=[],
                team_model_name="default",
                execution_identity=execution_identity,
            )

        assert prepared is mock_prepare.return_value
        assert mock_prepare.await_count == 1
        preparation_kwargs = mock_prepare.await_args.kwargs
        preparation_ctx = mock_prepare.await_args.args[0]
        assert preparation_kwargs["built"] is built
        assert preparation_kwargs["message"] == "Build it"
        assert preparation_kwargs["thread_history"] == []
        assert preparation_ctx.reply_to_event_id is None
        assert preparation_ctx.active_event_ids == frozenset()
        assert preparation_kwargs["response_sender_id"] is None
        assert preparation_kwargs["current_sender_id"] is None
        assert preparation_ctx.room_id is None
        assert preparation_ctx.thread_id is None
        assert preparation_ctx.requester_id is None
        assert re.fullmatch(r"[0-9a-f]{32}", preparation_ctx.correlation_id)
        assert preparation_ctx.matrix_run_metadata is None
        assert preparation_kwargs["runtime_model"].model_name == "default"

    def test_team_listed_in_models(self, team_app_client: TestClient) -> None:
        """Teams appear in /v1/models with team/ prefix."""
        response = team_app_client.get("/v1/models")
        assert response.status_code == 200
        models = response.json()["data"]
        team_models = [m for m in models if m["id"].startswith("team/")]
        assert len(team_models) == 1
        assert team_models[0]["id"] == "team/super_team"
        assert team_models[0]["name"] == "Super Team"
        assert team_models[0]["description"] == "Collaborative engineering team"

    def test_unknown_team_404(self, team_app_client: TestClient) -> None:
        """Unknown team name returns 404."""
        response = team_app_client.post(
            "/v1/chat/completions",
            json={"model": "team/nonexistent", "messages": [{"role": "user", "content": "Hi"}]},
        )
        assert response.status_code == 404
        assert "nonexistent" in response.json()["error"]["message"]

    def test_team_non_streaming(self, team_app_client: TestClient) -> None:
        """Non-streaming team completion returns proper OpenAI response."""
        team = _scripted_team(answer("Team consensus result"))

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            patch(
                "mindroom.api.openai_compat.prepare_materialized_team_execution",
                new_callable=AsyncMock,
            ) as mock_prepare,
            _recorded_team_runs() as runs,
        ):
            mock_prepare.return_value = _prepared_team_execution_context(
                final_prompt="Build a feature",
                prepared_context_tokens=321,
            )
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build a feature"}],
                },
            )

        assert response.status_code == 200
        data = response.json()
        assert data["model"] == "team/super_team"
        assert data["object"] == "chat.completion"
        assert data["choices"][0]["finish_reason"] == "stop"
        assert "Team consensus result" in data["choices"][0]["message"]["content"]
        assert mock_prepare.await_count == 1
        assert mock_prepare.await_args.kwargs["built"] is team
        assert mock_prepare.await_args.kwargs["message"] == "Build a feature"
        [run] = runs
        assert [message.content for message in run.messages] == ["Build a feature"]
        assert run.ctx.metadata is not None
        assert run.ctx.metadata[constants.AI_RUN_METADATA_KEY]["prepared_context"]["tokens"] == 321

    def test_a_team_member_s_tool_calls_belong_to_the_team_s_stored_conversation(
        self,
        team_app_client: TestClient,
    ) -> None:
        """A member's calls carry the team scope, so its compact_context compacts the team's history, not its own."""
        scopes: list[HistoryScope | None] = []

        async def which_scope() -> str:
            """Report the stored conversation this call belongs to."""
            call = current_tool_call()
            assert call is not None
            scopes.append(call.context.history_scope)
            return "noted"

        team = _scripted_team(
            calls(tool_call("delegate_task_to_member", "call-team", member_id="general", task="Which scope?")),
            answer("Done."),
            members=[
                ("general", "General", ScriptedModel([calls(tool_call("which_scope", "call-scope")), answer("Ok.")])),
            ],
            member_toolkits=(adapt_toolkit(Toolkit(name="probe", tools=[which_scope])),),
        )

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            patch(
                "mindroom.api.openai_compat.prepare_materialized_team_execution",
                new=AsyncMock(return_value=_PREPARED_TEAM_TURN),
            ),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={"model": "team/super_team", "messages": [{"role": "user", "content": "Which scope?"}]},
            )

        assert response.status_code == 200
        assert scopes == [HistoryScope(kind="team", scope_id="super_team")]

    def test_team_non_streaming_is_the_leader_s_answer_as_a_streamed_completion_sends_it(
        self,
        team_app_client: TestClient,
    ) -> None:
        """The completion is the leader's answer, the text a streamed team completion sends, whatever the members did.

        The members' results reach the client through the leader, as they do when the completion streams.
        """

        async def overloaded() -> None:
            msg = "Checker overloaded"
            raise ProviderError(msg, provider="Synthetic", status_code=503)

        def delegation(call_id: str, member_id: str, task: str) -> list[ScriptStep]:
            return calls(tool_call("delegate_task_to_member", call_id, member_id=member_id, task=task))

        team = _scripted_team(
            delegation("call-check", "checker", "Check"),
            delegation("call-draft", "writer", "Draft"),
            delegation("call-revise", "writer", "Revise"),
            answer("All three done."),
            members=[
                ("writer", "Writer", ScriptedModel([answer("Draft one."), answer("Draft two.")])),
                ("checker", "Checker", ScriptedModel([[Await(overloaded)]])),
            ],
        )

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            patch(
                "mindroom.api.openai_compat.prepare_materialized_team_execution",
                new=AsyncMock(return_value=_PREPARED_TEAM_TURN),
            ),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={"model": "team/super_team", "messages": [{"role": "user", "content": "Check and write"}]},
            )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "All three done."

    def test_team_non_streaming_reports_leader_and_member_token_usage(self, team_app_client: TestClient) -> None:
        """Team usage sums every run's prompt tokens, each normalized for its own provider's cache accounting."""
        member = ScriptedModel(
            [answer("Member view", usage(50, 10, cache_read_tokens=30))],
            identity=ModelIdentity(id="gpt-6-astra", provider="OpenAI"),
        )
        team = _scripted_team(
            calls(
                tool_call("delegate_task_to_member", "call-1", member_id="general", task="View"),
                metrics=usage(0, 0),
            ),
            answer("Team consensus result", usage(100, 20, cache_read_tokens=1000, cache_write_tokens=200)),
            members=[("general", "GeneralAgent", member)],
            leader_identity=ModelIdentity(id="claude-sonnet-5-5", provider="Anthropic"),
        )

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            patch(
                "mindroom.api.openai_compat.prepare_materialized_team_execution",
                new_callable=AsyncMock,
            ) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_team_execution_context(final_prompt="Build a feature")
            response = team_app_client.post(
                "/v1/chat/completions",
                json={"model": "team/super_team", "messages": [{"role": "user", "content": "Build a feature"}]},
            )

        assert response.status_code == 200
        assert response.json()["usage"] == {"prompt_tokens": 1350, "completion_tokens": 30, "total_tokens": 1380}

    @pytest.mark.parametrize(
        ("availability", "search_available", "notice"),
        [
            (
                KnowledgeAvailability.INITIALIZING,
                False,
                "Knowledge base `docs` is initializing and unavailable for semantic search this turn.",
            ),
            (
                KnowledgeAvailability.CONFIG_MISMATCH,
                True,
                "Knowledge base `docs` is refreshing against newer config and may be stale this turn.",
            ),
        ],
    )
    @pytest.mark.parametrize("stream", [False, True])
    def test_team_completion_degraded_kb_emits_system_hint(
        self,
        team_app_client: TestClient,
        availability: KnowledgeAvailability,
        search_available: bool,
        notice: str,
        stream: bool,
    ) -> None:
        """Team completions should prepend the degraded knowledge notice to the prompt."""
        app_refresh_scheduler = RecordingRefreshScheduler()
        member_models = [("general", "GeneralAgent", ScriptedModel([])), ("code", "CodeAgent", ScriptedModel([]))]
        members = _resolved_members(member_models)
        built = _scripted_team(answer("Team consensus result", chunk=99), members=member_models)

        def fake_materialize(
            requested_agent_names: list[str],
            **kwargs: object,
        ) -> ResolvedExactTeamMembers:
            unavailable_bases = cast(
                "dict[str, KnowledgeAvailabilityDetail] | None",
                kwargs["unavailable_bases"],
            )
            assert unavailable_bases is not None
            assert kwargs["refresh_scheduler"] is app_refresh_scheduler
            unavailable_bases["docs"] = KnowledgeAvailabilityDetail(
                availability=availability,
                search_available=search_available,
            )
            assert requested_agent_names == members.requested_agent_names
            return members

        bind_test_embedded_runtime(team_app_client.app, knowledge_refresh_scheduler=app_refresh_scheduler)
        with (
            patch("mindroom.api.openai_compat.resolve_exact_team_members", side_effect=fake_materialize),
            patch("mindroom.api.openai_compat.build_team_spec", return_value=built),
            patch(
                "mindroom.api.openai_compat.prepare_materialized_team_execution",
                new_callable=AsyncMock,
            ) as mock_prepare,
        ):
            mock_prepare.return_value = _prepared_team_execution_context(final_prompt="Build a feature")
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build a feature"}],
                    "stream": stream,
                },
            )

        assert response.status_code == 200
        assert notice in mock_prepare.await_args.kwargs["message"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_team_completion_passes_non_matrix_metadata_to_the_run(
        self,
        team_config: Config,
        tmp_path: Path,
        stream: bool,
    ) -> None:
        """Non-Matrix team completions pass minted run metadata through to the team run."""
        runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml")
        execution_identity = build_tool_execution_identity(
            channel="openai_compat",
            agent_name="team/super_team",
            session_id="session-123",
            runtime_paths=runtime_paths,
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
        )
        prepared_correlation_ids: list[str] = []
        request_log_contexts: list[dict[str, object]] = []
        run_metadata: list[dict[str, Any] | None] = []

        async def mock_prepare_team_execution(ctx: ResponseTurnContext, **_kwargs: object) -> PreparedTeamExecution:
            assert ctx.requester_id is None
            prepared_correlation_ids.append(ctx.correlation_id)
            return _prepared_team_execution_context(
                final_prompt="Build it",
                run_metadata={"correlation_id": ctx.correlation_id},
            )

        async def run(*_args: object, ctx: RunContext, **_kwargs: object) -> AsyncIterator[object]:
            request_log_contexts.append(current_llm_request_log_context())
            run_metadata.append(ctx.metadata)
            assert ctx.metadata is not None
            correlation_id = ctx.metadata["correlation_id"]
            assert isinstance(correlation_id, str)
            record_tool_success(
                tool_name="demo_tool",
                arguments={"query": "test"},
                result={"status": "ok"},
                timing=ToolCallTiming(result_ready_ms=1.0),
                agent_name="team/super_team",
                channel=execution_identity.channel,
                room_id=None,
                thread_id=None,
                reply_to_event_id=None,
                requester_id=None,
                session_id="session-123",
                correlation_id=correlation_id,
                runtime_paths=runtime_paths,
            )
            yield TextDelta("Hello ")
            yield TextDelta("world!")
            yield _finished("Hello world!")

        completion = openai_compat._stream_team_completion if stream else openai_compat._non_stream_team_completion
        with (
            patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team()),
            patch(
                "mindroom.api.openai_compat.prepare_materialized_team_execution",
                new=AsyncMock(side_effect=mock_prepare_team_execution),
            ),
            patch("mindroom.teams.leader.run_team", new=run),
        ):
            response = await completion(
                "super_team",
                "team/super_team",
                "Build it",
                "session-123",
                team_config,
                runtime_paths,
                None,
                _team_models("super_team", team_config, runtime_paths),
                execution_identity=execution_identity,
                refresh_scheduler=None,
                cost=openai_compat._CompletionCost(),
            )
            if isinstance(response, StreamingResponse):
                body_chunks = [
                    chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                    async for chunk in response.body_iterator
                ]
                body = "".join(body_chunks)
                assert "Hello " in body
                assert "world!" in body
            else:
                assert response.status_code == 200

        [metadata] = run_metadata
        assert isinstance(metadata, dict)
        correlation_id = metadata["correlation_id"]
        assert isinstance(correlation_id, str)
        assert re.fullmatch(r"[0-9a-f]{32}", correlation_id)
        assert prepared_correlation_ids == [correlation_id]
        assert request_log_contexts[0]["agent_id"] == "team/super_team"
        assert request_log_contexts[0]["session_id"] == "session-123"
        assert "requester_id" not in request_log_contexts[0]
        assert request_log_contexts[0]["correlation_id"] == correlation_id
        assert request_log_contexts[0]["full_prompt"] == "Build it"
        records = _read_jsonl(_tool_calls_path(runtime_paths))
        assert records[0]["correlation_id"] == correlation_id
        assert records[0]["reply_to_event_id"] is None

    def test_team_streaming(self, team_app_client: TestClient) -> None:
        """Streaming team completion streams the leader's text directly."""
        team = _scripted_team(answer("Hello world!", chunk=6))

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            patch(
                "mindroom.api.openai_compat.prepare_materialized_team_execution",
                new_callable=AsyncMock,
            ) as mock_prepare,
            _recorded_team_runs() as runs,
        ):
            mock_prepare.return_value = _prepared_team_execution_context(
                final_prompt="Build it",
                prepared_context_tokens=456,
            )
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build it"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        lines = response.text.strip().split("\n\n")

        # First chunk is role announcement
        first = json.loads(lines[0].removeprefix("data: "))
        assert first["choices"][0]["delta"] == {"role": "assistant"}
        assert first["model"] == "team/super_team"

        # Leader content is streamed directly (not buffered): each delta is its own chunk.
        content_parts = _sse_contents(response.text)
        assert "".join(content_parts) == "Hello world!"
        assert "Hello " in content_parts
        assert "world!" in content_parts
        assert mock_prepare.await_count == 1
        assert mock_prepare.await_args.kwargs["built"] is team
        [run] = runs
        assert [message.content for message in run.messages] == ["Build it"]
        assert run.ctx.metadata is not None
        assert run.ctx.metadata[constants.AI_RUN_METADATA_KEY]["prepared_context"]["tokens"] == 456

    @pytest.mark.parametrize("stream", [True, False])
    def test_a_team_completion_keeps_execution_identity_for_its_whole_run(
        self,
        team_app_client: TestClient,
        stream: bool,
    ) -> None:
        """A team completion keeps worker-routing identity active after preflight, streamed or not."""
        observed_session_ids: list[str | None] = []

        async def run(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            identity = current_execution_identity()
            observed_session_ids.append(identity.session_id if identity is not None else None)
            yield TextDelta("Hello ")

            identity = current_execution_identity()
            observed_session_ids.append(identity.session_id if identity is not None else None)
            yield TextDelta("world!")
            yield _finished("Hello world!")

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team()),
            patch("mindroom.teams.leader.run_team", new=run),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build it"}],
                    "stream": stream,
                },
            )

        assert response.status_code == 200
        assert len(observed_session_ids) == 2
        assert all(session_id is not None for session_id in observed_session_ids)

    @pytest.mark.asyncio
    async def test_team_streaming_close_from_other_task_keeps_execution_identity(
        self,
        team_config: Config,
    ) -> None:
        """Closing the team SSE body from another task should still clean up inside the execution identity."""
        runtime_paths = _runtime_paths()
        execution_identity = build_tool_execution_identity(
            channel="openai_compat",
            agent_name="team/super_team",
            session_id="session-123",
            runtime_paths=runtime_paths,
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
        )
        observed_final_identities: list[ToolExecutionIdentity | None] = []

        async def run(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            try:
                assert current_execution_identity() == execution_identity
                yield TextDelta("Hello")
                await asyncio.Future()
            finally:
                observed_final_identities.append(current_execution_identity())

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team()),
            patch(
                "mindroom.api.openai_compat._prepare_openai_team_execution",
                new=AsyncMock(return_value=_PREPARED_TEAM_TURN),
            ),
            patch("mindroom.teams.leader.run_team", new=run),
        ):
            response = await openai_compat._stream_team_completion(
                "super_team",
                "team/super_team",
                "Build it",
                "session-123",
                team_config,
                runtime_paths,
                None,
                _team_models("super_team", team_config, runtime_paths),
                execution_identity=execution_identity,
                refresh_scheduler=None,
                cost=openai_compat._CompletionCost(),
            )

        assert isinstance(response, openai_compat.OpenAIStreamingResponse)
        # The response closes the team stream too, for a body that never started.
        assert response.close_stream is not None
        body_iterator = response.body_iterator
        await asyncio.create_task(anext(body_iterator), context=Context())
        await asyncio.create_task(body_iterator.aclose(), context=Context())
        assert observed_final_identities == [execution_identity]

    def test_team_streaming_builds_team_inside_execution_identity(self, team_app_client: TestClient) -> None:
        """Streamed team requests must establish execution identity before member agents are built."""
        observed_agent_names: list[str | None] = []
        observed_session_ids: list[str | None] = []

        def fake_build_team(*_args: object, **_kwargs: object) -> BuiltTeam:
            identity = current_execution_identity()
            observed_agent_names.append(identity.agent_name if identity is not None else None)
            observed_session_ids.append(identity.session_id if identity is not None else None)
            return _scripted_team(answer("Hello world!"))

        with patch("mindroom.api.openai_compat._build_team", side_effect=fake_build_team):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build it"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert observed_agent_names == ["team/super_team"]
        assert len(observed_session_ids) == 1
        assert observed_session_ids[0] is not None

    def test_team_streaming_failure_after_content_closes_started_tools_and_emits_a_failure_chunk(
        self,
        team_app_client: TestClient,
    ) -> None:
        """A run that fails after streaming started keeps its text, closes its started call, and ends with a failure."""

        async def run(*_args: object, **_kwargs: object) -> AsyncIterator[object]:
            yield TextDelta("Team started. ")
            yield ToolStarted(
                ToolExecution(tool_name="run_shell", tool_args={"cmd": "ls"}, tool_call_id="tc-pending").call,
            )
            msg = "Internal Server Error"
            raise RuntimeError(msg)

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team()),
            patch("mindroom.teams.leader.run_team", new=run),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build it"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert _sse_contents(response.text) == [
            "Team started. ",
            '<tool id="1" state="start">run_shell(cmd=ls)</tool>',
            '<tool id="1" state="done">(interrupted)</tool>',
            "Team execution failed.",
        ]
        assert response.text.strip().split("\n\n")[-1] == "data: [DONE]"

    @pytest.mark.parametrize(
        "build_error",
        [
            ValueError("Team 'super_team' cannot be materialized"),
            RuntimeError("team build failed"),
        ],
    )
    def test_team_build_failure_returns_500(self, team_app_client: TestClient, build_error: Exception) -> None:
        """Team build failures should not expose internal exception details."""
        with patch("mindroom.api.openai_compat._build_team", side_effect=build_error):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 500
        assert response.json()["error"]["message"] == "Team execution failed"

    def test_team_streaming_build_failure_returns_500(self, team_app_client: TestClient) -> None:
        """Streaming team build failures should surface a server error response before SSE starts."""
        with patch("mindroom.api.openai_compat._build_team", side_effect=RuntimeError("team build failed")):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                },
            )

        assert response.status_code == 500
        assert response.json()["error"]["message"] == "Team execution failed"

    def test_a_member_that_fails_to_build_is_a_failed_delegation_the_leader_answers_around(
        self,
        team_app_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A member that cannot be built fails only the delegation to it; the completion is the leader's answer."""
        leader = ScriptedModel(
            [
                calls(tool_call("delegate_task_to_member", "call-general", member_id="general", task="Help")),
                answer("General could not start."),
            ],
        )
        install_model_providers(monkeypatch, {"default": leader})
        with (
            patch(
                "mindroom.teams.members.resolve_agent_knowledge_access",
                return_value=_KnowledgeResolution(knowledge=None),
            ),
            patch("mindroom.teams.members.create_agent", side_effect=RuntimeError("boom")),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == "General could not start."
        delegated = leader.requests[1].messages[-1]
        assert (delegated.tool_call_id, delegated.content) == ("call-general", "boom")

    @pytest.mark.parametrize("stream", [False, True])
    def test_team_execution_failure_500(self, team_app_client: TestClient, stream: bool) -> None:
        """A provider failure before the first chunk returns 500."""

        async def boom() -> None:
            msg = "Model error"
            raise RuntimeError(msg)

        with patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team([Await(boom)])):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": stream,
                },
            )

        assert response.status_code == 500
        assert response.json()["error"]["type"] == "server_error"

    @pytest.mark.parametrize(
        "answer_text",
        ["❌ The build failed: the disk is full.", "Error code: 404 means the page is gone."],
    )
    def test_a_team_answer_that_starts_like_an_error_is_an_answer(
        self,
        team_app_client: TestClient,
        answer_text: str,
    ) -> None:
        """A non-streaming team completion reads failure from how its run ended, not from the leader's text."""
        with patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team(answer(answer_text))):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={"model": "team/super_team", "messages": [{"role": "user", "content": "Why?"}]},
            )

        assert response.status_code == 200
        assert response.json()["choices"][0]["message"]["content"] == answer_text

    def test_team_streaming_skips_member_content(self, team_app_client: TestClient) -> None:
        """Member content is skipped; only the leader's content is streamed."""
        team = _scripted_team(members=[("general", "GeneralAgent", ScriptedModel([]))])

        def member(text: str) -> MemberEvent:
            return MemberEvent("general", "GeneralAgent", "member-run", TextDelta(text))

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            patch(
                "mindroom.teams.leader.run_team",
                new=_team_events(
                    member("member noise "),
                    TextDelta("leader "),
                    member("more noise "),
                    TextDelta("answer"),
                    _finished("leader answer"),
                ),
            ),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Build it"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        lines = response.text.strip().split("\n\n")
        full = "".join(_sse_contents(response.text))
        # Only leader content is present
        assert full == "leader answer"
        # Member content is skipped
        assert "member noise" not in full
        assert "more noise" not in full
        assert lines[-1] == "data: [DONE]"

    def test_team_streaming_skips_interleaved_parallel_member_content(self, team_app_client: TestClient) -> None:
        """Interleaved content from parallel members is skipped; the leader's content is streamed."""
        team = _scripted_team(
            members=[("a", "AgentA", ScriptedModel([])), ("b", "AgentB", ScriptedModel([]))],
            mode=TeamMode.COLLABORATE,
        )

        def member(member_id: str, text: str) -> MemberEvent:
            return MemberEvent(member_id, f"Agent{member_id.upper()}", f"{member_id}-run", TextDelta(text))

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            patch(
                "mindroom.teams.leader.run_team",
                new=_team_events(
                    # Interleaved chunks from two parallel members, then the leader's synthesis.
                    member("a", "From A chunk1 "),
                    member("b", "From B chunk1 "),
                    member("a", "From A chunk2 "),
                    member("b", "From B chunk2 "),
                    TextDelta("Combined answer from leader"),
                    _finished("Combined answer from leader"),
                ),
            ),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [{"role": "user", "content": "Analyze this"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        full = "".join(_sse_contents(response.text))
        # Only leader content is streamed
        assert full == "Combined answer from leader"
        # Member content is skipped entirely
        assert "From A chunk1" not in full
        assert "From B chunk1" not in full
        assert "From A chunk2" not in full
        assert "From B chunk2" not in full

    def test_team_non_streaming_includes_thread_history(self, team_app_client: TestClient) -> None:
        """The request history reaches the leader as the turn's context messages ahead of the prompt."""
        with (
            patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team(answer("ok"))),
            _recorded_team_runs() as runs,
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [
                        {"role": "user", "content": "Start"},
                        {"role": "assistant", "content": "Ack"},
                        {"role": "user", "content": "Follow-up"},
                    ],
                },
            )

        assert response.status_code == 200
        [run] = runs
        assert [(message.role, message.content) for message in run.messages] == [
            ("user", render_msg_tag(sender="user", body="Start", event_id="$openai-1")),
            ("user", render_msg_tag(sender="assistant", body="Ack", event_id="$openai-2")),
            ("user", "Follow-up"),
        ]

    def test_team_non_streaming_preserves_full_request_history_for_ad_hoc_team_runs(
        self,
        team_app_client: TestClient,
    ) -> None:
        """Ad hoc team runs must not apply Matrix-specific truncation to request history."""
        long_body = "L" * 250
        request_messages: list[dict[str, str]] = [
            {"role": "user", "content": "msg 0"},
            {"role": "assistant", "content": long_body},
        ]
        request_messages.extend(
            {
                "role": "user" if idx % 2 == 0 else "assistant",
                "content": "Final prompt" if idx == 34 else f"msg {idx}",
            }
            for idx in range(2, 35)
        )

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=_scripted_team(answer("ok"))),
            _recorded_team_runs() as runs,
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={"model": "team/super_team", "messages": request_messages},
            )

        assert response.status_code == 200
        [run] = runs
        contents = [str(message.content) for message in run.messages]
        assert render_msg_tag(sender="user", body="msg 0", event_id="$openai-1") in contents
        assert render_msg_tag(sender="assistant", body=long_body, event_id="$openai-2") in contents
        assert contents[-1] == "Final prompt"

    @pytest.mark.parametrize("stream", [False, True])
    def test_team_completion_prefers_persisted_history_over_thread_history(
        self,
        team_app_client: TestClient,
        stream: bool,
    ) -> None:
        """Persisted team history suppresses request-history stuffing; the run reads the stored replay the preparation selected."""
        team = _scripted_team(answer("ok", chunk=99))

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            patch(
                "mindroom.api.openai_compat.prepare_materialized_team_execution",
                new_callable=AsyncMock,
            ) as mock_prepare,
            _recorded_team_runs() as runs,
        ):
            mock_prepare.return_value = _prepared_team_execution_context(
                final_prompt="Follow-up",
                replay_plan=ResolvedReplayPlan(
                    mode="limited",
                    estimated_tokens=100,
                    add_history_to_context=True,
                    num_history_runs=1,
                    num_history_messages=None,
                ),
                replays_persisted_history=True,
                history=[Message(role="user", content="Start"), Message(role="assistant", content="Ack")],
            )
            response = team_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "team/super_team",
                    "messages": [
                        {"role": "user", "content": "Start"},
                        {"role": "assistant", "content": "Ack"},
                        {"role": "user", "content": "Follow-up"},
                    ],
                    "stream": stream,
                },
            )

        assert response.status_code == 200
        assert mock_prepare.await_args.kwargs["message"] == "Follow-up"
        assert mock_prepare.await_args.kwargs["built"] is team
        assert [message.body for message in mock_prepare.await_args.kwargs["thread_history"]] == ["Start", "Ack"]
        [run] = runs
        assert [message.content for message in run.messages] == ["Start", "Ack", "Follow-up"]

    @pytest.mark.parametrize("stream", [False, True])
    def test_team_completion_stores_its_run_in_the_team_scope_and_replays_it_next_time(
        self,
        team_config: Config,
        stream: bool,
    ) -> None:
        """A configured team's API run lands in the team scope store, and the session's next request replays it."""
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})
        persist_entity_accounts(team_config, runtime_paths)
        team = _scripted_team(answer("First answer", chunk=99), answer("Second answer", chunk=99), team_id="super_team")

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=team),
            _recorded_team_runs() as runs,
            _openai_client(team_config, runtime_paths) as client,
        ):
            for prompt in ("Start", "Follow-up"):
                response = client.post(
                    "/v1/chat/completions",
                    headers={"X-Session-Id": "team-session"},
                    json={
                        "model": "team/super_team",
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": stream,
                    },
                )
                assert response.status_code == 200

        first, second = runs
        storage = create_scope_session_storage(
            scope=HistoryScope(kind="team", scope_id="super_team"),
            config=team_config,
            runtime_paths=runtime_paths,
            execution_identity=None,
        )
        session = storage.load_session(first.ctx.session_id)
        assert session is not None
        assert [(run.run_id, run.status, run.content) for run in session.runs] == [
            (first.ctx.run_id, RunStatus.completed, "First answer"),
            (second.ctx.run_id, RunStatus.completed, "Second answer"),
        ]
        assert [message.content for message in second.messages] == ["Start", "First answer", "Follow-up"]

    @pytest.mark.parametrize("stream", [False, True])
    def test_team_completion_records_every_run_s_usage_and_the_session_state_its_tools_wrote(
        self,
        team_config: Config,
        stream: bool,
    ) -> None:
        """An API team run stores the leader's and the member's usage in the team scope and the state a tool changed."""
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})
        persist_entity_accounts(team_config, runtime_paths)
        member = ScriptedModel(
            [answer("Done", usage(40, 4))],
            identity=ModelIdentity(id="member-model", provider="Synthetic"),
        )
        built = _scripted_team(
            calls(
                tool_call("remember", "call-remember", label="blue"),
                tool_call("delegate_task_to_member", "call-delegate", member_id="general", task="Go"),
                metrics=usage(100, 10),
            ),
            answer("All done", usage(150, 5)),
            members=[("general", "GeneralAgent", member)],
            team_id="super_team",
        )
        spec = replace(
            built.spec,
            leader=replace(built.spec.leader, toolkits=(toolkit(remember, name="memory_probe"),)),
        )

        with (
            patch("mindroom.api.openai_compat._build_team", return_value=replace(built, spec=spec)),
            _recorded_team_runs() as runs,
            _openai_client(team_config, runtime_paths) as client,
        ):
            response = client.post(
                "/v1/chat/completions",
                headers={"X-Session-Id": "team-session"},
                json={"model": "team/super_team", "messages": [{"role": "user", "content": "Go"}], "stream": stream},
            )

        assert response.status_code == 200
        [run] = runs
        storage = create_scope_session_storage(
            scope=HistoryScope(kind="team", scope_id="super_team"),
            config=team_config,
            runtime_paths=runtime_paths,
            execution_identity=None,
        )
        session = storage.load_session(run.ctx.session_id)
        assert session is not None
        assert session.session_state == {"remembered": "blue"}
        report = collect_admin_usage(config=team_config, runtime_paths=runtime_paths)
        assert {row.model: row.totals.total_tokens for row in report.model_breakdown} == {
            SYNTHETIC.id: 265,
            "member-model": 44,
        }

    def test_a_leader_answers_a_call_the_output_limit_cut_off_instead_of_running_it(
        self,
        team_app_client: TestClient,
    ) -> None:
        """The output-limit policy answers an API team leader's cut-off delegation, which then never runs.

        The API and Matrix leaders get their hooks from one builder, whose hook set ``tests/test_loop_hooks.py`` pins.
        """
        cut_off = calls(tool_call("delegate_task_to_member", "call-cut", member_id="general", task="Write it all"))
        cut_off[-1] = replace(cut_off[-1], stop_reason="output_limit")
        member = ScriptedModel([])
        built = _scripted_team(
            cut_off,
            answer("I will split the work."),
            members=[("general", "GeneralAgent", member)],
        )
        leader = built.spec.leader.model

        with patch("mindroom.api.openai_compat._build_team", return_value=built):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={"model": "team/super_team", "messages": [{"role": "user", "content": "Write it all"}]},
            )

        assert response.status_code == 200
        assert "I will split the work." in response.json()["choices"][0]["message"]["content"]
        assert member.requests == []
        assert leader.requests[1].messages[-1].content == CUT_OFF_TOOL_CALL_RESULT

    def test_team_streaming_shows_the_tool_calls_of_the_leader_and_its_members_but_only_the_leader_s_text(
        self,
        team_app_client: TestClient,
    ) -> None:
        """Every tool call streams as a block with an id its start and completion share; member text stays out."""

        def delegation(result: str | None = None) -> ToolExecution:
            return ToolExecution(
                tool_name="delegate_task_to_member",
                tool_args={"member_id": "general"},
                tool_call_id="call-team",
                result=result,
            )

        def search(result: str | None = None) -> ToolExecution:
            return ToolExecution(
                tool_name="search",
                tool_args={"query": "X"},
                tool_call_id="call-member",
                result=result,
            )

        def member(event: TextDelta | ToolStarted | ToolCompleted) -> MemberEvent:
            return MemberEvent("general", "GeneralAgent", "member-run", event)

        with (
            patch(
                "mindroom.api.openai_compat._build_team",
                return_value=_scripted_team(members=[("general", "GeneralAgent", ScriptedModel([]))]),
            ),
            patch(
                "mindroom.teams.leader.run_team",
                new=_team_events(
                    ToolStarted(delegation().call),
                    member(TextDelta("member noise")),
                    member(ToolStarted(search().call)),
                    member(ToolCompleted(search("3 results").call)),
                    ToolCompleted(delegation("Found 3").call),
                    TextDelta("Final answer"),
                    _finished("Final answer"),
                ),
            ),
        ):
            response = team_app_client.post(
                "/v1/chat/completions",
                json={"model": "team/super_team", "messages": [{"role": "user", "content": "Search"}], "stream": True},
            )

        assert response.status_code == 200
        assert _sse_contents(response.text) == [
            '<tool id="1" state="start">delegate_task_to_member(member_id=general)</tool>',
            '<tool id="2" state="start">search(query=X)</tool>',
            '<tool id="2" state="done">search(query=X)\n3 results</tool>',
            '<tool id="1" state="done">delegate_task_to_member(member_id=general)\nFound 3</tool>',
            "Final answer",
        ]

    @pytest.mark.asyncio
    async def test_prepare_openai_team_execution_renders_the_messages_as_sent(self) -> None:
        """OpenAI team prep renders the leader's messages as they are sent."""
        config = Config(
            agents={"general": AgentConfig(display_name="GeneralAgent", role="General", rooms=[])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            router=RouterConfig(model="default"),
        )
        runtime_paths = _runtime_paths()
        built = _scripted_team(members=[("general", "GeneralAgent", ScriptedModel([]))])
        leader = ScriptedModel([], model_params={"temperature": 0.7})
        built = replace(built, spec=replace(built.spec, leader=replace(built.spec.leader, model=leader)))
        scope_context = load_bound_scope_session(
            member_names=["general"],
            team_name=None,
            session_id="session-openai-team-prep",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=None,
        )
        team_execution = _prepared_team_execution_context(
            final_prompt="Previous team reply\n\nAnalyze this.",
            messages=[
                Message(role="assistant", content="Previous team reply"),
                Message(role="user", content="Analyze this."),
            ],
        )
        prepared_execution = _PreparedExecutionContext(
            turn=team_execution.turn,
            unseen_event_ids=[],
            prepared_history=team_execution.prepared_history,
        )

        with patch(
            "mindroom.teams.envelope.prepare_bound_team_run_context",
            new=AsyncMock(return_value=prepared_execution),
        ):
            prepared = await openai_compat._prepare_openai_team_execution(
                scope_context=scope_context,
                team_name="general",
                built=built,
                prompt="Analyze this.",
                config=config,
                runtime_paths=runtime_paths,
                thread_history=[],
                team_model_name="default",
                execution_identity=_team_identity("general", runtime_paths, session_id="session-openai-team-prep"),
            )

        assert prepared.prompt_text == "Previous team reply\n\nAnalyze this."
        assert prepared.run_metadata is not None
        # The run records no route settings: nothing reads them, and the request log keeps them.
        assert {"tools_schema", "model_params"}.isdisjoint(prepared.run_metadata)
        assert prepared.run_metadata[constants.AI_RUN_METADATA_KEY]["compaction"]["decision"] == "none"

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("collaborate", TeamMode.COLLABORATE), ("coordinate", TeamMode.COORDINATE)],
    )
    def test_build_team_takes_the_configured_mode_and_id(
        self,
        mode: str,
        expected: TeamMode,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A configured team is built in its configured mode under its configured name."""
        config = Config(
            agents={"general": AgentConfig(display_name="GeneralAgent", role="General", rooms=[])},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
            teams={
                "coord_team": TeamConfig(
                    display_name="Coord Team",
                    role="Coordinated team",
                    agents=["general"],
                    mode=mode,
                ),
            },
        )
        runtime_paths = _runtime_paths_for_config(config)
        scope_context = load_bound_scope_session(
            member_names=[],
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=_team_identity("coord_team", runtime_paths),
            team_name="coord_team",
        )
        install_model_providers(monkeypatch, {"default": ScriptedModel([])})
        built = openai_compat._build_team(
            "coord_team",
            config,
            runtime_paths,
            execution_identity=_team_identity("coord_team", runtime_paths),
            scope_context=scope_context,
            models=_team_models("coord_team", config, runtime_paths),
            unavailable_bases={},
            refresh_scheduler=None,
        )

        assert (built.spec.leader.id, built.spec.mode) == ("coord_team", expected)
        assert [member.id for member in built.spec.members] == ["general"]

    def test_build_team_openai_compat_does_not_require_persisted_matrix_accounts(
        self,
        team_config: Config,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """OpenAI-compatible team construction should not require Matrix account preparation."""
        runtime_paths = resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "data",
        )
        execution_identity = build_tool_execution_identity(
            channel="openai_compat",
            agent_name="team/super_team",
            session_id="openai-team-session",
            runtime_paths=runtime_paths,
            requester_id=None,
            room_id=None,
            thread_id=None,
            resolved_thread_id=None,
        )

        scope_context = load_bound_scope_session(
            member_names=[],
            session_id="openai-team-session",
            runtime_paths=runtime_paths,
            config=team_config,
            execution_identity=execution_identity,
            team_name="super_team",
        )
        install_model_providers(monkeypatch, {"default": ScriptedModel([])})
        built = openai_compat._build_team(
            "super_team",
            team_config,
            runtime_paths,
            execution_identity=execution_identity,
            scope_context=scope_context,
            models=_team_models("super_team", team_config, runtime_paths),
            unavailable_bases={},
            refresh_scheduler=None,
        )

        assert built.spec.mode is TeamMode.COORDINATE
        assert [member.id for member in built.spec.members] == ["general", "code"]
        assert not constants.matrix_state_file(runtime_paths=runtime_paths).exists()

    def test_build_team_propagates_team_build_failures(self) -> None:
        """A team that cannot be built after its members fails the build loudly."""
        config = Config(
            agents={"general": AgentConfig(display_name="GeneralAgent", role="General", rooms=[])},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            router=RouterConfig(model="default"),
            teams={
                "coord_team": TeamConfig(
                    display_name="Coord Team",
                    role="Coordinated team",
                    agents=["general"],
                    mode="coordinate",
                ),
            },
        )
        runtime_paths = _runtime_paths()
        members = _resolved_members([("general", "GeneralAgent", ScriptedModel([]))])

        scope_context = load_bound_scope_session(
            member_names=[],
            session_id="session-1",
            runtime_paths=runtime_paths,
            config=config,
            execution_identity=_team_identity("coord_team", runtime_paths),
            team_name="coord_team",
        )
        with (
            patch("mindroom.api.openai_compat.resolve_exact_team_members", return_value=members),
            patch("mindroom.api.openai_compat.build_team_spec", side_effect=RuntimeError("team build failed")),
            pytest.raises(RuntimeError, match="team build failed"),
        ):
            openai_compat._build_team(
                "coord_team",
                config,
                runtime_paths,
                execution_identity=_team_identity("coord_team", runtime_paths),
                scope_context=scope_context,
                models=_team_models("coord_team", config, runtime_paths),
                unavailable_bases={},
                refresh_scheduler=None,
            )

    def test_build_team_passes_knowledge_to_member_agents(self) -> None:
        """Team member creation resolves and passes configured knowledge."""
        from mindroom.config.knowledge import KnowledgeBaseConfig  # noqa: PLC0415

        config = Config(
            agents={
                "research": AgentConfig(
                    display_name="Research",
                    role="Research role",
                    rooms=[],
                    knowledge_bases=["docs"],
                ),
            },
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
            teams={
                "team_with_kb": TeamConfig(
                    display_name="KB Team",
                    role="Team with KB",
                    agents=["research"],
                    mode="coordinate",
                ),
            },
            knowledge_bases={"docs": KnowledgeBaseConfig(path="./docs")},
        )
        mock_knowledge = MagicMock()
        built = _scripted_team()
        resolved: list[ResolvedExactTeamMembers] = []

        def build_team_spec(*, members: ResolvedExactTeamMembers, **_kwargs: object) -> BuiltTeam:
            resolved.append(members)
            return built

        with (
            patch("mindroom.teams.members.create_agent") as mock_create,
            patch(
                "mindroom.teams.members.resolve_agent_knowledge_access",
                return_value=_KnowledgeResolution(knowledge=mock_knowledge),
            ),
            patch("mindroom.api.openai_compat.build_team_spec", side_effect=build_team_spec),
        ):
            mock_create.return_value = MagicMock(name="Research")

            runtime_paths = _runtime_paths_for_config(config)
            scope_context = load_bound_scope_session(
                member_names=[],
                session_id="session-1",
                runtime_paths=runtime_paths,
                config=config,
                execution_identity=_team_identity("team_with_kb", runtime_paths),
                team_name="team_with_kb",
            )
            openai_compat._build_team(
                "team_with_kb",
                config,
                runtime_paths,
                execution_identity=_team_identity("team_with_kb", runtime_paths),
                scope_context=scope_context,
                models=_team_models("team_with_kb", config, runtime_paths),
                unavailable_bases={},
                refresh_scheduler=None,
            )
            # The member builds on its first delegation, with the knowledge the team build resolved for it.
            mock_create.assert_not_called()
            [members] = resolved
            members.build("research", members.model_names["research"])

            assert mock_create.call_args.kwargs["knowledge"] is mock_knowledge
            assert "include_default_tools" not in mock_create.call_args.kwargs
            assert mock_create.call_args.kwargs["include_interactive_questions"] is False


# ---------------------------------------------------------------------------
# Knowledge base integration (Phase 4)
# ---------------------------------------------------------------------------


@pytest.fixture
def knowledge_config() -> Config:
    """Config with an agent that has knowledge_bases assigned."""
    from mindroom.config.knowledge import KnowledgeBaseConfig  # noqa: PLC0415

    return Config(
        agents={
            "general": AgentConfig(
                display_name="GeneralAgent",
                role="General-purpose assistant",
                rooms=[],
            ),
            "research": AgentConfig(
                display_name="ResearchAgent",
                role="Research assistant with knowledge base",
                rooms=[],
                knowledge_bases=["docs"],
            ),
        },
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
        knowledge_bases={
            "docs": KnowledgeBaseConfig(path="./test_docs"),
        },
    )


@pytest.fixture
def knowledge_app_client(knowledge_config: Config) -> Iterator[TestClient]:
    """Create a FastAPI test client with knowledge-enabled config."""
    runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})
    with _openai_client(knowledge_config, runtime_paths) as client:
        yield client


class TestKnowledgeIntegration:
    """Tests for knowledge base integration (Phase 4)."""

    def test_knowledge_passed_when_configured(self, knowledge_app_client: TestClient) -> None:
        """Knowledge is passed to ai_response when agent has knowledge_bases."""
        mock_reader = MagicMock()

        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
            patch(
                "mindroom.knowledge.utils._lookup_knowledge_for_base",
                return_value=published_index_lookup("docs", mock_reader),
            ),
        ):
            mock_ai.return_value = "Response with knowledge"

            response = knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "What do the docs say?"}],
                },
            )

        assert response.status_code == 200
        assert _knowledge_indexes(mock_ai.call_args.args[0].knowledge) == [mock_reader]

    def test_knowledge_lookup_uses_explicit_runtime_key(self, knowledge_config: Config) -> None:
        """Shared knowledge lookup should resolve by config/runtime, not the static fallback map."""
        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        mock_reader = MagicMock()
        observed_calls: list[tuple[str, Config | None, RuntimePaths | None]] = []

        def fake_lookup_knowledge_for_base(
            base_id: str,
            *,
            config: Config | None = None,
            runtime_paths: RuntimePaths | None = None,
            execution_identity: object | None = None,  # noqa: ARG001
        ) -> PublishedIndexResolution | None:
            observed_calls.append((base_id, config, runtime_paths))
            if config is None or runtime_paths is None or base_id != "docs":
                return None
            return published_index_lookup(base_id, mock_reader)

        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
            patch("mindroom.knowledge.utils._lookup_knowledge_for_base", side_effect=fake_lookup_knowledge_for_base),
            _openai_client(knowledge_config, runtime_paths) as client,
        ):
            mock_ai.return_value = "Response with keyed knowledge"
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "What do the docs say?"}],
                },
            )

        assert response.status_code == 200
        assert _knowledge_indexes(mock_ai.call_args.args[0].knowledge) == [mock_reader]
        assert observed_calls == [("docs", knowledge_config, runtime_paths)]

    def test_knowledge_none_when_not_configured(self, knowledge_app_client: TestClient) -> None:
        """Knowledge is None when agent has no knowledge_bases."""
        with patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai:
            mock_ai.return_value = "Response"

            knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert mock_ai.call_args.args[0].knowledge is None

    def test_chat_completions_does_not_await_request_path_reindex(
        self,
        knowledge_app_client: TestClient,
    ) -> None:
        """Chat completions should not await shared-manager initialization on the request path."""
        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
            patch("mindroom.knowledge.manager.KnowledgeManager.reindex_all", new_callable=AsyncMock) as reindex_all,
        ):
            mock_ai.return_value = "Response"

            knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "general",
                    "messages": [{"role": "user", "content": "Hi"}],
                },
            )

        assert reindex_all.await_count == 0

    def test_unready_kb_emits_system_hint(self, knowledge_app_client: TestClient) -> None:
        """Missing published knowledge should inject a system-style degraded hint."""
        refresh_scheduler = RecordingRefreshScheduler()

        def fake_lookup_knowledge_for_base(base_id: str, **_kwargs: object) -> PublishedIndexResolution:
            return published_index_lookup(base_id, None)

        bind_test_embedded_runtime(knowledge_app_client.app, knowledge_refresh_scheduler=refresh_scheduler)
        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
            patch("mindroom.knowledge.utils._lookup_knowledge_for_base", side_effect=fake_lookup_knowledge_for_base),
        ):
            mock_ai.return_value = "Response without knowledge"

            response = knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "Hello"}],
                },
            )

        assert response.status_code == 200
        assert mock_ai.call_args.args[0].knowledge is None
        assert (
            "Knowledge base `docs` is initializing and unavailable for semantic search this turn."
            in mock_ai.call_args.args[0].prompt
        )
        assert refresh_scheduler.scheduled_base_ids == ["docs"]

    def test_streaming_with_knowledge(self, knowledge_app_client: TestClient) -> None:
        """Knowledge is passed through in streaming mode too."""
        mock_reader = MagicMock()

        async def mock_stream(*_args: object, **_kw: object) -> AsyncIterator[TextDelta]:
            yield TextDelta("Streamed!")

        with (
            patch("mindroom.api.openai_compat.stream_agent_response", side_effect=mock_stream) as mock_stream_fn,
            patch(
                "mindroom.knowledge.utils._lookup_knowledge_for_base",
                return_value=published_index_lookup("docs", mock_reader),
            ),
        ):
            response = knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "Stream with knowledge"}],
                    "stream": True,
                },
            )

        assert response.status_code == 200
        assert _knowledge_indexes(mock_stream_fn.call_args.args[0].knowledge) == [mock_reader]

    def test_multi_knowledge_bases_merged(self, knowledge_config: Config) -> None:
        """Agent with multiple knowledge_bases gets a merged Knowledge object."""
        from mindroom.config.knowledge import KnowledgeBaseConfig  # noqa: PLC0415

        # Add a second knowledge base and assign both to the research agent
        knowledge_config.knowledge_bases["wiki"] = KnowledgeBaseConfig(path="./test_wiki")
        knowledge_config.agents["research"].knowledge_bases = ["docs", "wiki"]

        runtime_paths = _runtime_paths({"OPENAI_COMPAT_ALLOW_UNAUTHENTICATED": "true"})

        mock_reader_docs = MagicMock()
        mock_reader_wiki = MagicMock()

        def fake_lookup_knowledge_for_base(base_id: str, **_kwargs: object) -> PublishedIndexResolution | None:
            reader = {"docs": mock_reader_docs, "wiki": mock_reader_wiki}.get(base_id)
            return published_index_lookup(base_id, reader) if reader is not None else None

        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
            patch("mindroom.knowledge.utils._lookup_knowledge_for_base", side_effect=fake_lookup_knowledge_for_base),
            _openai_client(knowledge_config, runtime_paths) as client,
        ):
            mock_ai.return_value = "Merged knowledge response"
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "Multi-KB query"}],
                },
            )

        assert response.status_code == 200
        knowledge_arg = mock_ai.call_args.args[0].knowledge
        assert _knowledge_indexes(knowledge_arg) == [mock_reader_docs, mock_reader_wiki]
        assert [source.description.base_id for source in knowledge_arg.sources] == ["docs", "wiki"]
        assert knowledge_arg.max_results == 10

    def test_knowledge_resolution_failure_graceful_fallback(self, knowledge_app_client: TestClient) -> None:
        """When knowledge resolution fails, request proceeds with knowledge=None."""
        with (
            patch("mindroom.api.openai_compat.ai_response", new_callable=completed_ai_response_mock) as mock_ai,
            patch(
                "mindroom.api.openai_compat.resolve_agent_knowledge_access_async",
                new_callable=AsyncMock,
                side_effect=RuntimeError("DB connection failed"),
            ),
        ):
            mock_ai.return_value = "Response without knowledge"

            response = knowledge_app_client.post(
                "/v1/chat/completions",
                json={
                    "model": "research",
                    "messages": [{"role": "user", "content": "Query with broken KB"}],
                },
            )

        assert response.status_code == 200
        assert mock_ai.call_args.args[0].knowledge is None
        data = response.json()
        assert data["choices"][0]["message"]["content"] == "Response without knowledge"


@pytest.mark.parametrize("stream", [False, True])
def test_response_activity_tracks_full_openai_request(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """OpenAI work remains visible through generation and streaming finalization."""
    state = config_lifecycle.app_state(app_client.app)
    observed: list[int] = []

    async def complete(*_args: object, **_kwargs: object) -> JSONResponse | StreamingResponse:
        observed.append(len(state.openai_responses))
        if not stream:
            return JSONResponse({"ok": True})

        async def chunks() -> AsyncIterator[str]:
            observed.append(len(state.openai_responses))
            yield "data: hello\n\n"
            observed.append(len(state.openai_responses))

        return StreamingResponse(chunks())

    monkeypatch.setattr(openai_compat, "_chat_completions", complete)
    response = app_client.post(
        "/v1/chat/completions",
        json={"model": "general", "messages": [{"role": "user", "content": "Hello"}], "stream": stream},
    )
    assert response.status_code == 200, response.text
    assert observed == ([1, 1, 1] if stream else [1])
    assert len(state.openai_responses) == 0


@pytest.mark.parametrize("stream", [False, True])
def test_response_activity_releases_failed_openai_request(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """Generation and stream failures cannot leak an activity slot."""
    state = config_lifecycle.app_state(app_client.app)
    observed: list[int] = []

    async def complete(*_args: object, **_kwargs: object) -> JSONResponse | StreamingResponse:
        observed.append(len(state.openai_responses))
        if not stream:
            message = "generation failed"
            raise RuntimeError(message)

        async def chunks() -> AsyncIterator[str]:
            observed.append(len(state.openai_responses))
            yield "data: hello\n\n"
            message = "generation failed"
            raise RuntimeError(message)

        return StreamingResponse(chunks())

    monkeypatch.setattr(openai_compat, "_chat_completions", complete)
    with pytest.raises(RuntimeError, match="generation failed"):
        app_client.post(
            "/v1/chat/completions",
            json={"model": "general", "messages": [{"role": "user", "content": "Hello"}], "stream": stream},
        )
    assert observed == ([1, 1] if stream else [1])
    assert len(state.openai_responses) == 0


@pytest.mark.parametrize("stream", [False, True])
def test_response_activity_releases_cancelled_openai_request(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    stream: bool,
) -> None:
    """A cancelled HTTP request releases activity only after its stream unwinds."""
    import httpx  # noqa: PLC0415

    state = config_lifecycle.app_state(app_client.app)

    async def check() -> None:
        started = asyncio.Event()
        unwound: list[int] = []

        async def wait_for_cancel() -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                unwound.append(len(state.openai_responses))

        async def complete(*_args: object, **_kwargs: object) -> JSONResponse | StreamingResponse:
            if not stream:
                await wait_for_cancel()
                return JSONResponse({})

            async def chunks() -> AsyncIterator[str]:
                yield "data: hello\n\n"
                await wait_for_cancel()

            return StreamingResponse(chunks())

        monkeypatch.setattr(openai_compat, "_chat_completions", complete)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_client.app),
            base_url="http://localhost",
        ) as client:
            task = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    json={"model": "general", "messages": [{"role": "user", "content": "Hello"}], "stream": stream},
                ),
            )
            try:
                await asyncio.wait_for(started.wait(), timeout=2)
                assert len(state.openai_responses) == 1
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert unwound == [1]
        assert len(state.openai_responses) == 0

    asyncio.run(check())
