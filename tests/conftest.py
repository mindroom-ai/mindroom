"""Test configuration and fixtures for MindRoom tests."""

import os
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from aioresponses import aioresponses

from mindroom.config.main import Config

__all__ = [
    "TEST_ACCESS_TOKEN",
    "TEST_PASSWORD",
    "FakeCredentialsManager",
    "aioresponse",
    "build_private_template_dir",
    "bypass_authorization",
    "create_mock_room",
]


class FakeCredentialsManager:
    """Stub credentials manager for tests that need credential lookup."""

    def __init__(
        self,
        credentials_by_service: dict[str, dict[str, object]],
        worker_managers: dict[str, "FakeCredentialsManager"] | None = None,
        *,
        storage_root: Path | None = None,
    ) -> None:
        self._credentials_by_service = credentials_by_service
        self._worker_managers = worker_managers or {}
        self.storage_root = storage_root or Path("/var/empty/mindroom-fake-storage")
        self.base_path = self.storage_root / "credentials"
        self.shared_base_path = self.base_path

    def load_credentials(self, service: str) -> dict[str, object]:
        """Return stored credentials for *service*, or empty dict."""
        return self._credentials_by_service.get(service, {})

    def for_worker(self, worker_key: str) -> "FakeCredentialsManager":
        """Return a worker-scoped credentials manager."""
        return self._worker_managers.get(
            worker_key,
            FakeCredentialsManager(
                {},
                storage_root=self.storage_root / "workers" / worker_key,
            ),
        )

    def shared_manager(self) -> "FakeCredentialsManager":
        """Return the shared credential layer for this fake manager."""
        return self


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


def create_mock_room(
    room_id: str = "!test:localhost",
    agents: list[str] | None = None,
    config: Config | None = None,
) -> MagicMock:
    """Create a mock room with specified agents."""
    room = MagicMock()
    room.room_id = room_id
    if agents:
        domain = config.domain if config else "localhost"
        room.users = {f"@mindroom_{agent}:{domain}": None for agent in agents}
    else:
        room.users = {}
    return room


@pytest.fixture
def build_private_template_dir(tmp_path: Path) -> Callable[..., Path]:
    """Return a helper that creates a local private-instance template directory."""

    def _build(
        name: str = "private_template",
        *,
        files: dict[str, str] | None = None,
    ) -> Path:
        template_dir = tmp_path / name
        template_dir.mkdir(parents=True, exist_ok=True)
        template_files = files or {
            "SOUL.md": "Template soul.\n",
            "USER.md": "Template user.\n",
            "MEMORY.md": "# Memory\n",
            "memory/notes.md": "Private note.\n",
        }
        for relative_path, content in template_files.items():
            destination = template_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
        return template_dir

    return _build


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
