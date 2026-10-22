"""Stored Claude history replays as a valid request: media within the limits, citations, tool order, compaction."""

from __future__ import annotations

import base64
import copy
import json
from datetime import UTC, datetime
from itertools import takewhile
from typing import TYPE_CHECKING, Any

import pytest

from mindroom.attachments.media import attachment_records_to_media
from mindroom.attachments.storage import AttachmentRecord
from mindroom.history.native import restore_native_history
from mindroom.history.types import ScopeSummary
from mindroom.llm.media import File, Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, ToolSpec
from mindroom.native_compaction import record_native_checkpoint
from mindroom.providers.anthropic import prompt_cache
from mindroom.providers.anthropic.native_compaction import native_replay_messages_for_route
from tests.history_helpers import _completed_run, _session
from tests.providers.anthropic_family_support import (
    claude_turn,
    json_reply,
    route_provider,
    sse_reply,
)
from tests.providers.golden_support import Wire
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.providers.anthropic.messages import ClaudeProvider

_CLAUDE_ROUTES = ("anthropic", "vertexai_claude", "bedrock_claude")
_PDF = b"%PDF-1.4\n%fake report\n"
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
)
_UNSUPPORTED_FILES = {
    "deck.pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "sheet.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "bundle.zip": "application/zip",
}
_CHECKPOINT = {"type": "compaction", "content": "Launch port 4321.", "encrypted_content": "opaque"}
_TEXT = {"type": "text", "text": "Ready"}
_POLICY = {"edits": [{"type": "compact_20260112", "instructions": "Keep launch facts."}]}


def _user_blocks(body: dict[str, Any], index: int = 0) -> list[dict[str, Any]]:
    """Return one user turn's blocks without prompt-cache markers."""
    users = [message for message in body["messages"] if message["role"] == "user"]
    return [{key: value for key, value in block.items() if key != "cache_control"} for block in users[index]["content"]]


def _notes(blocks: list[dict[str, Any]]) -> list[str]:
    return [block["text"] for block in blocks if block["type"] == "text" and "not sent inline" in block["text"]]


async def _sent(provider: ClaudeProvider, wire: Wire, messages: list[Message], **request: Any) -> dict[str, Any]:  # noqa: ANN401
    """Send one request, without streaming unless asked, and return its wire body."""
    await claude_turn(provider, ModelRequest(messages=messages, **{"stream": False, **request}))
    return wire.body()


# --- Inline media --------------------------------------------------------------------------------------------


def _attachment_turn(tmp_path: Path) -> Message:
    """A user turn carrying uploads converted exactly as inbound attachments are."""
    uploads = {"report.pdf": ("application/pdf", _PDF), "notes.txt": ("text/plain", b"Plain notes.")}
    uploads |= {filename: (mime_type, b"PK\x03\x04binary") for filename, mime_type in _UNSUPPORTED_FILES.items()}
    records = []
    for index, (filename, (mime_type, payload)) in enumerate(uploads.items()):
        local_path = tmp_path / f"att_{index}{filename[filename.rindex('.') :]}"
        local_path.write_bytes(payload)
        records.append(
            AttachmentRecord(
                attachment_id=f"att_{index}",
                local_path=local_path,
                kind="file",
                filename=filename,
                mime_type=mime_type,
            ),
        )
    _, _, files, _ = attachment_records_to_media(records)
    return Message(role="user", content="Summarize the attachments.", files=files)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _CLAUDE_ROUTES)
async def test_unsupported_documents_become_text_notes_while_pdf_stays_inline(tmp_path: Path, route: str) -> None:
    """Office and archive uploads never reach Claude as base64 documents."""
    wire = Wire(json_reply([{"type": "text", "text": "Read."}]))

    body = await _sent(route_provider(route, tmp_path, wire), wire, [_attachment_turn(tmp_path)])

    blocks = _user_blocks(body)
    documents = [block for block in blocks if block["type"] == "document"]
    assert [document["source"]["type"] for document in documents] == ["base64", "text"]
    assert documents[0]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(documents[0]["source"]["data"]) == _PDF
    assert documents[1]["source"]["data"] == "Plain notes."
    notes = _notes(blocks)
    assert len(notes) == len(_UNSUPPORTED_FILES)
    for note, mime_type in zip(notes, _UNSUPPORTED_FILES.values(), strict=True):
        assert mime_type in note
        assert "get_attachment without view" in note


