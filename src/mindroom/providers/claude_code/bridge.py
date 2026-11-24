"""Park Claude Code MCP calls until MindRoom supplies their results."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import mcp.types
from claude_agent_sdk import ClaudeSDKError
from mcp.server import Server

from mindroom import __version__
from mindroom.llm.errors import ProviderError
from mindroom.logging_config import get_logger
from mindroom.providers.claude_code.prompt import canonical_arguments
from mindroom.providers.claude_code.tool_naming import SERVER_NAME, TOOL_PREFIX, offered_name

if TYPE_CHECKING:
    from collections.abc import Sequence

    from claude_agent_sdk import Message as ClaudeMessage

    from mindroom.llm.provider import ToolSpec

__all__ = ["ToolBridge", "tool_names"]

logger = get_logger(__name__)
# The request metadata Claude Code sends with an MCP tool call: the id of the ``tool_use`` block that made the call.
_TOOL_USE_ID_META = "claudecode/toolUseId"
# How long a tool call waits for the model message that made it, which Claude Code sends before the call.
_UNMADE_CALL_SECONDS = 30.0
# Claude Code cuts a longer MCP tool description itself.
_DESCRIPTION_LIMIT = 2048
_CUT_DESCRIPTIONS: set[str] = set()


def tool_names(tools: Sequence[ToolSpec], provider: str) -> dict[str, str]:
    """Return the offered tools' names by the name Claude Code calls each with; two may not share one."""
    names: dict[str, str] = {}
    for tool in tools:
        short = offered_name(tool.name)
        if short in names:
            msg = f"Claude Code would call the tools {names[short]} and {tool.name} by one name, {short}."
            raise ProviderError(msg, provider=provider)
        names[short] = tool.name
    return names


@dataclass(eq=False)
class _Call:
    """One tool call a process waits on: what the model wrote, the result MindRoom gives, and whether it was made."""

    made: asyncio.Future[tuple[str, str]]
    result: asyncio.Future[dict[str, Any]]
    entered: bool = False


