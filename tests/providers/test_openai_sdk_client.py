"""The SDK client of the OpenAI-family routes encodes each JSON request body off the event loop, as the SDK would."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx2
import pytest
from openai._utils._json import openapi_dumps

from mindroom.providers.openai.sdk_client import OpenAIClient, encoded_body


def test_an_encoded_body_is_the_one_the_sdk_encodes() -> None:
    """Encoding each member and list item alone gives the bytes of encoding the body in one call."""
    body = {
        "model": "gpt-test",
        "messages": [
            {"role": "user", "content": "What should I pack? Café ☕"},
            {"role": "user", "content": [{"type": "text", "text": "sent"}], "at": datetime(2026, 1, 1, tzinfo=UTC)},
        ],
        "tools": (),
        "stop": ["\n\n"],
        "empty": [],
        "temperature": 0.5,
        "stream": True,
        "metadata": {"note": None},
        1: "a key that is not a string",
    }

    assert encoded_body(body) == openapi_dumps(body)
    assert encoded_body({}) == openapi_dumps({})


@pytest.mark.asyncio
async def test_sdk_calls_without_a_json_body_send_as_before() -> None:
    """A file upload still sends its multipart form, and a request without a body sends none."""
    sent: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        if request.method == "GET":
            return httpx2.Response(200, json={"object": "list", "data": []})
        file = {
            "id": "file_1",
            "object": "file",
            "bytes": 5,
            "created_at": 1,
            "filename": "notes.txt",
            "purpose": "assistants",
            "status": "processed",
        }
        return httpx2.Response(200, json=file)

    client = OpenAIClient(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        max_retries=0,
    )

    await client.files.create(file=("notes.txt", b"hello"), purpose="assistants")
    await client.models.list()

    upload, listing = sent
    assert upload.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="purpose"\r\n\r\nassistants' in upload.content
    assert listing.method == "GET"
    assert listing.content == b""
