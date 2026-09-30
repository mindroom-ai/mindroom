"""Claude requests only inline media the API accepts and describe the rest in text."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from agno.media import File
from agno.models.anthropic import Claude
from agno.models.message import Message

from mindroom import agno_compat_claude
from mindroom.attachment_media import attachment_records_to_media
from mindroom.attachments import AttachmentRecord
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.model_loading import get_model_instance
from mindroom.vertex_claude_compat import MindroomVertexAIClaude
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

_PDF = b"%PDF-1.4\n%fake report\n"
_UNSUPPORTED_FILES = {
    "deck.pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "sheet.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "bundle.zip": "application/zip",
}


def _message_body() -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "Read."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _capturing_http_client() -> tuple[httpx.Client, list[dict[str, Any]]]:
    """Return an SDK HTTP client that records each request body sent on the wire."""
    bodies: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=_message_body())

    return httpx.Client(transport=httpx.MockTransport(handle)), bodies


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


def _user_blocks(body: dict[str, Any]) -> list[dict[str, Any]]:
    [user] = [message for message in body["messages"] if message["role"] == "user"]
    return [{key: value for key, value in block.items() if key != "cache_control"} for block in user["content"]]


def _assert_only_supported_documents_inline(blocks: list[dict[str, Any]]) -> None:
    documents = [block for block in blocks if block["type"] == "document"]
    assert [document["source"]["type"] for document in documents] == ["base64", "text"]
    assert documents[0]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(documents[0]["source"]["data"]) == _PDF
    assert documents[1]["source"]["data"] == "Plain notes."
    notes = [block["text"] for block in blocks if block["type"] == "text" and "not sent inline" in block["text"]]
    assert len(notes) == len(_UNSUPPORTED_FILES)
    for note, mime_type in zip(notes, _UNSUPPORTED_FILES.values(), strict=True):
        assert mime_type in note
        assert "get_attachment without view" in note


@pytest.mark.parametrize("provider", ["anthropic", "bedrock_claude"])
def test_unsupported_documents_become_text_notes_while_pdf_stays_inline(tmp_path: Path, provider: str) -> None:
    """Office and archive uploads never reach Claude as base64 documents."""
    http_client, bodies = _capturing_http_client()
    model = _loaded_claude(tmp_path, provider, http_client)

    model.response(messages=[_attachment_turn(tmp_path)], compression_manager=None)

    _assert_only_supported_documents_inline(_user_blocks(bodies[0]))


def test_vertex_claude_request_payload_describes_unsupported_documents(tmp_path: Path) -> None:
    """Vertex shares the Claude request preparation, including its token-count payload."""
    model = MindroomVertexAIClaude(id="claude-opus-5", project_id="demo-project", region="us-central1")

    payload = model._request_input_kwargs(
        [_attachment_turn(tmp_path)],
        tools=None,
        response_format=None,
        compress_tool_results=False,
    )

    _assert_only_supported_documents_inline(payload["messages"][0]["content"])


def test_inline_media_past_the_request_size_budget_becomes_a_text_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PDF that would push the request past the provider body limit is described instead."""
    small_pdf = _PDF
    large_pdf = _PDF + b"x" * 3000
    monkeypatch.setattr(agno_compat_claude, "_MAX_INLINE_MEDIA_BYTES", 2000, raising=False)
    http_client, bodies = _capturing_http_client()
    model = _loaded_claude(tmp_path, "anthropic", http_client)
    turn = Message(
        role="user",
        content="Compare the reports.",
        files=[
            File(content=small_pdf, mime_type="application/pdf", filename="small.pdf"),
            File(content=large_pdf, mime_type="application/pdf", filename="large.pdf"),
        ],
    )

    model.response(messages=[turn], compression_manager=None)

    blocks = _user_blocks(bodies[0])
    [document] = [block for block in blocks if block["type"] == "document"]
    assert base64.b64decode(document["source"]["data"]) == small_pdf
    [note] = [block["text"] for block in blocks if block["type"] == "text" and "not sent inline" in block["text"]]
    assert "size limit" in note
    assert "get_attachment without view" in note