@pytest.mark.asyncio
async def test_inline_media_past_the_request_size_budget_becomes_a_text_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PDF that would push the request past the provider body limit is described instead."""
    monkeypatch.setattr(prompt_cache, "MAX_INLINE_MEDIA_BYTES", 2000)
    large_pdf = _PDF + b"x" * 3000
    turn = Message(
        role="user",
        content="Compare the reports.",
        files=[
            File(content=_PDF, mime_type="application/pdf", filename="small.pdf"),
            File(content=large_pdf, mime_type="application/pdf", filename="large.pdf"),
        ],
    )
    wire = Wire(json_reply([{"type": "text", "text": "Read."}]))

    blocks = _user_blocks(await _sent(route_provider("anthropic", tmp_path, wire), wire, [turn]))

    [document] = [block for block in blocks if block["type"] == "document"]
    assert base64.b64decode(document["source"]["data"]) == _PDF
    [note] = _notes(blocks)
    assert "size limit" in note
    assert "get_attachment without view" in note


@pytest.mark.asyncio
async def test_text_documents_count_toward_the_request_size_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A plain-text document whose escaped JSON would push the request past the provider body limit is described."""
    monkeypatch.setattr(prompt_cache, "MAX_INLINE_MEDIA_BYTES", 2000)
    turn = Message(
        role="user",
        content="Compare the exports.",
        files=[
            File(content=b"Short notes.", mime_type="text/plain", filename="notes.txt"),
            # 1.8 KB of raw text that escapes to 2.25 KB of JSON on the wire.
            File(content=b"a,b\n" * 450, mime_type="text/csv", filename="export.csv"),
        ],
    )
    wire = Wire(json_reply([{"type": "text", "text": "Read."}]))

    blocks = _user_blocks(await _sent(route_provider("anthropic", tmp_path, wire), wire, [turn]))

    [document] = [block for block in blocks if block["type"] == "document"]
    assert document["source"] == {"type": "text", "media_type": "text/plain", "data": "Short notes."}
    [note] = _notes(blocks)
    assert "size limit" in note


@pytest.mark.asyncio
async def test_bedrock_uses_a_smaller_inline_media_budget_than_vertex(tmp_path: Path) -> None:
    """A PDF under the Vertex budget but over Bedrock's 20 MB request limit is described on Bedrock only."""
    # 15 MB raw encodes to 20 MB of base64: between the Bedrock and Vertex budgets.
    large_pdf = _PDF + b"x" * 15_000_000

    def turn() -> Message:
        return Message(
            role="user",
            content="Summarize the report and the chart.",
            images=[Image(content=_PNG, mime_type="image/png")],
            files=[File(content=large_pdf, mime_type="application/pdf", filename="large.pdf")],
        )

    bodies = {}
    for route in ("bedrock_claude", "vertexai_claude"):
        wire = Wire(json_reply([{"type": "text", "text": "Read."}]))
        bodies[route] = _user_blocks(await _sent(route_provider(route, tmp_path, wire), wire, [turn()]))

    assert not [block for block in bodies["bedrock_claude"] if block["type"] == "document"]
    [note] = _notes(bodies["bedrock_claude"])
    assert "size limit" in note
    [document] = [block for block in bodies["vertexai_claude"] if block["type"] == "document"]
    assert base64.b64decode(document["source"]["data"]) == large_pdf
    for blocks in bodies.values():
        [image] = [block for block in blocks if block["type"] == "image"]
        assert base64.b64decode(image["source"]["data"]) == _PNG


@pytest.mark.asyncio
async def test_image_over_the_per_image_limit_becomes_a_text_note_on_bedrock_and_vertex(tmp_path: Path) -> None:
    """Bedrock and Vertex accept 5 MB base64 images and the direct API 10 MB, so only they describe a larger one."""
    # 4 MB raw encodes to about 5.3 MB of base64: over the 5 MB limit, under the direct API's 10 MB.
    large_png = _PNG + b"\0" * 4_000_000

    def turn() -> Message:
        return Message(
            role="user",
            content="Compare the charts.",
            images=[Image(content=_PNG, mime_type="image/png"), Image(content=large_png, mime_type="image/png")],
        )

    bodies = {}
    for route in _CLAUDE_ROUTES:
        wire = Wire(json_reply([{"type": "text", "text": "Read."}]))
        bodies[route] = _user_blocks(await _sent(route_provider(route, tmp_path, wire), wire, [turn()]))

    assert [base64.b64decode(block["source"]["data"]) for block in bodies["anthropic"] if block["type"] == "image"] == [
        _PNG,
        large_png,
    ]
    for route in ("bedrock_claude", "vertexai_claude"):
        [image] = [block for block in bodies[route] if block["type"] == "image"]
        assert base64.b64decode(image["source"]["data"]) == _PNG
        [note] = _notes(bodies[route])
        assert "per-image limit" in note
        assert "get_attachment without view" in note


# --- Citations ------------------------------------------------------------------------------------------------

_CITED_ANSWER = "The grass is green."
_THINKING_BLOCK = {"type": "thinking", "thinking": "Look it up.", "signature": "sig-1"}
_STORED_CITATIONS = {
    "char_location_without_title": {
        "type": "char_location",
        "cited_text": "The grass is green. ",
        "document_index": 0,
        "start_char_index": 0,
        "end_char_index": 20,
    },
    "char_location_with_title": {
        "type": "char_location",
        "cited_text": "The grass is green. ",
        "document_index": 0,
        "document_title": "notes.txt",
        "start_char_index": 0,
        "end_char_index": 20,
        "file_id": None,
    },
    "page_location_without_title": {
        "type": "page_location",
        "cited_text": "The grass is green.",
        "document_index": 0,
        "start_page_number": 1,
        "end_page_number": 2,
    },
    "content_block_location_without_title": {
        "type": "content_block_location",
        "cited_text": "The grass is green.",
        "document_index": 1,
        "start_block_index": 0,
        "end_block_index": 1,
    },
    "web_search_result_location_without_title": {
        "type": "web_search_result_location",
        "cited_text": "The grass is green.",
        "url": "https://example.com/grass",
        "encrypted_index": "enc-1",
    },
}


