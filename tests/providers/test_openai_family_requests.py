"""The request bodies of the OpenAI Responses and Codex routes: replay of stored turns, cache prefixes, Codex fields."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest
from openai.types.chat import ChatCompletion
from openai.types.responses import Response, ResponseUsage
from openai.types.responses.response_input_item_param import ResponseInputItemParam
from pydantic import BaseModel, ConfigDict, TypeAdapter

from mindroom.attachments.media import attachment_records_to_media
from mindroom.attachments.storage import AttachmentRecord
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.media import File
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides
from mindroom.providers.openai.build import ProviderRuntime, build_provider
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.openai.chat_wire import chat_request_kwargs, parse_chat_completion
from mindroom.providers.openai.legacy_replay import repair_legacy_responses_span
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.openai.responses_wire import (
    parse_response,
    responses_input,
    responses_replay,
    responses_request_kwargs,
)
from mindroom.providers.openai.schema import strict_response_schema
from mindroom.providers.openai.settings import ChatSettings
from mindroom.providers.openai.tool_search import openai_native_tool_search_supported
from mindroom.system_prompt import render_session_context
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from pathlib import Path

_CHECKPOINT = {"type": "compaction", "id": "cmp_latest", "encrypted_content": "opaque-checkpoint"}
_ANSWER = {
    "type": "message",
    "id": "msg_answer",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Ready", "annotations": []}],
}
_REASONING = {"type": "reasoning", "id": "rs_first", "summary": [], "encrypted_content": "opaque-reasoning"}
_CALL = {
    "type": "function_call",
    "id": "fc_lookup",
    "call_id": "call_lookup",
    "name": "lookup",
    "arguments": "{}",
    "status": "completed",
}
_SEARCH_CALL = {
    "id": "ts_1",
    "type": "tool_search_call",
    "call_id": "tsc_1",
    "arguments": {"queries": ["weather"]},
    "execution": "server",
    "status": "completed",
}
_SEARCH_OUTPUT = {
    "id": "tso_1",
    "type": "tool_search_output",
    "call_id": "tsc_1",
    "execution": "server",
    "status": "completed",
    "tools": [{"type": "function", "name": "get_weather", "parameters": {"type": "object"}}],
}


def _response(output: list[dict[str, Any]], *, status: str = "completed") -> Response:
    return Response.model_validate(
        {
            "id": "resp_done",
            "object": "response",
            "created_at": 1,
            "model": "gpt-6-astra",
            "status": status,
            "output": output,
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": {
                "input_tokens": 6000,
                "output_tokens": 10,
                "total_tokens": 6010,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
            "error": None,
            "incomplete_details": None,
        },
    )


def _parsed(adapter: ResponsesAdapter, output: list[dict[str, Any]], **message: Any) -> Message:  # noqa: ANN401
    """Return the stored assistant message of one response, parsed as the route parses it without native replay."""
    parts = parse_response(
        _response(output),
        wire=adapter.wire,
        store=adapter.wire.store,
        portable_replay=False,
        native_compaction=adapter.native_compaction_settings(threshold=1024),
        text_as_reasoning=False,
    )
    fields = {"content": parts.content, "tool_calls": parts.tool_calls or None, "provider_data": parts.provider_data}
    return Message(role="assistant", **{**fields, **message})


def _input(messages: list[Message], *, store: bool | None = False, portable: bool = False, **kwargs: Any) -> list[Any]:  # noqa: ANN401
    return responses_input(
        responses_replay(
            messages,
            store=store,
            portable_replay=portable,
            native_compaction=kwargs.get("native_compaction"),
        ),
        cache_system_prompt=kwargs.get("cache_system_prompt", False),
        explicit_breakpoint=kwargs.get("explicit_breakpoint", False),
    )


def _call(name: str, call_id: str | None = None) -> dict[str, Any]:
    return {
        "id": f"fc_{name}",
        "call_id": call_id or f"call_{name}",
        "type": "function",
        "function": {"name": "lookup", "arguments": json.dumps({"service": name})},
    }


# --- Ordered output replay ------------------------------------------------------------------------------------


def test_reasoning_and_hosted_search_replay_ahead_of_their_calls(tmp_path: Path) -> None:
    """Explicit replay sends every reasoning item and search before the call it produced, then the results."""
    adapter = responses_adapter(tmp_path, store=False)
    second_reasoning = {**_REASONING, "id": "rs_second"}
    second_call = {**_CALL, "id": "fc_second", "call_id": "call_second"}
    output = [_REASONING, _SEARCH_CALL, _CALL, second_reasoning, second_call]
    assistant = _parsed(adapter, output)
    messages = [
        Message(role="user", content="Look up the status."),
        assistant,
        Message(role="tool", tool_call_id="fc_lookup", content="Found it"),
        Message(role="tool", tool_call_id="fc_second", content="Found it"),
    ]

    replay = _input(messages)

    assert replay[1:6] == output
    assert replay[6:] == [
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
        {"type": "function_call_output", "call_id": "call_second", "output": "Found it"},
    ]


@pytest.mark.parametrize("retain_call", [False, True])
def test_ordered_replay_never_resurrects_a_filtered_call(tmp_path: Path, *, retain_call: bool) -> None:
    """Calls the history policy removed from canonical history stay out of the ordered replay."""
    adapter = responses_adapter(tmp_path, store=False)
    removed = {**_CALL, "id": "fc_removed", "call_id": "call_removed"}
    parsed = _parsed(adapter, [_REASONING, removed, _CALL])
    assert parsed.tool_calls is not None
    messages = [
        Message(role="user", content="Original request."),
        Message(
            role="assistant",
            tool_calls=parsed.tool_calls[1:] if retain_call else None,
            provider_data=parsed.provider_data,
        ),
    ]
    if retain_call:
        messages.append(Message(role="tool", tool_call_id="fc_lookup", content="Found it"))

    replay = _input(messages)

    expected = (
        [_REASONING, _CALL, {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"}]
        if retain_call
        else [_REASONING]
    )
    assert replay[1:] == expected


def test_a_reasoning_tail_after_a_checkpoint_replays_once(tmp_path: Path) -> None:
    """A later ordinary response keeps its reasoning without repeating the checkpoint's tail."""
    adapter = responses_adapter(tmp_path, store=False)
    native = adapter.native_compaction_settings(threshold=1024)
    checkpoint = _parsed(adapter, [_CHECKPOINT, _ANSWER])
    ordinary = _parsed(adapter, [_REASONING, _ANSWER])
    messages = [
        Message(role="user", content="Original request."),
        checkpoint,
        Message(role="user", content="Continue."),
        ordinary,
    ]

    assert _input(messages, native_compaction=native) == [
        _CHECKPOINT,
        _ANSWER,
        {"role": "user", "content": "Continue."},
        _REASONING,
        _ANSWER,
    ]


