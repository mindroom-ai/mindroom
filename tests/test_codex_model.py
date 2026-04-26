"""Tests for the Codex-backed OpenAI Responses model provider."""

from __future__ import annotations

import base64
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse

from mindroom import codex_model
from mindroom.codex_model import CODEX_BASE_URL, CodexResponses, borrow_codex_key
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.model_loading import get_model_instance
from mindroom.tool_system.worker_routing import ToolExecutionIdentity

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest


def _jwt_with_exp(exp: int) -> str:
    payload = json.dumps({"exp": exp}).encode()
    encoded_payload = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"header.{encoded_payload}.signature"


def _write_codex_auth(codex_home: Path, access_token: str, refresh_value: str) -> None:
    codex_home.mkdir()
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_value,
            "account_id": "acct_123",
        },
    }
    (codex_home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")


def test_borrow_codex_key_uses_unexpired_chatgpt_access_token(tmp_path: Path) -> None:
    """A valid Codex CLI ChatGPT token should be reused directly."""
    access_token = _jwt_with_exp(int(time.time()) + 3600)
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, access_token, "refresh-value")

    token, account_id = borrow_codex_key(codex_home=codex_home)

    assert token == access_token
    assert account_id == "acct_123"


def test_borrow_codex_key_refreshes_expired_access_token(tmp_path: Path) -> None:
    """Expired Codex CLI ChatGPT tokens should be refreshed and persisted."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, _jwt_with_exp(int(time.time()) - 60), "refresh-value")
    refreshed_token = _jwt_with_exp(int(time.time()) + 7200)
    new_id_value = "new-id-value"
    new_refresh_value = "new-refresh-value"

    with patch(
        "mindroom.codex_model._refresh_codex_tokens",
        return_value={
            "access_token": refreshed_token,
            "id_token": new_id_value,
            "refresh_token": new_refresh_value,
        },
    ):
        token, account_id = borrow_codex_key(codex_home=codex_home)

    auth = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert token == refreshed_token
    assert account_id == "acct_123"
    assert auth["tokens"]["access_token"] == refreshed_token
    assert auth["tokens"]["id_token"] == new_id_value
    assert auth["tokens"]["refresh_token"] == new_refresh_value
    assert "last_refresh" in auth


def test_write_codex_auth_creates_private_temp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refreshed Codex OAuth tokens should never be written through a world-readable temp file."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    auth_path = codex_home / "auth.json"
    observed_temp_modes: list[int] = []
    original_replace = Path.replace

    def spy_replace(self: Path, target: str | Path) -> Path:
        if self.name == "auth.json.tmp":
            observed_temp_modes.append(stat.S_IMODE(self.stat().st_mode))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)
    old_umask = os.umask(0o022)
    try:
        codex_model._write_codex_auth(auth_path, {"auth_mode": "chatgpt", "tokens": {"access_token": "token"}})
    finally:
        os.umask(old_umask)

    assert observed_temp_modes == [0o600]
    assert stat.S_IMODE(auth_path.stat().st_mode) == 0o600


def test_borrow_codex_key_serializes_concurrent_refreshes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent expired-token readers should share one refreshed token instead of racing refresh-token rotation."""
    codex_home = tmp_path / ".codex"
    _write_codex_auth(codex_home, _jwt_with_exp(int(time.time()) - 60), "refresh-value")
    refreshed_token = _jwt_with_exp(int(time.time()) + 7200)
    refresh_started = threading.Event()
    release_refresh = threading.Event()
    refresh_call_count = 0
    refresh_call_count_lock = threading.Lock()
    results: list[tuple[str, str | None]] = []
    errors: list[BaseException] = []

    def fake_refresh(received_refresh: str) -> dict[str, str]:
        nonlocal refresh_call_count
        assert received_refresh == "refresh-value"
        with refresh_call_count_lock:
            refresh_call_count += 1
        refresh_started.set()
        assert release_refresh.wait(timeout=2)
        return {
            "access_token": refreshed_token,
            "refresh_token": "new-refresh-value",
        }

    def borrow_key() -> None:
        try:
            results.append(borrow_codex_key(codex_home=codex_home))
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(codex_model, "_refresh_codex_tokens", fake_refresh)

    first_thread = threading.Thread(target=borrow_key)
    first_thread.start()
    assert refresh_started.wait(timeout=2)

    second_thread = threading.Thread(target=borrow_key)
    second_thread.start()
    release_refresh.set()
    first_thread.join(timeout=2)
    second_thread.join(timeout=2)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert errors == []
    assert refresh_call_count == 1
    assert results == [(refreshed_token, "acct_123"), (refreshed_token, "acct_123")]


def test_codex_responses_client_params_use_codex_endpoint_and_account_header(tmp_path: Path) -> None:
    """CodexResponses should translate Codex CLI auth into OpenAI client params."""
    codex_home = tmp_path / ".codex"
    access_token = _jwt_with_exp(int(time.time()) + 3600)
    _write_codex_auth(codex_home, access_token, "refresh-value")

    model = CodexResponses(id="gpt-5.5", codex_home=str(codex_home), default_headers={"X-Test": "1"})

    params = model._get_client_params()

    assert params["api_key"] == access_token
    assert params["base_url"] == CODEX_BASE_URL
    assert params["default_headers"] == {
        "X-Test": "1",
        "ChatGPT-Account-ID": "acct_123",
    }


def test_codex_responses_request_params_include_required_instructions() -> None:
    """Codex Responses requests should always include top-level instructions."""
    default_model = CodexResponses(id="gpt-5.5")
    configured_model = CodexResponses(id="gpt-5.5", instructions=["Be brief.", "Return plain text."])

    assert default_model.get_request_params()["instructions"] == "You are a helpful assistant."
    assert configured_model.get_request_params()["instructions"] == "Be brief.\n\nReturn plain text."


