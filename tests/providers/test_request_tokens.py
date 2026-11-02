"""Each provider route sizes a request from the body its own builder renders for it (``ModelProvider.request_tokens``).

The goldens under ``tests/fixtures/agno_parity/providers`` hold the bodies the routes send; sizing reads those bodies,
so whatever a route sends for a message, a tool, or a setting is what its size counts.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
import pytest

from mindroom.llm.media import Image
from mindroom.llm.messages import Message, model_provenance
from mindroom.llm.provider import ModelRequest, TokenCounter, ToolSpec
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.native_compaction import checkpoint_items
from mindroom.providers.claude_wire_blocks import CLAUDE_BLOCKS_KEY
from mindroom.token_counting.counts import count
from mindroom.token_counting.requests import MEDIA_ALLOWANCE_TOKENS, plain_request_parts, route_request_tokens
from tests.providers.anthropic_family_support import FAMILIES as CLAUDE_FAMILIES
from tests.providers.anthropic_family_support import provider_for
from tests.providers.gemini_ollama_support import FAMILIES as GEMINI_OLLAMA_FAMILIES
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.golden_support import golden_id, golden_paths, golden_request, load_golden, pipeline_provider
from tests.providers.openai_family_support import OPENAI_FAMILIES, golden_provider

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider

GOLDENS = golden_paths((*OPENAI_FAMILIES, *CLAUDE_FAMILIES, *GEMINI_OLLAMA_FAMILIES))
# Body fields no model reads as text: ids, roles, block types, cache breakpoints, signatures, ciphertext, media.
_TRANSPORT_KEYS = frozenset(
    {
        "id",
        "call_id",
        "tool_call_id",
        "tool_use_id",
        "type",
        "role",
        "status",
        "cache_control",
        "prompt_cache_breakpoint",
        "signature",
        "data",
        "encrypted_content",
        "encrypted_index",
        "thought_signature",
        "media_type",
        "mime_type",
        "image_url",
        "file_id",
        "file_data",
        "file_uri",
        "url",
        "detail",
        "format",
        "index",
        "thoughtSignature",
        "mimeType",
        "fileUri",
        "toolCallId",
        "filename",
        "displayName",
        "file_url",
        "images",
    },
)
# The body fields a request's model context lives in, per wire.
_CONTEXT_FIELDS = ("messages", "input", "contents", "system", "systemInstruction", "instructions", "tools")


def _recording_counter(texts: list[str]) -> TokenCounter:
    """Return a counter that appends every text it counts to ``texts`` and counts a quarter token per character."""

    def estimate(text: str) -> int:
        texts.append(text)
        return len(text) // 4

    return TokenCounter(estimate=estimate)


def _adapter(path: Path, root: Path) -> ModelProvider:
    golden = load_golden(path)
    family = path.parent.name
    config = golden["input"]["model_config"]
    if family in CLAUDE_FAMILIES:
        return provider_for(config, root, httpx2.AsyncClient())
    if family in GEMINI_OLLAMA_FAMILIES:
        return build_adapter(config, root, lambda _request: httpx.Response(500))
    return golden_provider(golden, root).adapter


def _strings(value: object, key: str | None = None) -> list[str]:
    """Return the string leaves of a sent body value that a model reads, leaving transport fields out."""
    if key in _TRANSPORT_KEYS:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        if value.get("defer_loading"):
            # A deferred tool ships for the API to load on a search, and costs nothing until then.
            return []
        return [text for name, nested in value.items() for text in _strings(nested, name)]
    if isinstance(value, list):
        return [text for nested in value for text in _strings(nested, key)]
    return []


@pytest.mark.parametrize("path", GOLDENS, ids=golden_id)
def test_a_route_sizes_every_text_its_body_sends(path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every text a route's golden body carries for the model is among what its size, through the pipeline, counts.

    The pipeline sizes the request its stages send the adapter, such as the media fallback's note in place of media a
    route omits and portable tool search's ``call_tool`` calls.
    """
    golden = load_golden(path)
    adapter = _adapter(path, tmp_path)
    request = golden_request(golden["input"], native=adapter.native_history_route)
    texts: list[str] = []
    monkeypatch.setattr(type(adapter), "token_counter", property(lambda _self: _recording_counter(texts)))
    pipeline_provider(adapter).request_tokens(request)
    counted = "".join(texts)
    body = golden["request"]["body"]
    sent = [text for field in _CONTEXT_FIELDS for text in _strings(body.get(field), field)]
    missing = [
        text
        for text in sent
        if text not in counted
        and json.dumps(text, ensure_ascii=False)[1:-1] not in counted
        # A route's note in place of a file it cannot send counts within that file's allowance.
        and not text.startswith(("[Attached document not sent inline", "[Attachment omitted"))
    ]
    assert missing == []