def test_ordered_output_never_restores_rewritten_text(tmp_path: Path) -> None:
    """A request-local edit of the canonical text wins over the captured provider output."""
    adapter = responses_adapter(tmp_path, store=False)
    parsed = _parsed(adapter, [_REASONING, _ANSWER], content="Rewritten answer.")

    replay = _input([parsed])

    assert replay[0] == {"role": "assistant", "content": "Rewritten answer."}
    assert "Ready" not in json.dumps(replay)


def test_a_rewritten_tool_turn_replays_its_calls_from_canonical_history(tmp_path: Path) -> None:
    """A rewritten answer keeps its calls without reusing the provider span it no longer matches."""
    adapter = responses_adapter(tmp_path, store=False)
    parsed = _parsed(adapter, [_REASONING, _ANSWER, _CALL], content="Rewritten answer.")
    messages = [parsed, Message(role="tool", tool_call_id="fc_lookup", content="Found it")]
    original = [message.to_dict() for message in messages]

    assert _input(messages) == [
        {"role": "assistant", "content": "Rewritten answer."},
        {"type": "function_call", "call_id": "call_lookup", "name": "lookup", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
    ]
    assert [message.to_dict() for message in messages] == original


def test_stored_output_missing_one_of_the_turn_s_calls_is_not_reused(tmp_path: Path) -> None:
    """Captured output that cannot account for every call cannot order them; the calls replay from canonical history."""
    adapter = responses_adapter(tmp_path, store=False)
    parsed = _parsed(adapter, [_REASONING, _CALL])
    assert parsed.tool_calls is not None
    messages = [
        Message(role="user", content="Check both services."),
        replace(parsed, tool_calls=[*parsed.tool_calls, _call("second")]),
        Message(role="tool", tool_call_id="fc_lookup", content="Found it"),
        Message(role="tool", tool_call_id="fc_second", content="Second ready"),
    ]

    replay = _input(messages)

    assert replay[1:] == [
        {"type": "function_call", "call_id": "call_lookup", "name": "lookup", "arguments": "{}", "status": "completed"},
        {
            "type": "function_call",
            "call_id": "call_second",
            "name": "lookup",
            "arguments": json.dumps({"service": "second"}),
            "status": "completed",
        },
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
        {"type": "function_call_output", "call_id": "call_second", "output": "Second ready"},
    ]


@pytest.mark.parametrize("reasoning", [None, _REASONING, {"type": "reasoning", "id": "rs_last", "summary": []}])
@pytest.mark.parametrize("retained", ["none", "second", "both"])
@pytest.mark.parametrize("portable", [False, True])
def test_legacy_responses_replay_rebuilds_calls_without_reasoning(
    reasoning: dict[str, Any] | None,
    retained: str,
    *,
    portable: bool,
) -> None:
    """A legacy reasoning tail cannot authorize replaying any original provider item id."""
    data: dict[str, Any] = {"response_id": "resp_legacy"}
    if reasoning is not None:
        data["reasoning_output"] = reasoning
    names = {"none": [], "second": ["second"], "both": ["first", "second"]}[retained]
    messages = [
        Message(role="user", content="Check both services."),
        Message(
            role="assistant",
            content="Current answer.",
            tool_calls=[_call(name) for name in names] or None,
            provider_data=data,
        ),
        *[Message(role="tool", tool_call_id=f"fc_{name}", content=f"{name.title()} ready") for name in names],
        Message(role="user", content="Continue."),
    ]
    original = [message.to_dict() for message in messages]

    replay = _input(messages, store=portable, portable=portable)

    assert replay[:2] == [
        {"role": "user", "content": "Check both services."},
        {"role": "assistant", "content": "Current answer."},
    ]
    assert replay[2:-1] == [
        *[
            {
                "type": "function_call",
                "call_id": f"call_{name}",
                "name": "lookup",
                "arguments": json.dumps({"service": name}),
                "status": "completed",
            }
            for name in names
        ],
        *[
            {"type": "function_call_output", "call_id": f"call_{name}", "output": f"{name.title()} ready"}
            for name in names
        ],
    ]
    assert replay[-1] == {"role": "user", "content": "Continue."}
    assert [message.to_dict() for message in messages] == original


def test_the_legacy_span_repair_keeps_only_the_calls_without_their_item_ids() -> None:
    """A tool turn keeps its function calls minus their provider item ids; a text turn keeps its message item."""
    message = Message(role="assistant", tool_calls=[_call("first")])
    span = [
        {"type": "function_call", "id": "fc_first", "call_id": "call_first", "name": "lookup", "arguments": "{}"},
        {"type": "reasoning", "id": "rs_tail"},
    ]

    assert repair_legacy_responses_span(message, span) == [
        {"type": "function_call", "call_id": "call_first", "name": "lookup", "arguments": "{}"},
    ]
    assert repair_legacy_responses_span(Message(role="assistant", content="Hi"), span) == span[:1]


@pytest.mark.parametrize("retain_call", [False, True])
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (["first", "second"], [{"type": "input_text", "text": "first"}, {"type": "input_text", "text": "second"}]),
        ([{"type": "text", "text": "answer"}], [{"type": "input_text", "text": "answer"}]),
        ([{"type": "output_text", "text": "answer", "annotations": []}], [{"type": "input_text", "text": "answer"}]),
        (
            [
                {"type": "input_text", "text": "answer"},
                {"type": "input_image", "file_id": "file_image", "detail": "auto"},
            ],
            [
                {"type": "input_text", "text": "answer"},
                {"type": "input_image", "file_id": "file_image", "detail": "auto"},
            ],
        ),
    ],
)
def test_reconstructed_assistant_content_is_valid_responses_input(
    content: list[Any],
    expected: list[dict[str, Any]],
    *,
    retain_call: bool,
) -> None:
    """Every canonical content shape becomes valid input without losing media or text."""
    messages = [
        Message(
            role="assistant",
            content=content,
            tool_calls=[_call("lookup", "call_lookup")] if retain_call else None,
            provider_data={"reasoning_output": _REASONING},
        ),
    ]
    if retain_call:
        messages.append(Message(role="tool", tool_call_id="fc_lookup", content="Found it"))
    original = [message.to_dict() for message in messages]

    replay = _input(messages)

    assert TypeAdapter(list[ResponseInputItemParam]).validate_python(replay)[0] == {
        "role": "assistant",
        "content": expected,
    }
    assert replay[0] == {"role": "assistant", "content": expected}
    assert len(replay) == (3 if retain_call else 1)
    assert [message.to_dict() for message in messages] == original


