"""Real toolkit calls retain their semantics across the worker JSON boundary."""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml
from agno.agent import Agent
from agno.models.openai import OpenAIChat
from agno.run import RunContext
from agno.team import Team
from agno.tools import openai as agno_openai

from mindroom.agents import create_agent
from mindroom.api import sandbox_runner
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.custom_tools.browser import BrowserTools
from mindroom.desktop.client import DesktopResponseRouter
from mindroom.desktop.protocol import DesktopResponse
from mindroom.message_target import MessageTarget
from mindroom.tool_jobs.execution import agent_tool_middleware
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.agno_toolkits import ToolCallActor
from mindroom.tool_system.declarations import tool_schema_source
from mindroom.tool_system.invocation import ToolCallContext, execute_tool_call
from mindroom.tool_system.runtime_context import (
    build_execution_identity_from_runtime_context,
    get_tool_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import descriptive_worker_id_for_key, resolve_worker_key
from tests.agent_tool_helpers import offered_function, run_offered, source_toolkit
from tests.authorization_helpers import make_test_tool_runtime_context
from tests.conftest import (
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    write_config_yaml,
)
from tests.identity_helpers import persist_entity_accounts
from tests.test_agent_worker_routing import _create_routing_agent

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.agent_loop.build import AgentBuild
    from mindroom.desktop.protocol import DesktopCommand
    from mindroom.matrix.olm_to_device import PinnedMatrixDevice
    from mindroom.tool_system.invocation import ToolCall, ToolNext
    from mindroom.tool_system.runtime_context import ToolRuntimeContext

_MODEL_KEY = "dummy-primary-model-key-not-granted-to-worker"
_TOOL_KEY = "dummy-media-tool-key"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_routing_preserves_pandas_frames_across_calls(
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
    created = await run_offered(
        agent,
        "create_pandas_dataframe",
        {
            "dataframe_name": "retained",
            "create_using_function": "DataFrame",
            "function_parameters": {"data": {"value": [42]}},
        },
    )
    assert not created.is_error
    assert created.content == "retained"

    operated = await run_offered(
        agent,
        "run_dataframe_operation",
        {"dataframe_name": "retained", "operation": "head", "operation_parameters": {}},
    )
    assert not operated.is_error
    assert "42" in operated.content


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_routing_preserves_sql_memory_database_across_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    explicit: bool,
) -> None:
    """An in-memory SQL engine must survive successive toolkit calls."""

    def no_worker(*_args: object, **_kwargs: object) -> object:
        pytest.fail("SQL call was sent to a fresh worker toolkit")

    monkeypatch.setattr(sandbox_proxy, "_call_proxy_sync", no_worker)
    process_env = {"MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid"}
    # A database URL can carry a password, so it is a stored credential rather than an inline override.
    get_runtime_credentials_manager(
        resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "storage",
            process_env=process_env,
        ),
    ).save_credentials(
        "sql",
        {"db_url": f"sqlite:///file:{tmp_path.name}?mode=memory&cache=shared&uri=true&check_same_thread=false"},
    )
    agent = _create_routing_agent(
        tmp_path,
        process_env,
        agent_settings={"tools": ["sql"], **({"worker_tools": ["sql"]} if explicit else {})},
    )
    toolkit = source_toolkit(offered_function(agent, "run_sql_query"))
    try:
        created = await run_offered(agent, "run_sql_query", {"query": "CREATE TABLE retained (value INTEGER)"})
        assert not created.is_error
        returned = await run_offered(
            agent,
            "run_sql_query",
            {"query": "SELECT name FROM sqlite_master WHERE name='retained'"},
        )
        assert not returned.is_error
        assert json.loads(returned.content) == [{"name": "retained"}]
    finally:
        toolkit.db_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("media_kind", ["image", "audio"])
