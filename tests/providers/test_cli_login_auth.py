"""The Codex and Kimi routes borrow their CLI's OAuth login: reuse a valid token, refresh an expired one once."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.providers.openai import codex_auth, kimi_auth
from tests.providers.openai_family_support import unsigned_jwt

if TYPE_CHECKING:
    from collections.abc import Callable


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


def _answer_refresh(monkeypatch: pytest.MonkeyPatch, reply: httpx.Response | httpx.HTTPError) -> None:
    """Answer the CLI's token refresh call with ``reply``: an HTTP response, or a transport failure."""

    def post(_url: str, **_kwargs: object) -> httpx.Response:
        if isinstance(reply, httpx.HTTPError):
            raise reply
        return reply

    monkeypatch.setattr(httpx, "post", post)


# Refresh answers a CLI login cannot use; the expected message tells the user how to recover.
_FAILED_REFRESHES = [
    pytest.param(httpx.ConnectError("connection refused"), "refresh failed: connection refused", id="unreachable"),
    pytest.param(httpx.Response(500, text="upstream down"), r"refresh failed \(HTTP 500\): upstream down", id="status"),
    pytest.param(httpx.Response(200, text="<html>"), "refresh returned invalid JSON", id="invalid-json"),
    pytest.param(httpx.Response(200, json={"token_type": "Bearer"}), "did not include an access token", id="no-token"),
    pytest.param(httpx.Response(200, json=["new-access"]), "refresh returned an unexpected payload", id="list"),
]


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
    access_token = unsigned_jwt({"exp": int(time.time()) + 3600})
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, access_token, "refresh-value")

    assert codex_auth.borrow_codex_key(codex_home=codex_home) == (access_token, "acct_123")


def test_an_expired_codex_token_is_refreshed_and_saved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The refreshed tokens are written back, so the Codex CLI keeps working with the rotated refresh token."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, unsigned_jwt({"exp": int(time.time()) - 60}), "refresh-value")
    refreshed_token = unsigned_jwt({"exp": int(time.time()) + 7200})
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
    _write_codex_auth(codex_home, unsigned_jwt({"exp": int(time.time()) - 60}), "refresh-value")
    refreshed_token = unsigned_jwt({"exp": int(time.time()) + 7200})
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


def test_a_codex_token_that_is_not_a_jwt_is_refreshed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A token whose expiry cannot be read counts as expired, so the login refreshes instead of failing later."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, "opaque-token", "refresh-value")
    refreshed_token = unsigned_jwt({"exp": int(time.time()) + 7200})
    _answer_refresh(monkeypatch, httpx.Response(200, json={"access_token": refreshed_token}))

    assert codex_auth.borrow_codex_key(codex_home=codex_home) == (refreshed_token, "acct_123")


@pytest.mark.parametrize(
    ("auth", "error"),
    [
        (None, "Codex auth file not found"),
        ({"auth_mode": "apikey", "OPENAI_API_KEY": "key-value"}, "must use ChatGPT OAuth auth_mode"),
        ({"auth_mode": "chatgpt", "tokens": {"refresh_token": "refresh-value"}}, "No ChatGPT access token"),
        (
            {"auth_mode": "chatgpt", "tokens": {"access_token": unsigned_jwt({"exp": int(time.time()) - 60})}},
            "No Codex refresh token",
        ),
    ],
    ids=["no-login", "api-key-login", "no-access-token", "expired-without-refresh-token"],
)
def test_an_unusable_codex_login_tells_the_user_to_log_in(
    tmp_path: Path,
    auth: dict[str, object] | None,
    error: str,
) -> None:
    """A login that cannot yield a token fails with the reason, instead of sending a request without credentials."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    if auth is not None:
        (codex_home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

    with pytest.raises(codex_auth.CodexAuthError, match=error):
        codex_auth.borrow_codex_key(codex_home=codex_home)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        *_FAILED_REFRESHES,
        *(
            pytest.param(
                httpx.Response(401, json={"error": code}),
                rf"refresh token is no longer valid \({code}\)\. Run `codex login` again",
                id=code,
            )
            for code in ("refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated")
        ),
        pytest.param(
            httpx.Response(401, json=["refresh_token_expired"]),
            r"refresh failed \(HTTP 401\)",
            id="error-list",
        ),
    ],
)
def test_a_failed_codex_refresh_keeps_the_saved_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: httpx.Response | httpx.HTTPError,
    error: str,
) -> None:
    """Nothing is written when the refresh fails, so a later ``codex login`` or retry starts from the same file."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, unsigned_jwt({"exp": int(time.time()) - 60}), "refresh-value")
    saved = (codex_home / "auth.json").read_text(encoding="utf-8")
    _answer_refresh(monkeypatch, reply)

    with pytest.raises(codex_auth.CodexAuthError, match=error):
        codex_auth.borrow_codex_key(codex_home=codex_home)

    assert (codex_home / "auth.json").read_text(encoding="utf-8") == saved


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


def test_a_kimi_refresh_keeps_an_absolute_expiry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A refresh answer's absolute ``expires_at`` is saved as it is."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "old-access", "refresh-value", expires_at=int(time.time()) - 60)
    expires_at = int(time.time()) + 3600
    _answer_refresh(monkeypatch, httpx.Response(200, json={"access_token": "new-access", "expires_at": expires_at}))

    assert kimi_auth.borrow_kimi_token(kimi_home=kimi_home) == "new-access"

    credentials = json.loads((kimi_home / "credentials" / "kimi-code.json").read_text(encoding="utf-8"))
    assert credentials["expires_at"] == expires_at


@pytest.mark.parametrize(
    ("credentials", "error"),
    [
        (None, "Kimi Code credentials not found"),
        ({"refresh_token": "refresh-value"}, "No Kimi Code access token"),
        ({"access_token": "access-value"}, "No Kimi Code refresh token"),
    ],
    ids=["no-login", "no-access-token", "no-expiry-and-no-refresh-token"],
)
def test_an_unusable_kimi_login_tells_the_user_to_log_in(
    tmp_path: Path,
    credentials: dict[str, object] | None,
    error: str,
) -> None:
    """A login that cannot yield a token fails with the reason, instead of sending a request without credentials."""
    credentials_dir = tmp_path / ".kimi-code" / "credentials"
    credentials_dir.mkdir(parents=True)
    if credentials is not None:
        (credentials_dir / "kimi-code.json").write_text(json.dumps(credentials), encoding="utf-8")

    with pytest.raises(kimi_auth.KimiAuthError, match=error):
        kimi_auth.borrow_kimi_token(kimi_home=tmp_path / ".kimi-code")


@pytest.mark.parametrize(
    ("reply", "error"),
    _FAILED_REFRESHES,
)
def test_a_failed_kimi_refresh_keeps_the_saved_login(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reply: httpx.Response | httpx.HTTPError,
    error: str,
) -> None:
    """Nothing is written when the refresh fails, so a later ``/login`` or retry starts from the same file."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "old-access", "refresh-value", expires_at=int(time.time()) - 60)
    credentials_path = kimi_home / "credentials" / "kimi-code.json"
    saved = credentials_path.read_text(encoding="utf-8")
    _answer_refresh(monkeypatch, reply)

    with pytest.raises(kimi_auth.KimiAuthError, match=error):
        kimi_auth.borrow_kimi_token(kimi_home=kimi_home)

    assert credentials_path.read_text(encoding="utf-8") == saved
