"""What every family's provider-golden harness shares: loading, the owned request, the pipeline, normalization.

A golden (``tests/fixtures/agno_parity/providers/<family>/<scenario>.json``, format in the README beside them)
pins one request as the Agno-backed provider layer sent it and the assistant message it parsed. Each family
builds its own adapter from the golden's model config and answers its HTTP traffic with the canned reply; this
module turns the golden input into the owned ``ModelRequest``, runs it through the production pipeline stages
(portable tool search, media fallback, retry, request log), and normalizes what came out the way C1 recorded it.
"""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel

from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec, TurnCompleted
from mindroom.prompts import INLINE_MEDIA_FALLBACK_PROMPT
from mindroom.providers.pipeline.compose import wrap_provider
from mindroom.providers.tool_policy import without_provider_tools

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

    from mindroom.llm.provider import ModelProvider, NativeCompactionReplay, StopReason, StreamEvent

FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "agno_parity" / "providers"

# Headers the SDK or transport derives from the environment, the credentials, or the body length.
_VOLATILE_HEADER_PREFIXES = ("x-stainless-", "x-amz-", "amz-sdk-")
_VOLATILE_HEADERS = frozenset(
    {
        "accept-encoding",
        "api-key",
        "authorization",
        "connection",
        "content-length",
        "host",
        "user-agent",
        "x-api-key",
        "x-goog-api-client",
        "x-goog-api-key",
    },
)
_UUID4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
GENERATED_ID = "<uuid4>"
_VOLATILE_MESSAGE_KEYS = ("id", "created_at")
_TIMING_METRIC_KEYS = ("duration", "time_to_first_token")
_STOP_KEYS = ("stop_reason", "finish_reason", "response_status")
# Every golden input key the harness turns into the owned request; a new key fails loudly instead of silently.
_HANDLED_INPUT_KEYS = frozenset(
    {
        "deferred_tool_search",
        "messages",
        "model_config",
        "native_compaction",
        "portable_replay",
        "provider_tools_disabled",
        "response_format",
        "stream",
        "tool_choice",
        "tool_jobs",
        "tools",
    },
)
# Agno's Gemini adapter forwarded a forced choice as its own ``"any"`` mode; the owned protocol says ``required``.
_TOOL_CHOICE_ALIASES = {"any": "required"}
# The optional waiting budget a turn that can start tool jobs adds to each managed function's schema, as
# ``tool_jobs/agno_compat_execution.py`` projects it; the owned loop (B3a) owns the production copy.
BACKGROUND_AFTER_SCHEMA = {
    "anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}],
    "description": (
        "Seconds to wait for this call before it continues in the background, without cancelling it. "
        "Omit or pass null to wait until completion or a newer message you answer; "
        "zero continues in the background at once and returns a job handle."
    ),
}


class PacedBytes(httpx.AsyncByteStream):
    """Response body chunks sent one at a time after a pause each, optionally ending in a dropped connection."""

    def __init__(self, chunks: Sequence[bytes], *, pause_seconds: float = 0.0, drop: bool = False) -> None:
        self._chunks = list(chunks)
        self._pause_seconds = pause_seconds
        self._drop = drop

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """Yield each chunk after its pause, then drop the connection when asked."""
        for chunk in self._chunks:
            await asyncio.sleep(self._pause_seconds)
            yield chunk
        if self._drop:
            msg = "connection dropped"
            raise httpx.ReadError(msg)


@dataclass(frozen=True)
class Replay:
    """What the owned adapter sent and produced for one golden."""

    request: dict[str, Any]
    parsed: dict[str, Any]
    events: list[StreamEvent]
    stop_reason: StopReason | None


def golden_paths(families: Sequence[str]) -> list[Path]:
    """Return every golden of the given families, in a stable order."""
    return sorted(path for family in families for path in (FIXTURE_ROOT / family).glob("*.json"))


def golden_id(path: Path) -> str:
    """Return the ``family/scenario`` id of one golden."""
    return f"{path.parent.name}/{path.stem}"


def load_golden(path: Path) -> dict[str, Any]:
    """Read one golden file."""
    return json.loads(path.read_text(encoding="utf-8"))


def _json_roundtrip(value: object) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads(json.dumps(value, sort_keys=True))


