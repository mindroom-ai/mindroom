"""Login-shell environment capture for desktop commands, using fake shells instead of the real profile."""

from __future__ import annotations

import asyncio
import contextlib
import os
import pwd
import signal
import time
from typing import TYPE_CHECKING

import pytest

from mindroom.desktop.login_environment import capture_login_environment

if TYPE_CHECKING:
    from pathlib import Path

ACCOUNT = pwd.getpwuid(os.getuid())


def _fake_shell(tmp_path: Path, body: str) -> str:
    shell = tmp_path / "fake-login-shell"
    shell.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    shell.chmod(0o700)
    return str(shell)


def _fallback_path() -> str:
    prepends = ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", f"{ACCOUNT.pw_dir}/.local/bin"]
    return ":".join([*prepends, "/usr/bin", "/bin", "/usr/sbin", "/sbin"])


@pytest.fixture(autouse=True)
def helper_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the helper values that must never reach commands, plus allowlisted ones that may."""
    monkeypatch.setenv("MINDROOM_HELPER_SECRET", "helper-only")
    monkeypatch.setenv("PATH", "/helper/bin")
    monkeypatch.setenv("HOME", "/helper/home")
    monkeypatch.setenv("TMPDIR", "/helper/tmp")
    monkeypatch.setenv("LANG", "en_US.UTF-8")


@pytest.mark.asyncio
async def test_login_shell_environment_is_parsed_after_the_sentinel(tmp_path: Path) -> None:
    """Profile noise is ignored, profile exports arrive, and only the clean base reaches the login shell."""
    body = (
        f'printf "%s\\n" "$@" > {tmp_path / "args"}\n'
        f"/usr/bin/env > {tmp_path / 'base-env'}\n"
        "printf 'profile noise FAKE=spoofed\\0'\n"
        "export MINDROOM_PROFILE_VALUE='from profile'\n"
        "export PATH=/profile/bin:$PATH\n"
        'exec /bin/sh -c "$4"'
    )
    environment = await capture_login_environment(shell=_fake_shell(tmp_path, body))

    args = (tmp_path / "args").read_text().splitlines()
    assert args[:3] == ["-l", "-i", "-c"]
    base = dict(line.split("=", 1) for line in (tmp_path / "base-env").read_text().splitlines())
    for shell_state in ("PWD", "OLDPWD", "SHLVL", "_"):
        base.pop(shell_state, None)
    assert base == {
        "HOME": ACCOUNT.pw_dir,
        "USER": ACCOUNT.pw_name,
        "LOGNAME": ACCOUNT.pw_name,
        "SHELL": ACCOUNT.pw_shell,
        "TMPDIR": "/helper/tmp",
        "LANG": "en_US.UTF-8",
        "PATH": _fallback_path(),
    }
    assert environment["MINDROOM_PROFILE_VALUE"] == "from profile"
    assert environment["PATH"] == f"/profile/bin:{_fallback_path()}"
    assert environment["HOME"] == ACCOUNT.pw_dir
    assert "FAKE" not in environment
    assert "MINDROOM_HELPER_SECRET" not in environment
    assert not {"PWD", "OLDPWD", "SHLVL", "_"} & set(environment)


@pytest.mark.asyncio
async def test_environment_values_may_contain_newlines(tmp_path: Path) -> None:
    """NUL-separated capture keeps multi-line values intact."""
    body = "export MINDROOM_MULTILINE='first\nsecond=third'\nexec /bin/sh -c \"$4\""
    environment = await capture_login_environment(shell=_fake_shell(tmp_path, body))
    assert environment["MINDROOM_MULTILINE"] == "first\nsecond=third"


def _expected_fallback() -> dict[str, str]:
    return {
        "HOME": ACCOUNT.pw_dir,
        "USER": ACCOUNT.pw_name,
        "LOGNAME": ACCOUNT.pw_name,
        "SHELL": ACCOUNT.pw_shell,
        "TMPDIR": "/helper/tmp",
        "LANG": "en_US.UTF-8",
        "PATH": _fallback_path(),
    }


@pytest.mark.parametrize(
    "body",
    [
        "printf 'no sentinel here'\nexit 0",
        'printf "%s" "$4" > /dev/null\nexit 3',
        "exec /bin/sh -c 'printf MINDROOM_LOGIN_ENVIRONMENT_; exit 1'",
        'head -c 11000000 /dev/zero | tr "\\000" x\nexec /bin/sh -c "$4"',
    ],
    ids=["no-sentinel", "failing-profile", "spoofed-sentinel", "oversized-output"],
)
@pytest.mark.asyncio
async def test_failed_capture_uses_the_fixed_allowlist(tmp_path: Path, body: str) -> None:
    """Without a trustworthy environment dump, commands get the fixed allowlist and Homebrew/user PATH."""
    assert await capture_login_environment(shell=_fake_shell(tmp_path, body)) == _expected_fallback()


@pytest.mark.asyncio
async def test_missing_login_shell_uses_the_fixed_allowlist(tmp_path: Path) -> None:
    """A missing or unusable login shell falls back instead of failing startup."""
    assert await capture_login_environment(shell=str(tmp_path / "missing-shell")) == _expected_fallback()


async def _wait_for_path(path: Path, *, timeout_seconds: float) -> bool:
    """Poll for *path* to appear; under xdist load the writer can lag its own process start."""
    deadline = time.monotonic() + timeout_seconds
    while not path.exists():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


@pytest.mark.asyncio
async def test_hanging_profile_is_bounded_and_its_process_group_killed(tmp_path: Path) -> None:
    """A profile that never finishes is stopped at the bound, including its background helpers."""
    # Own pid written first, before forking the background helper, so the write that matters most
    # under scheduling pressure has the best chance to land before the capture bound fires.
    body = f"echo $$ > {tmp_path / 'shell.pid'}\nsleep 30 & echo $! > {tmp_path / 'helper.pid'}\nexec sleep 30"
    # A generous bound (well above the 0.5s that flaked under xdist load) so a contended CI host
    # still reliably schedules the fake profile's first instructions before capture falls back.
    capture_timeout = 3.0
    started = time.monotonic()
    try:
        environment = await capture_login_environment(
            shell=_fake_shell(tmp_path, body),
            timeout_seconds=capture_timeout,
        )
        assert environment == _expected_fallback()
        assert time.monotonic() - started < capture_timeout + 10
        for name in ("helper.pid", "shell.pid"):
            path = tmp_path / name
            if not await _wait_for_path(path, timeout_seconds=5.0):
                pytest.fail(
                    f"{name} never appeared: the fake profile was never scheduled before the "
                    f"{capture_timeout}s capture bound, even under load",
                )
            pid = int(path.read_text())
            for _ in range(300):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail(f"{name} survived the bounded capture")
    finally:
        for name in ("helper.pid", "shell.pid"):
            if (tmp_path / name).exists():
                with contextlib.suppress(ProcessLookupError, ValueError):
                    os.kill(int((tmp_path / name).read_text()), signal.SIGKILL)
