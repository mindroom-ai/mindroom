"""Bounded personal tool discovery and execution through canonical toolkits."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from agno.tools import Toolkit
from agno.tools.function import Function
from mcp.types import CallToolResult, TextContent

from mindroom import agents, constants
from mindroom.api.config_lifecycle import ApiSnapshot
from mindroom.api.personal_agent import resolve_personal_agent
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.credentials import get_runtime_credentials_manager, save_scoped_credentials
from mindroom.hooks import ToolAfterCallContext, ToolBeforeCallContext, hook
from mindroom.mcp.config import MCPServerConfig
from mindroom.mcp.manager import MCPServerManager
from mindroom.mcp_gateway import toolkits as gateway_toolkits
from mindroom.mcp_gateway import tools as gateway
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.tool_system.catalog import TOOL_METADATA, ConfigField, ensure_tool_registry_loaded
from mindroom.tool_system.registry_state import BUILTIN_TOOL_METADATA, BUILTIN_TOOL_REGISTRY, TOOL_REGISTRY
from mindroom.tool_system.runtime_context import (
    get_tool_runtime_context,
    get_worker_runtime_context,
    tool_runtime_context,
)
from mindroom.tool_system.worker_routing import get_tool_execution_identity, tool_execution_identity
from tests.test_mcp_manager import _FakeClientSession, _patch_manager, _tool
from tests.test_tool_hooks import _tool_runtime_context

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.api.personal_agent import PersonalAgentContext


@pytest.fixture
def context(tmp_path: Path) -> PersonalAgentContext:
    """Use the same personal scope resolver as the HTTP boundary."""
    paths = constants.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_CONNECTIONS_AGENT": "personal",
            "MATRIX_HOMESERVER": "https://example.org",
            "MINDROOM_PUBLIC_URL": "https://assistant.example.org",
        },
    )
    config = Config.model_validate(
        {
            "defaults": {"tools": []},
            "models": {"default": {"provider": "ollama", "id": "test-model"}},
            "agents": {
                "personal": {
                    "display_name": "Personal assistant",
                    "role": "Personal assistant",
                    "tools": ["calculator", {"name": "duckduckgo", "defer": True}],
                    "private": {"per": "user_agent"},
                    "access": {"users": ["@alice:example.org", "@bob:example.org"]},
                },
            },
        },
    )
    return resolve_personal_agent(
        ApiSnapshot(generation=1, runtime_paths=paths, config_data=config.model_dump(), runtime_config=config),
        "@alice:example.org",
    )


@pytest.mark.asyncio
async def test_metadata_search_never_constructs_toolkit(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Metadata search never constructs toolkit."""

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Metadata search constructed a toolkit")

    monkeypatch.setattr("mindroom.agents.build_agent_toolkit", forbidden)
    result = await gateway.search_tools(context)
    assert "error" not in result
    assert {item["toolkit"] for item in result["results"]} == {"calculator", "duckduckgo"}
    assert "inputSchema" not in json.dumps(result)
    limited = await gateway.search_tools(context, limit=1)
    assert "error" not in limited
    assert len(limited["results"]) == 1


