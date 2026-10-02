"""Shared scripted provider, shell, and fake integration for minimal-agent tests."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agno.models.openai import OpenAIChat
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from mindroom import agents
from mindroom.agent_cli.shell_contract import current_agent_cli_shell_env
from mindroom.agent_cli.turn import LiveTurnTools
from mindroom.runtime_state import clear_api_server_address, set_api_server_address

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterator

    from mindroom.agent_cli.session import TurnToolRegistry


@pytest.fixture
def agent_cli_api() -> Iterator[None]:
    """Run like `mindroom run`, whose API server minimal Bash's CLI calls back."""
    set_api_server_address("127.0.0.1", 8765)
    yield
    clear_api_server_address()


def install_scripted_shell(monkeypatch: pytest.MonkeyPatch, run_shell_command: Callable[..., Awaitable[str]]) -> None:
    """Keep each agent's real shell toolkit, but run ``run_shell_command`` in place of its subprocess."""
    original = agents.get_tool_by_name

    def build(name: str, *args: object, **kwargs: object) -> object:
        toolkit = original(name, *args, **kwargs)
        if name == "shell" and "run_shell_command" in toolkit.async_functions:
            toolkit.async_functions["run_shell_command"].entrypoint = run_shell_command
        return toolkit

    monkeypatch.setattr(agents, "get_tool_by_name", build)


def shell_cli_owner(registry: TurnToolRegistry) -> LiveTurnTools:
    """Resolve the response owner from the grant Bash exports, as the API does for `mindroom-agent`."""
    shell_env = current_agent_cli_shell_env()
    assert shell_env is not None
    owner = registry.resolve("Bearer " + shell_env.token, now_ns=time.time_ns())
    assert isinstance(owner, LiveTurnTools)
    return owner


PLUGIN = """from hashlib import sha256
from agno.tools import Toolkit
from agno.tools.function import ToolResult
from agno.media import Image, File
from mindroom.tool_system.declarations import ConfigField, ToolCategory, ToolExecutionTarget, ToolFileAccess
from mindroom.tool_system.registration import register_tool_with_metadata

class ParityTools(Toolkit):
    def __init__(self, api_key: str):
        self.key = api_key
        super().__init__(name="parity", tools=[self.integration, self.approved, self.media])
        self.get_async_functions()["approved"].requires_confirmation = True

    def integration(self, digest: str) -> str:
        assert sha256(self.key.encode()).hexdigest() == digest
        return "primary credential accepted"

    def approved(self, value: str) -> str:
        return "approved:" + value

    def media(self) -> ToolResult:
        import base64
        png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        return ToolResult(content="image and file", images=[Image(content=png, mime_type="image/png")],
                          files=[File(content=b"same-agent file", mime_type="text/plain", filename="report.txt")])

@register_tool_with_metadata(name="parity", display_name="Local parity", description="Fake local credential integration",
    category=ToolCategory.DEVELOPMENT, default_execution_target=ToolExecutionTarget.PRIMARY,
    file_access=ToolFileAccess.NONE,
    config_fields=[ConfigField(name="api_key", label="API key", type="password", required=True)])
def parity():
    return ParityTools
"""


class ScriptedProvider:
    """Capture actual SDK requests while returning deterministic tool choices."""

    def __init__(self) -> None:
        self.requests = []
        self.steps = []

    async def send(self, **request: object) -> object:
        """Return an SDK-shaped response and retain the exact submitted request."""
        self.requests.append(request)
        step = self.steps.pop(0) if self.steps else "done"

        async def chunks() -> AsyncIterator[ChatCompletionChunk]:
            delta = {"role": "assistant"}
            if isinstance(step, str):
                delta["content"] = step
            else:
                delta["tool_calls"] = [
                    {
                        "index": index,
                        "id": f"provider-{len(self.requests)}-{index}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                    for index, (name, arguments) in enumerate(step)
                ]
            yield ChatCompletionChunk(
                id="completion",
                model=request["model"],
                created=0,
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": delta, "finish_reason": None}],
            )
            yield ChatCompletionChunk(
                id="completion",
                model=request["model"],
                created=0,
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": {}, "finish_reason": "stop" if isinstance(step, str) else "tool_calls"}],
            )

        if not request.get("stream"):
            message = {"role": "assistant", "content": step if isinstance(step, str) else None}
            if not isinstance(step, str):
                message["tool_calls"] = [
                    {
                        "id": f"provider-{len(self.requests)}-{index}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                    for index, (name, arguments) in enumerate(step)
                ]
            return ChatCompletion(
                id="completion",
                model=request["model"],
                created=0,
                object="chat.completion",
                choices=[
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "stop" if isinstance(step, str) else "tool_calls",
                    },
                ],
            )
        return chunks()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Replace only the provider network client."""
        monkeypatch.setattr(
            OpenAIChat,
            "get_async_client",
            lambda _self: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=self.send))),
        )