def _cited_history(citation: dict[str, Any]) -> list[Message]:
    """A thread whose stored assistant turn cites a document that is no longer replayed."""
    return [
        Message(role="user", content="What color is the grass?", from_history=True),
        Message(
            role="assistant",
            content=f"{_CITED_ANSWER} It really is.",
            reasoning_content="Look it up.",
            provider_data={
                "signature": "sig-1",
                "content_blocks": [
                    dict(_THINKING_BLOCK),
                    {"type": "text", "text": _CITED_ANSWER, "citations": [dict(citation)]},
                    {"type": "text", "text": " It really is."},
                ],
            },
            from_history=True,
        ),
        Message(role="user", content="And the sky?"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _CLAUDE_ROUTES)
@pytest.mark.parametrize("citation", _STORED_CITATIONS.values(), ids=_STORED_CITATIONS.keys())
async def test_cited_answers_replay_as_plain_text(tmp_path: Path, route: str, citation: dict[str, Any]) -> None:
    """A stored cited answer replays as its text, because the cited document is not resent; history stays intact."""
    wire = Wire(json_reply([{"type": "text", "text": "Blue."}]))
    messages = _cited_history(citation)
    stored = copy.deepcopy(messages[1].provider_data)

    body = await _sent(route_provider(route, tmp_path, wire), wire, messages)

    [assistant] = [message for message in body["messages"] if message["role"] == "assistant"]
    assert [
        {key: value for key, value in block.items() if key != "cache_control"} for block in assistant["content"]
    ] == [
        _THINKING_BLOCK,
        {"type": "text", "text": _CITED_ANSWER},
        {"type": "text", "text": " It really is."},
    ]
    assert messages[1].provider_data == stored


# --- Tool result order ----------------------------------------------------------------------------------------

# The user message the Agno-era loop appended after a tool batch whose results carried media.
_MEDIA_FOLLOW_UP = "The tool call above generated the attached media."


def _thread_with_media_between_results() -> list[Message]:
    """A stored thread whose approved call resumed after a sibling's media follow-up, as Responses runs saved it."""
    calls = [
        {
            "id": f"fc_{name}",
            "call_id": f"call_{name}",
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }
        for name in ("view_image", "run_subagent")
    ]
    return [
        Message(role="user", content="Show the chart and summarize it.", from_history=True),
        Message(role="assistant", tool_calls=calls, from_history=True),
        Message(role="tool", tool_call_id="fc_view_image", tool_name="view_image", content="{}", from_history=True),
        Message(role="user", content=_MEDIA_FOLLOW_UP, from_history=True),
        Message(role="tool", tool_call_id="fc_run_subagent", tool_name="run_subagent", content="ok", from_history=True),
        Message(role="assistant", content="Here is the summary.", from_history=True),
        Message(role="user", content="Thanks, what next?"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", _CLAUDE_ROUTES)
async def test_tool_results_lead_the_user_turn_after_their_calls(tmp_path: Path, route: str) -> None:
    """Anthropic pairs only the tool results that open the next user turn, so stored media follow-ups move after them."""
    wire = Wire(json_reply([{"type": "text", "text": "Next steps."}]))
    messages = _thread_with_media_between_results()

    body = await _sent(route_provider(route, tmp_path, wire), wire, messages)

    wire_messages = body["messages"]
    for index, message in enumerate(wire_messages):
        tool_use_ids = {block["id"] for block in message["content"] if block["type"] == "tool_use"}
        if message["role"] != "assistant" or not tool_use_ids:
            continue
        next_turn = wire_messages[index + 1]
        leading = takewhile(lambda block: block["type"] == "tool_result", next_turn["content"])
        assert (next_turn["role"], {block["tool_use_id"] for block in leading}) == ("user", tool_use_ids)
    assert [block.get("text") for message in wire_messages for block in message["content"]].count(_MEDIA_FOLLOW_UP) == 1
    assert [message.role for message in messages[:6]] == ["user", "assistant", "tool", "user", "tool", "assistant"]


# --- Tool definitions and foreign history --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vertex_strips_provider_level_strict_but_keeps_a_schema_property_named_strict(tmp_path: Path) -> None:
    """Vertex rejects the tool-level strict field, while a parameter that happens to be called strict is valid."""
    parameters = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "strict": {"type": "boolean"}},
        "required": ["name"],
        "additionalProperties": False,
    }
    tool = ToolSpec(name="update_profile", description="Update profile", parameters=parameters, strict=True)
    wire = Wire(json_reply([{"type": "text", "text": "Done."}]))

    body = await _sent(
        route_provider("vertexai_claude", tmp_path, wire, extra_kwargs={"cache_system_prompt": False}),
        wire,
        [Message(role="user", content="Update it.")],
        tools=[tool],
    )

    [sent] = body["tools"]
    assert "strict" not in sent
    assert set(sent["input_schema"]["properties"]) == {"name", "strict"}
    assert tool.parameters["properties"] == {"name": {"type": "string"}, "strict": {"type": "boolean"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_data", [{"response_id": "response-1"}, {"signature": ""}], ids=["none", "empty"])
async def test_reasoning_without_an_anthropic_signature_never_becomes_a_thinking_block(
    tmp_path: Path,
    foreign_data: dict[str, str],
) -> None:
    """Another provider's reasoning replays as plain history, while Claude's own signed thinking still replays."""
    foreign = Message(
        role="assistant",
        content="Codex answer",
        reasoning_content="Unsigned Codex reasoning",
        provider_data=foreign_data,
        tool_calls=[{"id": "call-1", "type": "function", "function": {"name": "demo_tool", "arguments": "{}"}}],
        from_history=True,
    )
    own = Message(
        role="assistant",
        content="Claude answer",
        reasoning_content="Signed Claude reasoning",
        provider_data={"signature": "signature-1"},
        from_history=True,
    )
    messages = [
        Message(role="user", content="First question", from_history=True),
        foreign,
        Message(role="tool", tool_call_id="call-1", tool_name="demo_tool", content="ok", from_history=True),
        Message(role="user", content="Second question", from_history=True),
        own,
        Message(role="user", content="Current question"),
    ]
    wire = Wire(json_reply([{"type": "text", "text": "Done."}]))

    body = await _sent(route_provider("vertexai_claude", tmp_path, wire), wire, messages)

    assistants = [message["content"] for message in body["messages"] if message["role"] == "assistant"]
    assert [block["type"] for block in assistants[0]] == ["text", "tool_use"]
    assert [block["type"] for block in assistants[1]] == ["thinking", "text"]
    assert foreign.reasoning_content == "Unsigned Codex reasoning"


# --- Native compaction ----------------------------------------------------------------------------------------

_COMPACTION_USAGE = {
    "input_tokens": 2000,
    "output_tokens": 100,
    "cache_read_input_tokens": 30,
    "cache_creation_input_tokens": 20,
    "iterations": [
        {
            "type": "compaction",
            "input_tokens": 60000,
            "output_tokens": 1000,
            "cache_read_input_tokens": 500,
            "cache_creation_input_tokens": 100,
        },
        {
            "type": "message",
            "model": "claude-sonnet-5-5",
            "input_tokens": 2000,
            "output_tokens": 100,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": 20,
        },
    ],
}


def _compacted(blocks: list[dict[str, Any]], *, stream: bool = False, stop_reason: str = "end_turn") -> Any:  # noqa: ANN401
    reply = sse_reply if stream else json_reply
    return reply([dict(block) for block in blocks], stop_reason=stop_reason, usage=_COMPACTION_USAGE)


def _assistant(turn: Any) -> Message:  # noqa: ANN401 - the TurnCompleted of one request
    """Return the stored copy of a finished turn's message, as the next request reads it back from history."""
    return Message.from_dict(json.loads(json.dumps(turn.message.to_dict())))


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["anthropic", "vertexai_claude"])
@pytest.mark.parametrize("stream", [False, True])
async def test_a_native_checkpoint_replays_in_place_of_the_prefix_it_summarized(
    tmp_path: Path,
    route: str,
    *,
    stream: bool,
) -> None:
    """The edit asks for compaction; the next request starts at the checkpoint; without native replay all is canonical."""
    thinking = {"type": "thinking", "thinking": "Recall the port", "signature": "checkpoint-bound"}
    wire = Wire(
        _compacted([_CHECKPOINT, _TEXT], stream=stream),
        _compacted([thinking, _TEXT], stream=stream),
        _compacted([_TEXT], stream=stream),
    )
    provider = route_provider(route, tmp_path, wire, extra_kwargs={"max_tokens": 4096})
    replay = provider.native_compaction_settings(threshold=60000)
    assert replay is not None
    messages = [Message(role="user", content="Remember the launch port is 4321.")]
    for index, prompt in enumerate(("What port?", "Repeat the port.")):
        turn = await claude_turn(
            provider,
            ModelRequest(messages=messages, native_compaction=replay, stream=stream),
        )
        messages = [*messages, _assistant(turn), Message(role="user", content=prompt)]
        if index == 1:
            await claude_turn(provider, ModelRequest(messages=messages, stream=stream))

    edit = wire.body(0)["context_management"]["edits"][0]
    assert (edit["type"], edit["trigger"]) == ("compact_20260112", {"type": "input_tokens", "value": 60000})
    assert edit.get("pause_after_compaction", False) is False
    replayed = wire.body(1)["messages"]
    assert replayed[0]["role"] == "assistant"
    assert replayed[0]["content"][0]["type"] == "compaction"
    assert "Remember the launch port" not in json.dumps(replayed)
    assert "What port?" in json.dumps(replayed)
    canonical = [block for message in wire.body(-1)["messages"] for block in message["content"]]
    assert all(block["type"] not in {"compaction", "thinking"} for block in canonical)
    assert "Remember the launch port" in json.dumps(canonical)


@pytest.mark.asyncio
async def test_compaction_iterations_are_billed_and_the_last_one_is_the_context(tmp_path: Path) -> None:
    """Compaction costs stay in the request's usage, while the active context counts only the final iteration."""
    wire = Wire(_compacted([_CHECKPOINT, _TEXT]))
    provider = route_provider("anthropic", tmp_path, wire)
    native = provider.native_compaction_settings(threshold=60000)

    turn = await claude_turn(provider, ModelRequest(messages=[_user("Hi")], native_compaction=native, stream=False))

    metrics = turn.message.metrics
    assert (metrics.input_tokens, metrics.output_tokens, metrics.total_tokens) == (62000, 1100, 63100)
    assert (metrics.cache_read_tokens, metrics.cache_write_tokens) == (530, 120)
    assert metrics.provider_metrics["context_usage"] == {
        "input_tokens": 2000,
        "cache_read_tokens": 30,
        "cache_write_tokens": 20,
    }


def _user(text: str) -> Message:
    return Message(role="user", content=text)


async def _recorded_reply(
    provider: ClaudeProvider,
    wire: Wire,
    blocks: list[dict[str, Any]],
    *,
    threshold: int | None = 60000,
    stop_reason: str = "end_turn",
) -> Message:
    """Return the stored assistant message of one reply with ``blocks``, recorded under ``threshold``'s route."""
    wire.replies[:] = [json_reply(blocks, stop_reason=stop_reason, usage=_COMPACTION_USAGE)]
    replay = provider.native_compaction_settings(threshold=threshold) if threshold is not None else None
    request = ModelRequest(messages=[_user("Hi")], native_compaction=replay, stream=False)
    return _assistant(await claude_turn(provider, request))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, ""])