@pytest.mark.asyncio
async def test_selected_schema_builds_only_selected_toolkit(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selected schema builds only selected toolkit."""
    original = agents.build_agent_toolkit

    def selected(name: str, **kwargs: object) -> Toolkit:
        assert name == "calculator", "Unrelated backend initialized"
        return original(name, **cast("dict[str, Any]", kwargs))

    monkeypatch.setattr(agents, "build_agent_toolkit", selected)
    result = await gateway.get_tool(context, toolkit="calculator", function="add")
    assert "error" not in result
    assert result["inputSchema"]["properties"]["a"]["type"] == "number"
    call = await gateway.invoke_tool(context, toolkit="calculator", function="add", arguments={"a": 2, "b": 3})
    assert "error" not in call
    assert json.loads(call["result"])["result"] == 5


@pytest.mark.asyncio
async def test_unassigned_tool_and_unknown_function_fail_closed(context: PersonalAgentContext) -> None:
    """Unassigned tool and unknown function fail closed."""
    for toolkit, function in [("shell", "run_shell_command"), ("calculator", "run_shell_command")]:
        result = await gateway.invoke_tool(context, toolkit=toolkit, function=function, arguments={})
        assert "result" not in result
        assert "error" in result
        assert result["error"]["code"] == "tool_not_found"


@pytest.mark.asyncio
async def test_invalid_arguments_are_rejected_before_tool_body(context: PersonalAgentContext) -> None:
    """Invalid arguments are rejected before tool body."""
    for arguments in [{"a": "wrong", "b": 1}, {"a": 1}, {"a": float("nan"), "b": 1}, {"x": "a" * 65536}]:
        result = await gateway.invoke_tool(context, toolkit="calculator", function="add", arguments=arguments)
        assert "result" not in result
        assert "error" in result
        assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_policy_approval_is_excluded_and_rejected(context: PersonalAgentContext) -> None:
    """Policy approval is excluded and rejected."""
    config = context.config.model_copy(deep=True)
    config.tool_approval.default = "require_approval"
    context = replace(context, config=config)
    search_result = await gateway.search_tools(context, toolkit="calculator")
    assert "error" not in search_result
    assert search_result["results"] == []
    schema_result = await gateway.get_tool(context, toolkit="calculator", function="add")
    assert "inputSchema" not in schema_result
    assert schema_result["error"]["code"] == "approval_required"
    invocation_result = await gateway.invoke_tool(
        context,
        toolkit="calculator",
        function="add",
        arguments={"a": 1, "b": 2},
    )
    assert "result" not in invocation_result
    assert invocation_result["error"]["code"] == "approval_required"


def _replace_calculator(monkeypatch: pytest.MonkeyPatch, toolkit: Toolkit) -> None:
    monkeypatch.setattr("mindroom.agents.build_agent_toolkit", lambda *_args, **_kwargs: toolkit)


@pytest.mark.asyncio
async def test_missing_connection_is_redacted_with_portal_link(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing connection is redacted with portal link."""

    def account() -> str:
        message = "secret backend diagnostics"
        raise OAuthConnectionRequired(
            message,
            provider_id="example",
            connect_url="https://secret.invalid",
        )

    _replace_calculator(monkeypatch, Toolkit(name="calculator", tools=[account]))
    result = await gateway.invoke_tool(context, toolkit="calculator", function="account", arguments={})
    assert "result" not in result
    assert "error" in result
    assert result["error"]["code"] == "connection_required"
    assert result["error"]["connection_url"] == "https://assistant.example.org/connections"
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_authored_confirmation_never_runs_body(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Authored confirmation never runs body."""

    def account() -> str:
        pytest.fail("Approval-required body ran")

    toolkit = Toolkit(name="calculator", tools=[account])
    toolkit.functions["account"].requires_confirmation = True
    _replace_calculator(monkeypatch, toolkit)
    result = await gateway.invoke_tool(context, toolkit="calculator", function="account", arguments={})
    assert "result" not in result
    assert "error" in result
    assert result["error"]["code"] == "approval_required"


@pytest.mark.asyncio
async def test_schema_and_result_bounds(context: PersonalAgentContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Schema and result bounds."""

    def large() -> str:
        return "x" * 65536

    toolkit = Toolkit(name="calculator", tools=[large])
    _replace_calculator(monkeypatch, toolkit)
    result = await gateway.invoke_tool(context, toolkit="calculator", function="large", arguments={})
    assert "result" not in result
    assert "error" in result
    assert result["error"]["code"] == "result_too_large"
    toolkit.functions["large"] = Function(
        name="large",
        parameters={"description": "x" * 32768},
        skip_entrypoint_processing=True,
    )
    result = await gateway.get_tool(context, toolkit="calculator", function="large")
    assert "inputSchema" not in result
    assert "error" in result
    assert result["error"]["code"] == "schema_too_large"


@pytest.mark.asyncio
async def test_backend_failure_does_not_leak_or_block_another(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend failure does not leak or block another."""
    original = agents.build_agent_toolkit

    def selected(name: str, **kwargs: object) -> Toolkit:
        if name == "duckduckgo":
            message = "secret backend token"
            raise RuntimeError(message)
        return original(name, **cast("dict[str, Any]", kwargs))

    monkeypatch.setattr(agents, "build_agent_toolkit", selected)
    result = await gateway.search_tools(context, toolkit="duckduckgo")
    assert "results" not in result
    assert "error" in result
    assert result["error"]["code"] == "tool_unavailable"
    assert "secret" not in json.dumps(result)
    assert "inputSchema" in await gateway.get_tool(context, toolkit="calculator", function="add")


@pytest.mark.asyncio
@pytest.mark.parametrize("async_body", [False, True])
async def test_native_body_failure_returns_redacted_error_without_retry(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
    async_body: bool,
) -> None:
    """A failed Agno execution is an error for both synchronous and asynchronous bodies."""
    calls: list[str] = []

    def fail_sync() -> str:
        calls.append("executed")
        message = "secret backend diagnostics"
        raise RuntimeError(message)

    async def fail_async() -> str:
        return fail_sync()

    entrypoint = fail_async if async_body else fail_sync
    _replace_calculator(monkeypatch, Toolkit(name="calculator", tools=[entrypoint]))
    result = await gateway.invoke_tool(context, toolkit="calculator", function=entrypoint.__name__, arguments={})
    assert calls == ["executed"]
    assert result == {"error": {"code": "tool_unavailable", "message": "This tool is currently unavailable."}}
    assert "secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_cancelled_sync_body_retains_cleanup(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled sync body retains cleanup."""
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    class BlockingToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.block])
            self._requires_connect = True

        def connect(self) -> None:
            pass

        def close(self) -> None:
            closed.set()

        def block(self) -> str:
            started.set()
            assert release.wait(5)
            return "done"

    _replace_calculator(monkeypatch, BlockingToolkit())
    task = asyncio.create_task(gateway.invoke_tool(context, toolkit="calculator", function="block", arguments={}))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not closed.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(closed.wait, 5)


@pytest.mark.asyncio
async def test_mcp_lazy_configure_never_contacts_backends(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mcp lazy configure never contacts backends."""
    config = context.config.model_copy(deep=True)
    config.mcp_servers["example"] = MCPServerConfig(transport="stdio", command="not-a-real-command")
    manager = MCPServerManager(context.runtime_paths)

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Lazy configure contacted a backend")

    monkeypatch.setattr(manager, "_refresh_server_catalog", forbidden)
    try:
        await manager.sync_servers(config, discover=False)
        assert manager.has_server("example")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_worker_context_covers_construction_body_and_cleanup(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Detached worker configuration follows the complete selected toolkit lifecycle."""
    phases = []

    def check(phase: str) -> None:
        assert get_tool_runtime_context() is None
        assert get_tool_execution_identity() == context.execution_identity
        worker_context = get_worker_runtime_context()
        assert worker_context is not None
        assert worker_context.config is context.config
        assert worker_context.runtime_paths == context.runtime_paths
        phases.append(phase)

    class ScopedToolkit(Toolkit):
        def __init__(self) -> None:
            check("build")
            super().__init__(name="calculator", tools=[self.account])
            self._requires_connect = True

        def connect(self) -> None:
            check("connect")

        def account(self) -> str:
            check("body")
            return "account"

        def close(self) -> None:
            check("close")

    monkeypatch.setattr(agents, "build_agent_toolkit", lambda *_args, **_kwargs: ScopedToolkit())
    ambient = _tool_runtime_context(context.runtime_paths.storage_root, agent_name="other")
    other_identity = replace(context.execution_identity, requester_id="@bob:example.org")
    with tool_runtime_context(ambient), tool_execution_identity(other_identity):
        result = await gateway.invoke_tool(context, toolkit="calculator", function="account", arguments={})
        assert get_tool_runtime_context() is ambient
        assert get_tool_execution_identity() == other_identity
    assert result == {"result": "account"}
    assert phases == ["build", "connect", "body", "close"]
    assert get_worker_runtime_context() is None


@pytest.mark.asyncio
async def test_native_deferred_filters_remain_authoritative(context: PersonalAgentContext) -> None:
    """Deferred discovery must not remove authored function restrictions."""
    raw = context.config.model_dump()
    raw["agents"]["personal"]["tools"] = [{"calculator": {"defer": True, "include_tools": ["add"]}}]
    context = replace(context, config=Config.model_validate(raw))
    result = await gateway.search_tools(context, toolkit="calculator")
    assert "error" not in result
    assert [item["function"] for item in result["results"]] == ["add"]
    result = await gateway.invoke_tool(context, toolkit="calculator", function="multiply", arguments={"a": 2, "b": 3})
    assert "result" not in result
    assert "error" in result
    assert result["error"]["code"] == "tool_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["build", "connect"])
async def test_cancelled_sync_preparation_keeps_cleanup_owner(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """Cancelled construction and connect must close only after the sync operation exits."""
    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    class SlowToolkit(Toolkit):
        def __init__(self) -> None:
            if phase == "build":
                started.set()
                assert release.wait(5)
            super().__init__(name="calculator")
            self._requires_connect = True

        def connect(self) -> None:
            if phase == "connect":
                started.set()
                assert release.wait(5)

        def close(self) -> None:
            closed.set()

    monkeypatch.setattr(agents, "build_agent_toolkit", lambda *_args, **_kwargs: SlowToolkit())
    task = asyncio.create_task(gateway.get_tool(context, toolkit="calculator", function="missing"))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not closed.is_set()
    finally:
        release.set()
        await gateway_toolkits.drain_gateway_tool_cleanup()
        assert closed.is_set()


@pytest.mark.asyncio
async def test_close_failure_does_not_swallow_cancellation(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation stays cancellation even when a backend fails during cleanup."""
    started = asyncio.Event()

    class FailingCloseToolkit(Toolkit):
        def __init__(self) -> None:
            super().__init__(name="calculator", tools=[self.wait])
            self._requires_connect = True

        async def connect(self) -> None:  # ty: ignore[invalid-method-override]
            pass

        async def close(self) -> None:  # ty: ignore[invalid-method-override]
            message = "backend cleanup failed"
            raise RuntimeError(message)

        async def wait(self) -> str:
            started.set()
            await asyncio.Event().wait()
            return "never"

    _replace_calculator(monkeypatch, FailingCloseToolkit())
    task = asyncio.create_task(gateway.invoke_tool(context, toolkit="calculator", function="wait", arguments={}))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_arguments_cannot_fetch_remote_schema(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Argument validation must never resolve remote schema URLs over the network."""

    def unsafe() -> str:
        pytest.fail("An unresolved schema reached the tool body")

    toolkit = Toolkit(name="calculator", auto_register=False)
    toolkit.functions["unsafe"] = Function(
        name="unsafe",
        entrypoint=unsafe,
        skip_entrypoint_processing=True,
        parameters={"$ref": "https://schema.example.org/remote.json"},
    )
    _replace_calculator(monkeypatch, toolkit)
    result = await gateway.invoke_tool(context, toolkit="calculator", function="unsafe", arguments={})
    assert "result" not in result
    assert "error" in result
    assert result["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize("private_scope", ["user", "user_agent"])
async def test_native_credentials_are_user_scoped_and_refreshed(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
    private_scope: str,
) -> None:
    """Both private modes isolate users and refresh only the reconnected user's account."""
    raw = context.config.model_dump()
    raw["agents"]["personal"]["private"]["per"] = private_scope
    config = Config.model_validate(raw)
    snapshot = ApiSnapshot(
        generation=1,
        runtime_paths=context.runtime_paths,
        config_data=config.model_dump(),
        runtime_config=config,
    )
    context = resolve_personal_agent(snapshot, "@alice:example.org")
    bob = resolve_personal_agent(snapshot, "@bob:example.org")
    assert context.worker_target.worker_scope == bob.worker_target.worker_scope == private_scope
    assert context.worker_target.worker_key != bob.worker_target.worker_key
    ensure_tool_registry_loaded(context.runtime_paths, context.config)

    class AccountTools(Toolkit):
        def __init__(self, account_name: str | None = None) -> None:
            self.account_name = account_name
            super().__init__(name="calculator", tools=[self.account])

        def account(self) -> str:
            return self.account_name or "missing"

    monkeypatch.setitem(TOOL_REGISTRY, "calculator", lambda: AccountTools)
    monkeypatch.setitem(BUILTIN_TOOL_REGISTRY, "calculator", lambda: AccountTools)
    monkeypatch.setitem(
        TOOL_METADATA,
        "calculator",
        replace(
            TOOL_METADATA["calculator"],
            config_fields=[ConfigField(name="account_name", label="Account")],
        ),
    )
    monkeypatch.setitem(BUILTIN_TOOL_METADATA, "calculator", TOOL_METADATA["calculator"])
    credentials = get_runtime_credentials_manager(context.runtime_paths)
    for user, account in [(context, "alice-account"), (bob, "bob-account"), (context, "alice-reconnected")]:
        save_scoped_credentials(
            "calculator",
            {"account_name": account},
            credentials_manager=credentials,
            worker_target=user.worker_target,
        )
        result = await gateway.invoke_tool(user, toolkit="calculator", function="account", arguments={})
        assert result == {"result": account}
    assert await gateway.invoke_tool(bob, toolkit="calculator", function="account", arguments={}) == {
        "result": "bob-account",
    }


@pytest.mark.asyncio
async def test_cache_cannot_skip_hooks_or_tool_body(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cache cannot skip hooks or tool body."""
    events = []

    @hook("tool:before_call")
    async def before(event: ToolBeforeCallContext) -> None:
        events.append(("before", event.requester_id))

    @hook("tool:after_call")
    async def after(event: ToolAfterCallContext) -> None:
        events.append(("after", event.requester_id))

    def account() -> str:
        events.append(("body", "called"))
        return "fresh"

    toolkit = Toolkit(name="calculator", tools=[account])
    toolkit.functions["account"].cache_results = True
    _replace_calculator(monkeypatch, toolkit)
    monkeypatch.setattr(
        gateway,
        "load_plugins",
        lambda *_args, **_kwargs: [
            SimpleNamespace(
                name="audit",
                entry_config=PluginEntryConfig(path="audit.py"),
                plugin_order=0,
                discovered_hooks=(before, after),
            ),
        ],
    )
    assert await gateway.invoke_tool(context, toolkit="calculator", function="account", arguments={}) == {
        "result": "fresh",
    }
    assert toolkit.functions["account"].cache_results is False
    assert events == [("before", "@alice:example.org"), ("body", "called"), ("after", "@alice:example.org")]


@pytest.mark.asyncio
async def test_search_total_size_includes_unicode_and_escaped_text(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search total size includes unicode and escaped text."""
    toolkit = Toolkit(name="calculator", auto_register=False)
    for index in range(10):
        name = f"tool_{index}" + "界" * 100
        toolkit.functions[name] = Function(name=name, description="\u0001" * 256, skip_entrypoint_processing=True)
    _replace_calculator(monkeypatch, toolkit)
    result = await gateway.search_tools(context, toolkit="calculator", limit=10)
    assert "results" in result
    assert len(json.dumps(result).encode()) <= 16384


def _mcp_context(context: PersonalAgentContext, *, oauth: bool = False) -> PersonalAgentContext:
    raw = context.config.model_dump()
    server = {"transport": "streamable-http", "url": "https://mcp.example.org/mcp"}
    if oauth:
        server.update(
            description="Calendar tools",
            auth={
                "type": "oauth",
                "discovery": "manual",
                "authorization_url": "https://auth.example.org/authorize",
                "token_url": "https://auth.example.org/token",
            },
        )
    raw["mcp_servers"] = {"example": server, "unrelated": {"transport": "stdio", "command": "never-start"}}
    raw["agents"]["personal"]["tools"] = [{"mcp_example": {"defer": True, "include_tools": ["echo"]}}]
    return replace(context, config=Config.model_validate(raw))


def _connected_mcp_context(context: PersonalAgentContext, monkeypatch: pytest.MonkeyPatch) -> PersonalAgentContext:
    """Keep real scoped OAuth storage and replace only upstream network transport."""
    _patch_manager(monkeypatch)
    monkeypatch.setattr(_FakeClientSession, "tool_list", [_tool("echo")])
    monkeypatch.setattr(_FakeClientSession, "planned_tool_pages", [])
    monkeypatch.setattr(_FakeClientSession, "call_tool_invocation_count", 0)
    monkeypatch.setattr(_FakeClientSession, "call_tool_arguments", [])
    monkeypatch.setattr(
        _FakeClientSession,
        "planned_tool_results",
        [CallToolResult(content=[TextContent(type="text", text="unexpected dispatch")])],
    )
    context = _mcp_context(context, oauth=True)
    credentials = get_runtime_credentials_manager(context.runtime_paths)
    credentials.save_credentials("mcp_example_oauth_client", {"client_id": "public-client"})
    save_scoped_credentials(
        "mcp_example_oauth",
        {
            "token": "test-access",
            "client_id": "public-client",
            "scopes": [],
            "_source": "oauth",
            "_oauth_provider": "mcp_example",
        },
        credentials_manager=credentials,
        worker_target=context.worker_target,
    )
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["transport", "tool"])
async def test_mcp_body_failure_returns_redacted_error_without_retry(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    """Neither upstream session exceptions nor MCP error results become successful null results."""
    context = _connected_mcp_context(context, monkeypatch)
    message = "secret upstream diagnostics"
    failure = (
        RuntimeError(message)
        if failure_kind == "transport"
        else CallToolResult(isError=True, content=[TextContent(type="text", text=message)])
    )
    monkeypatch.setattr(
        _FakeClientSession,
        "planned_tool_results",
        [failure, CallToolResult(content=[TextContent(type="text", text="unexpected retry")])],
    )
    manager = MCPServerManager(context.runtime_paths, validate_agent_function_names=False)
    try:
        await manager.sync_servers(context.config, discover=False)
        result = await gateway.invoke_tool(
            context,
            toolkit="mcp_example",
            function="example_echo",
            arguments={},
            manager=manager,
        )
        assert _FakeClientSession.call_tool_invocation_count == 1
        assert _FakeClientSession.call_tool_arguments == [{}]
        assert result == {"error": {"code": "tool_unavailable", "message": "This tool is currently unavailable."}}
        assert "secret" not in json.dumps(result)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_policy_approval_blocks_discovery_schema_and_dispatch(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An authenticated upstream still cannot run a policy-gated MCP function."""
    context = _connected_mcp_context(context, monkeypatch)
    config = context.config.model_copy(deep=True)
    config.tool_approval.default = "require_approval"
    context = replace(context, config=config)
    manager = MCPServerManager(context.runtime_paths, validate_agent_function_names=False)
    try:
        await manager.sync_servers(config, discover=False)
        result = await gateway.search_tools(context, toolkit="mcp_example", manager=manager)
        assert result == {"results": []}
        result = await gateway.get_tool(context, toolkit="mcp_example", function="example_echo", manager=manager)
        assert "inputSchema" not in result
        assert "error" in result
        assert result["error"]["code"] == "approval_required"
        result = await gateway.invoke_tool(
            context,
            toolkit="mcp_example",
            function="example_echo",
            arguments={},
            manager=manager,
        )
        assert "result" not in result
        assert "error" in result
        assert result["error"]["code"] == "approval_required"
        assert _FakeClientSession.call_tool_invocation_count == 0
        assert _FakeClientSession.call_tool_arguments == []
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("function", "arguments"),
    [
        ("example_connection_status", {}),
        ("example_list_tools", {}),
        ("example_call_tool", {"tool_name": "echo", "arguments": {}}),
    ],
)
async def test_mcp_oauth_bridge_handles_cannot_bypass_typed_dispatch(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
    function: str,
    arguments: dict[str, object],
) -> None:
    """Connected OAuth bridge names remain uncallable even with valid bridge arguments."""
    context = _connected_mcp_context(context, monkeypatch)
    manager = MCPServerManager(context.runtime_paths, validate_agent_function_names=False)
    try:
        await manager.sync_servers(context.config, discover=False)
        catalog = await gateway.search_tools(context, toolkit="mcp_example", manager=manager)
        assert "error" not in catalog
        assert [item["function"] for item in catalog["results"]] == ["example_echo"]
        result = await gateway.invoke_tool(
            context,
            toolkit="mcp_example",
            function=function,
            arguments=arguments,
            manager=manager,
        )
        assert "result" not in result
        assert "error" in result
        assert result["error"]["code"] == "tool_not_found"
        assert _FakeClientSession.call_tool_invocation_count == 0
        assert _FakeClientSession.call_tool_arguments == []
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_mcp_selected_catalog_preserves_filters_and_never_initializes_other_backend(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mcp selected catalog preserves filters and never initializes other backend."""
    _patch_manager(monkeypatch)
    monkeypatch.setattr(_FakeClientSession, "tool_list", [_tool("echo"), _tool("delete")])
    monkeypatch.setattr(_FakeClientSession, "planned_tool_pages", [])
    monkeypatch.setattr(
        _FakeClientSession,
        "planned_tool_results",
        [CallToolResult(content=[TextContent(type="text", text="hello")])],
    )
    context = _mcp_context(context)
    manager = MCPServerManager(context.runtime_paths)
    try:
        await manager.sync_servers(context.config, discover=False)
        result = await gateway.search_tools(context, toolkit="mcp_example", manager=manager)
        assert "error" not in result
        assert [item["function"] for item in result["results"]] == ["example_echo"]
        assert not manager._states["unrelated"].connected
        result = await gateway.invoke_tool(
            context,
            toolkit="mcp_example",
            function="example_delete",
            arguments={},
            manager=manager,
        )
        assert "result" not in result
        assert "error" in result
        assert result["error"]["code"] == "tool_not_found"
        result = await gateway.invoke_tool(
            context,
            toolkit="mcp_example",
            function="example_echo",
            arguments={},
            manager=manager,
        )
        assert "hello" in json.dumps(result)
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_namespaced_mcp_discovery_never_builds_other_native_tools(
    context: PersonalAgentContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Namespaced catalogs need no cross-agent toolkit construction or shared credentials."""
    _patch_manager(monkeypatch)
    monkeypatch.setattr(_FakeClientSession, "tool_list", [_tool("echo")])
    monkeypatch.setattr(_FakeClientSession, "planned_tool_pages", [])
    context = _mcp_context(context)
    raw = context.config.model_dump()
    raw["agents"]["personal"]["tools"].append("calculator")
    context = replace(context, config=Config.model_validate(raw))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("MCP discovery constructed an unrelated native toolkit")

    monkeypatch.setattr("mindroom.mcp.surface_projection.get_tool_by_name", forbidden)
    manager = MCPServerManager(context.runtime_paths, validate_agent_function_names=False)
    try:
        await manager.sync_servers(context.config, discover=False)
        result = await gateway.get_tool(context, toolkit="mcp_example", function="example_echo", manager=manager)
        assert "inputSchema" in result
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_unconnected_mcp_is_discoverable_but_cannot_borrow_shared_connection(
    context: PersonalAgentContext,
) -> None:
    """Unconnected mcp is discoverable but cannot borrow shared connection."""
    context = _mcp_context(context, oauth=True)
    manager = MCPServerManager(context.runtime_paths)
    try:
        await manager.sync_servers(context.config, discover=False)
        result = await gateway.search_tools(context, query="calendar", manager=manager)
        assert "error" not in result
        assert result["results"][0]["toolkit"] == "mcp_example"
        result = await gateway.search_tools(context, toolkit="mcp_example", manager=manager)
        assert "results" not in result
        assert "error" in result
        assert result["error"]["code"] == "connection_required"
        assert result["error"]["connection_url"].endswith("/connections")
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_changed_manager_config_rejects_old_request_before_backend_contact(context: PersonalAgentContext) -> None:
    """Changed manager config rejects old request before backend contact."""
    context = _mcp_context(context)
    manager = MCPServerManager(context.runtime_paths)
    try:
        await manager.sync_servers(context.config, discover=False)
        changed = context.config.model_copy(deep=True)
        changed.agents["personal"].tools = []
        await manager.sync_servers(changed, discover=False)
        result = await gateway.search_tools(context, toolkit="mcp_example", manager=manager)
        assert "results" not in result
        assert "error" in result
        assert result["error"]["code"] == "tool_unavailable"
        assert not manager._states["example"].connected
    finally:
        await manager.shutdown()