def scrub_generated_ids(value: object) -> Any:  # noqa: ANN401 - arbitrary JSON
    """Replace uuid4 strings, which adapters generate for id-less tool calls, by the golden placeholder."""
    if isinstance(value, dict):
        return {key: scrub_generated_ids(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_generated_ids(item) for item in value]
    if isinstance(value, str) and _UUID4.match(value):
        return GENERATED_ID
    return value


def normalized_request(request: httpx.Request) -> dict[str, Any]:
    """Return the method, URL, stable headers, and JSON body of one SDK request, as C1 recorded them."""
    headers = {
        name: value
        for name, value in sorted(request.headers.items())
        if name not in _VOLATILE_HEADERS and not name.startswith(_VOLATILE_HEADER_PREFIXES)
    }
    return {
        "method": request.method,
        "url": str(request.url),
        "headers": headers,
        "body": json.loads(request.content) if request.content else None,
    }


def normalized_parsed(message: Message) -> dict[str, Any]:
    """Return the assistant message dict, its usage, and the stored stop reason, as C1 recorded them."""
    message_dict = message.to_dict()
    for key in _VOLATILE_MESSAGE_KEYS:
        message_dict.pop(key, None)
    metrics = message_dict.pop("metrics", {})
    for key in _TIMING_METRIC_KEYS:
        metrics.pop(key, None)
    provider_data = message.provider_data or {}
    parsed = {
        "message": message_dict,
        "metrics": metrics,
        "stop_reason": next((provider_data[key] for key in _STOP_KEYS if provider_data.get(key) is not None), None),
    }
    return scrub_generated_ids(_json_roundtrip(parsed))


def expected_parsed(golden: dict[str, Any]) -> dict[str, Any]:
    """Return a golden's parsed reply as the owned message type stores it.

    Agno's ``Message.citations`` was a view ``Message.to_dict()`` never stored, and the owned message has no such
    field (``mindroom.llm.messages``), so the key is left out of the comparison. Claude's raw citations still
    compare inside ``provider_data.content_blocks``; Gemini's grounding sources lived only in that view and in
    the run's ``citations``, which nothing reads and the owned run records drop (``mindroom.llm.runs``).
    """
    parsed = deepcopy(golden["parsed"])
    parsed["message"].pop("citations", None)
    return parsed


def comparable_metrics(metrics: Any) -> dict[str, Any]:  # noqa: ANN401 - MessageMetrics
    """Return a usage report without the wall-clock timings the goldens drop."""
    values = metrics.to_dict()
    for key in _TIMING_METRIC_KEYS:
        values.pop(key, None)
    return values


def response_format_from_golden(encoded: dict[str, Any] | None) -> dict[str, Any] | type[BaseModel] | None:
    """Return the golden's response format: a dict as recorded, or the pydantic class whose schema it pinned."""
    if encoded is None:
        return None
    if encoded["type"] == "dict":
        return encoded["value"]
    module_name, _, class_name = encoded["model"].partition(":")
    schema = getattr(import_module(module_name), class_name)
    assert issubclass(schema, BaseModel)
    if schema.model_json_schema() != encoded["json_schema"]:
        msg = f"{encoded['model']} no longer matches the schema recorded in the golden"
        raise AssertionError(msg)
    return schema


def tool_spec_from_dict(tool: dict[str, Any]) -> ToolSpec:
    """Return the definition of one ``Function.to_dict()`` dict.

    The capture rebuilt each tool with Agno's ``Function.from_dict``, which defaulted the approval flags to False,
    so a golden tool without them stands for False; every other omitted field stays None.
    """
    return ToolSpec(
        name=tool["name"],
        description=tool.get("description"),
        parameters=tool.get("parameters") or {},
        strict=tool.get("strict"),
        requires_confirmation=tool.get("requires_confirmation", False),
        external_execution=tool.get("external_execution", False),
        approval_type=tool.get("approval_type"),
    )


def with_background_after(tools: Sequence[ToolSpec], managed: frozenset[str]) -> list[ToolSpec]:
    """Stand in for the loop's tool-job projection: each managed function gains the optional wait parameter."""
    projected: list[ToolSpec] = []
    for tool in tools:
        if tool.name not in managed:
            projected.append(tool)
            continue
        parameters = deepcopy(dict(tool.parameters))
        parameters.setdefault("properties", {})["background_after"] = deepcopy(BACKGROUND_AFTER_SCHEMA)
        projected.append(replace(tool, parameters=parameters))
    return projected


def golden_request(
    case_input: dict[str, Any],
    *,
    native_compaction_settings: Callable[..., NativeCompactionReplay | None] | None = None,
) -> ModelRequest:
    """Return the owned request a golden's input describes.

    Deferred tool names always travel on the request: the portable tool-search stage hides them behind
    ``call_tool`` on routes without native tool search, and the adapters defer them server-side elsewhere.
    A native-compaction input becomes the adapter's replay settings, as history planning asks for them.
    """
    unhandled = set(case_input) - _HANDLED_INPUT_KEYS
    assert not unhandled, f"the harness does not know how to send golden input {sorted(unhandled)}"
    tools = [tool_spec_from_dict(tool) for tool in case_input["tools"]]
    if (tool_jobs := case_input.get("tool_jobs")) is not None:
        tools = with_background_after(tools, frozenset(tool_jobs["managed"]))
    if case_input.get("provider_tools_disabled"):
        assert case_input["tool_choice"] == "none", "decision requests disable tool selection through tool_choice"
    native_compaction = None
    if (native := case_input.get("native_compaction")) is not None:
        assert native_compaction_settings is not None, "the golden's route has no native compaction"
        native_compaction = native_compaction_settings(
            threshold=native["threshold"],
            history_generation=native["history_generation"],
            allow_authored=True,
        )
        assert native_compaction is not None, "the golden's route supports native compaction"
    deferred = case_input["deferred_tool_search"]
    tool_choice = case_input["tool_choice"]
    return ModelRequest(
        messages=[Message.from_dict(deepcopy(message)) for message in case_input["messages"]],
        tools=tools,
        tool_choice=_TOOL_CHOICE_ALIASES.get(tool_choice, tool_choice) if isinstance(tool_choice, str) else tool_choice,
        response_format=response_format_from_golden(case_input["response_format"]),
        stream=case_input["stream"],
        deferred_tool_names=frozenset(deferred["tool_names"]) if deferred is not None else frozenset(),
        portable_replay=bool(case_input["portable_replay"]),
        native_compaction=native_compaction,
    )


def pipeline_provider(adapter: ModelProvider, *, idle_timeout_seconds: float | None = None) -> ModelProvider:
    """Wrap a bare adapter in the production pipeline stages, without request logging."""
    return wrap_provider(
        adapter,
        idle_timeout_seconds=idle_timeout_seconds,
        media_fallback_prompt=INLINE_MEDIA_FALLBACK_PROMPT,
        request_log=None,
    )


async def replay_through_pipeline(
    adapter: ModelProvider,
    case_input: dict[str, Any],
    recorded: list[httpx.Request],
    *,
    native_compaction_settings: Callable[..., NativeCompactionReplay | None] | None = None,
) -> Replay:
    """Send a golden's request through the pipeline around ``adapter`` and return what it sent and produced.

    A decision request runs inside ``without_provider_tools()``, as participation decisions and judgments do.
    """
    request = golden_request(case_input, native_compaction_settings=native_compaction_settings)
    decision = without_provider_tools() if case_input.get("provider_tools_disabled") else nullcontext()
    with decision:
        events = [event async for event in pipeline_provider(adapter).stream(request)]
    if len(recorded) != 1:
        msg = f"expected one provider request, got {[str(request.url) for request in recorded]}"
        raise AssertionError(msg)
    await recorded[0].aread()
    completed = events[-1]
    assert isinstance(completed, TurnCompleted), f"the stream ended with {completed!r}"
    return Replay(
        request=normalized_request(recorded[0]),
        parsed=normalized_parsed(completed.message),
        events=events,
        stop_reason=completed.stop_reason,
    )


def expected_stop_reason(golden: dict[str, Any]) -> StopReason:
    """Return the normalized stop reason a golden's reply ends with: cut off by the limit, a tool call, or done."""
    if golden["scenario"].startswith("output_limit_stop"):
        return "output_limit"
    return "tool_use" if golden["parsed"]["message"].get("tool_calls") else "end"
