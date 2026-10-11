"""Authenticated Home Assistant API requests that renew expired OAuth access tokens."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx


async def send_homeassistant_request(
    client: httpx.AsyncClient,
    fetch_url: str,
    config: dict[str, Any],
    method: str,
    endpoint: str,
    *,
    save_config: Callable[[dict[str, Any]], None],
    json_data: dict[str, Any] | None = None,
) -> httpx.Response:
    """Send one request with the stored token, renewing a rejected OAuth access token once.

    The renewed token is written into ``config``, so later requests with it use the new token, and saved with ``save_config``.
    Long-lived tokens and failed renewals return the rejected response.
    """

    async def send() -> httpx.Response:
        token = config.get("access_token") or config.get("long_lived_token")
        return await client.request(
            method=method,
            url=urljoin(fetch_url, endpoint),
            headers={"Authorization": f"Bearer {token}"},
            json=json_data,
            timeout=10.0,
        )

    response = await send()
    refresh_token = config.get("refresh_token")
    client_id = config.get("client_id")
    if response.status_code != 401 or not refresh_token or not client_id:
        return response
    renewal = await client.post(
        urljoin(fetch_url, "/auth/token"),
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
        timeout=10.0,
    )
    if renewal.status_code != 200:
        return response
    config["access_token"] = renewal.json()["access_token"]
    save_config(config)
    return await send()