# --- Hosted tool-search items ---------------------------------------------------------------------------------


def test_search_items_replay_ahead_of_the_discovered_function_call() -> None:
    """Captured search items go back immediately ahead of the message's replayed function calls."""
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "fc_1",
                "call_id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": "{}"},
            },
        ],
        provider_data={"tool_search_items": [dict(_SEARCH_CALL), dict(_SEARCH_OUTPUT)]},
    )
    messages = [
        Message(role="user", content="weather?"),
        assistant,
        Message(role="tool", content="sunny", tool_call_id="fc_1"),
    ]

    assert _input(messages) == [
        {"role": "user", "content": "weather?"},
        _SEARCH_CALL,
        _SEARCH_OUTPUT,
        {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
    ]


def test_search_replay_anchors_on_its_own_turn_not_an_identical_earlier_one() -> None:
    """An earlier assistant turn with the same text cannot claim a later turn's search items."""
    later = Message(role="assistant", content="same text", provider_data={"tool_search_items": [dict(_SEARCH_CALL)]})
    messages = [
        Message(role="user", content="q1"),
        Message(role="assistant", content="same text"),
        Message(role="user", content="q2"),
        later,
    ]

    assert _input(messages) == [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "same text"},
        {"role": "user", "content": "q2"},
        _SEARCH_CALL,
        {"role": "assistant", "content": "same text"},
    ]


