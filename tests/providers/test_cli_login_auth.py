"""The Codex and Kimi routes borrow their CLI's OAuth login: reuse a valid token, refresh an expired one once."""

from __future__ import annotations

import base64
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.providers.openai import codex_auth, kimi_auth

if TYPE_CHECKING:
    from collections.abc import Callable


def _jwt_with_exp(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _write_codex_auth(codex_home: Path, access_token: str, refresh_value: str) -> None:
    codex_home.mkdir()
    tokens = {"access_token": access_token, "refresh_token": refresh_value, "account_id": "acct_123"}
    (codex_home / "auth.json").write_text(json.dumps({"auth_mode": "chatgpt", "tokens": tokens}), encoding="utf-8")


def _write_kimi_credentials(kimi_home: Path, access_token: str, refresh_value: str, *, expires_at: int) -> None:
    credentials_dir = kimi_home / "credentials"
    credentials_dir.mkdir(parents=True, exist_ok=True)
    credentials = {
        "access_token": access_token,
        "refresh_token": refresh_value,
        "expires_at": expires_at,
        "expires_in": 900,
        "token_type": "Bearer",
        "scope": "kimi-code",
    }
    (credentials_dir / "kimi-code.json").write_text(json.dumps(credentials), encoding="utf-8")


def _temp_file_modes(monkeypatch: pytest.MonkeyPatch, temp_name: str) -> list[int]:
    """Record the mode of ``temp_name`` at the moment it replaces the credentials file."""
    observed: list[int] = []
    original_replace = Path.replace

    def spy_replace(self: Path, target: str | Path) -> Path:
        if self.name == temp_name:
            observed.append(stat.S_IMODE(self.stat().st_mode))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    return observed


def _borrow_concurrently[T](
    borrow: Callable[[], T],
    refresh_started: threading.Event,
    release_refresh: threading.Event,
) -> list[T]:
    """Borrow twice from two threads, the second while the first is refreshing, and return both results."""
    results: list[T] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(borrow())
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=run)
    first.start()
    assert refresh_started.wait(timeout=2)
    second = threading.Thread(target=run)
    second.start()
    release_refresh.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    return results


# --- Codex --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured_id", "endpoint_id"),
    [
        ("gpt-6-astra", "gpt-6-astra"),
        ("openai-codex/gpt-6-astra", "gpt-6-astra"),
        ("gpt-5.6", "gpt-5.6-sol"),
        ("openai-codex/gpt-5.6", "gpt-5.6-sol"),
        ("gpt-5.6-sol", "gpt-5.6-sol"),
        ("openai-codex/gpt-5.6-terra", "gpt-5.6-terra"),
        ("gpt-5.4", "gpt-5.4"),
    ],
)
def test_codex_model_ids_resolve_to_the_endpoint_slug(configured_id: str, endpoint_id: str) -> None:
    """The plugin-style prefix is dropped and the public GPT-5.6 alias resolves to the slug Codex accepts."""
    assert codex_auth.normalize_codex_model_id(configured_id) == endpoint_id


def test_codex_home_expands_an_explicit_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit Codex home expands a user-home prefix like the default path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert codex_auth._codex_home_path(codex_home="~/custom-codex") == tmp_path / "custom-codex"


def test_a_valid_codex_token_is_reused(tmp_path: Path) -> None:
    """An unexpired ChatGPT access token is borrowed as it is, with its account id."""
    access_token = _jwt_with_exp(int(time.time()) + 3600)
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, access_token, "refresh-value")

    assert codex_auth.borrow_codex_key(codex_home=codex_home) == (access_token, "acct_123")


def test_an_expired_codex_token_is_refreshed_and_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refreshed tokens are written back, so the Codex CLI keeps working with the rotated refresh token."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, _jwt_with_exp(int(time.time()) - 60), "refresh-value")
    refreshed_token = _jwt_with_exp(int(time.time()) + 7200)
    new_id, new_refresh = "new-id-value", "new-refresh-value"
    refreshed = {"access_token": refreshed_token, "id_token": new_id, "refresh_token": new_refresh}
    monkeypatch.setattr(codex_auth, "_refresh_codex_tokens", lambda _refresh: refreshed)

    assert codex_auth.borrow_codex_key(codex_home=codex_home) == (refreshed_token, "acct_123")

    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert auth["tokens"]["access_token"] == refreshed_token
    assert auth["tokens"]["id_token"] == new_id
    assert auth["tokens"]["refresh_token"] == new_refresh
    assert "last_refresh" in auth


