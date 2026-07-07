"""Live probe for Anthropic server-side tool search (defer_loading) on the MindRoom hook path.

Closes the verification gap left by PR #1412: all defer_loading behavior was tested
against fakes because no API key was available. This probe drives the real merged
code path — an Agno Claude model with the MindRoom prompt-cache and deferred-tool
hooks installed — against the live API and checks that:

1. the request with ``defer_loading`` tools plus the regex search tool is accepted;
2. the model discovers the deferred tool via ``tool_search_tool_result`` and calls it;
3. the follow-up turn reads the cached prefix (``cache_read_tokens > 0``).

Usage:
    ANTHROPIC_API_KEY=... uv run python scripts/testing/tool_search_live_probe.py
    uv run python scripts/testing/tool_search_live_probe.py --dry-run  # no key needed

Exit codes: 0 = pass, 1 = fail, 2 = no API key.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from agno.models.anthropic import Claude
from agno.models.message import Message

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mindroom.claude_prompt_cache import install_claude_deferred_tool_search  # noqa: E402

DEFERRED_TOOL_NAME = "get_weather"
# Deterministic padding so the system prompt clears every model's minimum
# cacheable prefix (4096 tokens on Opus-tier models).
_SYSTEM_PROMPT = "You are a MindRoom tool-search live probe agent. " + (
    "This sentence is deterministic cache padding for the probe system prompt. " * 700
)

_PROBE_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "echo",
            "description": "Echo the provided text back to the user.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "Text to echo."}},
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": DEFERRED_TOOL_NAME,
            "description": "Get the current weather for a location.",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string", "description": "City name."}},
                "required": ["location"],
            },
        },
    },
]


def build_probe_model(model_id: str, api_key: str) -> Claude:
    """Build a Claude model mirroring the production Anthropic settings, hooks not yet installed."""
    return Claude(
        id=model_id,
        api_key=api_key,
        cache_system_prompt=True,
        extended_cache_time=True,
    )


def install_probe_hooks(model: Claude) -> None:
    """Install the production deferred-tool-search (and prompt-cache) hooks.

    Must run after any fake client is planted on the model: the hook wraps the
    current ``get_client``, so installing first would let a later fake bypass it.
    """
    install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({DEFERRED_TOOL_NAME}))


def run_dry_run(model_id: str) -> int:
    """Print the prepared wire request via a capturing fake client; no network."""
    captured: list[dict[str, Any]] = []

    class _FakeMessagesAPI:
        def create(self, **kwargs: Any) -> Any:  # noqa: ANN401
            captured.append(kwargs)
            message = "dry-run: no live response"
            raise SystemExit(message)

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessagesAPI()

    model = build_probe_model(model_id, api_key="dry-run-key")
    vars(model)["get_client"] = lambda: _FakeClient()
    install_probe_hooks(model)

    messages = [Message(role="system", content=_SYSTEM_PROMPT), Message(role="user", content="probe")]
    with contextlib.suppress(SystemExit):
        model.response(messages=messages, tools=_PROBE_TOOLS, compression_manager=None)

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
    print(f"system markers: {sum(1 for block in system_blocks if block.get('cache_control'))}")
    deferred = [tool["name"] for tool in wire["tools"] if tool.get("defer_loading") is True]
    ok = (
        wire["tools"][0].get("type") == "tool_search_tool_regex_20251119"
        and deferred == [DEFERRED_TOOL_NAME]
        and not any(tool.get("cache_control") for tool in wire["tools"] if tool.get("defer_loading"))
    )
    print(f"dry-run wire shape: {'OK' if ok else 'UNEXPECTED'}")
    return 0 if ok else 1


def run_live(model_id: str, api_key: str) -> int:
    """Run the two-turn live probe and report discovery and cache reuse."""
    model = build_probe_model(model_id, api_key)
    install_probe_hooks(model)
    messages = [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(
            role="user",
            content="What is the weather in Paris right now? Use your available tools; search for one if needed.",
        ),
    ]

    first = model.response(messages=messages, tools=_PROBE_TOOLS, compression_manager=None)
    server_blocks = (first.provider_data or {}).get("server_tool_blocks", [])
    block_types = [block.get("type") for block in server_blocks]
    discovered = "tool_search_tool_result" in block_types
    tool_calls = first.tool_executions or []
    called_deferred = any(execution.tool_name == DEFERRED_TOOL_NAME for execution in tool_calls)
    usage_first = first.response_usage
    print(f"turn 1: server blocks={block_types} tool calls={[execution.tool_name for execution in tool_calls]}")
    if usage_first is not None:
        print(f"turn 1 usage: cache_write={usage_first.cache_write_tokens} cache_read={usage_first.cache_read_tokens}")

    if not called_deferred:
        print(f"FAIL: model did not call the deferred tool '{DEFERRED_TOOL_NAME}'.")
        return 1

    messages.extend(
        Message(
            role="tool",
            tool_call_id=execution.tool_call_id,
            tool_name=execution.tool_name,
            content="22 degrees Celsius and sunny.",
        )
        for execution in tool_calls
    )
    second = model.response(messages=messages, tools=_PROBE_TOOLS, compression_manager=None)
    usage_second = second.response_usage
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
    parser.add_argument("--model-id", default="claude-sonnet-4-6", help="Claude model id to probe.")
    parser.add_argument("--dry-run", action="store_true", help="Print the prepared wire request without any API call.")
    args = parser.parse_args()

    if args.dry_run:
        return run_dry_run(args.model_id)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("No ANTHROPIC_API_KEY set; nothing probed. Use --dry-run to inspect the wire request offline.")
        return 2
    return run_live(args.model_id, api_key)


if __name__ == "__main__":
    sys.exit(main())