def test_claude_server_tool_blocks_are_not_replayed_to_responses() -> None:
    """Anthropic-native server tool blocks stay out when a thread switches to a Responses model."""
    assistant = Message(
        role="assistant",
        content="Claude searched here.",
        provider_data={"server_tool_blocks": [{"type": "server_tool_use", "id": "srv_1"}]},
    )

    assert _input([assistant]) == [{"role": "assistant", "content": "Claude searched here."}]


# --- Storage, continuation, and recorded policy ---------------------------------------------------------------


def _request(adapter: ResponsesAdapter, messages: list[Message], **request: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return one request's body fields, SDK keywords and ``extra_body`` merged as the SDK sends them."""
    kwargs = responses_request_kwargs(
        model_id=adapter.identity.id,
        request=ModelRequest(messages=messages, **request),
        settings=adapter.settings,
        wire=adapter.wire,
    )
    return {**kwargs.pop("extra_body", {}), **kwargs}


def test_a_stored_tool_turn_continues_whatever_the_model_name(tmp_path: Path) -> None:
    """Continuation does not depend on the model name, so aliases and non-reasoning models continue too."""
    adapter = responses_adapter(tmp_path, model_id="custom-alias", store=True)
    assistant = Message(
        role="assistant",
        tool_calls=[_call("status", "call_1")],
        provider_data={"response_id": "resp_1"},
    )
    messages = [assistant, Message(role="tool", content="ready", tool_call_id="fc_status")]

    request = _request(adapter, messages)

    assert (request["store"], request["previous_response_id"]) == (True, "resp_1")
    assert request["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": "ready"}]


@pytest.mark.parametrize("storage", [{"store": False}, {"request_params": {"store": False}}])
def test_disabled_storage_replays_explicitly_with_encrypted_reasoning(tmp_path: Path, storage: dict[str, Any]) -> None:
    """A stateless request never continues a stored response and asks for the reasoning it must replay."""
    adapter = responses_adapter(tmp_path, model_id="reasoning-alias", reasoning_effort="high", **storage)
    messages = [
        Message(role="assistant", content="Earlier reply", provider_data={"response_id": "resp_1"}),
        Message(role="user", content="Follow up"),
    ]

    request = _request(adapter, messages)

    assert request["store"] is False
    assert "previous_response_id" not in request
    assert "reasoning.encrypted_content" in request["include"]
    assert request["input"] == [
        {"role": "assistant", "content": "Earlier reply"},
        {"role": "user", "content": "Follow up"},
    ]


@pytest.mark.parametrize(
    "authored",
    [
        {"request_params": {"previous_response_id": "resp_stale"}},
        {"extra_body": {"previous_response_id": "resp_stale", "custom_option": True}},
        {"request_params": {"extra_body": {"previous_response_id": "resp_stale", "custom_option": True}}},
    ],
)
def test_portable_replay_drops_an_authored_continuation(tmp_path: Path, authored: dict[str, Any]) -> None:
    """An authored override cannot reintroduce the context history fitting left out."""
    adapter = responses_adapter(tmp_path, **authored)

    request = _request(adapter, [Message(role="user", content="Continue")], portable_replay=True)

    assert "previous_response_id" not in request
    assert "previous_response_id" not in json.dumps(request)


@pytest.mark.parametrize("key", ["context_management", "store", "previous_response_id", "background"])
def test_an_authored_replay_policy_turns_native_compaction_off(tmp_path: Path, key: str) -> None:
    """A raw SDK override must not contradict automatic compaction's replay policy."""
    adapter = responses_adapter(tmp_path, request_params={"extra_body": {key: None}})

    assert adapter.native_compaction_settings(threshold=1024) is None


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("status", ["completed", "incomplete"])
def test_only_a_completed_response_records_its_replay_policy(tmp_path: Path, *, portable: bool, status: str) -> None:
    """An approval rebuild trusts the replay policy only from a response that completed."""
    adapter = responses_adapter(tmp_path, store=True)
    parts = parse_response(
        _response([_ANSWER], status=status),
        wire=adapter.wire,
        store=True,
        portable_replay=portable,
        native_compaction=None,
        text_as_reasoning=False,
    )

    if status == "completed":
        assert parts.provider_data["mindroom_portable_replay"] is portable
    else:
        assert "mindroom_portable_replay" not in parts.provider_data