async def test_worker_openai_media_preserves_typed_result_without_primary_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker_requests: list[dict[str, object]],
    media_kind: str,
) -> None:
    """Only provider IO is stubbed; argument injection, proxy and runner stay real."""
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
    name, arguments = (
        ("generate_image", {"prompt": "Offline image"})
        if media_kind == "image"
        else ("generate_speech", {"text_input": "Offline speech"})
    )
    # The primary's run state stays on the primary, though the call's injected agent carries it.
    context = ToolCallContext(run_id="run", session_id="session", session_state={"secret": _MODEL_KEY})
    response = await run_offered(agent, name, arguments, context=context)

    assert not response.is_error
    assert len(worker_requests) == 1
    assert _MODEL_KEY not in json.dumps(worker_requests)
    if media_kind == "audio":
        assert worker_requests[0]["kwargs"]["agent"] is None
    media = response.images if media_kind == "image" else response.audios
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
def test_declared_unused_agent_keeps_call_arguments(positional: bool, tmp_path: Path) -> None:
    """Discard only the declared runtime slot while retaining normal call data."""

    def entrypoint(agent: object, path: Path, *, options: object) -> None:
        del agent, path, options

    owner = ToolCallActor(session_state={"secret": _MODEL_KEY})
    path = tmp_path / "output.wav"
    options = {"values": (1, True, None, 2.5)}
    args = (owner, path) if positional else ()
    kwargs = {"options": options} if positional else {"agent": owner, "path": path, "options": options}

    encoded_args, encoded_kwargs = sandbox_proxy._worker_call_arguments(
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
        sandbox_proxy._worker_call_arguments((), {1: "value"}, entrypoint=None, inert_agent=False)  # type: ignore[invalid-argument-type]


def _context_agent(
    tmp_path: Path,
    tool_name: str,
    *,
    routing: str = "url",
    tool_config: dict[str, object] | None = None,
    extra_env: dict[str, str] | None = None,
    roomless: bool = False,
    require_approval: bool = False,
) -> tuple[AgentBuild, ToolRuntimeContext]:
    """Materialize a real agent with authored config and matching requester context."""
    process_env = {
        "MINDROOM_SANDBOX_PROXY_URL": "http://sandbox.invalid",
        "MINDROOM_SANDBOX_PROXY_TOKEN": "dummy-worker-token",
        **(extra_env or {}),
    }
    if routing != "url":
        process_env["MINDROOM_SANDBOX_EXECUTION_MODE"] = "off" if routing == "explicit" else "all"
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env=process_env,
    )
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "administrators": ["@alice:example.test"],
            "tool_approval": {"default": "require_approval" if require_approval else "auto_approve"},
            "agents": {
                "routing": {
                    "display_name": "Routing",
                    "role": "Original role",
                    "tools": [{tool_name: tool_config}] if tool_config else [tool_name],
                    "worker_tools": [tool_name] if routing == "explicit" else None,
                    "worker_scope": "user_agent" if not roomless else None,
                    "include_default_tools": False,
                    "memory_backend": "none",
                },
            },
        },
        runtime_paths,
    )
    write_config_yaml(config, runtime_paths.config_path)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "router", "routing": "routing"})
    context = make_test_tool_runtime_context(
        agent_name="routing",
        target=MessageTarget.resolve(room_id="!tools:example.test", thread_id="$thread", reply_to_event_id=None),
        requester_id="@alice:example.test",
        client=make_matrix_client_mock(),
        config=config,
        runtime_paths=runtime_paths,
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
    )
    agent = create_agent(
        "routing",
        config,
        runtime_paths,
        execution_identity=None if roomless else build_execution_identity_from_runtime_context(context),
        include_interactive_questions=False,
        persist_runtime_state=False,
        supports_native_tool_approval=True,
    )
    return agent, context