async def test_an_empty_summary_keeps_the_canonical_history(tmp_path: Path, content: str | None) -> None:
    """An unsuccessful summary cannot replace the original conversation."""
    wire = Wire(json_reply([_TEXT]))
    provider = route_provider("anthropic", tmp_path, wire)
    reply = await _recorded_reply(provider, wire, [{**_CHECKPOINT, "content": content}, _TEXT])
    replay = provider.native_compaction_settings(threshold=60000)
    assert replay is not None

    projected = native_replay_messages_for_route(
        [_user("Original facts."), reply],
        route=replay.route,
        manual_thinking=False,
    )

    assert projected[0].content == "Original facts."


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("no_op", [False, True])
async def test_an_empty_summary_keeps_the_tool_turn_thinking(tmp_path: Path, *, native: bool, no_op: bool) -> None:
    """A no-op summary leaves the thinking prefix unchanged, so the tool turn keeps its signed thinking."""
    thinking = {"type": "thinking", "thinking": "Need a lookup.", "signature": "unchanged-prefix"}
    tool_use = {"type": "tool_use", "id": "toolu_lookup", "name": "lookup", "input": {}}
    wire = Wire(json_reply([_TEXT]))
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"thinking": {"type": "adaptive"}})
    blocks = [*([{**_CHECKPOINT, "content": None}] if no_op else []), thinking, tool_use]
    reply = await _recorded_reply(provider, wire, blocks, stop_reason="tool_use")
    messages = [_user("Look up the status."), reply, Message(role="tool", tool_call_id="toolu_lookup", content="Found")]
    wire.replies[:] = [json_reply([_TEXT])]

    replay = provider.native_compaction_settings(threshold=60000) if native else None
    body = await _sent(provider, wire, messages, native_compaction=replay)

    assert body["messages"][1]["content"] == [thinking, tool_use]


