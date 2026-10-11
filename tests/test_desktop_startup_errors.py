"""Desktop recovery distinguishes missing sessions from failed server operations."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import aiohttp
import nio
import pytest

from mindroom.desktop.session import DesktopSessionError, DesktopSessionNotFoundError, prepare_desktop_client
from mindroom.desktop.startup_errors import desktop_startup_error


@pytest.mark.asyncio
async def test_key_upload_rejection_does_not_request_signin_repair() -> None:
    """A server operation failure alone does not invalidate the saved login."""
    client = SimpleNamespace(
        olm=object(),
        should_upload_keys=True,
        keys_upload=AsyncMock(return_value=nio.KeysUploadError("Try later", "M_LIMIT_EXCEEDED")),
    )
    with pytest.raises(DesktopSessionError) as caught:
        await prepare_desktop_client(cast("nio.AsyncClient", client))

    error = desktop_startup_error(caught.value)
    assert error.code == "internal_error"
    assert "sign-in" not in (error.recovery or "")
    assert "Try later" in str(error)


def test_missing_session_requires_signin_repair() -> None:
    """Positive evidence of a missing session gets sign-in recovery."""
    error = desktop_startup_error(DesktopSessionNotFoundError("Saved session missing"))
    assert error.code == "session_missing"
    assert "sign-in" in (error.recovery or "")


@pytest.mark.parametrize("suppress", [False, True])
def test_implicit_transport_context_respects_explicit_suppression(suppress: bool) -> None:
    """Implicit causes are classified, except when a wrapper deliberately hides them."""
    failure = RuntimeError("Startup failed")
    failure.__context__ = aiohttp.ClientConnectionError("Connection closed")
    failure.__suppress_context__ = suppress
    error = desktop_startup_error(failure)
    assert error.code == ("internal_error" if suppress else "connection_failed")