@pytest.mark.parametrize("path", GOLDENS, ids=golden_id)
def test_an_answer_and_what_follows_it_add_their_own_size(path: Path, tmp_path: Path) -> None:
    """A request sized from an answer on is the request before it plus that tail, never less and barely more.

    The tail counts nothing the request before it held; counted apart, the two can split a token or a JSON list where
    they join. Only answers after the latest native checkpoint anchor, since the checkpoint replaces what precedes it.
    """
    golden = load_golden(path)
    adapter = _adapter(path, tmp_path)
    request = golden_request(golden["input"], native=adapter.native_history_route)
    route = request.native_compaction.route if request.native_compaction is not None else None
    checkpoints = [index for index, message in enumerate(request.messages) if checkpoint_items(message, route)]
    whole = adapter.request_tokens(request)
    for index, message in enumerate(request.messages):
        if message.role != "assistant" or index == 0 or index <= max(checkpoints, default=-1):
            continue
        prefix = replace(request, messages=request.messages[:index])
        assert whole <= adapter.request_tokens(prefix) + adapter.request_tokens(request, start=index) <= whole + 4


@pytest.mark.parametrize("path", GOLDENS, ids=golden_id)
def test_an_answer_and_what_follows_it_count_none_of_the_definitions_sent_before_it(path: Path, tmp_path: Path) -> None:
    """The tools and output schema a request repeats are in the answer's reported size, so its tail never counts them.

    Only a deferred tool the tail's own tool search loaded is new there, so a request that defers tools keeps them.
    """
    golden = load_golden(path)
    adapter = _adapter(path, tmp_path)
    request = golden_request(golden["input"], native=adapter.native_history_route)
    bare = replace(request, tools=(), output_schema=None)
    for index, message in enumerate(request.messages):
        if message.role != "assistant" or index == 0 or request.deferred_tools.names:
            continue
        assert adapter.request_tokens(request, start=index) == adapter.request_tokens(bare, start=index)


_REASONING = "The forecast says rain in the evening, so a light jacket and closed shoes are the right call. " * 12
_THINKING = "Weigh the evening forecast against the walking plan before suggesting what to pack. " * 12


def _route(family: str, tmp_path: Path) -> ModelProvider:
    """Return the adapter one of ``family``'s goldens configures, built under its own directory."""
    root = tmp_path / family
    root.mkdir()
    return _adapter(golden_paths((family,))[0], root)


def _claude_answer(route: ModelProvider, *blocks: dict[str, Any], **fields: Any) -> Message:  # noqa: ANN401
    """Return an earlier answer of ``route``'s Claude model that stored ``blocks``, with ``fields`` set."""
    return Message(
        role="assistant",
        content="Pack a light rain jacket.",
        provider_data={**model_provenance(route.identity.id, route.identity.provider), CLAUDE_BLOCKS_KEY: list(blocks)},
        from_history=True,
        **fields,
    )


def _conversation(answer: Message) -> ModelRequest:
    return ModelRequest(
        messages=(
            Message(role="system", content="You are a concise travel assistant."),
            Message(role="user", content="What should I pack for Paris tomorrow?"),
            answer,
            Message(role="user", content="And for the evening?"),
        ),
    )


def _difference(route: ModelProvider, answer: Message, without: Message) -> int:
    """Return how much more the request holding ``answer`` gives the model than the one holding ``without``."""
    return route.request_tokens(_conversation(answer)) - route.request_tokens(_conversation(without))


@pytest.mark.parametrize(("family", "replays"), [("kimi", True), ("deepseek", True), ("openai_chat", False)])
def test_a_chat_route_counts_the_reasoning_content_it_sends_back(family: str, replays: bool, tmp_path: Path) -> None:
    """The Kimi and DeepSeek routes send each earlier answer's ``reasoning_content`` back, so its size counts it."""
    route = _route(family, tmp_path)
    plain = Message(role="assistant", content="Pack a light rain jacket.", from_history=True)
    reasoned = replace(plain, reasoning_content=_REASONING)

    added = _difference(route, reasoned, plain)

    reasoning = route.request_tokens(ModelRequest(messages=(Message(role="user", content=_REASONING),)))
    assert added >= reasoning * 0.9 if replays else added == 0