# --- The shared instruction prefix ----------------------------------------------------------------------------


def _messages(day: str) -> list[Message]:
    return [
        Message(role="system", content="Shared agent instructions.\n" + render_session_context(f"Current day: {day}.")),
        Message(role="user", content="Write an introduction."),
    ]


@pytest.mark.parametrize("explicit", [False, True])
def test_the_shared_instructions_are_independent_of_the_session_context(*, explicit: bool) -> None:
    """A changed date leaves the first developer message, the cacheable prefix, byte-identical."""
    original = _messages("Monday")
    snapshot = [message.to_dict() for message in original]

    first = _input(original, cache_system_prompt=True, explicit_breakpoint=explicit)
    second = _input(_messages("Tuesday"), cache_system_prompt=True, explicit_breakpoint=explicit)

    assert first[0] == second[0]
    assert first[0]["role"] == first[1]["role"] == "developer"
    assert first[0]["content"][0]["text"] == "Shared agent instructions.\n"
    assert "Current day: Monday." in first[1]["content"]
    assert first[0]["content"][0]["text"] + first[1]["content"] == original[0].content
    assert first[2] == {"role": "user", "content": "Write an introduction."}
    assert ("prompt_cache_breakpoint" in first[0]["content"][0]) is explicit
    assert [message.to_dict() for message in original] == snapshot


@pytest.mark.parametrize(
    ("model_id", "base_url", "supported"),
    [
        ("gpt-6-astra", None, True),
        ("gpt-5.6", None, True),
        ("gpt-5.6-2026-07-01", None, True),
        ("gpt-5.5", None, False),
        ("gpt-5", None, False),
        ("o3", None, False),
        ("gpt-6-astra", "https://proxy.example.test/v1", False),
    ],
)
def test_explicit_breakpoints_need_a_supported_model_on_the_public_api(
    tmp_path: Path,
    model_id: str,
    base_url: str | None,
    *,
    supported: bool,
) -> None:
    """Legacy models and OpenAI-compatible proxies never receive the explicit-cache field."""
    extra_kwargs = {"base_url": base_url} if base_url else {}

    assert responses_adapter(tmp_path, model_id=model_id, **extra_kwargs).wire.explicit_breakpoint is supported


def test_codex_sends_the_shared_prefix_without_an_explicit_breakpoint(tmp_path: Path) -> None:
    """The Codex endpoint caches the prefix implicitly."""
    adapter = _codex(tmp_path)

    first = _request(adapter, _messages("Monday"))["input"][0]

    assert first["content"][0]["text"] == "Shared agent instructions.\n"
    assert "prompt_cache_breakpoint" not in first["content"][0]


def test_a_disabled_cache_boundary_sends_the_instructions_unsplit() -> None:
    """``cache_system_prompt: false`` keeps the wire prompt as one developer message."""
    messages = _messages("Monday")

    assert _input(messages, cache_system_prompt=False) == [
        {"role": "developer", "content": messages[0].content},
        {"role": "user", "content": messages[1].content},
    ]


def test_only_the_initial_text_system_message_is_split() -> None:
    """Custom system content and a boundary inside a user message are sent as authored."""
    custom = [{"type": "input_text", "text": render_session_context("Custom content.")}]
    messages = [
        Message(role="system", content=custom),
        Message(role="user", content=render_session_context("User text.")),
    ]

    assert _input(messages, cache_system_prompt=True) == [
        {"role": "developer", "content": custom},
        {"role": "user", "content": messages[1].content},
    ]


def test_a_stored_continuation_does_not_resend_the_instructions() -> None:
    """Chaining to a stored response sends only what follows it."""
    messages = [
        *_messages("Monday"),
        Message(role="assistant", content="Introduction.", provider_data={"response_id": "resp_previous"}),
        Message(role="user", content="Continue."),
    ]

    assert _input(messages, store=True, cache_system_prompt=True) == [{"role": "user", "content": "Continue."}]


