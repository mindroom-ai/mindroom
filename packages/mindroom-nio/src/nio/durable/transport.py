"""Bounded HTTP reads without applying responses to live nio state."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from aiohttp import ClientConnectionError, ClientPayloadError

from ..exceptions import LocalProtocolError

if TYPE_CHECKING:
    from ..client.async_client import AsyncClient


class HttpError(LocalProtocolError):
    def __init__(self, status: int, errcode: str | None = None):
        self.status = status
        self.errcode = errcode
        super().__init__(f"durable HTTP request failed: status={status}")


class ConnectionRetriesExhausted(LocalProtocolError):
    """A transient network failure outlasted the bounded HTTP retry budget."""

    def __init__(self):
        super().__init__("durable HTTP connection retries exhausted")


class ResponseTooLarge(LocalProtocolError):
    def __init__(self):
        super().__init__("HTTP response exceeds the durable input bound")


class Transport:
    def __init__(self, client: AsyncClient, max_bytes: int):
        self.client = client
        self.max_bytes = max_bytes

    async def request(
        self,
        method: str,
        path: str,
        body: str | None = None,
        *,
        request_timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        parts = urlsplit(path)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query)
            if key != "access_token"
        ]
        path = urlunsplit(parts._replace(query=urlencode(query)))
        for attempt in range(5):
            delay = min(0.5 * 2**attempt, 30.0)
            try:
                response = await self.client.send(
                    method,
                    path,
                    body,
                    headers={
                        "Authorization": f"Bearer {self.client.access_token}",
                        "Content-Type": "application/json",
                    },
                    timeout=request_timeout,
                )
                try:
                    limit = max_bytes or self.max_bytes
                    content = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        content.extend(chunk)
                        if len(content) > limit:
                            raise ResponseTooLarge
                    if response.status == 200:
                        return bytes(content)
                    if (
                        response.status not in (408, 429) and response.status < 500
                    ) or attempt == 4:
                        try:
                            code = json.loads(content).get("errcode")
                        except (ValueError, AttributeError):
                            code = None
                        raise HttpError(
                            response.status, code if isinstance(code, str) else None
                        )
                    try:
                        retry_after = response.headers.get("Retry-After")
                        delay = (
                            float(retry_after)
                            if retry_after is not None
                            else float(
                                json.loads(content).get("retry_after_ms", delay * 1000)
                            )
                            / 1000
                        )
                    except (ValueError, TypeError, AttributeError):
                        pass
                finally:
                    response.release()
            except (ClientConnectionError, ClientPayloadError, TimeoutError) as exc:
                if attempt == 4:
                    raise ConnectionRetriesExhausted from exc
            await asyncio.sleep(max(0, min(delay, 30)))
        raise AssertionError("unreachable retry state")
