"""Real toolkit calls retain their semantics across the worker JSON boundary."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.run import RunContext
from agno.team import Team
from agno.tools import openai as agno_openai
from agno.tools.function import FunctionCall, ToolResult
from agno.tools.sql import SQLTools
from agno.tools.toolkit import Toolkit

from mindroom.api import sandbox_runner
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.worker_arguments import prepare_worker_call_arguments
from tests.test_agent_worker_routing import _create_routing_agent

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.function import Function

_MODEL_KEY = "dummy-primary-model-key-not-granted-to-worker"
_TOOL_KEY = "dummy-media-tool-key"


def _function(agent: Agent, name: str) -> Function:
    toolkit = next(tool for tool in agent.tools or [] if isinstance(tool, Toolkit) and name in tool.functions)
    return toolkit.functions[name]


@pytest.fixture
def worker_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Use the real runner behind an in-memory HTTP transport."""
    requests: list[dict[str, object]] = []
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "worker" / "config.yaml",
        storage_path=tmp_path / "worker" / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_MODE": "true", "OPENAI_API_KEY": _TOOL_KEY},
    )
    config = Config.validate_with_runtime({}, runtime_paths)
    client_type = httpx.Client

    def execute(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sandbox.invalid"
        assert request.url.path.endswith("/execute")
        payload = json.loads(request.content)
        requests.append(payload)
        prepared = sandbox_runner.PreparedSandboxRunnerExecuteRequest.model_validate(payload)
        response = asyncio.run(sandbox_runner._execute_prepared_request_inprocess(prepared, runtime_paths, config))
        return httpx.Response(200, json=response.model_dump(mode="json"))

    monkeypatch.setattr(
        sandbox_proxy.httpx,
        "Client",
        lambda **kwargs: client_type(transport=httpx.MockTransport(execute), **kwargs),
    )
    monkeypatch.setenv("OPENAI_API_KEY", _TOOL_KEY)
    return requests


@pytest.mark.parametrize(
    "tool_result",
    [
        {"mindroom_tool_result": {"version": 1, "kind": "json", "value": 7}},
        {"mindroom_tool_result": {"version": 777}},
        {"mindroom_tool_result": "ordinary data", "other": [1, 2]},
    ],
)
def test_ordinary_json_is_opaque_across_worker_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_requests: list[dict[str, object]],
    tool_result: dict[str, object],
) -> None:
    """A toolkit's JSON keys cannot turn its data into a transport control record."""

    async def entrypoint(*_args: object, **_kwargs: object) -> object:
        return tool_result

    monkeypatch.setattr(sandbox_runner, "_run_toolkit_entrypoint", entrypoint)
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid",
            "MINDROOM_SANDBOX_PROXY_TOKEN": "dummy-worker-token",
        },
    )

    result = sandbox_proxy._call_proxy_sync(
        runtime_paths=paths,
        tool_name="calculator",
        function_name="add",
        args=(),
        kwargs={"a": 1, "b": 2},
        credentials_manager=None,
    )

    assert len(worker_requests) == 1
    assert result == tool_result


