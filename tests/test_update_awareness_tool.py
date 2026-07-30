"""Tests for daily-cached MindRoom update awareness."""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError
from typing import TYPE_CHECKING
from unittest.mock import patch

from agno.agent import Agent
from agno.agent._tools import parse_tools
from agno.models.openai import OpenAIChat
from agno.run import RunContext
from agno.session import AgentSession

from mindroom.constants import resolve_runtime_paths
from mindroom.custom_tools.update_awareness import (
    _RELEASE_CACHE_TTL_SECONDS,
    UpdateAwarenessTools,
    _current_mindroom_version,
    _mindroom_release_status,
    _MindRoomReleaseStatus,
    _ReleaseLookupError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )


def test_current_version_is_unknown_when_distribution_metadata_is_missing() -> None:
    """A missing installed distribution should not prevent agent startup."""
    with patch(
        "mindroom.custom_tools.update_awareness.installed_distribution_version",
        side_effect=PackageNotFoundError("mindroom"),
    ):
        assert _current_mindroom_version() == "unknown"


def test_release_status_uses_persistent_cache_within_daily_ttl(tmp_path: Path) -> None:
    """A successful lookup should be reused until the daily TTL expires."""
    runtime_paths = _runtime_paths(tmp_path)
    fetches: list[str] = []

    def fetch_latest() -> str:
        fetches.append("fetch")
        return "2.0.0"

    with patch("mindroom.custom_tools.update_awareness.installed_distribution_version", return_value="1.0.0"):
        first = _mindroom_release_status(runtime_paths, now=1000, fetch_latest_version=fetch_latest)
        with patch.dict("mindroom.custom_tools.update_awareness._MEMORY_CACHE", clear=True):
            second = _mindroom_release_status(runtime_paths, now=2000, fetch_latest_version=fetch_latest)

    assert (
        first
        == second
        == _MindRoomReleaseStatus(
            current_version="1.0.0",
            latest_version="2.0.0",
            update_available=True,
            release_check_succeeded=True,
        )
    )
    assert fetches == ["fetch"]
    cache_payload = json.loads((runtime_paths.storage_root / "cache" / "update_awareness.json").read_text())
    assert cache_payload == {
        "checked_at": 1000,
        "latest_version": "2.0.0",
        "refresh_succeeded": True,
    }


def test_release_status_refreshes_after_daily_ttl(tmp_path: Path) -> None:
    """An expired cached release should trigger one new lookup."""
    runtime_paths = _runtime_paths(tmp_path)
    versions = iter(("1.0.0", "1.1.0"))

    with patch("mindroom.custom_tools.update_awareness.installed_distribution_version", return_value="1.0.0"):
        first = _mindroom_release_status(runtime_paths, now=1000, fetch_latest_version=lambda: next(versions))
        refreshed = _mindroom_release_status(
            runtime_paths,
            now=1000 + _RELEASE_CACHE_TTL_SECONDS + 1,
            fetch_latest_version=lambda: next(versions),
        )

    assert first.update_available is False
    assert refreshed.latest_version == "1.1.0"
    assert refreshed.update_available is True


def test_failed_refresh_is_cached_and_preserves_last_known_release(tmp_path: Path) -> None:
    """A failed daily lookup should retain stale data without retrying every turn."""
    runtime_paths = _runtime_paths(tmp_path)
    fetches = 0

    def fail_fetch() -> str:
        nonlocal fetches
        fetches += 1
        message = "offline"
        raise _ReleaseLookupError(message)

    with patch("mindroom.custom_tools.update_awareness.installed_distribution_version", return_value="1.0.0"):
        _mindroom_release_status(runtime_paths, now=1000, fetch_latest_version=lambda: "2.0.0")
        failed = _mindroom_release_status(
            runtime_paths,
            now=1000 + _RELEASE_CACHE_TTL_SECONDS + 1,
            fetch_latest_version=fail_fetch,
        )
        cached_failure = _mindroom_release_status(
            runtime_paths,
            now=1000 + _RELEASE_CACHE_TTL_SECONDS + 2,
            fetch_latest_version=fail_fetch,
        )

    assert (
        failed
        == cached_failure
        == _MindRoomReleaseStatus(
            current_version="1.0.0",
            latest_version="2.0.0",
            update_available=True,
            release_check_succeeded=False,
        )
    )
    assert fetches == 1


def test_tool_adds_release_awareness_to_system_prompt(tmp_path: Path) -> None:
    """The toolkit should render cached release status into Agno's system prompt."""
    status = _MindRoomReleaseStatus(
        current_version="1.0.0",
        latest_version="2.0.0",
        update_available=True,
        release_check_succeeded=True,
    )
    with patch("mindroom.custom_tools.update_awareness._mindroom_release_status", return_value=status):
        toolkit = UpdateAwarenessTools(runtime_paths=_runtime_paths(tmp_path))

    model = OpenAIChat(id="test")
    agent = Agent(id="assistant", model=model, tools=[toolkit], instructions=["Help the user."])
    parse_tools(agent, [toolkit], model)
    message = agent.get_system_message(
        session=AgentSession(session_id="session", agent_id="assistant"),
        run_context=RunContext(run_id="run", session_id="session", session_state={}),
        tools=None,
        add_session_state_to_context=False,
    )

    assert message is not None
    assert "This runtime is running MindRoom 1.0.0." in str(message.content)
    assert "The latest published MindRoom release on PyPI is 2.0.0." in str(message.content)
    assert "A newer MindRoom release is available." in str(message.content)
    assert json.loads(toolkit.get_mindroom_update_status()) == {
        "current_version": "1.0.0",
        "latest_version": "2.0.0",
        "release_check_succeeded": True,
        "update_available": True,
    }
