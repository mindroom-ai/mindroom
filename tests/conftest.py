from collections.abc import AsyncGenerator

import pytest_asyncio
from aioresponses import aioresponses

__all__ = ["aioresponse"]


@pytest_asyncio.fixture
async def aioresponse() -> AsyncGenerator[aioresponses, None]:
    # Based on https://github.com/matrix-nio/matrix-nio/blob/main/tests/conftest_async.py
    with aioresponses() as m:
        yield m