def test_refreshed_codex_tokens_are_never_world_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The temporary file the refreshed tokens go through is private from the start, as is the auth file."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    observed = _temp_file_modes(monkeypatch, "auth.json.tmp")
    old_umask = os.umask(0o022)
    try:
        codex_auth._write_codex_auth(auth_path, {"auth_mode": "chatgpt", "tokens": {"access_token": "token"}})
    finally:
        os.umask(old_umask)

    assert observed == [0o600]
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600


def test_concurrent_codex_borrowers_share_one_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second borrower waits for the first one's refresh instead of racing the refresh-token rotation."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, _jwt_with_exp(int(time.time()) - 60), "refresh-value")
    refreshed_token = _jwt_with_exp(int(time.time()) + 7200)
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    refreshes: list[str] = []

    def fake_refresh(received_refresh: str) -> dict[str, str]:
        refreshes.append(received_refresh)
        refresh_started.set()
        assert release_refresh.wait(timeout=2)
        return {"access_token": refreshed_token, "refresh_token": "new-refresh-value"}

    monkeypatch.setattr(codex_auth, "_refresh_codex_tokens", fake_refresh)

    results = _borrow_concurrently(
        lambda: codex_auth.borrow_codex_key(codex_home=codex_home),
        refresh_started,
        release_refresh,
    )

    assert refreshes == ["refresh-value"]
    assert results == [(refreshed_token, "acct_123"), (refreshed_token, "acct_123")]


# --- Kimi ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured_id", "endpoint_id"),
    [("k3", "k3"), ("kimi-code/k3", "k3"), ("k3-256k", "k3-256k"), ("kimi-code/kimi-for-coding", "kimi-for-coding")],
)
def test_kimi_model_ids_drop_the_cli_prefix(configured_id: str, endpoint_id: str) -> None:
    """The kimi-code/ prefix from CLI config model names resolves to the bare slug."""
    assert kimi_auth.normalize_kimi_model_id(configured_id) == endpoint_id


def test_kimi_home_expands_an_explicit_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit Kimi home expands a user-home prefix like the default path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert kimi_auth._kimi_home_path(kimi_home="~/custom-kimi") == tmp_path / "custom-kimi"


def test_a_valid_kimi_token_is_reused(tmp_path: Path) -> None:
    """An unexpired Kimi Code CLI token is borrowed as it is."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "access-value", "refresh-value", expires_at=int(time.time()) + 3600)

    assert kimi_auth.borrow_kimi_token(kimi_home=kimi_home) == "access-value"


def test_an_expired_kimi_token_is_refreshed_and_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refreshed token, its rotated refresh token, and its new expiry are written back."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "old-access", "refresh-value", expires_at=int(time.time()) - 60)
    new_access, new_refresh = "new-access", "new-refresh-value"
    refreshed = {"access_token": new_access, "refresh_token": new_refresh, "expires_in": 900}
    monkeypatch.setattr(kimi_auth, "_refresh_kimi_tokens", lambda _refresh: refreshed)

    assert kimi_auth.borrow_kimi_token(kimi_home=kimi_home) == new_access

    credentials = json.loads((kimi_home / "credentials" / "kimi-code.json").read_text(encoding="utf-8"))
    assert credentials["access_token"] == new_access
    assert credentials["refresh_token"] == new_refresh
    assert credentials["expires_at"] > int(time.time())


def test_refreshed_kimi_tokens_are_never_world_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The temporary file the refreshed credentials go through is private from the start, as is the file itself."""
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    credentials_path = credentials_dir / "kimi-code.json"
    observed = _temp_file_modes(monkeypatch, "kimi-code.json.tmp")
    old_umask = os.umask(0o022)
    try:
        kimi_auth._write_kimi_credentials(credentials_path, {"access_token": "token"})
    finally:
        os.umask(old_umask)

    assert observed == [0o600]
    assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600


def test_concurrent_kimi_borrowers_share_one_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A second borrower waits for the first one's refresh instead of racing the refresh-token rotation."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "old-access", "refresh-value", expires_at=int(time.time()) - 60)
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    refreshes: list[str] = []

    def fake_refresh(received_refresh: str) -> dict[str, object]:
        refreshes.append(received_refresh)
        refresh_started.set()
        assert release_refresh.wait(timeout=2)
        return {"access_token": "new-access", "refresh_token": "new-refresh-value", "expires_in": 900}

    monkeypatch.setattr(kimi_auth, "_refresh_kimi_tokens", fake_refresh)

    results = _borrow_concurrently(
        lambda: kimi_auth.borrow_kimi_token(kimi_home=kimi_home),
        refresh_started,
        release_refresh,
    )

    assert refreshes == ["refresh-value"]
    assert results == ["new-access", "new-access"]