# --- Files ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "declared_mime", "expected_mime"),
    [
        ("report.pdf", None, "application/pdf"),
        ("report.docx", None, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("report.pptx", None, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("report.xlsx", None, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("notes.txt", "application/pdf", "application/pdf"),
        ("unknown.bin", None, "application/octet-stream"),
        ("archive.zip", "application/zip", "application/zip"),
        ("notes.txt.gz", None, "application/octet-stream"),
    ],
)
def test_an_attachment_s_type_comes_from_its_original_filename(
    tmp_path: Path,
    filename: str,
    declared_mime: str | None,
    expected_mime: str,
) -> None:
    """The opaque storage name never hides a file's original type from Responses."""
    stored_path = tmp_path / "stored.bin"
    payload = b"synthetic attachment"
    stored_path.write_bytes(payload)
    record = AttachmentRecord(
        attachment_id="att_document",
        local_path=stored_path,
        kind="file",
        filename=filename,
        mime_type=declared_mime,
    )
    _, _, files, _ = attachment_records_to_media([record])

    replay = _input([Message(role="user", content="Read the attachment.", files=files)])

    assert replay[0]["content"] == [
        {"type": "input_text", "text": "Read the attachment."},
        {
            "type": "input_file",
            "filename": filename,
            "file_data": f"data:{expected_mime};base64,{base64.b64encode(payload).decode()}",
        },
    ]


def test_an_attachment_without_a_filename_is_named_by_its_stored_file(tmp_path: Path) -> None:
    """An attachment recorded without a filename or type still reaches Responses, named and typed by its file."""
    stored_path = tmp_path / "att_document.bin"
    stored_path.write_bytes(b"synthetic attachment")
    _, _, files, _ = attachment_records_to_media(
        [AttachmentRecord(attachment_id="att_document", local_path=stored_path, kind="file")],
    )

    replay = _input([Message(role="user", content="Read the attachment.", files=files)])

    assert replay[0]["content"][1] == {
        "type": "input_file",
        "filename": "att_document.bin",
        "file_data": f"data:application/octet-stream;base64,{base64.b64encode(b'synthetic attachment').decode()}",
    }


def test_remote_file_references_stay_references() -> None:
    """Filename inference never turns a remote file into a local data block."""
    file = File(url="https://example.com/report.pdf", filename="notes.txt")

    replay = _input([Message(role="user", content="Read the attachment.", files=[file])])

    assert replay[0]["content"][1] == {"type": "input_file", "file_url": "https://example.com/report.pdf"}


# --- Structured output ----------------------------------------------------------------------------------------


class _Tally(BaseModel):
    model_config = ConfigDict(extra="forbid")

    counts: dict[str, int]
    note: str | None = None


def test_the_strict_schema_keeps_closed_objects_and_open_maps() -> None:
    """A model that already forbids extra keys stays closed, and a ``dict`` field stays an open, optional map."""
    assert strict_response_schema(_Tally) == {
        "additionalProperties": False,
        "properties": {
            "counts": {"additionalProperties": {"type": "integer"}, "title": "Counts", "type": "object"},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "Note"},
        },
        "required": ["note"],
        "title": "_Tally",
        "type": "object",
    }


# --- Usage ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("include_cache_write", [True, False], ids=["current", "older"])
@pytest.mark.parametrize("route", ["responses", "chat"])
def test_usage_keeps_every_input_counter_the_sdk_reports(
    tmp_path: Path,
    route: str,
    *,
    include_cache_write: bool,
) -> None:
    """Cached, cache-write, reasoning, and audio counters all reach the stored usage, which billing reads."""
    input_details: dict[str, int] = {"cached_tokens": 12_000}
    if include_cache_write:
        input_details["cache_write_tokens"] = 3_000
    if route == "chat":
        input_details["audio_tokens"] = 11
        adapter = build_provider(
            ModelConfig(provider="openai", id="gpt-6-astra", api="chat_completions", api_key="test-key"),
            ProviderRuntime(runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})),
        )
        assert isinstance(adapter, ChatAdapter)
        completion = ChatCompletion.model_validate(
            {
                "id": "chatcmpl_test",
                "object": "chat.completion",
                "created": 1,
                "model": "gpt-6-astra",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Ready"}},
                ],
                "usage": {
                    "prompt_tokens": 15_000,
                    "completion_tokens": 50,
                    "total_tokens": 15_050,
                    "prompt_tokens_details": input_details,
                    "completion_tokens_details": {"audio_tokens": 13, "reasoning_tokens": 7},
                },
            },
        )
        usage = parse_chat_completion(completion, adapter.wire, provider=adapter.identity.provider_name).usage
    else:
        response = _response([]).model_copy(
            update={
                "usage": ResponseUsage.model_validate(
                    {
                        "input_tokens": 15_000,
                        "output_tokens": 50,
                        "total_tokens": 15_050,
                        "input_tokens_details": {"cache_write_tokens": 0, **input_details},
                        "output_tokens_details": {"reasoning_tokens": 7},
                    },
                ),
            },
        )
        adapter = responses_adapter(tmp_path)
        usage = parse_response(
            response,
            wire=adapter.wire,
            store=None,
            portable_replay=False,
            native_compaction=None,
            text_as_reasoning=False,
        ).usage

    assert usage is not None
    assert (usage.tokens.input_tokens, usage.tokens.output_tokens, usage.tokens.total_tokens) == (15_000, 50, 15_050)
    assert usage.tokens.cache_read_tokens == 12_000
    assert usage.tokens.cache_write_tokens == (3_000 if include_cache_write else 0)
    assert usage.tokens.reasoning_tokens == 7
    audio = (11, 13, 24) if route == "chat" else (0, 0, 0)
    assert (usage.tokens.audio_input_tokens, usage.tokens.audio_output_tokens, usage.tokens.audio_total_tokens) == audio