def test_codex_responses_request_params_drop_unsupported_limits() -> None:
    """Unsupported OpenAI Responses parameters should not be sent to Codex."""
    model = CodexResponses(id="gpt-5.5", max_output_tokens=40)

    assert "max_output_tokens" not in model.get_request_params()


def test_codex_responses_request_params_include_prompt_cache_key(tmp_path: Path) -> None:
    """Codex should expose OpenAI's cache-routing key when configured."""
    model = CodexResponses(id="gpt-5.5", prompt_cache_key="mindroom-code-agent", codex_home=str(tmp_path))

    params = model.get_request_params()

    assert params["prompt_cache_key"] == "mindroom-code-agent"
    assert params["extra_headers"] == {
        "session_id": "mindroom-code-agent",
        "x-client-request-id": "mindroom-code-agent",
        "x-codex-window-id": "mindroom-code-agent:0",
    }


def test_codex_responses_request_params_include_installation_metadata(tmp_path: Path) -> None:
    """Codex should forward the local CLI installation id in Responses client_metadata."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "installation_id").write_text("install-123\n", encoding="utf-8")
    model = CodexResponses(id="gpt-5.5", prompt_cache_key="mindroom-code-agent", codex_home=str(codex_home))

    params = model.get_request_params()

    assert params["extra_body"] == {
        "client_metadata": {
            "x-codex-installation-id": "install-123",
        },
    }


def test_codex_responses_request_params_preserve_existing_extra_body(tmp_path: Path) -> None:
    """Codex client metadata should merge into caller-supplied extra_body."""
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "installation_id").write_text("install-123\n", encoding="utf-8")
    model = CodexResponses(
        id="gpt-5.5",
        prompt_cache_key="mindroom-code-agent",
        codex_home=str(codex_home),
        extra_body={
            "debug": True,
            "client_metadata": {
                "x-codex-installation-id": "custom-install",
                "x-test": "1",
            },
        },
    )

    assert model.get_request_params()["extra_body"] == {
        "debug": True,
        "client_metadata": {
            "x-codex-installation-id": "custom-install",
            "x-test": "1",
        },
    }


def test_codex_responses_request_params_preserve_existing_extra_headers(tmp_path: Path) -> None:
    """Codex prompt-cache headers should not clobber caller-supplied headers."""
    model = CodexResponses(
        id="gpt-5.5",
        prompt_cache_key="mindroom-code-agent",
        codex_home=str(tmp_path),
        extra_headers={"X-Test": "1", "x-codex-window-id": "custom-window"},
    )

    assert model.get_request_params()["extra_headers"] == {
        "X-Test": "1",
        "session_id": "mindroom-code-agent",
        "x-client-request-id": "mindroom-code-agent",
        "x-codex-window-id": "custom-window",
    }


def test_codex_model_loader_derives_prompt_cache_key_from_execution_identity(tmp_path: Path) -> None:
    """MindRoom should use a stable per-agent/session Codex cache key by default."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "default": ModelConfig(
                provider="codex",
                id="gpt-5.5",
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
    params = model.get_request_params()

    assert isinstance(model, CodexResponses)
    assert params["prompt_cache_key"] == "mindroom-7ac97f304c4001bd9939c88ddba8b0e2"
    assert params["extra_headers"] == {
        "session_id": "mindroom-7ac97f304c4001bd9939c88ddba8b0e2",
        "x-client-request-id": "mindroom-7ac97f304c4001bd9939c88ddba8b0e2",
        "x-codex-window-id": "mindroom-7ac97f304c4001bd9939c88ddba8b0e2:0",
    }


def test_codex_responses_invoke_aggregates_streaming_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-streaming callers should still work with Codex's stream-only endpoint."""
    model = CodexResponses(id="gpt-5.5")
    usage = MessageMetrics(input_tokens=7, output_tokens=3, total_tokens=10)

    def fake_invoke_stream(
        *,
        messages: list[Message],
        assistant_message: Message,
        response_format: object | None = None,
        tools: list[dict[str, object]] | None = None,
        tool_choice: str | dict[str, object] | None = None,
        run_response: object | None = None,
        compress_tool_results: bool = False,
    ) -> Iterator[ModelResponse]:
        del messages, response_format, tools, tool_choice, run_response, compress_tool_results
        model._ensure_message_metrics_initialized(assistant_message)
        yield ModelResponse(provider_data={"response_id": "resp_123"})
        yield ModelResponse(content="mindroom")
        yield ModelResponse(content="-codex-live-ok")
        yield ModelResponse(response_usage=usage)

    monkeypatch.setattr(model, "invoke_stream", fake_invoke_stream)
    assistant_message = Message(role="assistant")

    response = model.invoke([Message(role="user", content="hello")], assistant_message)

    assert response.content == "mindroom-codex-live-ok"
    assert response.provider_data == {"response_id": "resp_123"}
    assert response.response_usage == usage
    assert assistant_message.content == "mindroom-codex-live-ok"
    assert assistant_message.provider_data == {"response_id": "resp_123"}
    assert assistant_message.metrics.input_tokens == 7


def test_get_model_instance_supports_codex_provider(tmp_path: Path) -> None:
    """The model loader should expose Codex as a first-class model provider."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    config = Config(
        models={
            "default": ModelConfig(
                provider="codex",
                id="openai-codex/gpt-5.5",
            ),
        },
        agents={},
    )

    model = get_model_instance(config, runtime_paths)

    assert isinstance(model, CodexResponses)
    assert model.id == "gpt-5.5"
    assert model.store is False
    assert str(model.base_url) == CODEX_BASE_URL
