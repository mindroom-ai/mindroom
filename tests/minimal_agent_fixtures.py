"""Shared scripted provider and fake integration for minimal-agent tests."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING

from agno.models.openai import OpenAIChat
from openai.types.chat import ChatCompletion, ChatCompletionChunk

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest


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