@pytest.fixture
def forbid_worker_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Primary-owned calls must never reach worker allocation or credential grants."""

    def forbidden(*_args: object, **_kwargs: object) -> object:
        pytest.fail("Primary-owned tool call attempted to lease a worker")

    monkeypatch.setattr(sandbox_proxy, "lease_primary_worker_manager", forbidden)


@pytest.fixture
def desktop_commands(monkeypatch: pytest.MonkeyPatch) -> list[DesktopCommand]:
    """Stub only desktop transport, retaining command construction and live context."""
    commands: list[DesktopCommand] = []

    async def request(
        router: DesktopResponseRouter,
        target: PinnedMatrixDevice,
        command: DesktopCommand,
        *,
        timeout_seconds: float,
    ) -> DesktopResponse:
        context = get_tool_runtime_context()
        assert context is not None
        assert router._client_ref() is context.client
        assert target.user_id == "@desktop:example.test"
        assert command.requester_id == context.requester_id
        assert command.agent_name == context.agent_name
        assert timeout_seconds > 0
        commands.append(command)
        return DesktopResponse(
            request_id=command.request_id,
            session_id=command.session_id,
            ok=True,
            result={"provider": "desktop", "result": "page tree"},
        )

    monkeypatch.setattr(DesktopResponseRouter, "request", request)
    return commands


_DESKTOP_CONFIG = {
    "device_user_id": "@desktop:example.test",
    "device_id": "DESKTOP",
    "device_ed25519": "fingerprint",
}


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid_worker_lease")
@pytest.mark.parametrize("routing", ["url", "all", "explicit"])
async def test_config_manager_mutation_keeps_primary_runtime(tmp_path: Path, routing: str) -> None:
    """Authorized config mutations persist the primary file under broad and explicit routing."""
    agent, context = _context_agent(tmp_path, "config_manager", routing=routing)
    with tool_runtime_context(context):
        response = await run_offered(
            agent,
            "manage_config",
            {
                "operation": "patch",
                "changes": [{"op": "replace", "path": "/agents/routing/role", "value": "Updated role"}],
            },
        )

    assert not response.is_error
    assert "Authored configuration patch updated" in response.content
    assert yaml.safe_load(context.runtime_paths.config_path.read_text())["agents"]["routing"]["role"] == "Updated role"


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid_worker_lease")
async def test_config_manager_remains_available_for_roomless_inspection(tmp_path: Path) -> None:
    """Primary ownership must not introduce a room requirement for read-only inspection."""
    agent, _context = _context_agent(tmp_path, "config_manager", roomless=True)
    with tool_runtime_context(None):
        response = await run_offered(agent, "get_info", {"info_type": "agents"})

    assert not response.is_error
    assert "Routing" in response.content


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid_worker_lease")
@pytest.mark.parametrize("routing", ["url", "all", "explicit"])
async def test_vault_access_uses_primary_owner_token_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    routing: str,
) -> None:
    """Self-service grants keep the primary-mounted owner token and requester worker identity."""
    token_file = tmp_path / "owner-token"
    token_file.write_text("owner-token-value\n")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(201, json={})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    agent, context = _context_agent(
        tmp_path,
        "agent_vault_access",
        routing=routing,
        extra_env={
            "MATRIX_HOMESERVER": "https://example.test",
            "MINDROOM_AGENT_VAULT_ACCESS_API_URL": "https://vault.example.test",
            "MINDROOM_AGENT_VAULT_ACCESS_ADMIN_TOKEN_FILE": str(token_file),
            "MINDROOM_AGENT_VAULT_ACCESS_UI_BASE_URL": "https://vault.example.test/ui",
            "MINDROOM_AGENT_VAULT_ACCESS_EMAIL_DOMAIN": "example.test",
        },
    )
    with tool_runtime_context(context):
        response = await run_offered(agent, "request_vault_access")

    assert not response.is_error
    payload = json.loads(response.content)
    assert payload["access"] == "granted"
    identity = build_execution_identity_from_runtime_context(context)
    worker_key = resolve_worker_key("user_agent", identity)
    assert worker_key is not None
    assert payload["vault"] == descriptive_worker_id_for_key(worker_key, prefix="agent-vault")
    assert requests
    assert {request.headers["authorization"] for request in requests} == {"Bearer owner-token-value"}
    grants = [json.loads(request.content) for request in requests if request.url.path.endswith("/users")]
    assert grants == [{"email": "alice@example.test", "role": "admin"}]


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid_worker_lease")
@pytest.mark.parametrize("routing", ["url", "all", "explicit"])
@pytest.mark.parametrize(("default_target", "target"), [("desktop", None), ("host", "desktop")])
async def test_browser_desktop_keeps_primary_matrix_context(
    tmp_path: Path,
    desktop_commands: list[DesktopCommand],
    routing: str,
    default_target: str,
    target: str | None,
) -> None:
    """Desktop default and explicit desktop calls use the caller's Matrix device without a worker lease."""
    agent, context = _context_agent(
        tmp_path,
        "browser",
        routing=routing,
        tool_config={**_DESKTOP_CONFIG, "default_target": default_target},
    )
    arguments: dict[str, object] = {"action": "snapshot", "maxChars": 200}
    if target is not None:
        arguments["target"] = target
    with tool_runtime_context(context):
        response = await run_offered(agent, "browser_control", arguments)

    assert not response.is_error
    assert json.loads(response.content)["provider"] == "desktop"
    assert len(desktop_commands) == 1
    assert desktop_commands[0].action == "browser_observe"
    assert desktop_commands[0].parameters == {
        "browser_action": "snapshot",
        "browser_parameters": {"maxChars": 200},
    }


