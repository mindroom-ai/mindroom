"""Tests for the Claude Agent SDK-backed persistent session tool."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar, get_type_hints

import httpx
import pytest
from agno.run.base import RunContext
from claude_agent_sdk import AssistantMessage, ClaudeSDKError, ResultMessage, TextBlock

import mindroom.tools  # noqa: F401
from mindroom.custom_tools import claude_agent as claude_agent_module
from mindroom.tools_metadata import TOOL_METADATA

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator


@dataclass
class _FakeClaudeSDKClient:
    """Minimal ClaudeSDKClient test double."""

    options: Any

    instances: ClassVar[list[_FakeClaudeSDKClient]] = []

    def __post_init__(self) -> None:
        self.connected = False
        self.interrupted = False
        self.queries: list[str] = []
        _FakeClaudeSDKClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
        self.queries.append(prompt)

    async def receive_response(self) -> AsyncGenerator[AssistantMessage | ResultMessage, None]:
        last_prompt = self.queries[-1]
        yield AssistantMessage(content=[TextBlock(text=f"Echo: {last_prompt}")], model="claude-sonnet")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="claude-session-123",
            total_cost_usd=0.0012,
        )

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.connected = False


@dataclass
class _SlowFakeClaudeSDKClient(_FakeClaudeSDKClient):
    """Slow fake client used to validate session locking behavior."""

    instances: ClassVar[list[_SlowFakeClaudeSDKClient]] = []
    active_queries: ClassVar[int] = 0
    max_concurrent_queries: ClassVar[int] = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        _SlowFakeClaudeSDKClient.instances.append(self)

    async def query(self, prompt: str, session_id: str = "default") -> None:
        type(self).active_queries += 1
        type(self).max_concurrent_queries = max(type(self).max_concurrent_queries, type(self).active_queries)
        await asyncio.sleep(0.03)
        await super().query(prompt, session_id=session_id)
        type(self).active_queries -= 1


@dataclass
class _GatewayProbeClaudeSDKClient:
    """Gateway probe client that sends Anthropic-compatible requests to a local stub server."""

    options: Any

    instances: ClassVar[list[_GatewayProbeClaudeSDKClient]] = []

    def __post_init__(self) -> None:
        self.connected = False
        self.interrupted = False
        self._last_text = ""
        self._last_session_id = "gateway-session-0"
        _GatewayProbeClaudeSDKClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
        env = self.options.env or {}
        base_url = env.get("ANTHROPIC_BASE_URL")
        if not base_url:
            message = "ANTHROPIC_BASE_URL is required for gateway probe test client"
            raise ClaudeSDKError(message)

        token = env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY")
        headers = {
            "authorization": f"Bearer {token}" if token else "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in env:
            headers["anthropic-beta"] = "tools-2024-04-04"

        body = {
            "model": self.options.model or "claude-sonnet-4-5",
            "max_tokens": 256,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.post(f"{base_url.rstrip('/')}/v1/messages", headers=headers, json=body)

        if response.status_code >= 400:
            message = f"{response.status_code} {response.text}"
            raise ClaudeSDKError(message)

        payload = response.json()
        content = payload.get("content", [])
        text = ""
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = str(block.get("text", ""))
                    break
        self._last_text = text or "gateway-empty"
        self._last_session_id = str(payload.get("id", "gateway-session-unknown"))

    async def receive_response(self) -> AsyncGenerator[AssistantMessage | ResultMessage, None]:
        yield AssistantMessage(content=[TextBlock(text=self._last_text)], model="claude-probe")
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self._last_session_id,
            total_cost_usd=0.001,
        )

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.connected = False


@pytest.fixture
def fake_manager(monkeypatch: pytest.MonkeyPatch) -> claude_agent_module.ClaudeSessionManager:
    """Use an isolated in-memory manager and fake SDK client for each test."""
    _FakeClaudeSDKClient.instances = []
    manager = claude_agent_module.ClaudeSessionManager()
    monkeypatch.setattr(claude_agent_module.ClaudeAgentTools, "_session_manager", manager)
    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _FakeClaudeSDKClient)
    return manager


@pytest.fixture
def fake_anthropic_gateway() -> Iterator[dict[str, Any]]:
    """Start a local fake Anthropic-compatible gateway for integration-style tests."""
    requests: list[dict[str, Any]] = []
    state: dict[str, Any] = {"fail_next": False}

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers.get("content-length", "0"))
            raw_body = self.rfile.read(content_length)
            payload = json.loads(raw_body.decode("utf-8") or "{}")
            request_info = {
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "payload": payload,
            }
            requests.append(request_info)

            if self.path != "/v1/messages":
                self.send_response(404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"not_found"}')
                return

            if state["fail_next"]:
                state["fail_next"] = False
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"forced gateway error"}}')
                return

            messages = payload.get("messages", [])
            user_text = ""
            if isinstance(messages, list) and messages:
                first = messages[0]
                if isinstance(first, dict):
                    user_text = str(first.get("content", ""))

            response = {
                "id": f"msg-{len(requests)}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": f"gateway:{user_text}"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            encoded = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format_str: str, *args: object) -> None:
            _ = (format_str, args)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    address = server.server_address
    host_raw = address[0]
    port = int(address[1])
    host = host_raw.decode("utf-8") if isinstance(host_raw, bytes) else str(host_raw)
    base_url = f"http://{host}:{port}"

    try:
        yield {"base_url": base_url, "requests": requests, "state": state}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def test_claude_agent_tool_registered_in_metadata() -> None:
    """Claude session tool should be visible in metadata."""
    assert "claude_agent" in TOOL_METADATA
    metadata = TOOL_METADATA["claude_agent"]
    assert metadata.display_name == "Claude Agent SDK"


@pytest.mark.parametrize(
    ("method_name", "expected_params"),
    [
        ("claude_start_session", ("session_label", "resume", "fork_session", "run_context", "agent")),
        ("claude_send", ("prompt", "session_label", "resume", "fork_session", "run_context", "agent")),
        ("claude_session_status", ("session_label", "run_context", "agent")),
        ("claude_interrupt", ("session_label", "run_context", "agent")),
        ("claude_end_session", ("session_label", "run_context", "agent")),
    ],
)
def test_claude_tool_type_hints_resolve_at_runtime(
    method_name: str,
    expected_params: tuple[str, ...],
) -> None:
    """All tool methods should have resolvable runtime type hints for Agno parser compatibility."""
    method = getattr(claude_agent_module.ClaudeAgentTools, method_name)
    hints = get_type_hints(method, globalns=vars(claude_agent_module))
    for param_name in expected_params:
        assert param_name in hints


@pytest.mark.asyncio
async def test_claude_send_reuses_session(fake_manager: claude_agent_module.ClaudeSessionManager) -> None:  # noqa: ARG001
    """Repeated calls in the same run context should reuse one SDK client session."""
    tools = claude_agent_module.ClaudeAgentTools(api_key="sk-test")
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    first = await tools.claude_send("hello", run_context=run_context, agent=agent)
    second = await tools.claude_send("again", run_context=run_context, agent=agent)

    assert "Echo: hello" in first
    assert "Echo: again" in second
    assert len(_FakeClaudeSDKClient.instances) == 1
    assert _FakeClaudeSDKClient.instances[0].queries == ["hello", "again"]


@pytest.mark.asyncio
async def test_claude_send_sets_gateway_env_vars(fake_manager: claude_agent_module.ClaudeSessionManager) -> None:  # noqa: ARG001
    """Gateway configuration should be propagated to Claude SDK env vars."""
    tools = claude_agent_module.ClaudeAgentTools(
        api_key="sk-test",
        anthropic_base_url="http://litellm.local",
        anthropic_auth_token="gateway-token",  # noqa: S106
        disable_experimental_betas=True,
    )
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    await tools.claude_send("hello", run_context=run_context, agent=agent)

    options = _FakeClaudeSDKClient.instances[0].options
    assert options.env == {
        "ANTHROPIC_API_KEY": "sk-test",
        "ANTHROPIC_BASE_URL": "http://litellm.local",
        "ANTHROPIC_AUTH_TOKEN": "gateway-token",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }


@pytest.mark.asyncio
async def test_claude_send_sets_session_control_options(
    fake_manager: claude_agent_module.ClaudeSessionManager,  # noqa: ARG001
) -> None:
    """Runtime session control fields should pass through to ClaudeAgentOptions."""
    tools = claude_agent_module.ClaudeAgentTools(
        api_key="sk-test",
        continue_conversation=True,
    )
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    await tools.claude_send(
        "hello",
        resume="session_resume_123",
        fork_session=True,
        run_context=run_context,
        agent=agent,
    )

    options = _FakeClaudeSDKClient.instances[0].options
    assert options.continue_conversation is True
    assert options.resume == "session_resume_123"
    assert options.fork_session is True


@pytest.mark.asyncio
async def test_claude_send_uses_agent_model_when_tool_model_unset(
    fake_manager: claude_agent_module.ClaudeSessionManager,  # noqa: ARG001
) -> None:
    """Agent model id should be used when tool-config model is not set."""
    tools = claude_agent_module.ClaudeAgentTools(api_key="sk-test")
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general", model=SimpleNamespace(id="claude-sonnet-4-5"))

    await tools.claude_send("hello", run_context=run_context, agent=agent)

    options = _FakeClaudeSDKClient.instances[0].options
    assert options.model == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_claude_send_tool_model_overrides_agent_model(
    fake_manager: claude_agent_module.ClaudeSessionManager,  # noqa: ARG001
) -> None:
    """Explicit tool-config model should override the calling agent model id."""
    tools = claude_agent_module.ClaudeAgentTools(api_key="sk-test", model="claude-opus-4-6")
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general", model=SimpleNamespace(id="claude-sonnet-4-5"))

    await tools.claude_send("hello", run_context=run_context, agent=agent)

    options = _FakeClaudeSDKClient.instances[0].options
    assert options.model == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_claude_send_with_different_session_labels_creates_multiple_sessions(
    fake_manager: claude_agent_module.ClaudeSessionManager,  # noqa: ARG001
) -> None:
    """Different explicit session labels should map to independent sessions."""
    tools = claude_agent_module.ClaudeAgentTools(api_key="sk-test")
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    await tools.claude_send("first", session_label="a", run_context=run_context, agent=agent)
    await tools.claude_send("second", session_label="b", run_context=run_context, agent=agent)

    assert len(_FakeClaudeSDKClient.instances) == 2


@pytest.mark.asyncio
async def test_claude_send_same_session_is_serialized_by_lock(
    fake_manager: claude_agent_module.ClaudeSessionManager,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent sends to the same session key should serialize query execution."""
    _SlowFakeClaudeSDKClient.instances = []
    _SlowFakeClaudeSDKClient.active_queries = 0
    _SlowFakeClaudeSDKClient.max_concurrent_queries = 0
    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _SlowFakeClaudeSDKClient)

    tools = claude_agent_module.ClaudeAgentTools(api_key="sk-test")
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    first, second = await asyncio.wait_for(
        asyncio.gather(
            tools.claude_send("one", run_context=run_context, agent=agent),
            tools.claude_send("two", run_context=run_context, agent=agent),
        ),
        timeout=3,
    )

    assert "Echo: one" in first or "Echo: one" in second
    assert "Echo: two" in first or "Echo: two" in second
    assert len(_SlowFakeClaudeSDKClient.instances) == 1
    assert _SlowFakeClaudeSDKClient.max_concurrent_queries == 1