@pytest.mark.asyncio
async def test_checkpoint_replay_drops_the_unsigned_thinking_after_the_checkpoint(tmp_path: Path) -> None:
    """Checkpoint items replace the stored blocks on replay, so they must drop unsigned thinking too."""
    signed = {"type": "thinking", "thinking": "", "signature": "complete"}
    unsigned = {"type": "thinking", "thinking": "", "signature": ""}
    tool_use = {"type": "tool_use", "id": "toolu_lookup", "name": "lookup", "input": {}}
    wire = Wire(json_reply([_TEXT]))
    provider = route_provider("anthropic", tmp_path, wire, extra_kwargs={"thinking": {"type": "adaptive"}})
    reply = await _recorded_reply(provider, wire, [_CHECKPOINT, signed, tool_use, unsigned], stop_reason="tool_use")
    messages = [_user("Look it up."), reply, Message(role="tool", tool_call_id="toolu_lookup", content="Found it")]
    wire.replies[:] = [json_reply([_TEXT])]

    body = await _sent(provider, wire, messages, native_compaction=provider.native_compaction_settings(threshold=60000))

    blocks = body["messages"][0]["content"]
    assert unsigned not in blocks
    assert signed in blocks
    assert tool_use in blocks


@pytest.mark.asyncio
async def test_native_reactivation_discards_only_mismatched_thinking(tmp_path: Path) -> None:
    """Restoring an old checkpoint drops thinking signed against the canonical prefix and keeps new valid reasoning."""
    native_thinking = {"type": "thinking", "thinking": "", "signature": "native-prefix"}
    canonical_thinking = {"type": "thinking", "thinking": "", "signature": "canonical-prefix"}
    resumed_thinking = {"type": "thinking", "thinking": "", "signature": "resumed-native-prefix"}
    wire = Wire(
        json_reply([_CHECKPOINT, native_thinking, _TEXT], usage=_COMPACTION_USAGE),
        json_reply([canonical_thinking, _TEXT]),
        json_reply([resumed_thinking, _TEXT]),
        json_reply([_TEXT]),
    )
    provider = route_provider("anthropic", tmp_path, wire, id="claude-fable-5-1")
    native = provider.native_compaction_settings(threshold=60000)
    messages = [_user("Original facts.")]
    anchor = await claude_turn(provider, ModelRequest(messages=messages, native_compaction=native, stream=False))
    messages = [*messages, _assistant(anchor), _user("Continue.")]
    canonical = await claude_turn(provider, ModelRequest(messages=messages, stream=False))
    messages = [*messages, _assistant(canonical), _user("Continue again.")]

    resumed = await claude_turn(provider, ModelRequest(messages=messages, native_compaction=native, stream=False))
    native_body = wire.body()
    messages = [*messages, _assistant(resumed), _user("Finish.")]
    await claude_turn(provider, ModelRequest(messages=messages, native_compaction=native, stream=False))

    assert native_thinking in native_body["messages"][0]["content"]
    assert canonical_thinking not in [block for message in native_body["messages"] for block in message["content"]]
    assert resumed_thinking in [block for message in wire.body()["messages"] for block in message["content"]]
    assert canonical_thinking in canonical.message.provider_data["content_blocks"]


