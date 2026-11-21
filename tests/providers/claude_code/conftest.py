"""Keep the ``claude_code`` route's tests apart: no inherited Anthropic settings, and no process left running."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
import pytest_asyncio

from mindroom.providers.claude_code import session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.fixture(autouse=True)
def _no_inherited_anthropic_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear what the shell exports for other Anthropic routes, which this route refuses to start beside."""
    for name in session.ENVIRONMENT_OVERRIDES:
        monkeypatch.delenv(name, raising=False)


@pytest_asyncio.fixture(autouse=True)
async def _no_processes_left() -> AsyncIterator[None]:
    yield
    await session.SESSIONS.close_all()
    assert session.SESSIONS.slots == 0
