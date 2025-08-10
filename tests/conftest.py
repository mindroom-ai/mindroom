"""Test configuration and fixtures for MindRoom tests."""

from collections.abc import AsyncGenerator

import pytest_asyncio
from aioresponses import aioresponses

__all__ = ["TEST_ACCESS_TOKEN", "TEST_MEMORY_DIR", "TEST_PASSWORD", "TEST_TMP_DIR", "aioresponse"]

# Test credentials constants - not real credentials, safe for testing
TEST_PASSWORD = "mock_test_password"  # noqa: S105
TEST_ACCESS_TOKEN = "mock_test_token"  # noqa: S105

# Test directory constants - safe temporary directories for testing
import tempfile

TEST_TMP_DIR = tempfile.gettempdir() + "/mindroom_test"
TEST_MEMORY_DIR = tempfile.gettempdir() + "/mindroom_test_memory"


@pytest_asyncio.fixture
async def aioresponse() -> AsyncGenerator[aioresponses, None]:
    # Based on https://github.com/matrix-nio/matrix-nio/blob/main/tests/conftest_async.py
    with aioresponses() as m:
        yield m
