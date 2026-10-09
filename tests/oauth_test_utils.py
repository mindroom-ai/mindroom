"""Test helpers for publishing and corrupting current OAuth credential state."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client

from mindroom.constants import resolve_runtime_paths
from mindroom.oauth.credential_store import oauth_credential_transaction

if TYPE_CHECKING:
    import threading
    from collections.abc import Mapping
    from pathlib import Path

    import pytest

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


@dataclass(frozen=True, slots=True)
class _OAuthStoreTestContext:
    runtime_paths: RuntimePaths
    provider: OAuthProvider
    credentials_manager: CredentialsManager
    worker_target: ResolvedWorkerTarget | None


def publish_oauth_credentials(
    provider: OAuthProvider,
    credentials: Mapping[str, Any],
    *,
    credentials_manager: CredentialsManager,
    worker_target: ResolvedWorkerTarget | None,
) -> None:
    """Publish credentials through the real SQLite transaction owner from any test context."""
    context = _OAuthStoreTestContext(
        resolve_runtime_paths(storage_path=credentials_manager.storage_root, process_env={}),
        provider,
        credentials_manager,
        worker_target,
    )

    async def publish() -> None:
        async with oauth_credential_transaction(context) as transaction:
            await transaction.publish(credentials, advance_connection_generation=True)
            await transaction.commit()

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, publish()).result()


async def oauth_authorization_url(
    provider: OAuthProvider,
    runtime_paths: RuntimePaths,
    *,
    state: str,
    code_verifier: str | None = None,
) -> str:
    """Resolve endpoints once and build the authorization URL, as the connect endpoint does."""
    endpoints = await provider.runtime_endpoints(runtime_paths)
    return await provider.authorization_uri_async(runtime_paths, endpoints, state=state, code_verifier=code_verifier)


def corrupt_oauth_credential_payload(database_path: Path, payload: bytes) -> None:
    """Replace a current credential payload with unreadable bytes for recovery tests."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "UPDATE oauth_credential_state SET credential_payload = ? WHERE singleton = 1",
            (payload,),
        )
        connection.commit()


type ImmediateTokenEndpointOutcome = httpx.Response | Callable[[httpx.Request], Exception]


@dataclass(frozen=True, slots=True)
class DelayedTokenEndpointOutcome:
    """Answer a token request only after the given number of seconds."""

    seconds: float
    outcome: ImmediateTokenEndpointOutcome


type TokenEndpointOutcome = ImmediateTokenEndpointOutcome | DelayedTokenEndpointOutcome


def serve_token_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    outcomes: list[TokenEndpointOutcome],
    *,
    request_received: threading.Event | None = None,
) -> list[str | None]:
    """Answer token requests through the real OAuth client in order and record each presented refresh token."""
    presented_refresh_tokens: list[str | None] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        presented_refresh_tokens.append(parse_qs(request.content.decode()).get("refresh_token", [None])[0])
        if request_received is not None:
            request_received.set()
        queued = outcomes.pop(0)
        outcome: ImmediateTokenEndpointOutcome
        if isinstance(queued, DelayedTokenEndpointOutcome):
            await asyncio.sleep(queued.seconds)
            outcome = queued.outcome
        else:
            outcome = queued
        if isinstance(outcome, httpx.Response):
            return outcome
        raise outcome(request)

    class _MockTransportClient(AsyncOAuth2Client):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr("authlib.integrations.httpx_client.AsyncOAuth2Client", _MockTransportClient)
    return presented_refresh_tokens


def rotated_token_response() -> httpx.Response:
    """Return a successful refresh response that rotates the refresh token."""
    return httpx.Response(
        200,
        json={
            "access_token": "rotated-access-token",
            "refresh_token": "rotated-refresh-token",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "read",
        },
    )