@pytest.mark.parametrize("route", ["groq", "cerebras"])
def test_groq_and_cerebras_usage_keeps_cached_input_and_reasoning(tmp_path: Path, route: str) -> None:
    """Cached input and reasoning stay inside the prompt and completion totals these routes report."""
    adapter = build_provider(
        ModelConfig(provider=route, id="gpt-oss-120b", api_key="test-key"),
        ProviderRuntime(runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})),
    )
    assert isinstance(adapter, ChatAdapter)
    completion = ChatCompletion.model_validate(
        {
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "created": 1,
            "model": "gpt-oss-120b",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Ready"}}],
            "usage": {
                "prompt_tokens": 5000,
                "completion_tokens": 300,
                "total_tokens": 5300,
                "prompt_tokens_details": {"cached_tokens": 4608},
                "completion_tokens_details": {"reasoning_tokens": 120},
            },
        },
    )

    usage = parse_chat_completion(completion, adapter.wire, provider=adapter.identity.provider_name).usage

    assert usage is not None
    assert (usage.tokens.input_tokens, usage.tokens.output_tokens, usage.tokens.total_tokens) == (5000, 300, 5300)
    assert (usage.tokens.cache_read_tokens, usage.tokens.reasoning_tokens) == (4608, 120)


# A value for every Chat request setting that travels in the request body.
_CHAT_BODY_VALUES: dict[str, Any] = {
    "store": True,
    "reasoning_effort": "low",
    "verbosity": "low",
    "frequency_penalty": 0.5,
    "logit_bias": {"50256": -100},
    "logprobs": True,
    "top_logprobs": 2,
    "max_tokens": 10,
    "max_completion_tokens": 11,
    "presence_penalty": 0.25,
    "seed": 7,
    "stop": ["END"],
    "temperature": 0.3,
    "user": "user-1",
    "top_p": 0.9,
    "metadata": {"team": "a"},
    "service_tier": "auto",
    "repetition_penalty": 1.1,
    "top_k": 5,
}
# Request settings that are SDK options, a body to merge, or the request's own output format, not body fields.
_CHAT_NON_BODY_FIELDS = frozenset({"extra_headers", "extra_query", "extra_body", "response_format"})


@pytest.mark.parametrize(
    ("provider", "request_fields"),
    [
        ("openai", tuple(ChatSettings().openai_request_fields())),
        ("groq", tuple(ChatSettings().groq_request_fields())),
        ("cerebras", tuple(ChatSettings().cerebras_request_fields())),
    ],
)
def test_every_request_setting_a_chat_dialect_accepts_reaches_its_body(
    provider: str,
    request_fields: tuple[str, ...],
    tmp_path: Path,
) -> None:
    """A request setting the dialect's routes accept is sent in the body, so no accepted setting is dropped."""
    body_fields = [name for name in request_fields if name not in _CHAT_NON_BODY_FIELDS]
    authored = {name: _CHAT_BODY_VALUES[name] for name in body_fields}
    adapter = build_provider(
        ModelConfig(
            provider=provider,
            id="m",
            api="chat_completions" if provider == "openai" else None,
            api_key="test-key",
            extra_kwargs=authored,
        ),
        ProviderRuntime(runtime_paths=resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})),
    )
    assert isinstance(adapter, ChatAdapter)

    kwargs = chat_request_kwargs(
        model_id="m",
        messages=[Message(role="user", content="Hi")],
        tools=(),
        tool_choice=None,
        output_schema=None,
        structured_output=adapter.capabilities.structured_output,
        stream=True,
        settings=adapter.settings,
        wire=adapter.wire,
    )

    body = {**kwargs.pop("extra_body", {}), **kwargs}
    assert {name: body.get(name) for name in body_fields} == authored


# --- Codex ----------------------------------------------------------------------------------------------------


