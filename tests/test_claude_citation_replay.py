"""Claude history stays replayable after an answer cites an attached document."""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.media import File
from agno.models.anthropic import Claude
from agno.models.message import Message

from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_loading import get_model_instance
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_DOCUMENT_TEXT = "The grass is green. The sky is blue."
_CITED_ANSWER = "The grass is green."
# Exactly what the Messages API returns for a document attached without a
# title, which is how Agno always formats attachments.
_CHAR_LOCATION_WITHOUT_TITLE = {
    "type": "char_location",
    "cited_text": "The grass is green. ",
    "document_index": 0,
    "document_title": None,
    "start_char_index": 0,
    "end_char_index": 20,
    "file_id": None,
}
_THINKING_BLOCK = {"type": "thinking", "thinking": "Look it up.", "signature": "sig-1"}


def _message_body(content: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": content,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _capturing_http_client(responses: list[dict[str, Any]]) -> tuple[httpx.Client, list[dict[str, Any]]]:
    """Return an SDK HTTP client that records each request body sent on the wire."""
    bodies: list[dict[str, Any]] = []
    pending = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=next(pending))

    return httpx.Client(transport=httpx.MockTransport(handle)), bodies


def _document_turn() -> Message:
    return Message(
        role="user",
        content="What color is the grass?",
        files=[File(content=_DOCUMENT_TEXT.encode(), mime_type="text/plain", filename="notes.txt")],
    )


def _assistant_blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the single replayed assistant turn, ignoring prompt-cache markers."""
    [assistant] = [message for message in body["messages"] if message["role"] == "assistant"]
    return [{key: value for key, value in block.items() if key != "cache_control"} for block in assistant["content"]]


def _loaded_claude(tmp_path: Path, provider: str, http_client: httpx.Client) -> Claude:
    extra_kwargs: dict[str, Any] = (
        {"aws_region": "us-east-1", "aws_access_key": "dummy-access", "aws_secret_key": "dummy-secret"}
        if provider == "bedrock_claude"
        else {"api_key": "dummy-key"}
    )
    model_id = "anthropic.claude-opus-5" if provider == "bedrock_claude" else "claude-opus-5"
    config = bind_runtime_paths(
        Config(models={"claude": ModelConfig(provider=provider, id=model_id, extra_kwargs=extra_kwargs)}),
        test_runtime_paths(tmp_path),
    )
    model = get_model_instance(config, runtime_paths_for(config), "claude")
    assert isinstance(model, Claude)
    model.http_client = http_client
    return model


def test_agno_replays_untitled_document_citation_without_its_required_title() -> None:
    """Agno-only reproducer: the stored citation loses its null title and replays invalid."""
    http_client, bodies = _capturing_http_client(
        [
            _message_body([{"type": "text", "text": _CITED_ANSWER, "citations": [_CHAR_LOCATION_WITHOUT_TITLE]}]),
            _message_body([{"type": "text", "text": "The sky is blue."}]),
        ],
    )
    model = Claude(id="claude-opus-5", api_key="dummy-key", http_client=http_client)
    messages = [_document_turn()]

    model.response(messages=messages, compression_manager=None)
    messages.append(Message(role="user", content="And the sky?"))
    model.response(messages=messages, compression_manager=None)

    assert bodies[0]["messages"][0]["content"][-1]["citations"] == {"enabled": True}
    assert "title" not in bodies[0]["messages"][0]["content"][-1]
    [replayed_citation] = _assistant_blocks(bodies[1])[0]["citations"]
    # The API rejects this with "char_location.document_title: Field required".
    assert "document_title" not in replayed_citation


@pytest.mark.parametrize("provider", ["anthropic", "bedrock_claude"])
def test_loaded_claude_replays_cited_answer_as_plain_text(tmp_path: Path, provider: str) -> None:
    """The follow-up request carries the earlier answer's text without its citations."""
    http_client, bodies = _capturing_http_client(
        [
            _message_body([{"type": "text", "text": _CITED_ANSWER, "citations": [_CHAR_LOCATION_WITHOUT_TITLE]}]),
            _message_body([{"type": "text", "text": "The sky is blue."}]),
        ],
    )
    model = _loaded_claude(tmp_path, provider, http_client)
    messages = [_document_turn()]

    model.response(messages=messages, compression_manager=None)
    messages.append(Message(role="user", content="And the sky?"))
    model.response(messages=messages, compression_manager=None)

    assert _assistant_blocks(bodies[1]) == [{"type": "text", "text": _CITED_ANSWER}]
    assert bodies[1]["messages"][0]["content"][-1]["citations"] == {"enabled": True}
    assert messages[1].provider_data is not None
    assert messages[1].provider_data["content_blocks"][0]["citations"]


_STORED_CITATIONS = {
    "char_location_without_title": {key: value for key, value in _CHAR_LOCATION_WITHOUT_TITLE.items() if value},
    "char_location_with_title": {**_CHAR_LOCATION_WITHOUT_TITLE, "document_title": "notes.txt"},
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


def _poisoned_history(citation: dict[str, Any]) -> list[Message]:
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


@pytest.mark.parametrize("citation", _STORED_CITATIONS.values(), ids=_STORED_CITATIONS.keys())
def test_poisoned_stored_thread_replays_without_citations(tmp_path: Path, citation: dict[str, Any]) -> None:
    """Already-stored cited answers replay as their text, leaving stored history intact."""
    http_client, bodies = _capturing_http_client([_message_body([{"type": "text", "text": "Blue."}])])
    model = _loaded_claude(tmp_path, "anthropic", http_client)
    messages = _poisoned_history(citation)
    stored_provider_data = copy.deepcopy(messages[1].provider_data)

    model.response(messages=messages, compression_manager=None)

    assert _assistant_blocks(bodies[0]) == [
        _THINKING_BLOCK,
        {"type": "text", "text": _CITED_ANSWER},
        {"type": "text", "text": " It really is."},
    ]
    assert messages[1].provider_data == stored_provider_data


def test_vertex_claude_request_payload_replays_without_citations() -> None:
    """Vertex shares the Claude request preparation, including its token-count payload."""
    model = MindroomVertexAIClaude(id="claude-opus-5", project_id="demo-project", region="us-central1")

    payload = model._request_input_kwargs(
        _poisoned_history(_STORED_CITATIONS["char_location_without_title"]),
        tools=None,
        response_format=None,
        compress_tool_results=False,
    )

    replayed_blocks = _assistant_blocks(payload)
    assert all("citations" not in block for block in replayed_blocks)
    assert [block["text"] for block in replayed_blocks if block["type"] == "text"] == [
        _CITED_ANSWER,
        " It really is.",
    ]