@pytest.mark.asyncio
async def test_switching_the_claude_model_restores_the_canonical_history(tmp_path: Path) -> None:
    """A checkpoint belongs to its model's route, so another model replays the original conversation."""
    wire = Wire(json_reply([_TEXT]))
    sonnet = route_provider("anthropic", tmp_path, wire)
    reply = await _recorded_reply(sonnet, wire, [_CHECKPOINT, _TEXT])
    opus = route_provider("anthropic", tmp_path, wire, id="claude-opus-5-5")
    wire.replies[:] = [json_reply([_TEXT])]

    body = await _sent(
        opus,
        wire,
        [_user("Original facts."), reply, _user("Continue.")],
        native_compaction=opus.native_compaction_settings(threshold=60000),
    )

    assert body["messages"][0]["content"][0]["text"] == "Original facts."
    assert "compaction" not in json.dumps(body["messages"])
    assert reply.provider_data["content_blocks"][0]["type"] == "compaction"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["anthropic", "vertexai_claude"])
@pytest.mark.parametrize("native", [False, True])
async def test_canonical_fallback_removes_the_thinking_bound_to_the_checkpoint(
    tmp_path: Path,
    route: str,
    *,
    native: bool,
) -> None:
    """Discarding a checkpoint must not send its signed thinking against the restored prefix."""
    thinking = {"type": "thinking", "thinking": "Internal reasoning.", "signature": "checkpoint-bound"}
    redacted = {"type": "redacted_thinking", "data": "opaque-thinking"}
    wire = Wire(json_reply([_TEXT]))
    provider = route_provider(route, tmp_path, wire, id="claude-fable-5-1")
    anchor = await _recorded_reply(provider, wire, [_CHECKPOINT, thinking, _TEXT])
    messages = [
        _user("Original facts."),
        anchor,
        _user("Continue."),
        Message(
            role="assistant",
            content="Ready",
            provider_data={"content_blocks": [redacted, _TEXT]},
            redacted_reasoning_content="opaque-thinking",
        ),
        _user("Continue again."),
        Message(
            role="assistant",
            content="Later reply.",
            reasoning_content="Later reasoning.",
            redacted_reasoning_content="later-opaque",
            provider_data={"signature": "later-bound"},
        ),
        _user("Finish."),
    ]
    original = [message.to_dict() for message in messages]
    wire.replies[:] = [json_reply([_TEXT])]

    replay = provider.native_compaction_settings(threshold=60000) if native else None
    body = await _sent(provider, wire, messages, native_compaction=replay)

    assert [message.to_dict() for message in messages] == original
    blocks = [block for message in body["messages"] for block in message["content"]]
    if native:
        assert {**_CHECKPOINT} in blocks
        assert thinking in blocks
        assert redacted in blocks
    else:
        assert all(block["type"] not in {"compaction", "thinking", "redacted_thinking"} for block in blocks)
        assert {"type": "text", "text": "Original facts."} in [
            {key: value for key, value in block.items() if key != "cache_control"} for block in blocks
        ]
        assert {"type": "text", "text": "Later reply."} in blocks


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["anthropic", "vertexai_claude"])
@pytest.mark.parametrize("pause", [False, True])
async def test_a_raw_context_management_override_owns_the_compaction_policy(
    tmp_path: Path,
    route: str,
    *,
    pause: bool,
) -> None:
    """An SDK body override turns MindRoom's own trigger off and cannot bypass the pause validation."""
    policy = {"edits": [{"type": "compact_20260112", "pause_after_compaction": pause}]}
    wire = Wire(json_reply([_TEXT]))
    provider = route_provider(
        route,
        tmp_path,
        wire,
        extra_kwargs={
            "betas": ["compact-2026-01-12"],
            "request_params": {"extra_body": {"context_management": policy}},
        },
    )

    assert provider.capabilities.native_compaction is False
    if pause:
        with pytest.raises(ValueError, match="pause_after_compaction"):
            await claude_turn(provider, ModelRequest(messages=[_user("Continue.")]))
        assert wire.requests == []
    else:
        body = await _sent(provider, wire, [_user("Continue.")])
        assert body["context_management"] == policy


