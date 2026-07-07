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
from agno.tools.function import Function

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from mindroom.claude_prompt_cache import install_claude_deferred_tool_search  # noqa: E402
from mindroom.vertex_claude_compat import MindroomVertexAIClaude  # noqa: E402

DEFERRED_TOOL_NAME = "get_weather"
# Deterministic padding so the system prompt clears every model's minimum
# cacheable prefix (4096 tokens on Opus-tier models).
_SYSTEM_PROMPT = "You are a MindRoom tool-search live probe agent. " + (
    "This sentence is deterministic cache padding for the probe system prompt. " * 700
)


def echo(text: str) -> str:
    """Echo the provided text back to the user.

    Args:
        text: Text to echo.

    """
    return text


def get_weather(location: str) -> str:
    """Get the current weather for a location.

    Args:
        location: City name.

    """
    return f"The weather in {location} is 22 degrees Celsius and sunny."


def build_probe_tools() -> list[Function]:
    """Build executable probe tools so Agno's response loop can run them for real."""
    return [Function.from_callable(echo), Function.from_callable(get_weather)]


def build_probe_model(args: argparse.Namespace) -> Claude:
    """Build a Claude model mirroring the production settings, hooks not yet installed.

    ``--provider vertexai`` builds the same ``MindroomVertexAIClaude`` class the
    production model loader uses, authenticated via GCP application-default
    credentials — no ``ANTHROPIC_API_KEY`` needed.
    """
    if args.provider == "vertexai":
        return MindroomVertexAIClaude(
            id=args.model_id,
            project_id=args.project_id,
            region=args.region,
            cache_system_prompt=True,
            extended_cache_time=True,
        )
    return Claude(
        id=args.model_id,
        api_key=os.environ.get("ANTHROPIC_API_KEY", "dry-run-key"),
        cache_system_prompt=True,
        extended_cache_time=True,
    )


def install_probe_hooks(model: Claude) -> None:
    """Install the production deferred-tool-search (and prompt-cache) hooks.

    Must run after any fake client is planted on the model: the hook wraps the
    current ``get_client``, so installing first would let a later fake bypass it.
    """
    install_claude_deferred_tool_search(model, deferred_tool_names=frozenset({DEFERRED_TOOL_NAME}))


class _DryRunStop(BaseException):
    """Raised by the dry-run fake client once the wire request has been captured.

    Derives from BaseException so Agno's invoke error handling (which wraps any
    Exception in ModelProviderError and retries) lets it propagate untouched.
    """


def run_dry_run(args: argparse.Namespace) -> int:
    """Print the prepared wire request via a capturing fake client; no network."""
    captured: list[dict[str, Any]] = []

    class _FakeMessagesAPI:
        def create(self, **kwargs: Any) -> Any:  # noqa: ANN401
            captured.append(kwargs)
            raise _DryRunStop

    class _FakeClient:
        def __init__(self) -> None:
            self.messages = _FakeMessagesAPI()

    model = build_probe_model(args)
    vars(model)["get_client"] = lambda: _FakeClient()
    install_probe_hooks(model)

    messages = [Message(role="system", content=_SYSTEM_PROMPT), Message(role="user", content="probe")]
    with contextlib.suppress(_DryRunStop):
        model.response(messages=messages, tools=build_probe_tools(), compression_manager=None)

    if not captured:
        print("FAIL: the hooked client was never invoked; the request never reached the fake SDK client.")
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


def run_live(args: argparse.Namespace) -> int:
    """Run the two-turn live probe and report discovery and cache reuse."""
    model = build_probe_model(args)
    install_probe_hooks(model)
    messages = [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(
            role="user",
            content="What is the weather in Paris right now? Use your available tools; search for one if needed.",
        ),
    ]

    tools = build_probe_tools()
    # Agno's response() runs the full production loop: the model searches, the
    # API expands the discovered schema, Agno executes the entrypoint, and the
    # model finishes with the tool result.
    first = model.response(messages=messages, tools=tools, compression_manager=None)
    block_types = [
        block.get("type")
        for message in messages
        if message.role == "assistant"
        for block in (message.provider_data or {}).get("server_tool_blocks", [])
    ]
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

    messages.append(Message(role="user", content="Thanks — and what is the weather in London?"))
    second = model.response(messages=messages, tools=tools, compression_manager=None)
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
    parser.add_argument("--model-id", default="claude-sonnet-5", help="Claude model id to probe.")
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