@pytest.mark.asyncio
async def test_session_status_interrupt_and_end(fake_manager: claude_agent_module.ClaudeSessionManager) -> None:  # noqa: ARG001
    """Status/interrupt/end management tools should operate on active sessions."""
    tools = claude_agent_module.ClaudeAgentTools(api_key="sk-test")
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    await tools.claude_send("status check", run_context=run_context, agent=agent)
    status = await tools.claude_session_status(run_context=run_context, agent=agent)
    interrupt = await tools.claude_interrupt(run_context=run_context, agent=agent)
    end = await tools.claude_end_session(run_context=run_context, agent=agent)

    assert "claude_session_id: claude-session-123" in status
    assert "Interrupt sent" in interrupt
    assert _FakeClaudeSDKClient.instances[0].interrupted is True
    assert "Closed Claude session" in end


@pytest.mark.asyncio
async def test_expired_session_is_cleaned_and_recreated(
    fake_manager: claude_agent_module.ClaudeSessionManager,
) -> None:
    """Expired sessions should be disconnected and recreated on next use."""
    tools = claude_agent_module.ClaudeAgentTools(api_key="sk-test", session_ttl_minutes=1)
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    first_response = await tools.claude_send("hello", run_context=run_context, agent=agent)
    assert "Echo: hello" in first_response

    session_key = "general:session-1"
    first_session = fake_manager._sessions[session_key]
    first_client = first_session.client
    first_session.last_used_at -= first_session.ttl_seconds + 1

    second_response = await tools.claude_send("again", run_context=run_context, agent=agent)
    assert "Echo: again" in second_response
    assert len(_FakeClaudeSDKClient.instances) == 2
    assert first_client.connected is False


