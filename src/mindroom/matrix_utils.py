"""Utility functions and helpers for Matrix operations."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import nio


@asynccontextmanager
async def matrix_client(
    homeserver: str,
    user_id: str | None = None,
    access_token: str | None = None,
) -> AsyncGenerator[nio.AsyncClient, None]:
    """Context manager for Matrix client that ensures proper cleanup.

    Args:
        homeserver: The Matrix homeserver URL
        user_id: Optional user ID for authenticated client
        access_token: Optional access token for authenticated client

    Yields:
        nio.AsyncClient: The Matrix client instance

    Example:
        async with matrix_client("http://localhost:8008") as client:
            response = await client.login(password="secret")
    """
    if access_token:
        client = nio.AsyncClient(homeserver, user_id, store_path=".nio_store")
        client.access_token = access_token
    else:
        client = nio.AsyncClient(homeserver, user_id)

    try:
        yield client
    finally:
        await client.close()
