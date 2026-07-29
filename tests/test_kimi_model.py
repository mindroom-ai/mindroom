"""Tests for the Kimi Code CLI OAuth-backed chat model provider."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from agno.models.message import Message

from mindroom import kimi_model
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.kimi_model import (
    _KIMI_BASE_URL,
    KimiChat,
    _borrow_kimi_token,
    _kimi_home_path,
    normalize_kimi_model_id,
)
from mindroom.model_loading import get_model_instance
from mindroom.tool_system.worker_routing import ToolExecutionIdentity


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


@pytest.mark.parametrize(
    ("configured_id", "endpoint_id"),
    [
        ("k3", "k3"),
        ("kimi-code/k3", "k3"),
        ("k3-256k", "k3-256k"),
        ("kimi-code/kimi-for-coding", "kimi-for-coding"),
    ],
)
def test_normalize_kimi_model_id_strips_cli_prefix(configured_id: str, endpoint_id: str) -> None:
    """The kimi-code/ prefix from CLI config model names should resolve to the bare slug."""
    assert normalize_kimi_model_id(configured_id) == endpoint_id


def test_kimi_home_expands_explicit_tilde(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit Kimi home paths should expand a user-home prefix like the default path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    assert _kimi_home_path(kimi_home="~/custom-kimi") == tmp_path / "custom-kimi"


def test_borrow_kimi_token_uses_unexpired_access_token(tmp_path: Path) -> None:
    """A valid Kimi Code CLI token should be reused directly."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "access-value", "refresh-value", expires_at=int(time.time()) + 3600)

    assert _borrow_kimi_token(kimi_home=kimi_home) == "access-value"


def test_borrow_kimi_token_refreshes_expired_access_token(tmp_path: Path) -> None:
    """Expired Kimi Code CLI tokens should be refreshed and persisted."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "old-access", "refresh-value", expires_at=int(time.time()) - 60)
    refreshed_access = "new-access"
    rotated_refresh = "new-refresh-value"

    with patch(
        "mindroom.kimi_model._refresh_kimi_tokens",
        return_value={
            "access_token": refreshed_access,
            "refresh_token": rotated_refresh,
            "expires_in": 900,
        },
    ):
        token = _borrow_kimi_token(kimi_home=kimi_home)

    credentials = json.loads((kimi_home / "credentials" / "kimi-code.json").read_text(encoding="utf-8"))
    assert token == refreshed_access
    assert credentials["access_token"] == refreshed_access
    assert credentials["refresh_token"] == rotated_refresh
    assert credentials["expires_at"] > int(time.time())


def test_write_kimi_credentials_creates_private_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refreshed Kimi OAuth tokens should never be written through a world-readable temp file."""
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    credentials_path = credentials_dir / "kimi-code.json"
    observed_temp_modes: list[int] = []
    original_replace = Path.replace

    def spy_replace(self: Path, target: str | Path) -> Path:
        if self.name == "kimi-code.json.tmp":
            observed_temp_modes.append(stat.S_IMODE(self.stat().st_mode))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    old_umask = os.umask(0o022)
    try:
        kimi_model._write_kimi_credentials(credentials_path, {"access_token": "token"})
    finally:
        os.umask(old_umask)

    assert observed_temp_modes == [0o600]
    assert stat.S_IMODE(credentials_path.stat().st_mode) == 0o600


def test_borrow_kimi_token_serializes_concurrent_refreshes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent expired-token readers should share one refreshed token instead of racing refresh-token rotation."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "old-access", "refresh-value", expires_at=int(time.time()) - 60)
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    refresh_call_count = 0
    refresh_call_count_lock = threading.Lock()
    results: list[str] = []
    errors: list[BaseException] = []

    def fake_refresh(received_refresh: str) -> dict[str, object]:
        nonlocal refresh_call_count
        assert received_refresh == "refresh-value"
        with refresh_call_count_lock:
            refresh_call_count += 1
        refresh_started.set()
        assert release_refresh.wait(timeout=2)
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh-value",
            "expires_in": 900,
        }

    def borrow_token() -> None:
        try:
            results.append(_borrow_kimi_token(kimi_home=kimi_home))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(kimi_model, "_refresh_kimi_tokens", fake_refresh)

    first_thread = threading.Thread(target=borrow_token)
    first_thread.start()
    assert refresh_started.wait(timeout=2)

    second_thread = threading.Thread(target=borrow_token)
    second_thread.start()
    release_refresh.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert refresh_call_count == 1
    assert results == ["new-access", "new-access"]


