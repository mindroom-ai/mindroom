"""Keep Claude Code processes from leaking between tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.providers.claude_code import session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture(autouse=True)
def _clear_parent_sdk_settings(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Keep developer SDK settings out of fake-CLI tests, but leave live tests alone."""
    if request.path.name != "test_live.py":
        monkeypatch.delenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", raising=False)
        monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)


@pytest_asyncio.fixture(autouse=True)
async def _no_processes_left() -> AsyncIterator[None]:
    yield
    await session.SESSIONS.close_all()
    assert (session.SESSIONS.live, session.SESSIONS.starting) == (set(), 0)