@pytest.mark.parametrize("region", ["global", "us", "eu", "us-central1"])
def test_a_vertex_checkpoint_route_is_stable_for_its_endpoint(tmp_path: Path, region: str) -> None:
    """Two builds of the same Vertex model and region label their checkpoints alike, so a rebuild still replays them."""
    wire = Wire(json_reply([_TEXT]))
    routes = [
        route_provider("vertexai_claude", tmp_path, wire, extra_kwargs={"region": region}).native_compaction_settings(
            threshold=60000,
        )
        for _ in range(2)
    ]
    assert routes[0] is not None
    assert routes[0] == routes[1]


# --- Caller-authored compaction policies ----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["anthropic", "vertexai_claude"])
@pytest.mark.parametrize("raw", [False, True])
async def test_an_authored_checkpoint_replays_after_a_rebuild_without_a_foreign_one(
    tmp_path: Path,
    route: str,
    *,
    raw: bool,
) -> None:
    """A model with its own compaction policy replays its checkpoint and never another model's."""
    wire = Wire(json_reply([_TEXT]))
    foreign = route_provider(route, tmp_path, wire, id="claude-opus-5-5")
    foreign_block = {"type": "compaction", "content": "Foreign model summary"}
    old = await _recorded_reply(foreign, wire, [foreign_block, _TEXT])
    policy_kwargs: dict[str, Any] = (
        {"request_params": {"extra_body": {"context_management": _POLICY}}} if raw else {"context_management": _POLICY}
    )
    authored = {"betas": ["compact-2026-01-12"], **policy_kwargs}
    model = route_provider(route, tmp_path, wire, extra_kwargs=authored)
    replay = model.native_compaction_settings(threshold=None, allow_authored=True)
    assert replay is not None
    assert replay.threshold is None
    messages = [_user("Original facts"), old, _user("Continue")]
    wire.replies[:] = [json_reply([_CHECKPOINT, _TEXT], usage=_COMPACTION_USAGE)]
    first = await claude_turn(model, ModelRequest(messages=messages, native_compaction=replay, stream=False))
    first_body = wire.body()
    rebuilt = route_provider(route, tmp_path, wire, extra_kwargs=authored)
    rebuilt_replay = rebuilt.native_compaction_settings(threshold=None, allow_authored=True)
    wire.replies[:] = [json_reply([_TEXT])]

    second = await _sent(
        rebuilt,
        wire,
        [*messages, _assistant(first), _user("Again")],
        native_compaction=rebuilt_replay,
    )

    assert first_body["context_management"] == _POLICY
    assert "Foreign model summary" not in json.dumps(first_body["messages"])
    assert second["context_management"] == _POLICY
    blocks = [block for message in second["messages"] for block in message["content"]]
    assert _CHECKPOINT in blocks
    assert foreign_block not in blocks


@pytest.mark.parametrize("change", ["none", "model", "summary", "missing_threshold", "openai"])
def test_an_authored_approval_restore_requires_a_compatible_saved_policy(tmp_path: Path, change: str) -> None:
    """A saved authored policy survives a pause without enabling automatic or foreign replay."""
    wire = Wire(json_reply([_TEXT]))
    authored = {"context_management": _POLICY}
    paused = route_provider("anthropic", tmp_path, wire, extra_kwargs=authored)
    saved = paused.native_compaction_settings(threshold=None, allow_authored=True)
    assert saved is not None
    checkpoint = Message(role="assistant", content="Ready", provider_data={"content_blocks": [_CHECKPOINT, _TEXT]})
    record_native_checkpoint(checkpoint, [_CHECKPOINT, _TEXT], saved)
    latest = Message(role="assistant", content="Waiting for approval")
    record_native_checkpoint(latest, [], saved)
    if change == "missing_threshold":
        assert latest.provider_data is not None
        latest.provider_data["mindroom_native_compaction"].pop("threshold")
    run = _completed_run("paused", messages=[checkpoint, _user("Continue"), latest])
    session = _session("session", runs=[run])
    if change == "summary":
        session.summary = ScopeSummary(text="New portable summary", updated_at=datetime.now(UTC))
    rebuilt = (
        responses_adapter(tmp_path)
        if change == "openai"
        else route_provider(
            "anthropic",
            tmp_path,
            wire,
            id="claude-opus-5-5" if change == "model" else "claude-sonnet-5-5",
            extra_kwargs=authored,
        )
    )

    restored = restore_native_history(rebuilt, persisted_run=run, session=session)

    assert restored is not None
    if change == "none":
        assert restored.native_compaction is not None
        assert restored.native_compaction.threshold is None
    else:
        assert restored.native_compaction is None


