"""The request bodies of the OpenAI Responses and Codex routes: replay of stored turns, cache prefixes, Codex fields."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Literal

import pytest
from openai.types.chat import ChatCompletionChunk
from openai.types.responses import Response, ResponseStreamEvent, ResponseUsage
from openai.types.responses.response_input_item_param import ResponseInputItemParam
from pydantic import BaseModel, ConfigDict, TypeAdapter

from mindroom.agent_loop.model_loop import LoopHooks
from mindroom.agent_loop.run import resume_agent, run_agent
from mindroom.agent_loop.spec import AgentSpec
from mindroom.approval.receipt import approval_receipt_hook, build_approval_receipt
from mindroom.approval.receipt_readers import approval_receipt_context
from mindroom.attachments.media import attachment_records_to_media
from mindroom.attachments.storage import AttachmentRecord
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.event_journal import ApprovalCall, ApprovalDecision, approval_arguments_digest
from mindroom.history.request_messages import TurnMessages, build_request, build_resume_request, scope_history_messages
from mindroom.history.types import HistoryScope
from mindroom.llm.media import VIEWED_IMAGE_ID_PREFIX, File, Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelRequest, RequestOverrides, TurnCompleted
from mindroom.llm.runs import RunRecord
from mindroom.media_inputs import MediaInputs
from mindroom.model_defaults import OPENAI_BASE_URL_DEFAULT
from mindroom.providers.openai.build import accepted_extra_kwargs, build_provider
from mindroom.providers.openai.chat import ChatAdapter
from mindroom.providers.openai.chat_wire import ChatStream, chat_request_kwargs
from mindroom.providers.openai.codex_auth import CODEX_BASE_URL
from mindroom.providers.openai.media import chat_file_part, responses_file_part
from mindroom.providers.openai.response_replay import RESPONSE_OUTPUT_KEY
from mindroom.providers.openai.responses import ResponsesAdapter
from mindroom.providers.openai.responses_wire import (
    ResponsesStream,
    conversation_digest,
    responses_input,
    responses_replay,
    responses_request_kwargs,
)
from mindroom.providers.openai.schema import strict_response_schema
from mindroom.providers.openai.settings import ChatSettings
from mindroom.providers.openai.tool_search import openai_native_tool_search_supported
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from tests.agent_loop.helpers import collect, context, gated
from tests.providers.family_support import unreadable_type_note
from tests.providers.golden_support import responses_events, streamed_reply
from tests.providers.openai_family_support import openai_family_adapter, provider_runtime, responses_adapter

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from mindroom.llm.provider import NativeCompactionReplay
    from mindroom.llm.usage import MessageMetrics
    from mindroom.providers.openai.responses_wire import ResponsesTurnParts

_CHECKPOINT = {"type": "compaction", "id": "cmp_latest", "encrypted_content": "opaque-checkpoint"}
_ANSWER = {
    "type": "message",
    "id": "msg_answer",
    "role": "assistant",
    "status": "completed",
    "content": [{"type": "output_text", "text": "Ready", "annotations": []}],
}
_REASONING = {"type": "reasoning", "id": "rs_first", "summary": [], "encrypted_content": "opaque-reasoning"}
# Reasoning as a stored response returns it when its request asks for no encrypted content.
_STORED_REASONING = {"type": "reasoning", "id": "rs_first", "summary": []}
_CALL = {
    "type": "function_call",
    "id": "fc_lookup",
    "call_id": "call_lookup",
    "name": "lookup",
    "arguments": "{}",
    "status": "completed",
}
# Hosted search items as the provider writes them: a server-executed search has no ``call_id``.
_SEARCH_CALL = {
    "id": "tsc_1",
    "type": "tool_search_call",
    "arguments": {"queries": ["weather"]},
    "execution": "server",
    "status": "completed",
}
_SEARCH_OUTPUT = {
    "id": "tso_1",
    "type": "tool_search_output",
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


_STREAM_EVENT = TypeAdapter(ResponseStreamEvent)


def _streamed(
    response: Response,
    *,
    store: bool | None,
    portable_replay: bool = False,
    native_compaction: NativeCompactionReplay | None = None,
) -> ResponsesTurnParts:
    """Return the finished turn of ``response`` streamed as the API streams it (``responses_events``)."""
    stream = ResponsesStream(
        provider="OpenAI Responses",
        store=store,
        portable_replay=portable_replay,
        native_compaction=native_compaction,
        conversation_digest=conversation_digest([]),
    )
    for event in responses_events(response.model_dump(mode="json", exclude_none=True)):
        stream.add(_STREAM_EVENT.validate_python(event))
    assert stream.ended
    return stream.finish()


def _parsed(adapter: ResponsesAdapter, output: list[dict[str, Any]], **message: Any) -> Message:  # noqa: ANN401
    """Return the stored assistant message of one response, parsed as the route parses it without native replay."""
    parts = _streamed(
        _response(output),
        store=adapter.wire.store,
        native_compaction=adapter.native_compaction_settings(
            threshold=1024,
            history_generation="",
            allow_authored=False,
        ),
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
        provider="OpenAI Responses",
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
    native = adapter.native_compaction_settings(threshold=1024, history_generation="", allow_authored=False)
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


@pytest.mark.parametrize(
    ("store", "portable"),
    [(True, False), (True, True), (False, False)],
    ids=["stored", "portable", "unstored"],
)
@pytest.mark.parametrize("status", ["completed", "incomplete"])
def test_every_response_records_its_ordered_output(*, store: bool, portable: bool, status: str) -> None:
    """Every response that ends records its output in order, hosted searches included, whatever its route stores.

    A cut-off response records it without its reasoning, which may lack the item that must follow it.
    """
    output = [_REASONING, _SEARCH_CALL, _SEARCH_OUTPUT, _ANSWER, _CALL]

    parts = _streamed(_response(output, status=status), store=store, portable_replay=portable)

    assert parts.provider_data[RESPONSE_OUTPUT_KEY] == (output if status == "completed" else output[1:])
    assert "tool_search_items" not in parts.provider_data


@pytest.mark.parametrize(
    ("output", "as_recorded"),
    [
        ([_REASONING, _ANSWER], True),
        ([_STORED_REASONING, _ANSWER], False),
        ([_REASONING, {**_STORED_REASONING, "id": "rs_second"}, _ANSWER], False),
        ([_ANSWER], False),
    ],
    ids=["encrypted-reasoning", "stored-reasoning", "partly-encrypted-reasoning", "no-reasoning"],
)
@pytest.mark.parametrize("store", [True, False])
def test_a_reply_replays_as_recorded_only_with_its_encrypted_reasoning(
    output: list[dict[str, Any]],
    *,
    as_recorded: bool,
    store: bool,
) -> None:
    """Recorded output goes as recorded only while all its reasoning can go with it; otherwise the turn is rebuilt.

    A rebuild sends the same text without reasoning, which output without encrypted reasoning cannot resend.
    """
    reply = Message(
        role="assistant",
        content="Ready",
        provider_data={"response_id": "resp_1", RESPONSE_OUTPUT_KEY: output},
    )

    replay = _input([reply, Message(role="user", content="Thanks.")], store=store)

    assert replay[:-1] == (output if as_recorded else [{"role": "assistant", "content": "Ready"}])


def test_a_cut_off_reply_replays_rebuilt_after_the_searches_it_ran() -> None:
    """A response the output limit cut off replays its text and calls without reasoning, after its searches."""
    parts = _streamed(_response([_REASONING, _SEARCH_CALL, _SEARCH_OUTPUT, _CALL], status="incomplete"), store=False)
    reply = Message(role="assistant", tool_calls=parts.tool_calls, provider_data=parts.provider_data)

    replay = _input([reply, Message(role="tool", tool_call_id="fc_lookup", content="Not run")])

    assert replay == [
        _without_item_id(_SEARCH_CALL),
        _without_item_id(_SEARCH_OUTPUT),
        {"type": "function_call", "call_id": "call_lookup", "name": "lookup", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Not run"},
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


def _without_item_id(item: dict[str, Any]) -> dict[str, Any]:
    """Return a search item as a replay without its reasoning sends it: the provider rejects its ID alone."""
    return {key: value for key, value in item.items() if key != "id"}


def test_search_items_replay_ahead_of_the_discovered_function_call() -> None:
    """Recorded search items go back immediately ahead of the message's replayed function calls.

    The output recorded no reasoning, so the items and the call go without their item IDs.
    """
    function = {"name": "get_weather", "arguments": "{}"}
    call = {"id": "fc_1", "call_id": "call_1", "type": "function", "function": function}
    recorded_call = {"type": "function_call", "id": "fc_1", "call_id": "call_1", **function}
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[call],
        provider_data={RESPONSE_OUTPUT_KEY: [dict(_SEARCH_CALL), dict(_SEARCH_OUTPUT), recorded_call]},
    )
    messages = [
        Message(role="user", content="weather?"),
        assistant,
        Message(role="tool", content="sunny", tool_call_id="fc_1"),
    ]

    assert _input(messages) == [
        {"role": "user", "content": "weather?"},
        _without_item_id(_SEARCH_CALL),
        _without_item_id(_SEARCH_OUTPUT),
        {"type": "function_call", "call_id": "call_1", "name": "get_weather", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "call_1", "output": "sunny"},
    ]


def test_search_items_replay_with_their_own_turn_not_an_identical_earlier_one() -> None:
    """An earlier assistant turn with the same text cannot claim a later turn's search items."""
    answer = {**_ANSWER, "content": [{"type": "output_text", "text": "same text", "annotations": []}]}
    later = Message(
        role="assistant",
        content="same text",
        provider_data={RESPONSE_OUTPUT_KEY: [dict(_SEARCH_CALL), answer]},
    )
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
        _without_item_id(_SEARCH_CALL),
        {"role": "assistant", "content": "same text"},
    ]


