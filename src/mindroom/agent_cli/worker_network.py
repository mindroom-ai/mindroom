"""Fail-closed checks for the protected-endpoint Docker CLI profile.

This profile preserves shell internet. It proves configured authority endpoints
reject shell credentials; it does not claim TCP isolation or discover arbitrary
operator listeners. Gateway-only routing is an operator deployment requirement.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from mindroom.agent_cli.worker_protocol import CliWorkerLaunch
    from mindroom.constants import RuntimePaths


def validate_cli_primary_auth(runtime_paths: RuntimePaths) -> None:
    """Reject primary auth modes which expose executable authority to the worker."""
    if not runtime_paths.env_value("MINDROOM_API_KEY"):
        msg = "CLI workers require protected primary MINDROOM_API_KEY authentication"
        raise ValueError(msg)
    if runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED") and not runtime_paths.env_flag(
        "MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT",
    ):
        msg = "CLI workers cannot use spoofable trusted-upstream header authentication"
        raise ValueError(msg)
    if runtime_paths.env_flag("OPENAI_COMPAT_ALLOW_UNAUTHENTICATED"):
        msg = "CLI workers cannot reach unauthenticated OpenAI execution"
        raise ValueError(msg)


def _headers_to_reject(*, grant: str, control_token: str) -> tuple[dict[str, str], ...]:
    return (
        {},
        {"Authorization": f"Bearer {grant}"},
        {"x-mindroom-sandbox-token": control_token},
        {"x-mindroom-sandbox-token": grant},
        {"x-forwarded-user": "admin", "x-auth-request-user": "admin", "x-auth-request-email": "admin@example.org"},
    )


async def probe_cli_network(  # noqa: C901 - explicit endpoint authority checks
    launch: CliWorkerLaunch,
    *,
    control_token: str,
    client: httpx.AsyncClient,
) -> None:
    """Probe real existing routes from inside the worker, before installing its grant."""
    grant = launch.token.get_secret_value()
    primary_headers = _headers_to_reject(grant=grant, control_token=control_token)
    # Peers validate neither CLI grants nor this worker's derived token, so live
    # values would prove nothing there and only expose them to other workers.
    peer_headers = _headers_to_reject(grant=secrets.token_urlsafe(32), control_token=secrets.token_urlsafe(32))
    # Bound independent route checks without caching a changing peer inventory.
    limit = asyncio.Semaphore(8)

    async def check_route(url: str, *, peer: bool = False) -> None:
        async with limit:
            for headers in peer_headers if peer else primary_headers:
                try:
                    response = await client.get(url, headers=headers)
                except httpx.ConnectError:
                    if peer:
                        # Peers may start/retire after Docker inspection. Keep
                        # checking other credentials in case the listener opens.
                        continue
                    raise
                if response.status_code not in {401, 403}:
                    msg = "CLI worker can reach an endpoint without verified protected authority"
                    raise ValueError(msg)

    results = await asyncio.gather(
        check_route(f"{launch.primary_url}/api/config/raw"),
        check_route(f"{launch.primary_url}/v1/models"),
        *(check_route(f"{origin}/api/sandbox-runner/workers", peer=True) for origin in launch.control_urls),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    # Authentication failure on a known existing gateway route proves forwarding;
    # a 404 on an arbitrary path cannot establish a working isolation boundary.
    response = await client.post(f"{launch.gateway_url}/api/agent-cli/operations", json={})
    if response.status_code != 401:
        msg = "CLI gateway does not expose the authenticated operations route"
        raise ValueError(msg)
    denied = (
        ("GET", "/api/config/raw"),
        ("POST", "/api/config/load"),
        ("GET", "/api/sandbox-runner/workers"),
        ("GET", "/v1/models"),
        ("GET", "/docs"),
        ("GET", "/api/agent-cli/operations"),
        ("POST", "/api/agent-cli/calls/probe"),
        ("CONNECT", "/"),
        ("GET", "/api/agent-cli/%2e%2e/config/raw"),
        ("GET", "/api/agent-cli/calls/probe%2f..%2f..%2fconfig/raw"),
    )
    for method, path in denied:
        response = await client.request(method, f"{launch.gateway_url}{path}")
        if response.status_code not in {403, 404, 405}:
            msg = "CLI gateway must forward only CLI operation and receipt routes"
            raise ValueError(msg)
