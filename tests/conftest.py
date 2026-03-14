"""Test configuration and fixtures for MindRoom tests."""

import itertools
import os
from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from aioresponses import aioresponses

from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths

__all__ = [
    "TEST_ACCESS_TOKEN",
    "TEST_PASSWORD",
    "FakeCredentialsManager",
    "aioresponse",
    "bypass_authorization",
    "create_mock_room",
    "orchestrator_runtime_paths",
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


def _make_test_runtime_paths(tmp_root: Path) -> RuntimePaths:
    """Create an isolated runtime context for one test config."""
    config_path = tmp_root / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_root / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


def orchestrator_runtime_paths(
    storage_path: Path,
    *,
    config_path: Path | None = None,
) -> RuntimePaths:
    """Build an explicit runtime context for orchestrator tests."""
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=storage_path,
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
        },
    )


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


@pytest_asyncio.fixture
async def aioresponse() -> AsyncGenerator[aioresponses, None]:
    """Async fixture for mocking HTTP responses in tests."""
    # Based on https://github.com/matrix-nio/matrix-nio/blob/main/tests/conftest_async.py
    with aioresponses() as m:
        yield m


@pytest.fixture(autouse=True)
def _pin_matrix_homeserver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep test runtime defaults isolated from shell-level runtime overrides.

    Tests use ':localhost' Matrix IDs and non-namespaced localparts unless they
    explicitly opt into a different runtime context.
    """
    monkeypatch.delenv("MATRIX_HOMESERVER", raising=False)
    monkeypatch.delenv("MATRIX_SERVER_NAME", raising=False)
    monkeypatch.delenv("MINDROOM_NAMESPACE", raising=False)
    monkeypatch.delenv("MINDROOM_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MINDROOM_STORAGE_PATH", raising=False)


@pytest.fixture(autouse=True)
def _reset_runtime_paths() -> Generator[None, None, None]:
    """Restore runtime-synced process env after each test."""
    from mindroom import constants  # noqa: PLC0415

    original_env = os.environ.copy()
    original_synced_env = dict(constants._RUNTIME_SYNCED_ENV_VALUES)
    yield
    os.environ.clear()
    os.environ.update(original_env)
    constants._replace_runtime_synced_env(original_synced_env)


@pytest.fixture(autouse=True)
def _bind_runtime_paths_to_test_configs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Attach a real runtime context to test-created Config objects by default."""
    counter = itertools.count()
    original_init = Config.__init__
    original_model_validate = Config.model_validate.__func__

    def _bind_runtime_paths(config: Config) -> None:
        if config.runtime_paths is not None:
            return
        runtime_root = tmp_path_factory.mktemp(f"config-runtime-{next(counter)}")
        config._runtime_paths = _make_test_runtime_paths(runtime_root)

    def patched_init(self: Config, /, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        _bind_runtime_paths(self)

    @classmethod
    def patched_model_validate(
        cls: type[Config],
        obj: object,
        *args: object,
        **kwargs: object,
    ) -> Config:
        config = original_model_validate(cls, obj, *args, **kwargs)
        _bind_runtime_paths(config)
        return config

    monkeypatch.setattr(Config, "__init__", patched_init)
    monkeypatch.setattr(Config, "model_validate", patched_model_validate)


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
