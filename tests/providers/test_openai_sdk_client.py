"""The SDK client of the OpenAI-family routes encodes each JSON request body off the event loop, as the SDK would."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx2
import openai
import pytest
from openai._models import FinalRequestOptions
from openai._utils._json import openapi_dumps

from mindroom.providers.openai.adapter_common import with_large_fields_untyped
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


def _client(
    sent: list[httpx2.Request],
    client_class: type[openai.AsyncOpenAI] = AsyncOpenAI,
    **options: object,
) -> openai.AsyncOpenAI:
    """Return a client built with ``options``, answering each request it sends with a listing or an uploaded file."""

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

    return client_class(
        api_key="test-key",
        base_url="https://api.example.test/v1",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(respond)),
        max_retries=0,
        **options,
    )


@pytest.mark.asyncio
async def test_requests_with_files_a_form_or_no_body_send_what_the_sdk_builds() -> None:
    """Requests with files, a multipart content type, no body, or content beside a body reach the SDK unchanged."""
    sent: list[httpx2.Request] = []
    client = _client(sent)

    await client.files.create(file=("notes.txt", b"hello"), purpose="assistants")
    multipart = {"headers": {"Content-Type": "multipart/form-data"}}
    await client.post("/uploads", cast_to=httpx2.Response, body={"purpose": "assistants"}, options=multipart)
    await client.post("/uploads", cast_to=httpx2.Response, body={"purpose": "assistants"}, files=[("file", b"hi")])
    await client.models.list()
    configured = _client(sent, default_headers={"Content-Type": "multipart/form-data"})
    await configured.post("/uploads", cast_to=httpx2.Response, body={"purpose": "assistants"})

    both = FinalRequestOptions.construct(method="post", url="/uploads", json_data={"purpose": "x"}, content=b"raw")
    with pytest.raises(TypeError, match="Passing both `content` and `json_data` is not supported"):
        await client.request(httpx2.Response, both)

    upload, form, files, listing, configured_form = sent
    sent_to = [(request.method, request.url.path) for request in sent]
    assert sent_to == [
        ("POST", "/v1/files"),
        *[("POST", "/v1/uploads")] * 2,
        ("GET", "/v1/models"),
        ("POST", "/v1/uploads"),
    ]
    assert upload.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="purpose"\r\n\r\nassistants' in upload.content
    assert b'filename="notes.txt"' in upload.content
    assert b"\r\n\r\nhello\r\n" in upload.content
    assert form.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="purpose"\r\n\r\nassistants' in form.content
    assert b'name="file"; filename="upload"\r\nContent-Type: application/octet-stream\r\n\r\nhi\r\n' in files.content
    assert listing.content == b""
    assert configured_form.headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="purpose"\r\n\r\nassistants' in configured_form.content


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.asyncio
async def test_a_request_is_the_one_the_sdks_own_client_sends(api: str) -> None:
    """The method, URL, headers (the User-Agent included) and body bytes are the SDK's own client's for the same call."""
    history = [{"role": "user", "content": f"Message {index}: Café ☕"} for index in range(3)]
    function = {"name": "lookup", "description": "Look it up.", "parameters": {"type": "object"}}
    if api == "chat":
        large = ("messages", "tools")
        kwargs = {"model": "gpt-test", "messages": history, "tools": [{"type": "function", "function": function}]}
    else:
        large = ("input", "tools")
        kwargs = {"model": "gpt-test", "input": history, "tools": [{"type": "function", **function}]}
    kwargs = with_large_fields_untyped({**kwargs, "temperature": 0.5, "extra_body": {"seed": 7}}, large)
    sent: list[httpx2.Request] = []

    for client_class in (openai.AsyncOpenAI, AsyncOpenAI):
        client = _client(sent, client_class)
        resource = client.chat.completions if api == "chat" else client.responses
        await resource.create(**kwargs)

    sdk, ours = sent
    assert (ours.method, ours.url, ours.headers.raw, ours.content) == (
        sdk.method,
        sdk.url,
        sdk.headers.raw,
        sdk.content,
    )
    assert b"Message 2" in ours.content
