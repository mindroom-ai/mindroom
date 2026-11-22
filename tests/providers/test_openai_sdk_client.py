"""The SDK client of the OpenAI-family routes encodes each JSON request body off the event loop, as the SDK would."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx2
import openai
import pytest
from openai._utils._json import openapi_dumps

from mindroom.providers.openai.sdk_client import AsyncOpenAI, encoded_body


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


def _client(sent: list[httpx2.Request]) -> AsyncOpenAI:
    """Return the client, answering each request it sends with a listing or an uploaded file."""

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

    return AsyncOpenAI(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        max_retries=0,
    )


@pytest.mark.asyncio
async def test_sdk_calls_without_a_json_body_send_as_before() -> None:
    """A request with files or a multipart content type still sends its form, and one without a body sends none."""
    sent: list[httpx2.Request] = []
    client = _client(sent)

    await client.files.create(file=("notes.txt", b"hello"), purpose="assistants")
    multipart = {"headers": {"Content-Type": "multipart/form-data"}}
    await client.post("/uploads", cast_to=httpx2.Response, body={"purpose": "assistants"}, options=multipart)
    await client.post("/uploads", cast_to=httpx2.Response, body={"purpose": "assistants"}, files=[("file", b"hi")])
    await client.models.list()

    upload, form, files, listing = sent
    assert upload.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="purpose"\r\n\r\nassistants' in upload.content
    assert b'filename="notes.txt"' in upload.content
    assert b"\r\n\r\nhello\r\n" in upload.content
    assert form.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="purpose"\r\n\r\nassistants' in form.content
    assert b'name="file"; filename="upload"\r\nContent-Type: application/octet-stream\r\n\r\nhi\r\n' in files.content
    assert listing.method == "GET"
    assert listing.content == b""


@pytest.mark.asyncio
async def test_the_client_sends_the_sdks_user_agent() -> None:
    """The client keeps the SDK class's name, which the SDK sends as its User-Agent."""
    sent: list[httpx2.Request] = []

    await _client(sent).models.list()

    assert sent[0].headers["user-agent"] == f"AsyncOpenAI/Python {openai.__version__}"