@pytest.mark.parametrize(("family", "replays"), [("anthropic", True), ("bedrock_claude", False)])
def test_claude_counts_earlier_thinking_where_it_replays_it(family: str, replays: bool, tmp_path: Path) -> None:
    """Claude with a thinking binding replays an earlier turn's thinking; Bedrock, which takes no binding, drops it."""
    route = _route(family, tmp_path)
    text = {"type": "text", "text": "Pack a light rain jacket."}
    thinking = {"type": "thinking", "thinking": _THINKING, "signature": "c2lnbmF0dXJl"}

    added = _difference(route, _claude_answer(route, thinking, text), _claude_answer(route, text))

    thought = route.request_tokens(ModelRequest(messages=(Message(role="user", content=_THINKING),)))
    assert added >= thought * 0.9 if replays else added == 0


def test_gemini_counts_a_replayed_thought_signature_as_its_reported_reasoning(tmp_path: Path) -> None:
    """A thought signature costs about the reasoning its reply reported, on the run's own replies and history alike."""
    route = _route("gemini", tmp_path)
    provenance = model_provenance(route.identity.id, route.identity.provider)
    metrics = MessageMetrics(tokens=TokenCounts(output_tokens=320, reasoning_tokens=300))
    plain = Message(role="assistant", content="Pack a light rain jacket.", provider_data=provenance, metrics=metrics)
    signed = replace(plain, provider_data={**provenance, "thought_signature": "c2lnbmF0dXJl"})

    assert _difference(route, signed, plain) == 300
    assert _difference(route, replace(signed, from_history=True), replace(plain, from_history=True)) == 300


def test_openrouter_counts_encrypted_reasoning_details_as_their_reported_reasoning(tmp_path: Path) -> None:
    """OpenRouter sends each answer's ``reasoning_details`` back; encrypted ones cost what their answer reported."""
    route = _route("openrouter", tmp_path)
    metrics = MessageMetrics(tokens=TokenCounts(output_tokens=420, reasoning_tokens=400))
    plain = Message(role="assistant", content="Pack a light rain jacket.", metrics=metrics, from_history=True)
    encrypted = replace(
        plain,
        provider_data={"reasoning_details": [{"type": "reasoning.encrypted", "data": "Z" * 4_000, "id": "rd-1"}]},
    )

    assert _difference(route, encrypted, plain) == 400


def test_openrouter_counts_reasoning_details_in_the_clear_as_their_text(tmp_path: Path) -> None:
    """A ``reasoning_details`` item sent in the clear counts its text, not the reasoning its answer reported."""
    route = _route("openrouter", tmp_path)
    metrics = MessageMetrics(tokens=TokenCounts(output_tokens=420, reasoning_tokens=400))
    plain = Message(role="assistant", content="Pack a light rain jacket.", metrics=metrics, from_history=True)
    summarized = replace(
        plain,
        provider_data={"reasoning_details": [{"type": "reasoning.summary", "summary": _REASONING, "id": "rd-1"}]},
    )

    # Counted within the joined request, the summary can round a token or two apart from its own count.
    assert abs(_difference(route, summarized, plain) - count(route.token_counter, _REASONING)) <= 2


def test_claude_counts_replayed_thinking_by_its_answer_s_hidden_output(tmp_path: Path) -> None:
    """Claude replays a thinking summary but bills the thinking its answer produced: its output beyond the visible part."""
    route = _route("anthropic", tmp_path)
    text = {"type": "text", "text": "Pack a light rain jacket."}
    thinking = {"type": "thinking", "thinking": "Rain is likely.", "signature": "c2lnbmF0dXJl"}
    metrics = MessageMetrics(tokens=TokenCounts(output_tokens=3_000))
    visible = count(route.token_counter, text["text"])

    added = _difference(
        route,
        _claude_answer(route, thinking, text, metrics=metrics),
        _claude_answer(route, text, metrics=metrics),
    )
    assert added == 3_000 - visible


def test_a_plain_route_sizes_an_answer_and_what_follows_it_alone() -> None:
    """From an answer on, a route counts only the messages it sends from there, neither tools nor earlier media."""
    texts: list[str] = []
    lookup = ToolSpec(name="lookup", description="Look something up.", parameters={"type": "object"}, strict=None)
    chart = Image(content=b"chart", mime_type="image/png")
    request = ModelRequest(
        messages=(
            Message(role="user", content="Read this chart", images=[chart]),
            Message(role="assistant", content="It rises."),
            Message(role="user", content="Why?"),
        ),
        tools=(lookup,),
    )

    tail = route_request_tokens(_recording_counter(texts), request, 1, plain_request_parts)

    assert texts == ["It rises.Why?"]
    assert tail == len("It rises.Why?") // 4
    assert route_request_tokens(_recording_counter([]), request, 0, plain_request_parts) > MEDIA_ALLOWANCE_TOKENS