@pytest.mark.parametrize("explicit", [False, True])
def test_routing_preserves_pandas_frames_across_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    """Named frames created by one call remain available to the next call."""

    def no_worker(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Stateful Pandas call was sent to a fresh worker toolkit")

    monkeypatch.setattr(sandbox_proxy, "_call_proxy_sync", no_worker)
    agent = _create_routing_agent(
        tmp_path,
        {"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
        agent_settings={"tools": ["pandas"], **({"worker_tools": ["pandas"]} if explicit else {})},
    )
    created = FunctionCall(
        function=_function(agent, "create_pandas_dataframe"),
        arguments={
            "dataframe_name": "retained",
            "create_using_function": "DataFrame",
            "function_parameters": {"data": {"value": [42]}},
        },
    ).execute()
    assert created.status == "success"
    assert created.result == "retained"

    operated = FunctionCall(
        function=_function(agent, "run_dataframe_operation"),
        arguments={"dataframe_name": "retained", "operation": "head", "operation_parameters": {}},
    ).execute()
    assert operated.status == "success"
    assert "42" in operated.result


@pytest.mark.parametrize("explicit", [False, True])
def test_routing_preserves_sql_memory_database_across_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    """An in-memory SQL engine must survive successive toolkit calls."""

    def no_worker(*_args: object, **_kwargs: object) -> object:
        pytest.fail("SQL call was sent to a fresh worker toolkit")

    monkeypatch.setattr(sandbox_proxy, "_call_proxy_sync", no_worker)
    agent = _create_routing_agent(
        tmp_path,
        {"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
        agent_settings={
            "tools": [
                {
                    "sql": {
                        "db_url": f"sqlite:///file:{tmp_path.name}?mode=memory&cache=shared&uri=true&check_same_thread=false",
                    },
                },
            ],
            **({"worker_tools": ["sql"]} if explicit else {}),
        },
    )
    toolkit = next(tool for tool in agent.tools or [] if isinstance(tool, SQLTools))
    try:
        created = FunctionCall(
            function=toolkit.functions["run_sql_query"],
            arguments={"query": "CREATE TABLE retained (value INTEGER)"},
        ).execute()
        assert created.status == "success"
        returned = FunctionCall(
            function=toolkit.functions["run_sql_query"],
            arguments={"query": "SELECT name FROM sqlite_master WHERE name='retained'"},
        ).execute()
        assert returned.status == "success"
        assert json.loads(returned.result) == [{"name": "retained"}]
    finally:
        toolkit.db_engine.dispose()


@pytest.mark.parametrize("media_kind", ["image", "audio"])
def test_worker_openai_media_preserves_typed_result_without_primary_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_requests: list[dict[str, object]],
    media_kind: str,
) -> None:
    """Only provider IO is stubbed; Agno injection, proxy and runner stay real."""
    content = b"provider-media-bytes"
    provider = SimpleNamespace(
        images=SimpleNamespace(
            generate=lambda **_kwargs: SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(content).decode("ascii"))],
            ),
        ),
        audio=SimpleNamespace(speech=SimpleNamespace(create=lambda **_kwargs: SimpleNamespace(content=content))),
    )
    monkeypatch.setattr(agno_openai, "OpenAIClient", lambda **_kwargs: provider)
    agent = _create_routing_agent(
        tmp_path / "primary",
        {
            "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid",
            "MINDROOM_SANDBOX_PROXY_TOKEN": "dummy-worker-token",
            "OPENAI_API_KEY": _TOOL_KEY,
        },
        agent_settings={"tools": ["openai"]},
    )
    agent.model = OpenAIChat(id="gpt-6-astra", api_key=_MODEL_KEY)
    name, arguments = (
        ("generate_image", {"prompt": "Offline image"})
        if media_kind == "image"
        else ("generate_speech", {"text_input": "Offline speech"})
    )
    function = _function(agent, name)
    function._agent = agent
    response = FunctionCall(function=function, arguments=arguments).execute()

    assert response.status == "success"
    assert len(worker_requests) == 1
    assert _MODEL_KEY not in json.dumps(worker_requests)
    if media_kind == "audio":
        assert worker_requests[0]["kwargs"]["agent"] is None
    assert isinstance(response.result, ToolResult)
    media = response.result.images if media_kind == "image" else response.result.audios
    assert media is not None
    assert len(media) == 1
    assert media[0].content == content


@pytest.mark.parametrize("shape", ["value", "nested", "key", "nonfinite", "positional", "agent", "context", "team"])
def test_invalid_arguments_rejected_before_worker_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
) -> None:
    """Unsupported arguments must never be rendered, provision a worker, or obtain grants."""

    class Opaque:
        def __str__(self) -> str:
            msg = "Input object must not be rendered"
            raise AssertionError(msg)

        __repr__ = __str__

    def provision_forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Invalid arguments reached worker provisioning")

    monkeypatch.setattr(sandbox_proxy, "_primary_worker_manager_context", provision_forbidden)
    value: object = {
        "value": Opaque(),
        "nested": {"payload": [Opaque()]},
        "key": {Opaque(): "value"},
        "nonfinite": float("nan"),
        "positional": Opaque(),
        "agent": Agent(model=OpenAIChat(id="gpt-6-astra", api_key=_MODEL_KEY)),
        "context": {"alias": RunContext(run_id="run", session_id="session", session_state={"secret": _MODEL_KEY})},
        "team": Team(members=[]),
    }[shape]
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"},
    )
    with pytest.raises((TypeError, ValueError), match=r"[Aa]rgument"):
        sandbox_proxy._call_proxy_sync(
            runtime_paths=paths,
            tool_name="calculator",
            function_name="add",
            args=(value,) if shape == "positional" else (),
            kwargs={} if shape == "positional" else {"value": value},
            credentials_manager=None,
        )


@pytest.mark.parametrize("positional", [True, False])
@pytest.mark.parametrize("use_team", [True, False])
def test_declared_unused_agent_keeps_call_arguments(positional: bool, use_team: bool, tmp_path: Path) -> None:
    """Discard only the declared runtime slot while retaining normal call data."""

    def entrypoint(agent: Agent | Team | None, path: Path, *, options: object) -> None:
        del agent, path, options

    owner = Team(members=[]) if use_team else Agent()
    path = tmp_path / "output.wav"
    options = {"values": (1, True, None, 2.5)}
    args = (owner, path) if positional else ()
    kwargs = {"options": options} if positional else {"agent": owner, "path": path, "options": options}

    encoded_args, encoded_kwargs = prepare_worker_call_arguments(
        args,
        kwargs,
        entrypoint=entrypoint,
        inert_agent=True,
    )

    expected_options = {"values": [1, True, None, 2.5]}
    assert encoded_args == ([None, str(path)] if positional else [])
    assert encoded_kwargs == (
        {"options": expected_options} if positional else {"agent": None, "path": str(path), "options": expected_options}
    )


def test_worker_keyword_names_must_be_strings() -> None:
    """The boundary rejects non-JSON keys instead of silently coercing them."""
    with pytest.raises(TypeError, match="worker argument"):
        prepare_worker_call_arguments((), {1: "value"}, entrypoint=None, inert_agent=False)  # type: ignore[invalid-argument-type]
