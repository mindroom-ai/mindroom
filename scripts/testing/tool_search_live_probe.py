"""Live probe for Anthropic server-side tool search (defer_loading) through MindRoom's Claude adapter.

All defer_loading behavior is otherwise tested against canned replies. This probe sends real requests
through the Claude adapter the runtime builds (``providers/anthropic/messages.py``) and checks that:

1. the request with ``defer_loading`` tools plus the regex search tool is accepted;
2. the model discovers the deferred tool via ``tool_search_tool_result`` and calls it;
3. the follow-up turn reads the cached prefix (``cache_read_tokens > 0``).

Usage:
    ANTHROPIC_API_KEY=... uv run python scripts/testing/tool_search_live_probe.py
    uv run python scripts/testing/tool_search_live_probe.py --provider vertexai --project-id <gcp-project>
    uv run python scripts/testing/tool_search_live_probe.py --dry-run  # no credentials needed

The vertexai provider authenticates via GCP application-default credentials and
requires the project to have Anthropic models enabled in Model Garden.

Exit codes: 0 = pass, 1 = fail, 2 = missing credentials/arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mindroom.config.models import ModelConfig  # noqa: E402
from mindroom.constants import resolve_runtime_paths  # noqa: E402
from mindroom.llm.messages import Message  # noqa: E402
from mindroom.llm.provider import ModelRequest, ModelTurn, ToolSpec, complete  # noqa: E402
from mindroom.model_defaults import CONFIG_INIT_MODEL_PRESETS  # noqa: E402
from mindroom.providers.anthropic.messages import ClaudeProvider, build_provider  # noqa: E402
from mindroom.providers.model_http_client import closing_model_connections  # noqa: E402

DEFAULT_MODEL_ID = CONFIG_INIT_MODEL_PRESETS["anthropic"].id

DEFERRED_TOOL_NAME = "get_weather"
# Deterministic padding so the system prompt clears every model's minimum
# cacheable prefix (4096 tokens on Opus-tier models).
_SYSTEM_PROMPT = "You are a MindRoom tool-search live probe agent. " + (
    "This sentence is deterministic cache padding for the probe system prompt. " * 700
)
_MAX_TOOL_ROUNDS = 4


def echo(text: str) -> str:
    """Echo the provided text back to the user."""
    return text


def get_weather(location: str) -> str:
    """Get the current weather for a location."""
    return f"The weather in {location} is 22 degrees Celsius and sunny."


_ENTRYPOINTS = {"echo": echo, "get_weather": get_weather}
PROBE_TOOLS = (
    ToolSpec(
        name="echo",
        description="Echo the provided text back to the user.",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo."}},
            "required": ["text"],
        },
    ),
    ToolSpec(
        name="get_weather",
        description="Get the current weather for a location.",
        parameters={
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City name."}},
            "required": ["location"],
        },
    ),
)


def build_probe_provider(args: argparse.Namespace, http_client: httpx.AsyncClient | None = None) -> ClaudeProvider:
    """Build the Claude adapter with the production cache and tool-search defaults.

    ``--provider vertexai`` builds the Vertex AI route, authenticated via GCP application-default
    credentials, so no ``ANTHROPIC_API_KEY`` is needed.
    """
    if args.provider == "vertexai":
        model_config = ModelConfig(
            provider="vertexai_claude",
            id=args.model_id,
            extra_kwargs={"project_id": args.project_id, "region": args.region},
        )
    else:
        model_config = ModelConfig(
            provider="anthropic",
            id=args.model_id,
            api_key=os.environ.get("ANTHROPIC_API_KEY", "dry-run-key"),
        )
    storage_root = Path(tempfile.mkdtemp(prefix="tool-search-probe-"))
    runtime_paths = resolve_runtime_paths(
        config_path=storage_root / "config.yaml",
        storage_path=storage_root,
        process_env=dict(os.environ),
    )
    provider = build_provider(model_config, runtime_paths)
    return provider if http_client is None else replace(provider, http_client=http_client)


def _request(messages: list[Message]) -> ModelRequest:
    return ModelRequest(
        messages=messages,
        tools=PROBE_TOOLS,
        deferred_tool_names=frozenset({DEFERRED_TOOL_NAME}),
        stream=False,
    )


def _send(provider: ClaudeProvider, messages: list[Message]) -> ModelTurn:
    return asyncio.run(closing_model_connections(complete(provider, _request(messages))))


def run_dry_run(args: argparse.Namespace) -> int:
    """Print the prepared wire request via a capturing HTTP transport; no network."""
    captured: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_dry_run",
                "type": "message",
                "role": "assistant",
                "model": args.model_id,
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    provider = build_probe_provider(args, httpx.AsyncClient(transport=httpx.MockTransport(respond)))
    _send(provider, [Message(role="system", content=_SYSTEM_PROMPT), Message(role="user", content="probe")])

    if not captured:
        print("FAIL: the adapter never sent the request.")
        return 1
    wire = captured[0]
    tool_summary = [
        {
            "name": tool.get("name"),
            "type": tool.get("type"),
            "defer_loading": tool.get("defer_loading"),
            "cache_control": tool.get("cache_control"),
        }
        for tool in wire["tools"]
    ]
    print("Prepared wire request (dry run):")
    print(json.dumps(tool_summary, indent=2))
    system_blocks = wire.get("system") or []
    system_markers = sum(1 for block in system_blocks if block.get("cache_control"))
    print(f"system markers: {system_markers}")
    tool_names = [tool.get("name") for tool in wire["tools"]]
    deferred = [tool["name"] for tool in wire["tools"] if tool.get("defer_loading") is True]
    checks = {
        "tool order (search tool, non-deferred, deferred)": tool_names
        == ["tool_search_tool_regex", "echo", DEFERRED_TOOL_NAME],
        "search tool type": wire["tools"][0].get("type") == "tool_search_tool_regex_20251119",
        "only get_weather deferred": deferred == [DEFERRED_TOOL_NAME],
        "no marker on deferred tools": not any(
            tool.get("cache_control") for tool in wire["tools"] if tool.get("defer_loading")
        ),
        "ladder marker on last non-deferred tool": wire["tools"][1].get("cache_control") is not None,
        "exactly one system marker": system_markers == 1,
    }
    for check_name, check_ok in checks.items():
        if not check_ok:
            print(f"UNEXPECTED: {check_name}")
    ok = all(checks.values())
    print(f"dry-run wire shape: {'OK' if ok else 'UNEXPECTED'}")
    return 0 if ok else 1


def _run_tool(call: dict[str, Any]) -> Message:
    function = call["function"]
    arguments = json.loads(function.get("arguments") or "{}")
    return Message(
        role="tool",
        tool_call_id=call["id"],
        tool_name=function["name"],
        content=_ENTRYPOINTS[function["name"]](**arguments),
    )


def _answer(provider: ClaudeProvider, messages: list[Message]) -> tuple[ModelTurn, list[str]]:
    """Send the conversation and run the tools the model calls until it answers; return the answer and calls."""
    called: list[str] = []
    for _ in range(_MAX_TOOL_ROUNDS):
        turn = _send(provider, messages)
        messages.append(turn.message)
        calls = turn.message.tool_calls or []
        if not calls:
            return turn, called
        called.extend(call["function"]["name"] for call in calls)
        messages.extend(provider.format_tool_results(messages, [_run_tool(call) for call in calls]))
    return turn, called


def run_live(args: argparse.Namespace) -> int:
    """Run the two-turn live probe and report discovery and cache reuse."""
    provider = build_probe_provider(args)
    messages = [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(
            role="user",
            content="What is the weather in Paris right now? Use your available tools; search for one if needed.",
        ),
    ]

    first, called = _answer(provider, messages)
    block_types = [
        block.get("type")
        for message in messages
        if message.role == "assistant"
        for block in (message.provider_data or {}).get("server_tool_blocks", [])
    ]
    discovered = "tool_search_tool_result" in block_types
    called_deferred = DEFERRED_TOOL_NAME in called
    usage_first = first.message.metrics
    print(f"turn 1: server blocks={block_types} tool calls={called}")
    if usage_first is not None:
        print(f"turn 1 usage: cache_write={usage_first.cache_write_tokens} cache_read={usage_first.cache_read_tokens}")

    if not called_deferred:
        print(f"FAIL: model did not call the deferred tool '{DEFERRED_TOOL_NAME}'.")
        return 1

    messages.append(Message(role="user", content="Thanks — and what is the weather in London?"))
    second, _ = _answer(provider, messages)
    usage_second = second.message.metrics
    cache_read = usage_second.cache_read_tokens if usage_second is not None else 0
    print(
        f"turn 2 usage: cache_write={usage_second.cache_write_tokens if usage_second else '?'} cache_read={cache_read}",
    )

    passed = discovered and called_deferred and cache_read > 0
    print(
        "PASS: deferred tool discovered, called, and the follow-up turn read the cached prefix."
        if passed
        else f"FAIL: discovered={discovered} called_deferred={called_deferred} cache_read={cache_read}",
    )
    return 0 if passed else 1


def main() -> int:
    """Parse arguments and run the requested probe mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Claude model id to probe.")
    parser.add_argument(
        "--provider",
        choices=("anthropic", "vertexai"),
        default="anthropic",
        help="anthropic uses ANTHROPIC_API_KEY; vertexai uses GCP application-default credentials.",
    )
    parser.add_argument("--project-id", default="", help="GCP project id (vertexai provider).")
    parser.add_argument("--region", default="global", help="Vertex AI region (vertexai provider). Default: global")
    parser.add_argument("--dry-run", action="store_true", help="Print the prepared wire request without any API call.")
    args = parser.parse_args()

    if args.dry_run:
        return run_dry_run(args)

    if args.provider == "vertexai":
        if not args.project_id:
            print("Missing --project-id for the vertexai provider; nothing probed.")
            return 2
    elif not os.environ.get("ANTHROPIC_API_KEY"):
        print("No ANTHROPIC_API_KEY set; nothing probed. Use --dry-run or --provider vertexai (GCP ADC).")
        return 2
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
