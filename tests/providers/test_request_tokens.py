"""Each provider route sizes a request from the body its own builder renders for it (``ModelProvider.request_tokens``).

The goldens under ``tests/fixtures/agno_parity/providers`` hold the bodies the routes send; sizing reads those bodies,
so whatever a route sends for a message, a tool, or a setting is what its size counts.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import httpx
import httpx2
import pytest
from pypdf import PdfWriter

from mindroom.llm.media import File, Image
from mindroom.llm.messages import Message, model_provenance, stable_serialize
from mindroom.llm.provider import DeferredTools, ModelRequest, TokenCounter, ToolSpec
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.native_compaction import checkpoint_items
from mindroom.providers.claude_wire_blocks import CLAUDE_BLOCKS_KEY
from mindroom.providers.openai.response_replay import RESPONSE_OUTPUT_KEY
from mindroom.token_counting.counts import count, opaque_tokens
from mindroom.token_counting.requests import MEDIA_ALLOWANCE_TOKENS, plain_request_parts, route_request_tokens
from tests.fakes.scripted_model import ScriptedModel
from tests.fakes.token_counter import quarter_tokens
from tests.history_helpers import record_native_checkpoint
from tests.providers.anthropic_family_support import FAMILIES as CLAUDE_FAMILIES
from tests.providers.anthropic_family_support import isolated_provider_environment, provider_for
from tests.providers.gemini_ollama_support import FAMILIES as GEMINI_OLLAMA_FAMILIES
from tests.providers.gemini_ollama_support import build_adapter
from tests.providers.golden_support import golden_id, golden_paths, golden_request, load_golden, pipeline_provider
from tests.providers.openai_family_support import OPENAI_FAMILIES, golden_provider, responses_adapter

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
# The fields holding a tool call's arguments as an object.
_ARGUMENT_KEYS = frozenset({"input", "args"})
# The body fields a request's model context lives in, per wire.
_CONTEXT_FIELDS = ("messages", "input", "contents", "system", "systemInstruction", "instructions", "tools")


def _recording_counter(texts: list[str]) -> TokenCounter:
    """Return a counter that appends every text it counts to ``texts`` and counts a quarter token per character."""

    def estimate(text: str) -> int:
        texts.append(text)
        return quarter_tokens(text)

    return TokenCounter(estimate=estimate)


def _adapter(path: Path, root: Path, **extra_kwargs: object) -> ModelProvider:
    """Return the adapter of a golden's model config, with ``extra_kwargs`` authored over its own."""
    golden = load_golden(path)
    family = path.parent.name
    config = golden["input"]["model_config"]
    config = {**config, "extra_kwargs": {**config.get("extra_kwargs", {}), **extra_kwargs}}
    if family in CLAUDE_FAMILIES:
        return provider_for(config, root, httpx2.AsyncClient())
    if family in GEMINI_OLLAMA_FAMILIES:
        return build_adapter(config, root, lambda _request: httpx.Response(500))
    return golden_provider({**golden, "input": {**golden["input"], "model_config": config}}, root).adapter


def _strings(value: object, key: str | None = None) -> list[str]:
    """Return the string leaves of a sent body value that a model reads, leaving transport fields out.

    A tool call's arguments (Claude's ``input``, Gemini's ``args``) are the model's own, read whole whatever their names.
    """
    if key in _TRANSPORT_KEYS:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        if value.get("defer_loading"):
            # A deferred tool ships for the API to load on a search, and costs nothing until then.
            return []
        return [
            text
            for name, nested in value.items()
            for text in (
                _leaves(nested) if name in _ARGUMENT_KEYS and isinstance(nested, dict) else _strings(nested, name)
            )
        ]
    if isinstance(value, list):
        return [text for nested in value for text in _strings(nested, key)]
    return []


