"""Tests for the mindroom-stack compose smoke script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from scripts import smoke_mindroom_stack

if TYPE_CHECKING:
    import pytest


def test_smoke_mindroom_stack_uses_env_port_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The smoke script should drive current mindroom-stack ports via env vars."""
    stack_dir = tmp_path / "mindroom-stack"
    stack_dir.mkdir()
    compose_file = stack_dir / "compose.yaml"
    compose_file.write_text("services: {}\n", encoding="utf-8")

    commands: list[list[str]] = []
    wait_match_calls: list[tuple[str, str, str]] = []
    wait_status_calls: list[tuple[str, int, str]] = []
    captured_env_text: str | None = None

    def fake_run_command(
        command: list[str],
        *,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        del check, capture_output
        nonlocal captured_env_text
        commands.append(command)
        env_file = Path(command[command.index("--env-file") + 1])
        if captured_env_text is None and "up" in command:
            captured_env_text = env_file.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    def fake_wait_for_http_match(url: str, expected: str, label: str, **_: object) -> None:
        wait_match_calls.append((url, expected, label))

    def fake_wait_for_http_status(url: str, expected_status: int, label: str, **_: object) -> None:
        wait_status_calls.append((url, expected_status, label))

    monkeypatch.setattr(smoke_mindroom_stack, "run_command", fake_run_command)
    monkeypatch.setattr(smoke_mindroom_stack, "wait_for_http_match", fake_wait_for_http_match)
    monkeypatch.setattr(smoke_mindroom_stack, "wait_for_http_status", fake_wait_for_http_status)
    monkeypatch.setattr(sys, "argv", ["smoke_mindroom_stack.py", str(stack_dir)])

    assert smoke_mindroom_stack.main() == 0

    assert captured_env_text is not None
    assert "HOST_HOMESERVER_PORT=18008" in captured_env_text
    assert "HOST_DASHBOARD_PORT=18765" in captured_env_text
    assert "HOST_CLIENT_PORT=18080" in captured_env_text
    assert "CLIENT_HOMESERVER_URL=http://localhost:18008" in captured_env_text
    assert "CLIENT_MINDROOM_URL=http://localhost:18765" in captured_env_text

    up_command = next(command for command in commands if "up" in command)
    assert up_command[up_command.index("-f") + 1] == str(compose_file)

    wait_match_urls = [url for url, _, _ in wait_match_calls]
    assert "http://127.0.0.1:18008/_matrix/client/versions" in wait_match_urls
    assert "http://127.0.0.1:18765/" in wait_match_urls
    assert "http://127.0.0.1:18080/config.json" in wait_match_urls
    assert all("/api/ready" not in url for url in wait_match_urls)
    assert "http://127.0.0.1:18008/_matrix/client/v3/directory/room/%23lobby%3Amatrix.localhost" in wait_match_urls
    assert "http://127.0.0.1:18008/_matrix/client/v3/directory/room/%23personal%3Amatrix.localhost" in wait_match_urls

    assert wait_status_calls == [
        ("http://127.0.0.1:18080/", 200, "MindRoom client"),
    ]
