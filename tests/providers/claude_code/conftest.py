"""Keep Claude Code processes from leaking between tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest_asyncio

from mindroom.providers.claude_code import session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest_asyncio.fixture(autouse=True)
async def _no_processes_left() -> AsyncIterator[None]:
    yield
    await session.SESSIONS.close_all()
    assert (session.SESSIONS.live, session.SESSIONS.starting) == (set(), 0)