@pytest.mark.asyncio
async def test_session_limits_are_namespace_scoped(
    fake_manager: claude_agent_module.ClaudeSessionManager,
) -> None:
    """Per-agent max_sessions should not overwrite limits for other agents."""
    tools_alpha = claude_agent_module.ClaudeAgentTools(api_key="sk-test", max_sessions=1)
    tools_beta = claude_agent_module.ClaudeAgentTools(api_key="sk-test", max_sessions=3)
    run_context = RunContext(run_id="run-1", session_id="session-1")
    alpha = SimpleNamespace(name="alpha")
    beta = SimpleNamespace(name="beta")

    await tools_alpha.claude_send("one", session_label="a", run_context=run_context, agent=alpha)
    await tools_alpha.claude_send("two", session_label="b", run_context=run_context, agent=alpha)
    await tools_beta.claude_send("three", session_label="a", run_context=run_context, agent=beta)
    await tools_beta.claude_send("four", session_label="b", run_context=run_context, agent=beta)

    alpha_sessions = [s for s in fake_manager._sessions.values() if s.namespace == "alpha"]
    beta_sessions = [s for s in fake_manager._sessions.values() if s.namespace == "beta"]
    assert len(alpha_sessions) == 1
    assert len(beta_sessions) == 2


@pytest.mark.asyncio
async def test_gateway_probe_posts_to_v1_messages_and_reuses_sdk_session(
    fake_manager: claude_agent_module.ClaudeSessionManager,  # noqa: ARG001
    fake_anthropic_gateway: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway-mode tool calls should hit /v1/messages with expected auth/version headers."""
    _GatewayProbeClaudeSDKClient.instances = []
    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _GatewayProbeClaudeSDKClient)

    tools = claude_agent_module.ClaudeAgentTools(
        api_key="sk-test",
        anthropic_base_url=fake_anthropic_gateway["base_url"],
        anthropic_auth_token="gateway-token",  # noqa: S106
        model="claude-opus-4-6",
        disable_experimental_betas=True,
    )
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    first = await tools.claude_send("hello", run_context=run_context, agent=agent)
    second = await tools.claude_send("again", run_context=run_context, agent=agent)

    assert "gateway:hello" in first
    assert "gateway:again" in second
    assert len(_GatewayProbeClaudeSDKClient.instances) == 1

    requests = fake_anthropic_gateway["requests"]
    assert len(requests) == 2
    for req in requests:
        assert req["path"] == "/v1/messages"
        assert req["headers"]["authorization"] == "Bearer gateway-token"
        assert req["headers"]["anthropic-version"] == "2023-06-01"
        assert "anthropic-beta" not in req["headers"]


@pytest.mark.asyncio
async def test_gateway_probe_can_include_anthropic_beta_header_when_not_disabled(
    fake_manager: claude_agent_module.ClaudeSessionManager,  # noqa: ARG001
    fake_anthropic_gateway: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When experimental betas are enabled, the Anthropic beta header should be present."""
    _GatewayProbeClaudeSDKClient.instances = []
    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _GatewayProbeClaudeSDKClient)

    tools = claude_agent_module.ClaudeAgentTools(
        api_key="sk-test",
        anthropic_base_url=fake_anthropic_gateway["base_url"],
        anthropic_auth_token="gateway-token",  # noqa: S106
        model="claude-opus-4-6",
        disable_experimental_betas=False,
    )
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    result = await tools.claude_send("hello", run_context=run_context, agent=agent)

    assert "gateway:hello" in result
    assert fake_anthropic_gateway["requests"][-1]["headers"]["anthropic-beta"] == "tools-2024-04-04"


