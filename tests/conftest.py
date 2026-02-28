"""Test configuration and fixtures for MindRoom tests."""

import os
from collections.abc import AsyncGenerator, Generator
from unittest.mock import patch

import pytest
import pytest_asyncio
from aioresponses import aioresponses

__all__ = ["TEST_ACCESS_TOKEN", "TEST_PASSWORD", "FakeCredentialsManager", "aioresponse", "bypass_authorization"]


class FakeCredentialsManager:
    """Stub credentials manager for tests that need credential lookup."""

    def __init__(self, credentials_by_service: dict[str, dict[str, object]]) -> None:
        self._credentials_by_service = credentials_by_service

    def load_credentials(self, service: str) -> dict[str, object]:
        """Return stored credentials for *service*, or empty dict."""
        return self._credentials_by_service.get(service, {})


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip tests marked with requires_matrix unless MATRIX_SERVER_URL is set."""
    if os.environ.get("MATRIX_SERVER_URL"):
        # Matrix server available, don't skip
        return

    skip_marker = pytest.mark.skip(reason="requires_matrix: no MATRIX_SERVER_URL set")
    for item in items:
        if "requires_matrix" in item.keywords:
            item.add_marker(skip_marker)


# Test credentials constants - not real credentials, safe for testing
TEST_PASSWORD = "mock_test_password"  # noqa: S105
TEST_ACCESS_TOKEN = "mock_test_token"  # noqa: S105


@pytest_asyncio.fixture
async def aioresponse() -> AsyncGenerator[aioresponses, None]:
    """Async fixture for mocking HTTP responses in tests."""
    # Based on https://github.com/matrix-nio/matrix-nio/blob/main/tests/conftest_async.py
    with aioresponses() as m:
        yield m


@pytest.fixture(autouse=True)
def _pin_matrix_homeserver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure config.domain resolves to 'localhost' for all tests.

    Tests use ':localhost' Matrix IDs.  Without this, an env-level
    MATRIX_HOMESERVER (e.g. pointing at a staging server) would cause
    agent_name() domain checks to fail.
    """
    monkeypatch.setattr("mindroom.config.main.MATRIX_HOMESERVER", "http://localhost:8008")


@pytest.fixture(autouse=True)
def bypass_authorization(request: pytest.FixtureRequest) -> Generator[None, None, None]:
    """Bypass authorization checks in tests by default.

    This allows test users like @user:example.com to interact with agents
    without needing to be in the authorized_users list.

    Tests in test_authorization.py are excluded since they test authorization itself.
    """
    # Don't bypass authorization for tests that are specifically testing it
    if "test_authorization" in request.node.parent.name:
        yield
    else:
        with patch("mindroom.bot.is_authorized_sender", return_value=True):
            yield