def _leaves(value: object) -> list[str]:
    """Return every non-blank string leaf of ``value``."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return [text for nested in value.values() for text in _leaves(nested)]
    if isinstance(value, list):
        return [text for nested in value for text in _leaves(nested)]
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


def _route(family: str, tmp_path: Path, name: str = "", **extra_kwargs: object) -> ModelProvider:
    """Return the adapter one of ``family``'s goldens configures, with ``extra_kwargs``, under its own directory."""
    root = tmp_path / f"{family}{name}"
    root.mkdir()
    return _adapter(golden_paths((family,))[0], root, **extra_kwargs)


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


def test_gemini_counts_a_signed_function_call_as_its_call_and_reported_reasoning(tmp_path: Path) -> None:
    """A function call Gemini signed replays its signature too, which costs the reasoning, not the ciphertext's length."""
    route = _route("gemini", tmp_path)
    provenance = model_provenance(route.identity.id, route.identity.provider)
    metrics = MessageMetrics(tokens=TokenCounts(output_tokens=320, reasoning_tokens=300))
    call = {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": '{"city": "Paris"}'}}
    plain = Message(role="assistant", tool_calls=[call], provider_data=provenance, metrics=metrics)
    signed = replace(plain, tool_calls=[{**call, "thought_signature": "c2lnbmF0dXJl" * 400}])

    assert _difference(route, signed, plain) == 300


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


def test_claude_counts_a_web_search_result_s_page_ciphertext_a_quarter_token_per_character(tmp_path: Path) -> None:
    """A server tool's result replays its page encrypted; the model reads it decrypted, which its length bounds."""
    route = _route("anthropic", tmp_path)
    search = {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search", "input": {"query": "Paris weather"}}
    text = {"type": "text", "text": "Pack a light rain jacket."}

    def answer(page: str) -> Message:
        result = {
            "type": "web_search_result",
            "url": "https://example.org/paris",
            "title": "Paris",
            "encrypted_content": page,
        }
        found = {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": [result]}
        return _claude_answer(route, search, found, text)

    assert _difference(route, answer("Q" * 8_000), answer("Q" * 4)) == opaque_tokens(8_000) - opaque_tokens(4)


@pytest.mark.parametrize("searched", ["before", "after", "both"])
def test_claude_counts_a_deferred_tool_in_the_tail_only_when_the_tail_loads_it(tmp_path: Path, searched: str) -> None:
    """A tool search before the answer a request is sized from loaded its tool into what that answer reported.

    A later search that finds the tool again sends its reference beside no new definition.
    """
    route = _route("anthropic", tmp_path)
    search = {
        "type": "server_tool_use",
        "id": "srvtoolu_1",
        "name": "tool_search_tool_regex",
        "input": {"pattern": "order"},
    }
    found = {
        "type": "tool_search_tool_result",
        "tool_use_id": "srvtoolu_1",
        "content": {
            "type": "tool_search_tool_search_result",
            "tool_references": [{"type": "tool_reference", "tool_name": "lookup_order"}],
        },
    }
    searching = _claude_answer(route, search, found, {"type": "text", "text": "Found the order tool."})
    plain = _claude_answer(route, {"type": "text", "text": "Pack a light rain jacket."})
    first, second = {"before": (searching, plain), "after": (plain, searching), "both": (searching, searching)}[
        searched
    ]

    def request(description: str) -> ModelRequest:
        lookup = ToolSpec(
            name="lookup_order",
            description=description,
            parameters={"type": "object", "properties": {"order_id": {"type": "string"}}},
            strict=None,
        )
        return ModelRequest(
            messages=(
                Message(role="user", content="Where is my order?"),
                first,
                Message(role="user", content="And the weather?"),
                second,
                Message(role="user", content="Thanks."),
            ),
            tools=(lookup,),
            deferred_tools=DeferredTools(names=frozenset({"lookup_order"})),
        )

    short, long = request("Look up an order by its id."), request(_REASONING)
    whole = route.request_tokens(short)
    prefix = route.request_tokens(replace(short, messages=short.messages[:3]))
    tail = route.request_tokens(short, start=3)

    assert whole <= prefix + tail <= whole + 4
    # Only a tail whose own search loads the tool first counts its definition.
    assert (route.request_tokens(long, start=3) > tail) is (searched == "after")


@pytest.mark.parametrize("thinking", [{"type": "disabled"}, {"type": "enabled", "budget_tokens": 2_048}])
def test_claude_identifies_a_request_by_whether_it_replays_earlier_thinking(
    tmp_path: Path,
    thinking: dict[str, object],
) -> None:
    """A thinking setting that sends no binding leaves earlier thinking out, so no size reported before describes it."""
    config = {"api_key": "parity-test-key", "id": "claude-sonnet-5-5", "provider": "anthropic"}
    (tmp_path / "default").mkdir()
    (tmp_path / "changed").mkdir()
    default = provider_for(config, tmp_path / "default", httpx2.AsyncClient())
    changed = provider_for(
        {**config, "extra_kwargs": {"thinking": thinking}},
        tmp_path / "changed",
        httpx2.AsyncClient(),
    )
    blocks = (
        {"type": "thinking", "thinking": _THINKING, "signature": "c2lnbmF0dXJl"},
        {"type": "text", "text": "Pack light."},
    )
    request = _conversation(_claude_answer(default, *blocks))

    assert default.request_tokens(request) > changed.request_tokens(request)
    assert default.context_key(request) != changed.context_key(request)


def test_claude_counts_a_tool_result_stored_as_a_list_by_its_blocks(tmp_path: Path) -> None:
    """A tool result an older runtime stored as a list of blocks counts the text of each block it sends."""
    route = _route("anthropic", tmp_path)
    call = {"id": "call_list", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}

    def request(content: object) -> ModelRequest:
        result = Message(role="tool", tool_call_id="call_list", tool_name="lookup", content=content)
        return ModelRequest(
            messages=(
                Message(role="user", content="Look it up."),
                Message(role="assistant", tool_calls=[call]),
                result,
            ),
        )

    added = route.request_tokens(request([{"type": "text", "text": _REASONING}])) - route.request_tokens(request([]))

    # Counted within the joined request, the block can round a token or two apart from its own count.
    assert abs(added - count(route.token_counter, _REASONING)) <= 2


@pytest.mark.parametrize("start", [0, 1], ids=["whole", "tail"])
def test_claude_counts_a_tool_call_s_arguments_whatever_their_names(tmp_path: Path, start: int) -> None:
    """A call's arguments are the model's own, so one named like a transport field, such as ``data``, counts as sent."""
    route = _route("anthropic", tmp_path)

    def request(data: str) -> ModelRequest:
        arguments = json.dumps({"id": "form-7", "data": data})
        call = {"id": "call_form", "type": "function", "function": {"name": "make_request", "arguments": arguments}}
        return ModelRequest(
            messages=(
                Message(role="user", content="Post the form."),
                Message(role="assistant", tool_calls=[call]),
                Message(role="tool", tool_call_id="call_form", tool_name="make_request", content="Posted."),
            ),
        )

    added = route.request_tokens(request(_REASONING), start=start) - route.request_tokens(request(""), start=start)

    assert abs(added - count(route.token_counter, _REASONING)) <= 2


_FUNCTION_TOOL = {
    "type": "function",
    "function": {
        "name": "web_lookup",
        "description": "Look a question up on the web.",
        "parameters": {"type": "object"},
    },
}


@pytest.mark.parametrize(
    ("family", "authored"),
    [
        ("anthropic", {"tools": [{"type": "web_search_20250305", "name": "web_search"}]}),
        ("anthropic", {"extra_body": {"system": "Answer in French. " * 40}}),
        ("openai_chat", {"tools": [_FUNCTION_TOOL]}),
        ("deepseek", {"tools": [_FUNCTION_TOOL]}),
        ("openai_responses", {"tools": [{"type": "web_search"}]}),
        ("ollama", {"tools": [_FUNCTION_TOOL]}),
    ],
    ids=["claude-tools", "claude-system", "chat-tools", "deepseek-tools", "responses-tools", "ollama-tools"],
)
def test_what_request_params_authors_counts_once_per_reported_request(
    tmp_path: Path,
    family: str,
    authored: dict[str, Any],
) -> None:
    """What ``request_params`` authors goes with every request, so an answer's reported size already holds it."""
    plain = _route(family, tmp_path)
    configured = _route(family, tmp_path, "-authored", request_params=authored)
    answer = Message(
        role="assistant",
        content="Pack a light rain jacket.",
        provider_data=model_provenance(plain.identity.id, plain.identity.provider),
    )
    request = _conversation(answer)

    assert configured.request_tokens(request) > plain.request_tokens(request)
    assert configured.request_tokens(request, start=2) == plain.request_tokens(request, start=2)


@pytest.mark.parametrize(("family", "beside"), [("gemini", True), ("gemini_vertexai", False)])
def test_gemini_counts_function_declarations_and_search_parts_only_where_search_goes_beside_them(
    tmp_path: Path,
    family: str,
    beside: bool,
) -> None:
    """Gemini 3 takes Google Search beside function tools and gets its search parts back; Vertex AI takes neither."""
    route = _route(family, tmp_path, search=True)

    def request(description: str, query: str) -> ModelRequest:
        lookup = ToolSpec(name="lookup", description=description, parameters={"type": "object"}, strict=None)
        searched = Message(
            role="assistant",
            content="Found it.",
            provider_data={"gemini_parts": [{"tool_call": {"args": {"query": query}}}, {"text": "Found it."}]},
        )
        return ModelRequest(
            messages=(Message(role="user", content="Look it up."), searched, Message(role="user", content="Thanks.")),
            tools=(lookup,),
        )

    plain = route.request_tokens(request("Look something up.", "Paris"))

    assert (route.request_tokens(request(_REASONING, "Paris")) > plain) is beside
    assert (route.request_tokens(request("Look something up.", _REASONING)) > plain) is beside


@pytest.mark.parametrize("field", ["generation_config", "generative_model_kwargs"])
def test_gemini_sizes_and_identifies_the_tools_its_generation_config_authors(tmp_path: Path, field: str) -> None:
    """Tools authored in the generation config go with a request that offers no function tools of its own."""

    def route(description: str, name: str) -> ModelProvider:
        tool = {"function_declarations": [{"name": "lookup", "description": description}]}
        return _route("gemini", tmp_path, name, **{field: {"tools": [tool]}})

    short, long = route("Look something up.", "-short"), route(_REASONING, "-long")
    request = ModelRequest(messages=(Message(role="user", content="Look it up."),))

    assert long.request_tokens(request) > short.request_tokens(request)
    assert long.context_key(request) != short.context_key(request)


def test_request_sizing_never_counts_attached_file_bytes(tmp_path: Path) -> None:
    """A route's estimate must not count an attachment's encoded bytes as prompt text."""
    model = responses_adapter(tmp_path, "gpt-6-luna", store=False)
    pdf = File(content=b"%PDF-1.7\n" + b"x" * 300_000, mime_type="application/pdf", filename="report.pdf")
    message = Message(role="user", content="Summarize the attached report.", files=[pdf])

    assert model.request_tokens(ModelRequest(messages=(message,))) < 10_000


def _pdf(pages: int, *, delete: int = 0) -> bytes:
    """Return a PDF of ``pages`` blank pages, then an incremental save that deletes the last ``delete`` of them."""
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    written = io.BytesIO()
    writer.write(written)
    if not delete:
        return written.getvalue()
    edited = PdfWriter(io.BytesIO(written.getvalue()), incremental=True)
    for _ in range(delete):
        del edited.pages[-1]
    saved = io.BytesIO()
    edited.write(saved)
    return saved.getvalue()


def _with_file(route: ModelProvider, file: File | None) -> int:
    """Return the size of a request whose prompt carries ``file``."""
    files = [file] if file is not None else None
    return route.request_tokens(ModelRequest(messages=(Message(role="user", content="Read this.", files=files),)))


@pytest.mark.parametrize("family", ["anthropic", "gemini", "openai_chat"])
def test_an_attached_text_file_counts_its_text(tmp_path: Path, family: str) -> None:
    """A route reads a text file as text, so a long log counts far beyond the allowance, and a short note far below."""
    route = _route(family, tmp_path)
    log = "2026-11-02 12:00:00 ERROR worker 7 crashed while reading the queue\n" * 5_000
    path = tmp_path / "server.log"
    path.write_text(log)
    without = _with_file(route, None)

    assert _with_file(route, File(filepath=str(path), mime_type="text/plain")) - without == count(
        route.token_counter,
        log,
    )
    assert _with_file(route, File(content="Meet at noon.", mime_type="text/plain")) - without < 100
    # A file that is not text, or whose bytes are gone, counts the allowance.
    assert _with_file(route, File(content=b"\x89\xff\x00binary", filename="data.bin")) - without == (
        MEDIA_ALLOWANCE_TOKENS
    )
    assert _with_file(route, File(filepath=str(tmp_path / "removed.log"))) - without == MEDIA_ALLOWANCE_TOKENS
    assert _with_file(route, File(url="https://example.com/notes.txt")) - without == MEDIA_ALLOWANCE_TOKENS


@pytest.mark.parametrize(
    ("family", "page_tokens"),
    [("anthropic", MEDIA_ALLOWANCE_TOKENS), ("openai_chat", MEDIA_ALLOWANCE_TOKENS), ("gemini", 1_120)],
)
def test_an_attached_pdf_counts_its_pages(tmp_path: Path, family: str, page_tokens: int) -> None:
    """A PDF counts the route's most per page for the pages its latest revision holds, so a long report counts long."""
    route = _route(family, tmp_path)
    without = _with_file(route, None)

    def counted(content: bytes) -> int:
        return _with_file(route, File(content=content, mime_type="application/pdf", filename="report.pdf")) - without

    assert counted(_pdf(1)) == page_tokens
    assert counted(_pdf(80)) == 80 * page_tokens
    # An incremental save that deleted pages keeps their objects, which the latest page tree no longer counts.
    assert counted(_pdf(80, delete=79)) == page_tokens
    # A PDF the parser cannot read, or whose page tree declares no page count, counts one page.
    assert counted(b"%PDF-1.7\ndamaged") == page_tokens
    # A cross-reference table cut off before its trailer, which pypdf before 6.21.4 read forever.
    assert counted(b"%PDF-1.4\nstartxref\n28\n%%EOF\nxref\n1 0\naaa") == page_tokens
    assert counted(_pdf(1).replace(b"/Count 1", b"/Count /X")) == page_tokens
    assert counted(_pdf(1).replace(b"/Count 1", b"/Count -7")) == page_tokens


def test_an_attached_path_that_is_not_a_regular_file_is_never_read(tmp_path: Path) -> None:
    """A FIFO, which no wire sends, counts the allowance without blocking the count on a read."""
    route = _route("anthropic", tmp_path)
    fifo = tmp_path / "pipe.log"
    os.mkfifo(fifo)

    assert _with_file(route, File(filepath=str(fifo))) - _with_file(route, None) == MEDIA_ALLOWANCE_TOKENS


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_an_attached_file_the_runtime_may_not_read_counts_the_allowance(tmp_path: Path) -> None:
    """A file the wires would refuse to read fails no count."""
    route = _route("anthropic", tmp_path)
    locked = tmp_path / "locked.log"
    locked.write_text("secret notes\n" * 1_000)
    locked.chmod(0)

    assert _with_file(route, File(filepath=str(locked))) - _with_file(route, None) == MEDIA_ALLOWANCE_TOKENS


# About 20,000 tokens; words, because tiktoken takes seconds on one 80,000-letter word.
_SEARCHED_TOOL = ToolSpec(name="big_tool", description="word " * 20_000, parameters={}, strict=None)


def _searched_request(provider: str, tmp_path: Path) -> tuple[Any, ModelRequest]:
    """Return a route and a request whose answer's tool search loaded the deferred ``big_tool``.

    OpenAI replays the loaded definitions inside the search output; Claude replays only references that the provider
    expands, from a turn stored now or one stored only with its server-tool blocks before v2026.9.44.
    """
    if provider == "openai":
        model: Any = responses_adapter(tmp_path, "gpt-6-luna", store=False)
        provider_data: dict[str, Any] = {
            RESPONSE_OUTPUT_KEY: [
                {
                    "type": "tool_search_call",
                    "id": "ts_1",
                    "call_id": "search_1",
                    "execution": "server",
                    "arguments": {"query": "big"},
                    "status": "completed",
                },
                {
                    "type": "tool_search_output",
                    "id": "tso_1",
                    "call_id": "search_1",
                    "execution": "server",
                    "status": "completed",
                    "tools": [
                        {
                            "type": "function",
                            "name": "big_tool",
                            "description": _SEARCHED_TOOL.description,
                            "parameters": {},
                        },
                    ],
                },
            ],
        }
    else:
        model = provider_for(
            {"provider": "anthropic", "id": "claude-sonnet-5", "extra_kwargs": {"api_key": "test-key"}},
            tmp_path / "claude",
            httpx2.AsyncClient(),
        )
        search = [
            {"type": "server_tool_use", "id": "srv_1", "name": "tool_search_tool_regex", "input": {"query": "big"}},
            {
                "type": "tool_search_tool_result",
                "tool_use_id": "srv_1",
                "content": {
                    "type": "tool_search_tool_search_result",
                    "tool_references": [{"type": "tool_reference", "tool_name": "big_tool"}],
                },
            },
        ]
        provider_data = {"claude_blocks" if provider == "claude" else "server_tool_blocks": search}
    messages = (
        Message(role="user", content="Use the big tool"),
        Message(role="assistant", provider_data=provider_data),
        Message(role="user", content="Go on"),
    )
    return model, ModelRequest(
        messages=messages,
        tools=(_SEARCHED_TOOL,),
        deferred_tools=DeferredTools(names=frozenset({"big_tool"})),
    )


@pytest.mark.parametrize("provider", ["openai", "claude", "claude_legacy"])
def test_request_sizing_counts_deferred_tool_definitions_once_tool_search_loads_them(
    tmp_path: Path,
    provider: str,
) -> None:
    """A deferred definition costs nothing until a tool search loads it, then enters the context once.

    A request anchored on the searching answer charges it with that answer; one anchored after it, never again.
    """
    with isolated_provider_environment():
        model, request = _searched_request(provider, tmp_path)
        unsearched = replace(request, messages=request.messages[:1])
        # What the definition costs a request that offers it outright.
        offered = model.request_tokens(replace(unsearched, deferred_tools=DeferredTools()))
        definition = offered - model.request_tokens(replace(unsearched, tools=()))

        assert definition > 15_000
        assert model.request_tokens(unsearched) < 1_000
        assert abs(model.request_tokens(request) - model.request_tokens(unsearched) - definition) < 1_000
        assert abs(model.request_tokens(request, start=1) - definition) < 1_000
        assert model.request_tokens(request, start=2) < 1_000


_SYSTEM_MESSAGE = Message(role="system", content="You are a careful engineer.\n\nStay concise.")
_SEARCH_PARAMETERS = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
    "required": ["query"],
}
_EXPORT_PARAMETERS = {
    "type": "object",
    "properties": {"title": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}
_SEARCH_DOCS = ToolSpec(
    name="search_docs",
    description="Search the engineering docs for a matching answer.",
    parameters=_SEARCH_PARAMETERS,
    strict=None,
)
_EXPORT_NOTES = ToolSpec(
    name="export_notes",
    description="Export the current working notes as markdown.",
    parameters=_EXPORT_PARAMETERS,
    strict=True,
)


def _plain_request_tokens(
    *messages: Message,
    tools: tuple[ToolSpec, ...] = (),
    deferred: frozenset[str] = frozenset(),
) -> int:
    request = ModelRequest(messages=messages, tools=tools, deferred_tools=DeferredTools(names=deferred))
    return ScriptedModel([]).request_tokens(request)


def test_a_plain_route_counts_a_system_message_and_a_prompt() -> None:
    """A route that sends messages by role counts each message's text as it sends it."""
    prompt = Message(role="user", content="Current prompt")

    assert _plain_request_tokens(_SYSTEM_MESSAGE, prompt) == quarter_tokens(
        f"{_SYSTEM_MESSAGE.content}{prompt.content}",
    )


def test_a_plain_route_counts_each_offered_tool_definition_as_it_sends_it() -> None:
    """Tool definitions count with every field the tool set; fields it left unset are not sent, so they do not count."""
    sent_definitions = [
        {
            "name": "search_docs",
            "description": "Search the engineering docs for a matching answer.",
            "parameters": _SEARCH_PARAMETERS,
        },
        {
            "name": "export_notes",
            "description": "Export the current working notes as markdown.",
            "parameters": _EXPORT_PARAMETERS,
            "strict": True,
        },
    ]

    assert _plain_request_tokens(_SYSTEM_MESSAGE, tools=(_SEARCH_DOCS, _EXPORT_NOTES)) == quarter_tokens(
        str(_SYSTEM_MESSAGE.content) + stable_serialize(sent_definitions),
    )


def test_a_plain_route_leaves_deferred_tools_out() -> None:
    """A tool that tool search keeps off the request until it is found costs nothing up front."""
    tools = (_SEARCH_DOCS, _EXPORT_NOTES)
    nothing_deferred = _plain_request_tokens(_SYSTEM_MESSAGE, tools=tools)
    export_deferred = _plain_request_tokens(_SYSTEM_MESSAGE, tools=tools, deferred=frozenset({"export_notes"}))

    assert export_deferred < nothing_deferred
    assert export_deferred == _plain_request_tokens(_SYSTEM_MESSAGE, tools=(_SEARCH_DOCS,))
    assert _plain_request_tokens(
        _SYSTEM_MESSAGE,
        tools=tools,
        deferred=frozenset({"search_docs", "export_notes"}),
    ) == _plain_request_tokens(_SYSTEM_MESSAGE)


@pytest.mark.parametrize(
    ("family", "checkpoint"),
    [
        ("anthropic", {"type": "compaction", "content": "Earlier turns, summarized."}),
        ("openai_responses", {"type": "compaction", "id": "cmp_1", "encrypted_content": "opaque-checkpoint"}),
    ],
)
def test_media_a_native_checkpoint_replaced_counts_nothing(
    tmp_path: Path,
    family: str,
    checkpoint: dict[str, Any],
) -> None:
    """A checkpoint replaces the transcript before it, so the files that transcript held go too; a later one counts."""
    route = _route(family, tmp_path)
    assert route.native_history_route is not None
    native = route.native_history_route.native_compaction_settings(
        threshold=60_000,
        history_generation="",
        allow_authored=False,
    )
    assert native is not None
    compacted = Message(role="assistant", content="Ready.")
    record_native_checkpoint(compacted, [checkpoint], native)

    def sized(*, before: bool, after: bool) -> int:
        report = [File(content=b"%PDF-1.7 report", mime_type="application/pdf", filename="report.pdf")]
        return route.request_tokens(
            ModelRequest(
                messages=(
                    Message(role="user", content="Read this.", files=report if before else None),
                    compacted,
                    Message(role="user", content="And this?", files=report if after else None),
                ),
                native_compaction=native,
            ),
        )

    assert sized(before=True, after=False) == sized(before=False, after=False)
    assert sized(before=False, after=True) > sized(before=False, after=False)


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