@dataclass(eq=False)
class ToolBridge:
    """The tool calls of one Claude Code process, by call id: each waits for the result MindRoom's loop produces.

    A call is in ``calls`` until Claude Code was handed its result, then its id is ``unconfirmed`` until Claude Code
    said it took the result, and ``spent`` for good. ``failure`` says why the process can no longer be trusted.
    """

    names: dict[str, str]
    messages: asyncio.Queue[ClaudeMessage | Exception] = field(default_factory=asyncio.Queue)
    calls: dict[str, _Call] = field(default_factory=dict)
    unconfirmed: set[str] = field(default_factory=set)
    spent: set[str] = field(default_factory=set)
    failure: str | None = None

    def _call(self, tool_use_id: str) -> _Call:
        if tool_use_id not in self.calls:
            loop = asyncio.get_running_loop()
            self.calls[tool_use_id] = _Call(loop.create_future(), loop.create_future())
        return self.calls[tool_use_id]

    def made(self, tool_use_id: str, name: str, arguments: object) -> bool:
        """Keep a tool call the model wrote, which Claude Code then brings to the tool server; False for a used id."""
        if tool_use_id in self.spent or (call := self._call(tool_use_id)).made.done():
            return False
        call.made.set_result((name, canonical_arguments(arguments)))
        return True

    def resolve(self, results: dict[str, dict[str, Any]]) -> None:
        """Give each waiting tool call its result, which continues the turn."""
        for tool_use_id, result in results.items():
            # Claude Code can give a call up at any moment, which the request that reads the process then learns.
            if not (call := self.calls[tool_use_id]).result.done():
                call.result.set_result(result)

    @property
    def undelivered(self) -> bool:
        """Whether Claude Code has not taken a result MindRoom gave, without which its model must not go on."""
        return bool(self.unconfirmed) or any(call.result.done() for call in self.calls.values())

    def fail(self, reason: str) -> None:
        """Mark the process as broken and wake the request that reads it."""
        if self.failure is None:
            self.failure = reason
            self.messages.put_nowait(ClaudeSDKError(reason))

    def _refuse(self, reason: str) -> mcp.types.CallToolResult:
        """Fail the process and answer a call with ``reason`` instead of a result: no call is ever guessed."""
        self.fail(reason)
        return mcp.types.CallToolResult(content=[mcp.types.TextContent(type="text", text=reason)], is_error=True)

    async def _answer(self, params: mcp.types.CallToolRequestParams) -> mcp.types.CallToolResult:
        """Wait for the result of the call the model wrote under this call's id, and for no other call."""
        tool_use_id = (params.meta or {}).get(_TOOL_USE_ID_META)
        if not isinstance(tool_use_id, str) or params.name not in self.names:
            return self._refuse(f"Claude Code called {params.name} without a usable call id")
        if tool_use_id in self.spent or (call := self._call(tool_use_id)).entered:
            return self._refuse(f"Claude Code made the tool call {tool_use_id} twice")
        call.entered = True
        try:
            try:
                # Only a call that outran its model message waits here.
                made = await asyncio.wait_for(asyncio.shield(call.made), _UNMADE_CALL_SECONDS)
            except TimeoutError:
                return self._refuse(f"Claude Code made a tool call the model did not write: {tool_use_id}")
            if made != (params.name, canonical_arguments(params.arguments or {})):
                return self._refuse(f"Claude Code made the tool call {tool_use_id} unlike the model wrote it")
            result = mcp.types.CallToolResult.model_validate(await call.result)
        except asyncio.CancelledError:
            self.fail(f"Claude Code gave up the tool call {tool_use_id}")
            raise
        # Nothing of a call that is done is kept but its id, which no later call may use.
        del self.calls[tool_use_id]
        self.spent.add(tool_use_id)
        self.unconfirmed.add(tool_use_id)
        return result

    def offered_names(self) -> list[str]:
        """Return the sorted, prefixed names Claude Code must report at the start of a turn."""
        return [f"{TOOL_PREFIX}{name}" for name in sorted(self.names)]

    def expected_inventory(self) -> dict[str, tuple[str, list[str]]]:
        """Return the only permitted connected servers and their tools, without the CLI's tool prefix."""
        return {SERVER_NAME: ("connected", sorted(self.names))} if self.names else {}

    def server(self, tools: Sequence[ToolSpec]) -> Server:
        """Return the MCP server that offers ``tools`` and parks each call until its result arrives."""
        listed = mcp.types.ListToolsResult(
            tools=[
                mcp.types.Tool(
                    name=short,
                    description=_description(tool),
                    input_schema=dict(tool.parameters),
                    # Claude Code runs calls that are not read-only one after another.
                    annotations=mcp.types.ToolAnnotations(read_only_hint=False),
                    _meta={"anthropic/alwaysLoad": True, "anthropic/maxResultSizeChars": 500_000},
                )
                for short, tool in zip(self.names, tools, strict=True)
            ],
        )

        async def on_list_tools(_context: object, _params: object) -> mcp.types.ListToolsResult:
            return listed

        async def on_call_tool(_context: object, params: mcp.types.CallToolRequestParams) -> mcp.types.CallToolResult:
            return await self._answer(params)

        return Server(SERVER_NAME, version=__version__, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def _description(tool: ToolSpec) -> str:
    """Return the description Claude Code takes for ``tool``, saying once when it loses its end."""
    description = tool.description or ""
    if len(description) > _DESCRIPTION_LIMIT and tool.name not in _CUT_DESCRIPTIONS:
        _CUT_DESCRIPTIONS.add(tool.name)
        logger.warning("claude_code_tool_description_cut", tool=tool.name, length=len(description))
    return description[:_DESCRIPTION_LIMIT]
