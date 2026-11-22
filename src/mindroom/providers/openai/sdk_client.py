"""The OpenAI SDK client of the OpenAI-family routes, which encodes each JSON request body off the event loop."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, override

import openai
from openai._base_client import _merge_mappings
from openai._utils._json import openapi_dumps

if TYPE_CHECKING:
    from openai._models import FinalRequestOptions

__all__ = ["AsyncOpenAI", "encoded_body"]


def encoded_body(body: dict[Any, Any]) -> bytes:
    """Return the bytes the SDK encodes ``body`` to, from each member and each item of a list member encoded alone.

    The JSON encoder holds the GIL through one call, so a worker thread encoding a long history in one call stalls the
    event loop as long as encoding it on the loop would; between calls the loop takes the GIL back. Each member is
    encoded as a one-member object without its braces, so its key is written as the encoder writes keys.
    """
    members = []
    for key, value in body.items():
        if isinstance(value, list):
            # The member of an empty array, ``"key":[]``, left open for the items.
            items = b",".join([openapi_dumps(item) for item in value])
            members.append(openapi_dumps({key: []})[1:-2] + items + b"]")
        else:
            members.append(openapi_dumps({key: value})[1:-1])
    return b"{" + b",".join(members) + b"}"


class AsyncOpenAI(openai.AsyncOpenAI):
    """An SDK client that encodes each JSON request body in a worker thread, which the SDK then sends as given.

    The SDK encodes the body on the event loop as it builds the request, and a long history or inline media make it
    megabytes. It keeps the SDK class's name, which the SDK sends in the User-Agent header. Azure's client stays the
    SDK's: it picks a request's deployment from the body before encoding it.
    """

    @override
    async def _prepare_options(self, options: FinalRequestOptions) -> FinalRequestOptions:
        options = await super()._prepare_options(options)
        # The SDK builds the multipart form of a request with files or a multipart content type from the mapping.
        if (
            not isinstance(options.json_data, dict)
            or options.files is not None
            or (self._build_headers(options).get("Content-Type") or "").startswith("multipart/form-data")
        ):
            return options
        body = options.json_data
        if options.extra_json is not None:
            body = _merge_mappings(body, options.extra_json)
        options.content = await asyncio.to_thread(encoded_body, body)
        options.json_data = options.extra_json = None
        return options