def test_a_reply_without_text_or_calls_replays_as_an_empty_answer() -> None:
    """An assistant message with neither text nor calls still replays as the turn it was, with empty text."""
    assert _input([Message(role="user", content="Hi."), Message(role="assistant")]) == [
        {"role": "user", "content": "Hi."},
        {"role": "assistant", "content": ""},
    ]


def test_claude_blocks_are_not_replayed_to_responses() -> None:
    """A Claude turn's stored blocks, server-tool blocks included, stay out when a thread switches to Responses."""
    assistant = Message(
        role="assistant",
        content="Claude searched here.",
        provider_data={
            "claude_blocks": [
                {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "here"}},
                {"type": "text", "text": "Claude searched here."},
            ],
        },
    )

    assert _input([assistant]) == [{"role": "assistant", "content": "Claude searched here."}]


# --- Replies an older release stored --------------------------------------------------------------------------

# A tool turn after a hosted tool search as v2026.10.651 stored it (``Message.to_dict``), keyed by how its response
# ended: on a stored route, on an unstored route without and with reasoning, and cut off by the output limit.
_V2026_10_651_SEARCH_ITEMS = [
    {
        "id": "tsc_1",
        "arguments": {"queries": ["weather"]},
        "execution": "server",
        "status": "completed",
        "type": "tool_search_call",
    },
    {
        "id": "tso_1",
        "execution": "server",
        "status": "completed",
        "tools": [{"name": "get_weather", "parameters": {"type": "object"}, "type": "function"}],
        "type": "tool_search_output",
    },
]
_V2026_10_651_PROVIDER_DATA: dict[str, dict[str, Any]] = {
    "stored": {
        "tool_search_items": _V2026_10_651_SEARCH_ITEMS,
        "response_id": "resp_1",
        "mindroom_response_stored": True,
        "mindroom_conversation_sha256": "d31229fc7f47dc5edce3e26cc3c5f1863d6a9936eeac56eb8d04fcbf185d9152",
        "mindroom_portable_replay": False,
        "mindroom_native_compaction": None,
    },
    "unstored-without-reasoning": {
        "tool_search_items": _V2026_10_651_SEARCH_ITEMS,
        "response_id": "resp_1",
        "mindroom_response_stored": False,
        "mindroom_portable_replay": False,
        "mindroom_native_compaction": None,
    },
    "unstored-with-reasoning": {
        "tool_search_items": _V2026_10_651_SEARCH_ITEMS,
        "response_id": "resp_1",
        "mindroom_response_stored": False,
        "mindroom_portable_replay": False,
        "mindroom_native_compaction": None,
        "mindroom_response_output": [
            {
                "id": "rs_1",
                "summary": [{"text": "Need the tool.", "type": "summary_text"}],
                "type": "reasoning",
                "encrypted_content": "opaque-reasoning",
            },
            *_V2026_10_651_SEARCH_ITEMS,
            {
                "id": "msg_1",
                "content": [{"annotations": [], "text": "Checking the forecast.", "type": "output_text"}],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
            {
                "arguments": '{"city": "Paris"}',
                "call_id": "call_1",
                "name": "get_weather",
                "type": "function_call",
                "id": "fc_1",
                "status": "completed",
            },
        ],
    },
    "cut-off": {
        "tool_search_items": _V2026_10_651_SEARCH_ITEMS,
        "response_id": "resp_1",
        "mindroom_response_stored": False,
    },
}


@pytest.mark.parametrize("store", [True, False])
@pytest.mark.parametrize("row", list(_V2026_10_651_PROVIDER_DATA))
def test_a_reply_v2026_10_651_stored_replays_as_that_release_sent_it(row: str, *, store: bool) -> None:
    """Each tool turn v2026.10.651 stored replays as that release sent it, its search items included.

    The expected inputs are the ones v2026.10.651 built for this history.
    """
    reply = Message.from_dict(
        {
            "role": "assistant",
            "content": "Checking the forecast.",
            "tool_calls": [
                {
                    "id": "fc_1",
                    "call_id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                },
            ],
            "provider_data": json.loads(json.dumps(_V2026_10_651_PROVIDER_DATA[row])),
        },
    )
    history = [
        Message(role="system", content="Changed prompt."),
        Message(role="user", content="Weather in Paris?"),
        reply,
        Message(role="tool", tool_call_id="fc_1", content="light rain"),
        Message(role="user", content="And tomorrow?"),
    ]
    call = {"call_id": "call_1", "name": "get_weather", "arguments": '{"city": "Paris"}', "status": "completed"}
    recorded = _V2026_10_651_PROVIDER_DATA[row].get("mindroom_response_output")
    turn = (
        [*recorded[:-1], {"type": "function_call", "id": "fc_1", **call}]
        if recorded is not None
        else [
            *(_without_item_id(item) for item in _V2026_10_651_SEARCH_ITEMS),
            {"role": "assistant", "content": "Checking the forecast."},
            {"type": "function_call", **call},
        ]
    )

    assert _input(history, store=store) == [
        {"role": "developer", "content": "Changed prompt."},
        {"role": "user", "content": "Weather in Paris?"},
        *turn,
        {"type": "function_call_output", "call_id": "call_1", "output": "light rain"},
        {"role": "user", "content": "And tomorrow?"},
    ]


# --- Storage, continuation, and recorded policy ---------------------------------------------------------------


def _request(adapter: ResponsesAdapter, messages: list[Message], **request: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return one request's body fields, SDK keywords and ``extra_body`` merged as the SDK sends them."""
    kwargs = responses_request_kwargs(
        model_id=adapter.identity.id,
        request=ModelRequest(messages=messages, **request),
        settings=adapter.settings,
        wire=adapter.wire,
        provider=adapter.identity.provider_name,
    )
    return {**kwargs.pop("extra_body", {}), **kwargs}


def _stored_response(response_id: str, before: list[Message] | None = None) -> dict[str, Any]:
    """Return the provider data of a stored response whose request sent the conversation ``before``."""
    return {"response_id": response_id, "mindroom_conversation_sha256": conversation_digest(before or [])}


def test_a_stored_tool_turn_continues_whatever_the_model_name(tmp_path: Path) -> None:
    """Continuation does not depend on the model name, so aliases and non-reasoning models continue too."""
    adapter = responses_adapter(tmp_path, model_id="custom-alias", store=True)
    assistant = Message(
        role="assistant",
        tool_calls=[_call("status", "call_1")],
        provider_data=_stored_response("resp_1"),
    )
    messages = [assistant, Message(role="tool", content="ready", tool_call_id="fc_status")]

    request = _request(adapter, messages)

    assert (request["store"], request["previous_response_id"]) == (True, "resp_1")
    assert request["input"] == [{"type": "function_call_output", "call_id": "call_1", "output": "ready"}]


# --- Replay after a provider switch ---------------------------------------------------------------------------


def _claude_tool_turn() -> list[Message]:
    """Return a Claude tool turn as a thread stores it: text beside a ``toolu_`` call, its result, then the answer."""
    claude = {"mindroom_model": {"id": "claude-sonnet-5-5", "provider": "Anthropic"}}
    call = {
        "id": "toolu_01Weather",
        "type": "function",
        "function": {"name": "get_weather", "arguments": json.dumps({"city": "Paris"})},
    }
    return [
        Message(role="user", content="What should I pack for Paris?"),
        Message(role="assistant", content="Let me check the forecast first.", tool_calls=[call], provider_data=claude),
        Message(role="tool", tool_call_id="toolu_01Weather", content="light rain"),
        Message(role="assistant", content="Pack a rain jacket.", provider_data=claude),
        Message(role="user", content="And for the evening?"),
    ]


# The Claude turn as a Responses request sends it: the text ahead of the call, the call under the request's own id.
_CLAUDE_TOOL_TURN_INPUT = [
    {"role": "user", "content": "What should I pack for Paris?"},
    {"role": "assistant", "content": "Let me check the forecast first."},
    {
        "type": "function_call",
        "call_id": "call_00000000",
        "name": "get_weather",
        "arguments": json.dumps({"city": "Paris"}),
        "status": "completed",
    },
    {"type": "function_call_output", "call_id": "call_00000000", "output": "light rain"},
    {"role": "assistant", "content": "Pack a rain jacket."},
    {"role": "user", "content": "And for the evening?"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_config",
    [
        {"provider": "codex", "id": "gpt-6.1-sol", "extra_kwargs": {"codex_home": "<codex_home>"}},
        {"provider": "openai", "id": "gpt-6-astra", "api": "responses", "api_key": "test-key"},
    ],
    ids=["codex", "stored-openai"],
)
async def test_a_claude_tool_turn_keeps_its_text_after_the_thread_switches_to_responses(
    tmp_path: Path,
    model_config: dict[str, Any],
) -> None:
    """The text a Claude turn wrote beside its tool call reaches the Codex or OpenAI request that replays the turn."""
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return streamed_reply(_response([_ANSWER]).model_dump(mode="json"))

    adapter = openai_family_adapter(model_config, tmp_path, respond)
    assert isinstance(adapter, ResponsesAdapter)

    await _reply(adapter, _claude_tool_turn())

    assert sent[0]["input"] == _CLAUDE_TOOL_TURN_INPUT


def test_a_claude_tool_turn_after_a_continued_response_sends_its_calls(tmp_path: Path) -> None:
    """A request continuing a stored response sends the Claude tool turn after it whole, each call beside its result."""
    adapter = responses_adapter(tmp_path, store=True)
    before = [Message(role="user", content="Hello.")]
    stored = Message(role="assistant", content="Hi.", provider_data=_stored_response("resp_1", before))

    request = _request(adapter, [*before, stored, *_claude_tool_turn()])

    assert request["previous_response_id"] == "resp_1"
    assert request["input"] == _CLAUDE_TOOL_TURN_INPUT


def _stored_reply_route(
    root: Path,
    *first_outputs: list[dict[str, Any]],
) -> tuple[ResponsesAdapter, list[dict[str, Any]]]:
    """Return a stored-response route answering with ``first_outputs`` in turn, then text replies, and its bodies."""
    sent: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        index = len(sent) - 1
        output = first_outputs[index] if index < len(first_outputs) else [{**_ANSWER, "id": f"msg_{len(sent)}"}]
        reply = _response(output).model_dump(mode="json")
        return streamed_reply({**reply, "id": f"resp_{len(sent)}"})

    config = {"provider": "openai", "id": "gpt-6-astra", "api": "responses", "api_key": "test-key"}
    adapter = openai_family_adapter(config, root, respond)
    assert isinstance(adapter, ResponsesAdapter)
    return adapter, sent


async def _reply(adapter: ResponsesAdapter, messages: list[Message]) -> Message:
    """Send one request and return the assistant message the route stores for it."""
    events = [event async for event in adapter.stream(ModelRequest(messages=messages))]
    return next(event.message for event in events if isinstance(event, TurnCompleted))


def _dated_prompt(day: str) -> list[Message]:
    return [
        Message(role="system", content="You are Parity."),
        Message(role="system", content=f"Today is {day}."),
    ]


@pytest.mark.asyncio
async def test_a_reply_with_an_unchanged_prompt_continues_the_stored_chain(tmp_path: Path) -> None:
    """A stored chain holds the prompt it started with, so the same prompt sends only what follows the stored reply."""
    adapter, sent = _stored_reply_route(tmp_path)
    question = Message(role="user", content="Plan my day.")
    reply = await _reply(adapter, [*_dated_prompt("Monday"), question])

    await _reply(adapter, [*_dated_prompt("Monday"), question, reply, Message(role="user", content="And tomorrow?")])

    assert (sent[1]["previous_response_id"], sent[1]["input"]) == (
        "resp_1",
        [{"role": "user", "content": "And tomorrow?"}],
    )


def _next_day(history: list[Message]) -> list[Message]:
    return [*_dated_prompt("Tuesday"), *history[2:]]


@pytest.mark.asyncio
async def test_a_reply_after_the_system_prompt_changes_sends_the_new_prompt(tmp_path: Path) -> None:
    """A stored chain holds the prompt it started with, so a changed prompt replays the history instead of chaining."""
    adapter, sent = _stored_reply_route(tmp_path)
    question = Message(role="user", content="Plan my day.")
    reply = await _reply(adapter, [*_dated_prompt("Monday"), question])
    follow_up = Message(role="user", content="And tomorrow?")

    await _reply(adapter, _next_day([*_dated_prompt("Monday"), question, reply, follow_up]))

    assert "previous_response_id" not in sent[1]
    assert [item["role"] for item in sent[1]["input"]] == ["developer", "developer", "user", "assistant", "user"]
    assert "Today is Tuesday." in sent[1]["input"][1]["content"]


@pytest.mark.asyncio
async def test_an_approval_resume_on_a_stored_chain_continues_it_with_the_result_and_the_receipt(
    tmp_path: Path,
) -> None:
    """The receipt follows the decided call's result and leaves the prompt as the chain stored it, so the chain goes on.

    The resumed request sends only what follows the stored reply: the result, then the receipt.
    """
    adapter, sent = _stored_reply_route(tmp_path, [_STORED_REASONING, _CALL])
    prompt = _dated_prompt("Monday")
    question = Message(role="user", content="Look it up.")
    call = await _reply(adapter, [*prompt, question])
    result = Message(role="tool", tool_call_id="fc_lookup", content="Found it")
    receipt = Message(role="user", content="trusted approval receipt", add_to_agent_memory=False)

    await _reply(adapter, [*prompt, question, call, result, receipt])

    assert (sent[1]["store"], sent[1]["previous_response_id"]) == (True, "resp_1")
    assert sent[1]["input"] == [
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
        {"role": "user", "content": "trusted approval receipt"},
    ]


def _lookup(order: str) -> dict[str, Any]:
    return {
        "id": f"fc_{order}",
        "call_id": f"call_{order}",
        "type": "function",
        "function": {"name": "lookup", "arguments": json.dumps({"order": order})},
    }


def _decided(call_id: str, decision: ApprovalDecision) -> ApprovalCall:
    return ApprovalCall(
        tool_call_id=call_id,
        tool_name="lookup",
        invoking_agent="code",
        expires_at_ns=1,
        decision=decision,
        human_approval_required=True,
    )


@pytest.mark.asyncio
async def test_an_approval_resume_on_a_stored_chain_names_each_receipt_call_as_its_request_sends_it(
    tmp_path: Path,
) -> None:
    """The receipt names each call by the ``call_id`` the replayed call and its result keep, not its item id.

    Two calls of one tool are told apart only by their ids, and the replay drops each call's ``fc_`` item id. The
    stored reply's prompt has changed since, so the request replays the calls instead of continuing the chain.
    """
    adapter, sent = _stored_reply_route(tmp_path)
    prompt = Message(role="system", content="You are Code.")
    calls = Message(
        role="assistant",
        tool_calls=[_lookup("first"), _lookup("second")],
        provider_data=_stored_response("resp_1", [Message(role="system", content="You were Code.")]),
    )
    history = [
        Message(role="user", content="Look up both orders."),
        calls,
        Message(role="tool", tool_call_id="fc_first", content="Found the first"),
        Message(role="tool", tool_call_id="fc_second", content="Declined"),
    ]
    decided = (_decided("fc_first", ApprovalDecision.APPROVED), _decided("fc_second", ApprovalDecision.DENIED))
    spec = AgentSpec(id="code", name="Code", model=adapter, system_messages=(prompt,), toolkits=())

    with approval_receipt_context(decided, run_id="run-1"):
        await collect(
            run_agent(
                spec,
                history,
                ctx=context(run_id="run-1"),
                persist=None,
                request_template=ModelRequest(messages=()),
                hooks=LoopHooks(before_request=(approval_receipt_hook,)),
            ),
        )

    [request] = sent
    assert "previous_response_id" not in request
    assert request["input"][-1]["role"] == "user"
    receipt_ids = re.findall(r"tool call id `([^`]+)`", request["input"][-1]["content"])
    assert receipt_ids == ["call_first", "call_second"]
    for item_type in ("function_call", "function_call_output"):
        assert [item["call_id"] for item in request["input"] if item.get("type") == item_type] == receipt_ids
    assert "fc_" not in json.dumps(request["input"])


def _stored(tmp_path: Path, run: RunRecord) -> RunRecord:
    """Save ``run`` to a conversation store and return it as the store loads it."""
    store = create_state_storage("code", tmp_path / "state")
    store.save_run(ConversationSession(session_id="session-1", kind="agent", owner_id="code"), run, record_usage=False)
    stored = store.get_run(run.run_id)
    assert isinstance(stored, RunRecord)
    return stored


@pytest.mark.asyncio
async def test_a_turn_after_an_approved_run_finished_replays_instead_of_continuing_the_chain_it_left(
    tmp_path: Path,
) -> None:
    """A stored chain continues only while it holds the conversation the request replays before it.

    The resumed run sends its paused request again, memory results included, so it continues the chain with the
    result and the receipt. Once the run finishes, history keeps neither, so the next turn's replay no longer matches
    what the chain holds: it sends its history explicitly, and the memory results and the receipt never reach it.
    """
    adapter, sent = _stored_reply_route(tmp_path, [_CALL])

    def lookup() -> str:
        """Look the order up."""
        return "Found it"

    spec = AgentSpec(
        id="code",
        name="Code",
        model=adapter,
        system_messages=(Message(role="system", content="You are Code."),),
        toolkits=(gated(lookup),),
    )
    memory = Message(role="user", content="<memory>Alice prefers short answers.</memory>", add_to_agent_memory=False)
    first_turn = TurnMessages(prompt=Message(role="user", content="Look it up."), transient=(memory,))
    _, paused = await collect(
        run_agent(
            spec,
            build_request(summary=None, history=[], turn=first_turn, media=MediaInputs()),
            ctx=context(),
            persist=None,
            request_template=ModelRequest(messages=()),
        ),
    )
    stored = _stored(tmp_path, paused)
    approval = ApprovalCall(
        tool_call_id="fc_lookup",
        tool_name="lookup",
        invoking_agent="code",
        expires_at_ns=1,
        decision=ApprovalDecision.APPROVED,
        human_approval_required=True,
        toolkit_name="gated",
        arguments_digest=approval_arguments_digest({}),
    )
    with approval_receipt_context((approval,), run_id=stored.run_id):
        _, finished = await collect(
            resume_agent(
                spec,
                stored,
                build_resume_request(paused=stored.messages, summary=None, history=[]),
                approved=[approval],
                denied={},
                external_results={},
                context=lambda run_id: context(run_id=run_id),
                hooks=LoopHooks(before_request=(approval_receipt_hook,)),
                persist=None,
                request_template=ModelRequest(messages=()),
            ),
        )
    history = scope_history_messages(
        [_stored(tmp_path, finished)],
        scope=HistoryScope(kind="agent", scope_id="code"),
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    next_turn = TurnMessages(prompt=Message(role="user", content="And the next one?"))
    await collect(
        run_agent(
            spec,
            build_request(summary=None, history=history, turn=next_turn, media=MediaInputs()),
            ctx=context(run_id="run-2"),
            persist=None,
            request_template=ModelRequest(messages=()),
        ),
    )

    _, resumed, later = sent
    assert resumed["previous_response_id"] == "resp_1"
    assert resumed["input"] == [
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
        # The receipt names the call by the ``call_id`` its result references.
        {"role": "user", "content": build_approval_receipt((replace(approval, tool_call_id="call_lookup"),))},
    ]
    assert "previous_response_id" not in later
    assert [item.get("role", item.get("type")) for item in later["input"]] == [
        "developer",
        "user",
        "function_call",
        "function_call_output",
        "assistant",
        "user",
    ]
    assert "Alice prefers short answers." not in json.dumps(later)
    assert "TOOL APPROVAL RECEIPT" not in json.dumps(later)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("image", "continues"),
    [
        (Image(id="att_car", url="https://example.org/car.png"), False),
        (Image(id=f"{VIEWED_IMAGE_ID_PREFIX}car", content=b"car pixels", mime_type="image/png"), True),
    ],
    ids=["attachment", "viewed-image"],
)
async def test_a_follow_up_to_a_photo_continues_the_chain_only_while_history_replays_the_photo(
    tmp_path: Path,
    image: Image,
    *,
    continues: bool,
) -> None:
    """A stored chain holds the photo its turn sent, so a follow-up continues it only while history replays the photo.

    History replays an earlier turn's attachment as its text alone, so the follow-up sends its history explicitly and
    the photo leaves the conversation; a viewed image that history keeps replays as it was sent, so the chain goes on.
    """
    adapter, sent = _stored_reply_route(tmp_path)
    spec = AgentSpec(
        id="code",
        name="Code",
        model=adapter,
        system_messages=(Message(role="system", content="You are Code."),),
        toolkits=(),
    )
    photo_turn = TurnMessages(prompt=Message(role="user", content="What is this?"))
    _, answered = await collect(
        run_agent(
            spec,
            build_request(summary=None, history=[], turn=photo_turn, media=MediaInputs(images=[image])),
            ctx=context(),
            persist=None,
            request_template=ModelRequest(messages=()),
        ),
    )
    history = scope_history_messages(
        [_stored(tmp_path, answered)],
        scope=HistoryScope(kind="agent", scope_id="code"),
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    follow_up = TurnMessages(prompt=Message(role="user", content="What color is the car?"))
    await collect(
        run_agent(
            spec,
            build_request(summary=None, history=history, turn=follow_up, media=MediaInputs()),
            ctx=context(run_id="run-2"),
            persist=None,
            request_template=ModelRequest(messages=()),
        ),
    )

    photo, later = sent
    assert "input_image" in json.dumps(photo["input"])
    if continues:
        assert (later["previous_response_id"], later["input"]) == (
            "resp_1",
            [{"role": "user", "content": "What color is the car?"}],
        )
    else:
        assert "previous_response_id" not in later
        assert [item["role"] for item in later["input"]] == ["developer", "user", "assistant", "user"]
        assert "input_image" not in json.dumps(later["input"])


@pytest.mark.asyncio
async def test_a_replay_after_the_prompt_changes_sends_hosted_search_items_without_their_item_ids(
    tmp_path: Path,
) -> None:
    """A stored reply's reasoning has no encrypted content, so its replayed search items and call drop the IDs it owns.

    The provider rejects a search item's ID sent without the reasoning item that preceded it, stored or not.
    """
    output = [_STORED_REASONING, _SEARCH_CALL, _SEARCH_OUTPUT, {**_STORED_REASONING, "id": "rs_second"}, _CALL]
    adapter, sent = _stored_reply_route(tmp_path, output)
    question = Message(role="user", content="Look it up.")
    call = await _reply(adapter, [*_dated_prompt("Monday"), question])
    result = Message(role="tool", tool_call_id="fc_lookup", content="Found it")

    await _reply(adapter, _next_day([*_dated_prompt("Monday"), question, call, result]))

    assert sent[1]["store"] is True
    assert "previous_response_id" not in sent[1]
    assert sent[1]["input"][2:] == [
        {"role": "user", "content": "Look it up."},
        _without_item_id(_SEARCH_CALL),
        _without_item_id(_SEARCH_OUTPUT),
        {"type": "function_call", "call_id": "call_lookup", "name": "lookup", "arguments": "{}", "status": "completed"},
        {"type": "function_call_output", "call_id": "call_lookup", "output": "Found it"},
    ]


def _legacy_system_text_digest(messages: list[Message]) -> str:
    """Return the system-text digest a stored reply recorded before replies recorded their conversation's."""
    system_text = [message.content for message in messages if message.role == "system"]
    return hashlib.sha256(json.dumps(system_text, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("system_digest", [False, True], ids=["no-digest", "system-text-digest"])
async def test_a_reply_stored_without_a_conversation_digest_replays_once(
    tmp_path: Path,
    *,
    system_digest: bool,
) -> None:
    """A reply an older release stored replays its history once, then the route continues the reply it stores.

    Releases before the conversation digest recorded no digest, or only the digest of their request's system text,
    which matched here.
    """
    adapter, sent = _stored_reply_route(tmp_path)
    before = [*_dated_prompt("Monday"), Message(role="user", content="What should I pack?")]
    legacy = {"mindroom_system_sha256": _legacy_system_text_digest(before)} if system_digest else {}
    older = Message(
        role="assistant",
        content="Pack a jacket.",
        provider_data={"response_id": "resp_older", "mindroom_response_stored": True, **legacy},
    )
    history = [*before, older]
    follow_up = Message(role="user", content="And shoes?")

    reply = await _reply(adapter, [*history, follow_up])
    await _reply(adapter, [*history, follow_up, reply, Message(role="user", content="Thanks.")])

    assert "previous_response_id" not in sent[0]
    assert [item["role"] for item in sent[0]["input"]] == ["developer", "developer", "user", "assistant", "user"]
    assert (sent[1]["previous_response_id"], sent[1]["input"]) == ("resp_1", [{"role": "user", "content": "Thanks."}])


@pytest.mark.parametrize("storage", [{"store": False}, {"request_params": {"store": False}}])
def test_disabled_storage_replays_explicitly_with_encrypted_reasoning(tmp_path: Path, storage: dict[str, Any]) -> None:
    """A stateless request never continues a stored response and asks for the reasoning it must replay."""
    adapter = responses_adapter(tmp_path, model_id="reasoning-alias", reasoning_effort="high", **storage)
    messages = [
        Message(role="assistant", content="Earlier reply", provider_data=_stored_response("resp_1")),
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

    assert adapter.native_compaction_settings(threshold=1024, history_generation="", allow_authored=False) is None


@pytest.mark.parametrize("portable", [False, True])
@pytest.mark.parametrize("status", ["completed", "incomplete"])
def test_only_a_completed_response_records_its_replay_policy(*, portable: bool, status: str) -> None:
    """An approval rebuild trusts the replay policy only from a response that completed."""
    parts = _streamed(_response([_ANSWER], status=status), store=True, portable_replay=portable)

    if status == "completed":
        assert parts.provider_data["mindroom_portable_replay"] is portable
    else:
        assert "mindroom_portable_replay" not in parts.provider_data


# --- The stable prompt prefix ---------------------------------------------------------------------------------


def _messages(day: str) -> list[Message]:
    return [
        Message(role="system", content="Shared agent instructions."),
        Message(role="system", content=f"Current day: {day}."),
        Message(role="user", content="Write an introduction."),
    ]


@pytest.mark.parametrize("explicit", [False, True])
def test_the_stable_prompt_is_independent_of_the_turn_context(*, explicit: bool) -> None:
    """A changed date leaves the first developer message, the cacheable prefix, byte-identical."""
    original = _messages("Monday")
    snapshot = [message.to_dict() for message in original]

    first = _input(original, cache_system_prompt=True, explicit_breakpoint=explicit)
    second = _input(_messages("Tuesday"), cache_system_prompt=True, explicit_breakpoint=explicit)

    assert first[0] == second[0]
    assert first[0]["role"] == first[1]["role"] == "developer"
    assert first[0]["content"] == (
        [{"type": "input_text", "text": "Shared agent instructions.", "prompt_cache_breakpoint": {"mode": "explicit"}}]
        if explicit
        else "Shared agent instructions."
    )
    assert first[1]["content"] == original[1].content
    assert first[2] == {"role": "user", "content": "Write an introduction."}
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


def test_codex_sends_the_stable_prompt_without_an_explicit_breakpoint(tmp_path: Path) -> None:
    """The Codex endpoint caches the prefix implicitly."""
    adapter = _codex(tmp_path)

    first = _request(adapter, _messages("Monday"))["input"][0]

    assert first == {"role": "developer", "content": "Shared agent instructions."}


def test_a_disabled_cache_sends_each_system_message_without_a_breakpoint() -> None:
    """``cache_system_prompt: false`` sends each system message as its own developer message, as authored."""
    messages = _messages("Monday")

    assert _input(messages, cache_system_prompt=False, explicit_breakpoint=True) == [
        {"role": "developer", "content": messages[0].content},
        {"role": "developer", "content": messages[1].content},
        {"role": "user", "content": messages[2].content},
    ]


def test_a_system_message_with_custom_content_goes_as_authored() -> None:
    """Only a text system message takes the breakpoint; custom content blocks are sent as they are."""
    custom = [{"type": "input_text", "text": "Custom content."}]
    messages = [Message(role="system", content=custom), Message(role="user", content="User text.")]

    assert _input(messages, cache_system_prompt=True, explicit_breakpoint=True) == [
        {"role": "developer", "content": custom},
        {"role": "user", "content": "User text."},
    ]


def test_a_stored_continuation_does_not_resend_the_instructions() -> None:
    """Chaining to a stored response sends only what follows it."""
    prompt = _messages("Monday")
    messages = [
        *prompt,
        Message(role="assistant", content="Introduction.", provider_data=_stored_response("resp_previous", prompt)),
        Message(role="user", content="Continue."),
    ]

    assert _input(messages, store=True, cache_system_prompt=True) == [{"role": "user", "content": "Continue."}]


def test_rebuilding_a_request_leaves_a_message_s_content_list_unchanged() -> None:
    """A message whose content is a list keeps it as stored, so every rebuild sends its file part once."""
    message = Message(
        role="user",
        content=[{"type": "input_text", "text": "Read the attachment."}],
        files=[File(content=b"%PDF", filename="report.pdf")],
    )

    first = _input([message])

    assert _input([message]) == first
    assert [part["type"] for part in first[0]["content"]] == ["input_text", "input_file"]
    assert message.content == [{"type": "input_text", "text": "Read the attachment."}]


# --- Files ----------------------------------------------------------------------------------------------------


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.parametrize(
    ("filename", "declared_mime", "expected_mime"),
    [
        ("notes.txt", "application/octet-stream", "text/plain"),
        ("data.csv", "application/octet-stream", "text/csv"),
        ("report.pdf", "application/octet-stream", "application/pdf"),
        ("notes.md", "text/markdown; charset=utf-8", "text/markdown"),
        ("script.sh", "text/x-sh", "text/x-sh"),
        ("report.pdf", None, "application/pdf"),
        ("report.docx", None, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("report.pptx", None, "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("report.xlsx", None, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("notes.txt", "application/pdf", "application/pdf"),
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


def _sent_type(part: dict[str, Any] | None) -> str:
    """Return the type a file part sends its data as, ``note <type>`` for a note, or ``text`` for the file's text."""
    assert part is not None
    if part["type"] in {"text", "input_text"}:
        noted = re.findall(r"^\[Attachment omitted: .+? \((.+?)\) is a file type", part["text"])
        return f"note {noted[0]}" if noted else "text"
    data_url = part["file"]["file_data"] if part["type"] == "file" else part["file_data"]
    return data_url.removeprefix("data:").split(";", 1)[0]


@pytest.mark.parametrize(
    ("file", "chat", "responses"),
    [
        (File(content=b"%PDF", filename="report.pdf"), "application/pdf", "application/pdf"),
        (File(content=b"PK", filename="report.docx"), "note " + _DOCX, _DOCX),
        (File(content=b"PK", filename="sheet.xlsx", mime_type="application/octet-stream"), "note " + _XLSX, _XLSX),
        (
            File(content=b"notes", filename="notes.txt", mime_type="application/octet-stream"),
            "text",
            "text/plain",
        ),
        (File(content=b"{}", filename="data.json"), "text", "application/json"),
        (File(content=b"From: a@example.org\n\nHi", filename="mail.eml"), "text", "message/rfc822"),
        (File(content=b"a: 1", filename="config.yaml"), "text", "application/yaml"),
        (File(content=b"x = 1", filename="app.toml"), "text", "application/toml"),
        (File(content=b"started", filename="server.log"), "text", "text/plain"),
        (File(content=b"let x = 1", filename="app.ts"), "text", "text/x-typescript"),
        (File(content=b'syntax = "proto3";', filename="api.proto"), "text", "text/x-protobuf"),
        (File(content=b"SELECT 1;", filename="q.sql", mime_type="application/sql"), "text", "note application/sql"),
        (File(content=b"print 1;", filename="a.pl", mime_type="application/x-perl"), "text", "note application/x-perl"),
        (File(content=b"puts 1", filename="a.rb", mime_type="application/x-ruby"), "text", "note application/x-ruby"),
        (
            File(content=b'{"cells": []}', filename="nb.ipynb", mime_type="application/x-ipynb+json"),
            "text",
            "note application/x-ipynb+json",
        ),
        (
            File(content=b"\x08\x96\x01", filename="payload.bin", mime_type="application/x-protobuf"),
            "note application/x-protobuf",
            "application/x-protobuf",
        ),
        (
            File(content=b"let x = 1", filename="app.js", mime_type="application/javascript"),
            "text",
            "application/javascript",
        ),
        (
            File(content=b"echo hi", filename="run", mime_type="application/x-shellscript"),
            "text",
            "note application/x-shellscript",
        ),
        (
            File(content=b"a,b", filename="report.csv.gz"),
            "note application/octet-stream",
            "note application/octet-stream",
        ),
        (File(content=b"PK", filename="bundle.zip"), "note application/zip", "note application/zip"),
        (
            File(content=b"blob", filename="unknown.bin"),
            "note application/octet-stream",
            "note application/octet-stream",
        ),
    ],
)
def test_chat_and_responses_send_only_the_file_types_their_api_reads(file: File, chat: str, responses: str) -> None:
    """Both wires type a file by one rule; Chat reads PDF and text, Responses also documents, as OpenAI documents.

    Chat sends a text file's contents as a text part. Any other file goes as a text part naming it and its type, and a
    compressed name has no type.
    """
    assert _sent_type(chat_file_part(file, provider="OpenAI Chat")) == chat
    assert _sent_type(responses_file_part(file, provider="OpenAI Responses")) == responses


def test_chat_sends_a_text_file_s_contents_read_as_claude_reads_them(tmp_path: Path) -> None:
    """Chat Completions takes only PDF file parts, so a text file goes as its UTF-8 text; other bytes read as U+FFFD."""
    notes = tmp_path / "att_notes.txt"
    notes.write_bytes("Café notes\n".encode())

    assert chat_file_part(File(filepath=str(notes), filename="notes.md"), provider="OpenAI Chat") == {
        "type": "text",
        "text": "Café notes\n",
    }
    assert chat_file_part(File(content=b"caf\xe9,ok", filename="latin.csv"), provider="OpenAI Chat") == {
        "type": "text",
        "text": "caf\ufffd,ok",
    }


def test_a_file_of_a_type_the_api_does_not_read_is_never_read(tmp_path: Path) -> None:
    """The note replaces a file before its bytes are read, so a missing file of such a type fails nothing."""
    file = File(filepath=str(tmp_path / "missing.zip"), filename="bundle.zip")

    assert chat_file_part(file, provider="OpenAI Chat") == {
        "type": "text",
        "text": unreadable_type_note("bundle.zip", "application/zip"),
    }
    assert responses_file_part(file, provider="OpenAI Responses") == {
        "type": "input_text",
        "text": unreadable_type_note("bundle.zip", "application/zip"),
    }


def test_an_attachment_without_a_filename_is_named_and_typed_by_its_stored_file(tmp_path: Path) -> None:
    """An attachment recorded without a filename or type still reaches Responses, named and typed by its file."""
    stored_path = tmp_path / "att_document.pdf"
    stored_path.write_bytes(b"%PDF synthetic attachment")
    _, _, files, _ = attachment_records_to_media(
        [AttachmentRecord(attachment_id="att_document", local_path=stored_path, kind="file")],
    )

    replay = _input([Message(role="user", content="Read the attachment.", files=files)])

    assert replay[0]["content"][1] == {
        "type": "input_file",
        "filename": "att_document.pdf",
        "file_data": f"data:application/pdf;base64,{base64.b64encode(b'%PDF synthetic attachment').decode()}",
    }


def test_an_untyped_attachment_is_noted_by_its_stored_file_name(tmp_path: Path) -> None:
    """Nothing types an attachment stored as ``.bin`` without a filename or type, so its note names the stored file."""
    stored_path = tmp_path / "att_document.bin"
    stored_path.write_bytes(b"synthetic attachment")
    _, _, files, _ = attachment_records_to_media(
        [AttachmentRecord(attachment_id="att_document", local_path=stored_path, kind="file")],
    )

    replay = _input([Message(role="user", content="Read the attachment.", files=files)])

    assert replay[0]["content"][1] == {
        "type": "input_text",
        "text": unreadable_type_note("att_document.bin", "application/octet-stream"),
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


def _chat_usage(adapter: ChatAdapter, usage: dict[str, Any]) -> MessageMetrics | None:
    """Return the usage a Chat stream reports in its usage chunk, where Groq streams it in its own extension."""
    report = {"x_groq": {"usage": usage}} if adapter.wire.route == "groq" else {"usage": usage}
    chunk = {"id": "chatcmpl_test", "object": "chat.completion.chunk", "created": 1, "model": "m", "choices": []}
    stream = ChatStream(adapter.wire)
    stream.add(ChatCompletionChunk.model_validate({**chunk, **report}))
    stream.finish()
    return stream.parts.usage


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
            provider_runtime(resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})),
        )
        assert isinstance(adapter, ChatAdapter)
        usage = _chat_usage(
            adapter,
            {
                "prompt_tokens": 15_000,
                "completion_tokens": 50,
                "total_tokens": 15_050,
                "prompt_tokens_details": input_details,
                "completion_tokens_details": {"audio_tokens": 13, "reasoning_tokens": 7},
            },
        )
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
        usage = _streamed(response, store=None).usage

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
        provider_runtime(resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})),
    )
    assert isinstance(adapter, ChatAdapter)

    usage = _chat_usage(
        adapter,
        {
            "prompt_tokens": 5000,
            "completion_tokens": 300,
            "total_tokens": 5300,
            "prompt_tokens_details": {"cached_tokens": 4608},
            "completion_tokens_details": {"reasoning_tokens": 120},
        },
    )

    assert usage is not None
    assert (usage.tokens.input_tokens, usage.tokens.output_tokens, usage.tokens.total_tokens) == (5000, 300, 5300)
    assert (usage.tokens.cache_read_tokens, usage.tokens.reasoning_tokens) == (4608, 120)


@pytest.mark.parametrize("provider", ["openai", "groq", "cerebras"])
def test_a_chat_reply_keeps_the_token_total_its_route_reports(tmp_path: Path, provider: str) -> None:
    """The stored total is the one the route reports, not input plus output."""
    adapter = build_provider(
        ModelConfig(provider=provider, id="m", api="chat_completions" if provider == "openai" else None, api_key="k"),
        provider_runtime(resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})),
    )
    assert isinstance(adapter, ChatAdapter)

    usage = _chat_usage(adapter, {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 57})

    assert usage is not None
    assert (usage.tokens.input_tokens, usage.tokens.output_tokens, usage.tokens.total_tokens) == (40, 10, 57)


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


@pytest.mark.parametrize("provider", ["openai", "groq", "cerebras"])
def test_every_request_setting_a_chat_route_accepts_reaches_its_body(provider: str, tmp_path: Path) -> None:
    """A request setting a route accepts is sent in the body, so no accepted setting is dropped."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    config = ModelConfig(
        provider=provider,
        id="m",
        api="chat_completions" if provider == "openai" else None,
        api_key="test-key",
    )
    accepted = accepted_extra_kwargs(config, runtime_paths)
    authored = {name: value for name, value in _CHAT_BODY_VALUES.items() if name in accepted}
    assert set(_CHAT_BODY_VALUES) == set(ChatSettings().request_fields()) - _CHAT_NON_BODY_FIELDS
    adapter = build_provider(config.model_copy(update={"extra_kwargs": authored}), provider_runtime(runtime_paths))
    assert isinstance(adapter, ChatAdapter)

    kwargs = chat_request_kwargs(
        model_id="m",
        messages=[Message(role="user", content="Hi")],
        tools=(),
        tool_choice=None,
        output_schema=None,
        structured_output=adapter.capabilities.structured_output,
        settings=adapter.settings,
        wire=adapter.wire,
        provider=adapter.identity.provider_name,
    )

    body = {**kwargs.pop("extra_body", {}), **kwargs}
    assert {name: body.get(name) for name in authored} == authored


@pytest.mark.parametrize("provider", ["openai", "groq", "cerebras"])
def test_a_chat_request_without_tools_sends_no_tool_list(provider: str, tmp_path: Path) -> None:
    """Every Chat route leaves ``tools`` out of a request that offers none, rather than sending an empty list."""
    adapter = build_provider(
        ModelConfig(provider=provider, id="m", api="chat_completions" if provider == "openai" else None, api_key="k"),
        provider_runtime(resolve_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})),
    )
    assert isinstance(adapter, ChatAdapter)

    kwargs = chat_request_kwargs(
        model_id="m",
        messages=[Message(role="user", content="Hi")],
        tools=(),
        tool_choice=None,
        output_schema=None,
        structured_output=adapter.capabilities.structured_output,
        settings=adapter.settings,
        wire=adapter.wire,
        provider=adapter.identity.provider_name,
    )

    assert "tools" not in {**kwargs.get("extra_body", {}), **kwargs}


# --- Codex ----------------------------------------------------------------------------------------------------


def _codex(tmp_path: Path, model_id: str = "gpt-5.6", **extra_kwargs: Any) -> ResponsesAdapter:  # noqa: ANN401
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "data", process_env={})
    adapter = build_provider(
        ModelConfig(
            provider="codex",
            id=model_id,
            extra_kwargs={"codex_home": str(tmp_path / ".codex"), **extra_kwargs},
        ),
        provider_runtime(paths, codex_default_instructions="Default Codex instructions."),
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
    ("route", "model_id", "base_url", "expected"),
    [
        ("codex", "gpt-5.6", CODEX_BASE_URL, True),
        ("codex", "gpt-5.6-codex", CODEX_BASE_URL, True),
        ("codex", "gpt-5.4-mini", CODEX_BASE_URL, True),
        # Unreleased GPT versions default to the native path, including major-only spellings.
        ("codex", "gpt-6.0", CODEX_BASE_URL, True),
        ("codex", "gpt-6", CODEX_BASE_URL, True),
        ("codex", "gpt-6-astra", "http://localhost:9292/v1", True),
        ("openai", "gpt-5.4", OPENAI_BASE_URL_DEFAULT, True),
        ("openai", "gpt-5.3", OPENAI_BASE_URL_DEFAULT, False),
        ("openai", "gpt-4o", OPENAI_BASE_URL_DEFAULT, False),
        ("openai", "gpt-6-astra", "http://localhost:9292/v1", False),
        ("openai", "gpt-6-astra", "https://api.openai.com/v1/", True),
    ],
)
def test_native_tool_search_gating(
    route: Literal["openai", "codex"],
    model_id: str,
    base_url: str,
    *,
    expected: bool,
) -> None:
    """The OpenAI API and Codex qualify from GPT-5.4 on; compatible OpenAI endpoints do not opt into Responses search."""
    assert openai_native_tool_search_supported(route, model_id, base_url=base_url) is expected