@pytest.mark.asyncio
async def test_gateway_probe_error_is_propagated_and_session_is_closed(
    fake_manager: claude_agent_module.ClaudeSessionManager,
    fake_anthropic_gateway: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway failures should surface as Claude session errors and tear down the bad session."""
    _GatewayProbeClaudeSDKClient.instances = []
    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _GatewayProbeClaudeSDKClient)

    tools = claude_agent_module.ClaudeAgentTools(
        api_key="sk-test",
        anthropic_base_url=fake_anthropic_gateway["base_url"],
        anthropic_auth_token="gateway-token",  # noqa: S106
        model="claude-opus-4-6",
        disable_experimental_betas=True,
    )
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    fake_anthropic_gateway["state"]["fail_next"] = True
    result = await tools.claude_send("boom", run_context=run_context, agent=agent)

    assert "Claude session error:" in result
    assert "500" in result
    assert not fake_manager._sessions


@pytest.mark.asyncio
async def test_claude_send_error_does_not_deadlock(
    fake_manager: claude_agent_module.ClaudeSessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Errors while querying should close the session without hanging on locks."""

    class _FailingClaudeSDKClient(_FakeClaudeSDKClient):
        async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
            self.queries.append(prompt)
            message = "boom"
            raise ClaudeSDKError(message)

    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _FailingClaudeSDKClient)
    tools = claude_agent_module.ClaudeAgentTools(api_key="sk-test")
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    result = await asyncio.wait_for(tools.claude_send("hello", run_context=run_context, agent=agent), timeout=1)

    assert "Claude session error: boom" in result
    assert "Session context:" in result
    assert "- continue_conversation: False" in result
    assert "- resume: (none)" in result
    assert not fake_manager._sessions