# --- Legacy manual thinking -----------------------------------------------------------------------------------

_MANUAL = {"type": "enabled", "budget_tokens": 1024}


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Using Claude with claude-opus-4-6.*:UserWarning")
@pytest.mark.parametrize("model_id", ["claude-sonnet-4-6", "claude-opus-4-6"])
@pytest.mark.parametrize("route", ["anthropic", "vertexai_claude"])
@pytest.mark.parametrize("source", ["typed", "top", "raw"])
@pytest.mark.parametrize("stream", [False, True])
async def test_manual_thinking_survives_a_checkpoint_fallback(
    tmp_path: Path,
    model_id: str,
    route: str,
    source: str,
    *,
    stream: bool,
) -> None:
    """Manual thinking must stay unchanged inside a tool turn, so a fallback to canonical history keeps it."""
    thinking = {"type": "thinking", "thinking": "Need a lookup.", "signature": "original-signature"}
    tool_use = {"type": "tool_use", "id": "toolu_lookup", "name": "lookup", "input": {}}
    settings: dict[str, Any] = {
        "max_tokens": 4096,
        "thinking": _MANUAL if source == "typed" else {"type": "adaptive"},
    }
    if source == "raw":
        settings["request_params"] = {"extra_body": {"thinking": _MANUAL}}
    elif source == "top":
        settings["request_params"] = {"thinking": _MANUAL}
    wire = Wire(json_reply([_TEXT]))
    provider = route_provider(route, tmp_path, wire, id=model_id, extra_kwargs=settings)
    anchor = await _recorded_reply(provider, wire, [_CHECKPOINT, _TEXT])
    messages = [_user("Original launch facts."), anchor, _user("Look up the status.")]
    wire.replies[:] = [json_reply([thinking, tool_use], stop_reason="tool_use")]
    native = provider.native_compaction_settings(threshold=60000)
    lookup = await claude_turn(provider, ModelRequest(messages=messages, native_compaction=native, stream=False))
    messages = [
        *messages,
        _assistant(lookup),
        Message(role="tool", tool_call_id="toolu_lookup", content="Found it. " * 5000),
    ]
    original = [message.to_dict() for message in messages]
    wire.replies[:] = [sse_reply([_TEXT]) if stream else json_reply([_TEXT])]

    # Mid-turn compaction turns native replay off when a checkpoint cannot keep the request in its window.
    body = await _sent(provider, wire, messages, stream=stream)

    effective = body.get("thinking", body.get("extra_body", {}).get("thinking"))
    assert effective == _MANUAL
    latest = next(message for message in reversed(body["messages"]) if message["role"] == "assistant")
    assert latest["content"] == [thinking, tool_use]
    assert "compaction" not in {block["type"] for message in body["messages"] for block in message["content"]}
    assert [message.to_dict() for message in messages] == original


@pytest.mark.parametrize(
    ("request_params", "expected_thinking", "keep_thinking"),
    [
        ({"thinking": {"type": "adaptive"}}, {"type": "adaptive"}, False),
        ({"extra_body": {"thinking": None}}, None, False),
        ({"extra_body": {"thinking": {"type": "adaptive"}}}, {"type": "adaptive"}, False),
        (
            {"thinking": {"type": "adaptive"}, "extra_body": {"thinking": _MANUAL}},
            _MANUAL,
            True,
        ),
    ],
)
@pytest.mark.asyncio
async def test_thinking_overrides_align_the_replay_with_the_effective_mode(
    tmp_path: Path,
    request_params: dict[str, Any],
    expected_thinking: dict[str, Any] | None,
    *,
    keep_thinking: bool,
) -> None:
    """The raw request overrides decide the thinking mode, and with it whether stale thinking may replay."""
    redacted = {"type": "redacted_thinking", "data": "unchanged-redacted-thinking"}
    wire = Wire(json_reply([_TEXT]))
    provider = route_provider(
        "vertexai_claude",
        tmp_path,
        wire,
        id="claude-sonnet-4-6",
        extra_kwargs={"thinking": _MANUAL, "request_params": request_params},
    )
    reply = await _recorded_reply(provider, wire, [_CHECKPOINT, redacted, _TEXT])

    projected = native_replay_messages_for_route(
        [_user("Original facts."), reply, _user("Continue.")],
        route=None,
        manual_thinking=(provider.settings.effective_thinking() or {}).get("type") == "enabled",
    )

    assert provider.settings.effective_thinking() == expected_thinking
    assert (redacted in (projected[1].provider_data or {})["content_blocks"]) is keep_thinking
    assert (reply.provider_data or {})["content_blocks"][1] == redacted