@pytest.mark.asyncio
async def test_browser_host_override_keeps_worker_transport_schema_and_hooks(
    tmp_path: Path,
    desktop_commands: list[DesktopCommand],
    worker_requests: list[dict[str, object]],
) -> None:
    """One approved function can dispatch desktop then host while keeping its schema and hook chain."""
    agent, context = _context_agent(
        tmp_path / "primary",
        "browser",
        tool_config={**_DESKTOP_CONFIG, "default_target": "desktop"},
        require_approval=True,
    )
    function = offered_function(agent, "browser_control")
    toolkit = source_toolkit(function)
    assert isinstance(toolkit, BrowserTools)
    assert inspect.unwrap(tool_schema_source(function.entrypoint)) == toolkit.browser
    schema = json.loads(json.dumps(function.parameters))
    assert function.requires_confirmation is True
    assert function.approval_type == "mindroom_policy"
    assert agent_tool_middleware(agent)
    hook_calls: list[tuple[str, str]] = []

    async def observe(call: ToolCall, call_next: ToolNext) -> object:
        assert call.function.name == "browser_control"
        assert get_tool_runtime_context() is context
        target = str(call.arguments["target"])
        hook_calls.append(("before", target))
        result = await call_next(call.arguments)
        hook_calls.append(("after", target))
        return result

    middleware = (*agent_tool_middleware(agent), observe)
    run = ToolCallContext(run_id="run", session_id="session")
    with tool_runtime_context(context):
        desktop = await execute_tool_call(
            function,
            {"action": "snapshot", "target": "desktop"},
            run,
            middleware=middleware,
        )
        assert not desktop.is_error
        assert worker_requests == []
        host = await execute_tool_call(function, {"action": "status", "target": "host"}, run, middleware=middleware)

    assert not host.is_error
    assert json.loads(host.content)["running"] is False
    assert len(desktop_commands) == len(worker_requests) == 1
    assert worker_requests[0]["kwargs"]["target"] == "host"
    assert hook_calls == [("before", "desktop"), ("after", "desktop"), ("before", "host"), ("after", "host")]
    assert function.parameters == schema
    assert function.requires_confirmation is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid_worker_lease")
async def test_browser_primary_placement_binds_positional_arguments(
    tmp_path: Path,
    desktop_commands: list[DesktopCommand],
) -> None:
    """Placement binds positional arguments against the actual browser signature."""
    agent, context = _context_agent(tmp_path, "browser", tool_config=_DESKTOP_CONFIG)
    function = offered_function(agent, "browser_control")
    with tool_runtime_context(context):
        result = await function.entrypoint("snapshot", " desktop ")

    assert json.loads(result)["provider"] == "desktop"
    assert len(desktop_commands) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("forbid_worker_lease")
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"target": "sandbox"}, "does not support sandbox or node"),
        ({"target": "host", "node": "unsupported"}, "node parameter is not supported"),
        ({"target": "desktop"}, "configured Matrix desktop device identity"),
    ],
)
async def test_browser_invalid_targets_fail_before_worker_lease(
    tmp_path: Path,
    arguments: dict[str, object],
    error: str,
) -> None:
    """Target validation and desktop configuration errors remain local and allocate nothing."""
    agent, context = _context_agent(tmp_path, "browser")
    with tool_runtime_context(context):
        result = await run_offered(agent, "browser_control", {"action": "status", **arguments})

    assert result.is_error
    assert error in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["help", "actions"])
async def test_browser_discovery_keeps_existing_worker_behavior(
    tmp_path: Path,
    worker_requests: list[dict[str, object]],
    action: str,
) -> None:
    """Discovery ignores target/node hints as before, including a desktop default."""
    agent, context = _context_agent(
        tmp_path / "primary",
        "browser",
        tool_config={**_DESKTOP_CONFIG, "default_target": "desktop"},
    )
    with tool_runtime_context(context):
        response = await run_offered(
            agent,
            "browser_control",
            {"action": action, "target": "sandbox", "node": "ignored"},
        )

    assert not response.is_error
    assert json.loads(response.content)["actionTable"]
    assert len(worker_requests) == 1