@pytest.mark.asyncio
async def test_claude_send_error_includes_runtime_session_hints(
    fake_manager: claude_agent_module.ClaudeSessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error output should include runtime session hints for LLM self-correction."""

    class _FailingClaudeSDKClient(_FakeClaudeSDKClient):
        async def query(self, prompt: str, session_id: str = "default") -> None:  # noqa: ARG002
            self.queries.append(prompt)
            message = "boom"
            raise ClaudeSDKError(message)

    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _FailingClaudeSDKClient)
    tools = claude_agent_module.ClaudeAgentTools(
        api_key="sk-test",
        continue_conversation=True,
        cwd="/workspace/demo-cwd",
    )
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    result = await asyncio.wait_for(
        tools.claude_send(
            "hello",
            resume="resume-xyz",
            fork_session=True,
            run_context=run_context,
            agent=agent,
        ),
        timeout=1,
    )

    assert "Claude session error: boom" in result
    assert "Session context:" in result
    assert "- continue_conversation: True" in result
    assert "- resume: resume-xyz" in result
    assert "- fork_session: True" in result
    assert "- cwd: /workspace/demo-cwd" in result
    assert "note: `resume` must exist" in result
    assert not fake_manager._sessions


@pytest.mark.asyncio
async def test_claude_start_session_error_includes_context(
    fake_manager: claude_agent_module.ClaudeSessionManager,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start-session failures should return context-rich diagnostics."""

    class _FailingConnectClaudeSDKClient(_FakeClaudeSDKClient):
        async def connect(self) -> None:
            message = "connect failed"
            raise RuntimeError(message)

    monkeypatch.setattr(claude_agent_module, "ClaudeSDKClient", _FailingConnectClaudeSDKClient)
    tools = claude_agent_module.ClaudeAgentTools(
        api_key="sk-test",
        continue_conversation=True,
        cwd="/workspace/demo-cwd",
    )
    run_context = RunContext(run_id="run-1", session_id="session-1")
    agent = SimpleNamespace(name="general")

    result = await tools.claude_start_session(
        resume="resume-xyz",
        fork_session=False,
        run_context=run_context,
        agent=agent,
    )

    assert "Failed to start Claude session: connect failed" in result
    assert "Session context:" in result
    assert "- continue_conversation: True" in result
    assert "- resume: resume-xyz" in result
    assert "- fork_session: False" in result