def test_kimi_chat_client_params_use_kimi_endpoint_and_borrowed_token(tmp_path: Path) -> None:
    """KimiChat should translate Kimi Code CLI auth into OpenAI client params."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "access-value", "refresh-value", expires_at=int(time.time()) + 3600)

    model = KimiChat(id="k3", kimi_home=str(kimi_home))

    params = model._get_client_params()

    assert params["api_key"] == "access-value"
    assert params["base_url"] == _KIMI_BASE_URL


def test_kimi_chat_client_params_refresh_expired_tokens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each client-params build should re-read the OAuth state so expired tokens refresh between requests."""
    kimi_home = tmp_path / ".kimi-code"
    _write_kimi_credentials(kimi_home, "first-access", "refresh-value", expires_at=int(time.time()) + 3600)
    model = KimiChat(id="k3", kimi_home=str(kimi_home))

    assert model._get_client_params()["api_key"] == "first-access"

    _write_kimi_credentials(kimi_home, "second-access", "refresh-value", expires_at=int(time.time()) + 3600)
    monkeypatch.setattr(kimi_model, "_KIMI_REFRESH_SKEW_SECONDS", 7200)

    with patch(
        "mindroom.kimi_model._refresh_kimi_tokens",
        return_value={"access_token": "refreshed-access", "expires_in": 900},
    ):
        assert model._get_client_params()["api_key"] == "refreshed-access"


def test_kimi_chat_keeps_system_role_for_kimi_endpoint() -> None:
    """Agno's system-to-developer role mapping must not leak to the Kimi Code endpoint."""
    model = KimiChat(id="k3")
    wire = model._format_message(Message(role="system", content="Be helpful."))
    assert wire["role"] == "system"


def test_kimi_chat_request_params_include_prompt_cache_key() -> None:
    """KimiChat should pin requests to its prompt-cache namespace like the Kimi Code CLI."""
    model = KimiChat(id="k3", prompt_cache_key="mindroom-code-agent")
    assert model.get_request_params()["prompt_cache_key"] == "mindroom-code-agent"

    model_without_key = KimiChat(id="k3")
    assert "prompt_cache_key" not in model_without_key.get_request_params()


def test_kimi_model_loader_derives_prompt_cache_key_from_execution_identity(tmp_path: Path) -> None:
    """MindRoom should use a stable per-agent/session Kimi cache key by default."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "default": ModelConfig(
                provider="kimi",
                id="k3",
            ),
        },
        agents={},
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="code",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id="$thread:example.org",
        resolved_thread_id="$thread:example.org",
        session_id="!room:example.org:$thread:example.org",
    )

    model = get_model_instance(config, runtime_paths, execution_identity=identity)

    assert isinstance(model, KimiChat)
    assert model.prompt_cache_key == "mindroom-7ac97f304c4001bd9939c88ddba8b0e2"
    assert model.get_request_params()["prompt_cache_key"] == "mindroom-7ac97f304c4001bd9939c88ddba8b0e2"


@pytest.mark.parametrize("provider", ["kimi", "kimi_code"])
def test_get_model_instance_supports_kimi_provider(tmp_path: Path, provider: str) -> None:
    """The model loader should expose Kimi as a first-class model provider."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "default": ModelConfig(
                provider=provider,
                id="kimi-code/k3",
            ),
        },
        agents={},
    )

    with patch("mindroom.model_loading.logger.info") as log_info:
        model = get_model_instance(config, runtime_paths)

    assert isinstance(model, KimiChat)
    assert model.id == "k3"
    assert str(model.base_url) == _KIMI_BASE_URL
    log_info.assert_called_once_with(
        "Using AI model",
        model="default",
        provider=provider,
        configured_id="kimi-code/k3",
        effective_id="k3",
    )
