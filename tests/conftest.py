"""Test configuration and fixtures for MindRoom tests."""

from collections.abc import AsyncGenerator

import pytest_asyncio
from aioresponses import aioresponses

__all__ = ["TEST_ACCESS_TOKEN", "TEST_PASSWORD", "aioresponse"]


# Test credentials constants - not real credentials, safe for testing
TEST_PASSWORD = "mock_test_password"  # noqa: S105
TEST_ACCESS_TOKEN = "mock_test_token"  # noqa: S105


@pytest_asyncio.fixture
async def aioresponse() -> AsyncGenerator[aioresponses, None]:
    """Async fixture for mocking HTTP responses in tests."""
    # Based on https://github.com/matrix-nio/matrix-nio/blob/main/tests/conftest_async.py
    with aioresponses() as m:
        yield m