def _codex(tmp_path: Path, model_id: str = "gpt-5.6", **extra_kwargs: Any) -> ResponsesAdapter:  # noqa: ANN401
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data", process_env={})
    adapter = build_provider(
        ModelConfig(
            provider="codex",
            id=model_id,
            extra_kwargs={"codex_home": str(tmp_path / ".codex"), **extra_kwargs},
        ),
        ProviderRuntime(runtime_paths=paths, codex_default_instructions="Default Codex instructions."),
    )
    assert isinstance(adapter, ResponsesAdapter)
    return adapter


def test_codex_requests_carry_instructions_and_drop_the_fields_codex_rejects(tmp_path: Path) -> None:
    """Codex requires top-level instructions and rejects the output limit and temperature, raw request params too."""
    adapter = _codex(tmp_path, request_params={"max_output_tokens": 40, "temperature": 0})

    request = _request(adapter, [Message(role="user", content="Hi")])

    assert request["instructions"] == "Default Codex instructions."
    assert "max_output_tokens" not in request
    assert "temperature" not in request
    assert adapter.identity.id == "gpt-5.6-sol"


def test_a_codex_request_drops_a_temperature_override_as_it_drops_the_configured_one(tmp_path: Path) -> None:
    """A request's temperature override, such as a thread summary's, reaches Codex no more than a configured one."""
    # A model that takes sampling controls, so the override is applied before Codex drops it.
    adapter = _codex(tmp_path, model_id="o4-mini")
    assert not adapter.capabilities.provider_default_sampling

    kwargs = adapter._request_kwargs(
        ModelRequest(messages=[Message(role="user", content="Hi")], overrides=RequestOverrides(temperature=0.2)),
    )

    assert "temperature" not in {**kwargs.pop("extra_body", {}), **kwargs}


def test_codex_requests_carry_the_cache_key_and_the_installation_metadata(tmp_path: Path) -> None:
    """A configured cache key is sent, and the CLI's installation id joins any authored client metadata."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "installation_id").write_text("install-123\n", encoding="utf-8")
    plain = _codex(tmp_path, prompt_cache_key="mindroom-code-agent")
    authored_metadata = {"x-codex-installation-id": "custom-install", "x-test": "1"}
    authored = _codex(
        tmp_path,
        prompt_cache_key="mindroom-code-agent",
        extra_body={"debug": True, "client_metadata": dict(authored_metadata)},
    )

    plain_request = _request(plain, [Message(role="user", content="Hi")])
    authored_request = _request(authored, [Message(role="user", content="Hi")])

    assert plain_request["prompt_cache_key"] == "mindroom-code-agent"
    assert plain_request["client_metadata"] == {"x-codex-installation-id": "install-123"}
    assert "extra_headers" not in plain_request
    assert authored_request["debug"] is True
    assert authored_request["client_metadata"] == authored_metadata


def test_codex_session_headers_keep_authored_headers(tmp_path: Path) -> None:
    """The conversation's session headers are added without overriding headers the config set."""
    adapter = _codex(
        tmp_path,
        prompt_cache_key="mindroom-code-agent",
        session_id="conversation-a",
        extra_headers={"X-Test": "1", "x-codex-window-id": "custom-window"},
    )

    assert _request(adapter, [Message(role="user", content="Hi")])["extra_headers"] == {
        "X-Test": "1",
        "session_id": "conversation-a",
        "x-client-request-id": "conversation-a",
        "x-codex-window-id": "custom-window",
    }


@pytest.mark.parametrize(
    ("provider", "model_id", "base_url", "expected"),
    [
        ("codex", "gpt-5.6", None, True),
        ("openai_codex", "openai-codex/gpt-5.6", None, True),
        ("codex", "gpt-5.6-codex", None, True),
        ("codex", "gpt-5.4-mini", None, True),
        # Unreleased GPT versions default to the native path, including major-only spellings.
        ("codex", "gpt-6.0", None, True),
        ("codex", "gpt-6", None, True),
        ("openai", "gpt-5.4", None, True),
        ("openai", "gpt-5.3", None, False),
        ("openai", "gpt-4o", None, False),
        ("anthropic", "gpt-5.6", None, False),
        ("openai", "gpt-6-astra", "http://localhost:9292/v1", False),
        ("openai", "gpt-6-astra", "https://api.openai.com/v1/", True),
    ],
)
def test_native_tool_search_gating(provider: str, model_id: str, base_url: str | None, *, expected: bool) -> None:
    """The OpenAI API and Codex qualify from GPT-5.4 on; compatible endpoints do not opt into Responses search."""
    assert openai_native_tool_search_supported(provider, model_id, base_url=base_url) is expected
