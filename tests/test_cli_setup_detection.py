"""Tests for smart first-run / `config init` provider auto-detection, live key checks, inline login, and starter setups."""

from __future__ import annotations

import base64
import json
import stat
import sys
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from mindroom import constants as constants_module
from mindroom.cli import config as config_cli
from mindroom.cli import setup_detection
from mindroom.cli.main import app
from mindroom.cli.setup_detection import (
    DetectedProvider,
    DetectionSnapshot,
    KeyCheckResult,
    OllamaModelInfo,
    detect_available_providers,
    detect_single_provider,
    run_inline_cli_login,
    verify_provider_api_key,
)
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT, OPENAI_BASE_URL_DEFAULT
from tests.conftest import load_config_yaml

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

runner = CliRunner()


def _make_jwt(payload: dict[str, Any]) -> str:
    """Build an unsigned JWT token with base64url-encoded JSON header and payload."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.sig"


def _runtime(tmp_path: Path, env: dict[str, str] | None = None) -> constants_module.RuntimePaths:
    process_env = {
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "KIMI_CODE_HOME": str(tmp_path / "kimi-home"),
        **(env or {}),
    }
    return constants_module.resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        process_env=process_env,
    )


def _write_fake_cli(bin_dir: Path, name: str, python_body: str) -> Path:
    """Write an executable Python script in ``bin_dir`` for realistic CLI subprocess tests."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / name
    script.write_text(f"#!{sys.executable}\n{python_body}\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _claude_status_cli(bin_dir: Path, payload: dict[str, Any], *, assert_scrubbed: bool = False) -> Path:
    scrub_check = (
        "leaked = [k for k in ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_BG_AUTH_SNAPSHOT_PATH') "
        "if k in os.environ]\n"
        "if leaked:\n"
        "    sys.exit(f'leaked env: {leaked}')\n"
        if assert_scrubbed
        else ""
    )
    return _write_fake_cli(
        bin_dir,
        "claude",
        f"import json, os, sys\nassert sys.argv[1:] == ['auth', 'status']\n{scrub_check}print({json.dumps(json.dumps(payload))})",
    )


def _snapshot(*providers: DetectedProvider) -> DetectionSnapshot:
    return DetectionSnapshot(providers={item.preset: item for item in providers})


class _FakeOllamaClient:
    """Minimal context-manager stand-in for `httpx.Client` in Ollama probe tests."""

    def __init__(
        self,
        response: httpx.Response | Exception,
        captured: list[tuple[str, bool]],
        *,
        trust_env: bool,
    ) -> None:
        self._response = response
        self._captured = captured
        self._trust_env = trust_env

    def __enter__(self) -> _FakeOllamaClient:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def get(self, url: str) -> httpx.Response:
        self._captured.append((url, self._trust_env))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _mock_ollama_client(
    responses: httpx.Response | Exception,
    captured: list[tuple[str, bool]],
) -> Callable[..., _FakeOllamaClient]:
    """Build a fake `httpx.Client` factory that records `(url, trust_env)` for Ollama probe tests."""

    def factory(*, timeout: float, follow_redirects: bool, trust_env: bool = True) -> _FakeOllamaClient:
        assert timeout > 0
        assert follow_redirects is False
        return _FakeOllamaClient(responses, captured, trust_env=trust_env)

    return factory


@pytest.fixture(autouse=True)
def _isolate_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent the developer's shell environment from leaking into setup detection tests."""
    for env_key in (
        *constants_module.PROVIDER_ENV_KEYS.values(),
        constants_module.OWNER_MATRIX_USER_ID_ENV,
        "CODEX_HOME",
        "KIMI_CODE_HOME",
        "OLLAMA_HOST",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
        "OPENROUTER_BASE_URL",
    ):
        monkeypatch.delenv(env_key, raising=False)
        monkeypatch.delenv(f"{env_key}_FILE", raising=False)


class TestProviderDetection:
    """Unit and subprocess tests for local provider detection."""

    def test_codex_detection_variants(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Codex detects valid/refreshable ChatGPT logins, enforces access_token parity, and detects CLI-only."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        rt = _runtime(tmp_path)
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        auth_file = codex_home / "auth.json"

        assert detect_single_provider("codex", rt) is None

        # Valid unexpired access token + id_token plan type -> ready with ChatGPT Pro.
        auth_file.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "access_token": _make_jwt({"exp": int(time.time()) + 3600}),
                        "id_token": _make_jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}}),
                    },
                },
            ),
            encoding="utf-8",
        )
        assert detect_single_provider("codex", rt) == DetectedProvider("codex", True, "ChatGPT Pro")

        # Expired access token + non-empty refresh token -> ready ("signed in").
        auth_file.write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "access_token": _make_jwt({"exp": int(time.time()) - 3600}),
                        "refresh_token": "rt_valid_token",
                    },
                },
            ),
            encoding="utf-8",
        )
        assert detect_single_provider("codex", rt) == DetectedProvider("codex", True, "signed in")

        # Missing access_token (even with refresh_token) fails runtime reader -> not ready.
        auth_file.write_text(
            json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt_only"}}),
            encoding="utf-8",
        )
        assert detect_single_provider("codex", rt) is None

        # Non-chatgpt mode with codex binary installed -> ready=False, CLI installed.
        codex_bin = _write_fake_cli(bin_dir, "codex", "import sys; sys.exit(0)")
        auth_file.write_text(
            json.dumps({"auth_mode": "api_key", "tokens": {"access_token": "x"}}),
            encoding="utf-8",
        )
        assert detect_single_provider("codex", rt) == DetectedProvider(
            "codex",
            False,
            "CLI installed",
            cli_path=str(codex_bin),
        )

    def test_claude_code_detection_scrubs_env_gates_paid_plans_and_handles_timeouts(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Claude probe scrubs env inside subprocess, requires paid subscription plans, and fails open on timeout."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        monkeypatch.setenv("CLAUDE_BG_AUTH_SNAPSHOT_PATH", str(tmp_path / "bad-snapshot"))
        rt = _runtime(tmp_path)

        assert detect_single_provider("claude_code", rt) is None

        # Paid subscription (with in-process scrubbed env assertion — never writes env to disk).
        claude_bin = _claude_status_cli(
            bin_dir,
            {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "max_5x"},
            assert_scrubbed=True,
        )
        assert detect_single_provider("claude_code", rt) == DetectedProvider(
            "claude_code",
            True,
            "Claude Max 5x",
            cli_path=str(claude_bin),
        )

        # Free plan, missing plan, API-key auth, and logged-out are all NOT ready.
        for bad_payload in (
            {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "free"},
            {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"},
            {"loggedIn": True, "authMethod": "apiKey", "apiProvider": "firstParty", "subscriptionType": "pro"},
            {"loggedIn": False},
        ):
            _claude_status_cli(bin_dir, bad_payload)
            assert detect_single_provider("claude_code", rt) == DetectedProvider(
                "claude_code",
                False,
                "CLI installed",
                cli_path=str(claude_bin),
            )

        # Timeout -> fails open to CLI installed.
        _write_fake_cli(bin_dir, "claude", "import time; time.sleep(5)")
        assert detect_single_provider("claude_code", rt, timeout_seconds=0.15) == DetectedProvider(
            "claude_code",
            False,
            "CLI installed",
            cli_path=str(claude_bin),
        )

    def test_kimi_and_env_api_key_detection(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Kimi requires access_token (parity with runtime reader) and env API keys ignore placeholders."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        kimi_creds = tmp_path / "kimi-home" / "credentials" / "kimi-code.json"
        kimi_creds.parent.mkdir(parents=True)

        # Expired access_token + refresh_token -> ready.
        kimi_creds.write_text(json.dumps({"access_token": "expired", "expires_at": 1, "refresh_token": "rt-kimi"}))
        rt = _runtime(
            tmp_path,
            env={
                "OPENAI_API_KEY": "sk-real-openai",
                "ANTHROPIC_API_KEY": "your-anthropic-key-here",
                "OPENROUTER_API_KEY": "sk-or-real",
            },
        )
        assert detect_single_provider("kimi", rt) == DetectedProvider("kimi", True, "signed in")
        assert detect_single_provider("openai", rt) == DetectedProvider("openai", True, "OPENAI_API_KEY set")
        assert detect_single_provider("anthropic", rt) is None
        assert detect_single_provider("openrouter", rt) == DetectedProvider(
            "openrouter",
            True,
            "OPENROUTER_API_KEY set",
        )

        # Refresh token without access_token -> not ready (matches _read_kimi_credentials).
        kimi_creds.write_text(json.dumps({"refresh_token": "rt-only"}))
        assert detect_single_provider("kimi", rt) is None

        # Missing credentials + installed binary -> CLI installed.
        kimi_creds.unlink()
        kimi_bin = _write_fake_cli(bin_dir, "kimi", "import sys; sys.exit(0)")
        assert detect_single_provider("kimi", rt) == DetectedProvider(
            "kimi",
            False,
            "CLI installed",
            cli_path=str(kimi_bin),
        )

    def test_ollama_detection_normalization_trust_env_embedding_filter_and_unreachable(self, tmp_path: Path) -> None:
        """Ollama normalizes bare hosts to :11434, disables trust_env on loopback, and filters embedding-only models."""
        rt = _runtime(tmp_path)
        calls: list[tuple[str, bool]] = []

        # Mixed chat + embedding models -> embedding models filtered out; loopback uses trust_env=False.
        mixed_resp = httpx.Response(
            200,
            json={
                "models": [
                    {"name": "gemma4:latest", "size": 17_200_000_000},
                    {"name": "nomic-embed-text:latest", "size": 274_000_000},
                    {"name": "qwen3.8:27b", "size": 450_000_000},
                    {"name": "bge-m3:latest", "size": 1_200_000_000},
                    {"name": "llama3.2:latest", "size": 2_100_000_000},
                    {"name": "mistral:7b"},
                ],
            },
        )
        with patch.object(setup_detection.httpx, "Client", side_effect=_mock_ollama_client(mixed_resp, calls)):
            detected = detect_single_provider("ollama", rt)
        assert detected is not None
        assert detected.ready is True
        assert [m.name for m in detected.ollama_models] == [
            "gemma4:latest",
            "qwen3.8:27b",
            "llama3.2:latest",
            "mistral:7b",
        ]
        assert detected.status_detail == "gemma4:latest, qwen3.8:27b, +2 more"
        assert detected.ollama_models[0].format_size() == "17.2 GB"
        assert detected.ollama_models[1].format_size() == "450 MB"
        assert detected.ollama_models[3].format_size() is None
        assert calls == [(f"{OLLAMA_HOST_DEFAULT}/api/tags", False)]

        # Bare OLLAMA_HOST=localhost normalizes to :11434 (not :80) with trust_env=False; embedding-only -> not ready.
        calls.clear()
        embed_only = httpx.Response(
            200,
            json={"models": [{"name": "nomic-embed-text:latest"}, {"name": "all-minilm:latest"}, {"name": "e5-large"}]},
        )
        bare_rt = _runtime(tmp_path, env={"OLLAMA_HOST": "localhost"})
        with patch.object(setup_detection.httpx, "Client", side_effect=_mock_ollama_client(embed_only, calls)):
            detected_embed = detect_single_provider("ollama", bare_rt)
        assert detected_embed == DetectedProvider("ollama", False, "running (no chat models pulled)")
        assert calls == [("http://localhost:11434/api/tags", False)]

        # Port-only OLLAMA_HOST=:11435 normalizes to http://127.0.0.1:11435 with trust_env=False (matching Ollama SDK).
        calls.clear()
        empty_resp = httpx.Response(200, json={"models": []})
        port_only_rt = _runtime(tmp_path, env={"OLLAMA_HOST": ":11435"})
        with patch.object(setup_detection.httpx, "Client", side_effect=_mock_ollama_client(empty_resp, calls)):
            assert detect_single_provider("ollama", port_only_rt) == DetectedProvider(
                "ollama",
                False,
                "running (no models pulled)",
            )
        assert calls == [("http://127.0.0.1:11435/api/tags", False)]

        # Explicit non-loopback custom OLLAMA_HOST with 0 models -> trust_env=True, no models pulled.
        calls.clear()
        remote_rt = _runtime(tmp_path, env={"OLLAMA_HOST": "http://ollama.lan:11434/"})
        with patch.object(setup_detection.httpx, "Client", side_effect=_mock_ollama_client(empty_resp, calls)):
            assert detect_single_provider("ollama", remote_rt) == DetectedProvider(
                "ollama",
                False,
                "running (no models pulled)",
            )
        assert calls == [("http://ollama.lan:11434/api/tags", True)]

        # Connection timeout -> fails open to None.
        with patch.object(
            setup_detection.httpx,
            "Client",
            side_effect=_mock_ollama_client(httpx.ConnectTimeout("timeout"), []),
        ):
            assert detect_single_provider("ollama", rt) is None

    def test_parallel_detection_snapshot_and_fail_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`detect_available_providers` aggregates probes in deterministic order and ignores individual probe errors."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        _claude_status_cli(
            bin_dir,
            {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty", "subscriptionType": "pro"},
        )
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {
                        "access_token": _make_jwt({"exp": int(time.time()) - 60}),
                        "refresh_token": "rt-ok",
                    },
                },
            ),
            encoding="utf-8",
        )
        rt = _runtime(tmp_path, env={"OPENAI_API_KEY": "sk-test"})

        with patch.object(setup_detection, "_probe_ollama", side_effect=RuntimeError("boom")):
            snapshot = detect_available_providers(rt)

        assert snapshot.both_claude_and_codex_ready() is True
        assert [preset for preset, info in snapshot.providers.items() if info.ready] == [
            "claude_code",
            "codex",
            "openai",
        ]
        summary = snapshot.summary_line()
        assert summary is not None
        assert "Claude (Claude Pro)" in summary
        assert "Codex (signed in)" in summary
        assert "OpenAI (OPENAI_API_KEY set)" in summary


class TestLiveApiKeyVerification:
    """Tests for read-only provider API key checks, input validation, custom endpoints, and retry loops."""

    def test_verify_provider_api_key_statuses_invalid_chars_and_custom_endpoint(self, tmp_path: Path) -> None:
        """Validates 2xx/401/5xx/network, rejects non-ASCII/control chars, and skips HTTP when base URL is custom."""
        sample_key = "sk-super-secret-value"
        with patch.object(setup_detection.httpx, "get", return_value=httpx.Response(200)) as mock_get:
            res = verify_provider_api_key("openai", sample_key)
        assert res == KeyCheckResult(status="valid")
        assert mock_get.call_args.kwargs["headers"]["Authorization"] == f"Bearer {sample_key}"
        assert mock_get.call_args.kwargs["follow_redirects"] is False

        with patch.object(setup_detection.httpx, "get", return_value=httpx.Response(401)):
            res = verify_provider_api_key("anthropic", sample_key)
        assert res.status == "rejected"
        assert sample_key not in res.detail

        with patch.object(setup_detection.httpx, "get", return_value=httpx.Response(403)):
            forbidden_res = verify_provider_api_key("openai", sample_key)
        assert forbidden_res == KeyCheckResult(status="inconclusive", detail="HTTP 403")

        with patch.object(setup_detection.httpx, "get", return_value=httpx.Response(503)):
            assert verify_provider_api_key("openrouter", sample_key).status == "inconclusive"

        with patch.object(setup_detection.httpx, "get", side_effect=httpx.ConnectError("offline")):
            assert verify_provider_api_key("openai", sample_key).status == "inconclusive"

        # Missing optional proxy backend (e.g. socksio with ALL_PROXY) or unreadable SSL_CERT_FILE fail open.
        for exc in (
            ImportError("Using SOCKS proxy, but the 'socksio' package is not installed."),
            FileNotFoundError("missing CA"),
        ):
            with patch.object(setup_detection.httpx, "get", side_effect=exc):
                assert verify_provider_api_key("openai", sample_key).status == "inconclusive"

        assert verify_provider_api_key("unknown_provider", sample_key).status == "inconclusive"

        # Non-ASCII (smart quote, zero-width space) and control chars are rejected before HTTP without echoing.
        with patch.object(setup_detection.httpx, "get", side_effect=AssertionError("must not send HTTP")):
            for bad_key in ("sk-smart\u2019quote", "sk-zero\u200bwidth", "sk-ctrl\x07char"):
                bad_res = verify_provider_api_key("openai", bad_key)
                assert bad_res.status == "rejected"
                assert "invalid characters" in bad_res.detail
                assert bad_key not in bad_res.detail

        # Stock provider base URL (even with trailing slash) still runs live verification.
        stock_rt = _runtime(tmp_path, env={"OPENAI_BASE_URL": f"{OPENAI_BASE_URL_DEFAULT}/"})
        with patch.object(setup_detection.httpx, "get", return_value=httpx.Response(200)) as stock_get:
            stock_res = verify_provider_api_key("openai", sample_key, runtime_paths=stock_rt)
        assert stock_res == KeyCheckResult(status="valid")
        assert stock_get.call_count == 1

        # Custom provider endpoint env var skips stock key check with zero HTTP calls.
        custom_rt = _runtime(tmp_path, env={"OPENAI_BASE_URL": "https://gateway.internal/v1"})
        with patch.object(setup_detection.httpx, "get", side_effect=AssertionError("must not send HTTP")):
            custom_res = verify_provider_api_key("openai", sample_key, runtime_paths=custom_rt)
        assert custom_res.status == "inconclusive"
        assert "OPENAI_BASE_URL" in custom_res.detail

    def test_rejected_and_non_ascii_keys_retry_until_valid_or_skipped(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Non-ASCII and rejected keys loop cleanly; valid key saves, empty Enter skips with 'Setup created'."""
        rt = _runtime(tmp_path)
        keys = iter(["sk-bad\u2019quote", "sk-good-key"])
        with (
            patch.object(setup_detection, "detect_single_provider", return_value=None),
            patch.object(config_cli, "secret", side_effect=lambda _msg: next(keys)),
            patch.object(setup_detection.httpx, "get", return_value=httpx.Response(200)) as mock_get,
        ):
            config_cli.create_first_run_config(rt, provider="openai", interactive=True)

        # Only the second (valid ASCII) key reached httpx.get.
        assert mock_get.call_count == 1
        out = capsys.readouterr().out
        assert "invalid characters" in out
        assert "Verified key with OpenAI." in out
        assert "Setup ready" in out
        assert "sk-bad\u2019quote" not in out
        assert "sk-good-key" not in out
        assert "OPENAI_API_KEY=sk-good-key\n" in rt.env_path.read_text(encoding="utf-8")

        # Pressing Enter after rejection skips, leaves placeholder, and prints "Setup created" (not "Setup ready").
        rt2 = _runtime(tmp_path / "skip")
        keys2 = iter(["sk-bad-key", ""])
        with (
            patch.object(setup_detection, "detect_single_provider", return_value=None),
            patch.object(config_cli, "secret", side_effect=lambda _msg: next(keys2)),
            patch.object(
                setup_detection,
                "verify_provider_api_key",
                return_value=KeyCheckResult("rejected", "HTTP 401"),
            ),
        ):
            config_cli.create_first_run_config(rt2, provider="openai", interactive=True)
        out2 = capsys.readouterr().out
        assert "Skipped." in out2
        assert "Setup created" in out2
        assert "Setup ready" not in out2
        assert "OPENAI_API_KEY=your-openai-key-here\n" in rt2.env_path.read_text(encoding="utf-8")

    def test_inconclusive_key_and_custom_endpoint_fail_open_and_save(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Network failures and custom base URLs warn and save the typed key without blocking setup."""
        rt = _runtime(tmp_path)
        with (
            patch.object(setup_detection, "detect_single_provider", return_value=None),
            patch.object(config_cli, "secret", return_value="sk-offline-key"),
            patch.object(
                setup_detection,
                "verify_provider_api_key",
                return_value=KeyCheckResult("inconclusive", "Could not reach provider."),
            ),
        ):
            config_cli.create_first_run_config(rt, provider="anthropic", interactive=True)
        assert "Could not verify key right now; saving anyway." in capsys.readouterr().out
        assert "ANTHROPIC_API_KEY=sk-offline-key\n" in rt.env_path.read_text(encoding="utf-8")

        rt_custom = _runtime(tmp_path / "custom", env={"OPENAI_BASE_URL": "https://proxy.example/v1"})
        with (
            patch.object(setup_detection, "detect_single_provider", return_value=None),
            patch.object(config_cli, "secret", return_value="sk-proxy-key"),
            patch.object(setup_detection.httpx, "get", side_effect=AssertionError("must not send HTTP")),
        ):
            config_cli.create_first_run_config(rt_custom, provider="openai", interactive=True)
        assert "OPENAI_BASE_URL" in capsys.readouterr().out
        assert "OPENAI_API_KEY=sk-proxy-key\n" in rt_custom.env_path.read_text(encoding="utf-8")


class TestCombinedClaudeCodexSetup:
    """Tests for combined Claude Code + Codex starter generation and menu ordering."""

    @pytest.mark.parametrize(
        ("default_pick", "expected_default_id", "expected_router", "expected_extra_keys"),
        [
            ("sonnet", "claude-sonnet-5-5", "haiku", {"opus", "haiku", "sol", "astra", "luna"}),
            ("sol", "gpt-6.2-sol", "luna", {"sonnet", "opus", "haiku", "astra", "luna"}),
            ("opus", "claude-opus-5-5", "haiku", {"sonnet", "haiku", "sol", "astra", "luna"}),
        ],
    )
    def test_combined_claude_and_codex_writes_six_valid_models_and_pins_cli_path(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        default_pick: str,
        expected_default_id: str,
        expected_router: str,
        expected_extra_keys: set[str],
    ) -> None:
        """`Claude + Codex` emits all 6 models cleanly, safely YAML-dumps tricky `cli_path`, and loads cleanly."""
        rt = _runtime(tmp_path)
        claude_cli = "/Users/Café's Mac/🚀/bin/claude:v2"
        snapshot = _snapshot(
            DetectedProvider("claude_code", True, "Claude Max", cli_path=claude_cli),
            DetectedProvider("codex", True, "ChatGPT Pro", cli_path="/usr/local/bin/codex"),
        )
        select_calls: list[tuple[str, list[str]]] = []

        def fake_select(message: str, choices: list[Any]) -> str:
            values = [c[0] for c in choices if not isinstance(c, config_cli.Separator)]
            select_calls.append((message, values))
            return default_pick if message == "Default model for Mind?" else values[0]

        with (
            patch.object(setup_detection, "detect_available_providers", return_value=snapshot),
            patch.object(config_cli, "select", side_effect=fake_select),
        ):
            config_cli.create_first_run_config(rt, provider=None, interactive=True)

        assert select_calls[0][1][0] == "claude_code+codex"
        assert select_calls[1] == ("Default model for Mind?", ["sonnet", "sol", "opus"])

        raw_cfg = yaml.safe_load(rt.config_path.read_text(encoding="utf-8"))
        assert set(raw_cfg["models"]) == {"default", *expected_extra_keys}
        assert len(raw_cfg["models"]) == 6
        assert raw_cfg["models"]["default"]["id"] == expected_default_id
        for model in raw_cfg["models"].values():
            if model["provider"] == "claude_code":
                assert model["extra_kwargs"]["cli_path"] == claude_cli
            else:
                assert "cli_path" not in model.get("extra_kwargs", {})

        loaded = load_config_yaml(rt.config_path)
        assert loaded.router.model == expected_router
        assert loaded.defaults.thread_summary_model == expected_router
        assert loaded.models["default"].id == expected_default_id

        env_text = rt.env_path.read_text(encoding="utf-8")
        assert "Claude Code" in env_text
        assert "Codex" in env_text
        assert "your-anthropic-key-here" not in env_text
        assert "your-openai-key-here" not in env_text

        out = capsys.readouterr().out
        assert "Setup ready" in out
        assert "MindRoom keeps starting; your agents can answer once you:" not in out

    def test_smart_menu_ordering_and_empty_snapshot_fallback(self) -> None:
        """Detected ready and installed providers lead above a Separator; empty detection keeps default order."""
        captured: list[list[Any]] = []

        def fake_select(_msg: str, choices: list[Any]) -> str:
            captured.append(list(choices))
            return str(next(c[0] for c in choices if not isinstance(c, config_cli.Separator)))

        with patch.object(config_cli, "select", side_effect=fake_select):
            config_cli._prompt_provider_choice(DetectionSnapshot())
        assert [c[0] for c in captured[0]] == list(config_cli._PROVIDER_PRESETS)
        assert not any(isinstance(c, config_cli.Separator) for c in captured[0])

        snapshot = _snapshot(
            DetectedProvider("claude_code", True, "Claude Max", cli_path="/usr/bin/claude"),
            DetectedProvider("codex", True, "ChatGPT Pro", cli_path="/usr/bin/codex"),
            DetectedProvider("kimi", False, "CLI installed", cli_path="/usr/bin/kimi"),
            DetectedProvider("openai", True, "OPENAI_API_KEY set"),
            DetectedProvider("ollama", True, "gemma4:latest"),
        )
        with patch.object(config_cli, "select", side_effect=fake_select):
            config_cli._prompt_provider_choice(snapshot)
        entries = captured[1]
        sep_idx = next(i for i, c in enumerate(entries) if isinstance(c, config_cli.Separator))
        assert entries[sep_idx] == config_cli.Separator("Other providers")
        above = entries[:sep_idx]
        assert [c[0] for c in above] == ["claude_code+codex", "claude_code", "codex", "openai", "ollama", "kimi"]
        assert "recommended" in above[0][1]
        assert "✓ signed in (Claude Max)" in above[1][1]
        assert "✓ OPENAI_API_KEY set" in above[3][1]
        assert "✓ running (gemma4:latest)" in above[4][1]
        assert "○ CLI installed" in above[5][1]
        assert [c[0] for c in entries[sep_idx + 1 :]] == [
            p for p in config_cli._PROVIDER_PRESETS if p not in snapshot.providers
        ]


class TestInlineCliLogin:
    """Tests for inline `codex login` / `claude auth login` during interactive setup."""

    def test_run_inline_cli_login_scrubs_claude_env_and_forwards_only_codex_home(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`run_inline_cli_login` scrubs Claude overrides and forwards only `CODEX_HOME` (never the rest of `.env`)."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-scrub-me")
        claude_bin = _write_fake_cli(
            bin_dir,
            "claude",
            (
                "import os, sys\n"
                "assert sys.argv[1:] == ['auth', 'login']\n"
                "assert 'ANTHROPIC_API_KEY' not in os.environ\n"
            ),
        )
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("MINDROOM_API_KEY", "shell-key")
        monkeypatch.delenv("DOTENV_ONLY_SENTINEL", raising=False)
        expanded_codex_home = fake_home / "work-codex"
        (tmp_path / ".env").write_text(
            "CODEX_HOME=~/work-codex\nMINDROOM_API_KEY=env-file-key\nDOTENV_ONLY_SENTINEL=secret-from-dotenv\n",
            encoding="utf-8",
        )
        rt = constants_module.resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
        codex_bin = _write_fake_cli(
            bin_dir,
            "codex",
            (
                "import os, sys\n"
                "assert sys.argv[1:] == ['login']\n"
                f"assert os.environ.get('CODEX_HOME') == {str(expanded_codex_home)!r}\n"
                "assert os.environ.get('MINDROOM_API_KEY') == 'shell-key'\n"
                "assert 'DOTENV_ONLY_SENTINEL' not in os.environ\n"
            ),
        )

        assert run_inline_cli_login("claude_code", str(claude_bin), runtime_paths=rt) is True
        assert run_inline_cli_login("codex", str(codex_bin), runtime_paths=rt) is True
        assert run_inline_cli_login("kimi", str(codex_bin), runtime_paths=rt) is False

        # Detection uses the same expanded CODEX_HOME (and CODEX_HOME_FILE) as runtime and inline login.
        expanded_codex_home.mkdir()
        (expanded_codex_home / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "tokens": {"access_token": _make_jwt({"exp": int(time.time()) + 3600})},
                },
            ),
            encoding="utf-8",
        )
        expected_codex = DetectedProvider("codex", True, "signed in", cli_path=str(codex_bin))
        assert detect_single_provider("codex", rt) == expected_codex

        codex_home_file = tmp_path / "codex-home-file"
        codex_home_file.write_text("~/work-codex\n", encoding="utf-8")
        rt_file = constants_module.resolve_primary_runtime_paths(
            config_path=tmp_path / "file-cfg" / "config.yaml",
            process_env={"CODEX_HOME_FILE": str(codex_home_file)},
        )
        assert run_inline_cli_login("codex", str(codex_bin), runtime_paths=rt_file) is True
        assert detect_single_provider("codex", rt_file) == expected_codex

    def test_inline_login_accept_marks_ready_and_decline_shows_hint(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Accepting inline login re-probes with same runtime_paths and suppresses hints; declining shows hint."""
        rt_ok = _runtime(tmp_path / "ok")
        claude_cli = "/opt/homebrew/bin/claude"
        installed_claude = _snapshot(DetectedProvider("claude_code", False, "CLI installed", cli_path=claude_cli))
        ready_claude = DetectedProvider("claude_code", True, "Claude Pro", cli_path=claude_cli)
        with (
            patch.object(setup_detection, "detect_available_providers", return_value=installed_claude),
            patch.object(config_cli, "select", return_value="claude_code"),
            patch.object(config_cli, "confirm", return_value=True),
            patch.object(setup_detection, "run_inline_cli_login", return_value=True) as mock_login,
            patch.object(setup_detection, "detect_single_provider", return_value=ready_claude) as mock_reprobe,
        ):
            config_cli.create_first_run_config(rt_ok, provider=None, interactive=True)

        mock_login.assert_called_once_with("claude_code", claude_cli, runtime_paths=rt_ok)
        mock_reprobe.assert_called_once_with("claude_code", rt_ok)
        out_ok = capsys.readouterr().out
        assert "Signed in to Claude subscription (Claude Pro)." in out_ok
        assert "Setup ready" in out_ok
        assert "MindRoom keeps starting; your agents can answer once you:" not in out_ok
        assert load_config_yaml(rt_ok.config_path).models["default"].extra_kwargs == {"cli_path": claude_cli}

        rt_no = _runtime(tmp_path / "no")
        installed_codex = _snapshot(DetectedProvider("codex", False, "CLI installed", cli_path="/usr/local/bin/codex"))
        with (
            patch.object(setup_detection, "detect_available_providers", return_value=installed_codex),
            patch.object(config_cli, "select", return_value="codex"),
            patch.object(config_cli, "confirm", return_value=False),
            patch.object(setup_detection, "run_inline_cli_login") as mock_login_no,
        ):
            config_cli.create_first_run_config(rt_no, provider=None, interactive=True)

        mock_login_no.assert_not_called()
        out_no = capsys.readouterr().out
        assert "Setup created" in out_no
        assert "MindRoom keeps starting; your agents can answer once you:" in out_no
        assert "codex login" in out_no


class TestOllamaModelDiscoverySetup:
    """Tests for choosing already-pulled Ollama models, YAML round-trips, and custom host retention."""

    def test_ollama_tricky_model_names_round_trip_and_skip_pull_hints(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """YAML-tricky Ollama model names (`yes:latest`, `123:latest`, `123:45`) sanitize safely and load cleanly."""
        rt = _runtime(tmp_path)
        pulled = (
            OllamaModelInfo("yes:latest", 17_200_000_000),
            OllamaModelInfo("123:latest", 4_100_000_000),
            OllamaModelInfo("123:45", 18_500_000_000),
            OllamaModelInfo("@a/b", 9_000_000_000),
        )
        snapshot = _snapshot(DetectedProvider("ollama", True, "yes:latest, 123:latest, +2 more", ollama_models=pulled))
        seen_titles: list[str] = []

        def fake_select(message: str, choices: list[Any]) -> str:
            if message == "Default local model?":
                seen_titles.extend(c[1] for c in choices)
                return "123:45"
            return "ollama"

        with (
            patch.object(setup_detection, "detect_available_providers", return_value=snapshot),
            patch.object(config_cli, "select", side_effect=fake_select),
        ):
            config_cli.create_first_run_config(rt, provider=None, interactive=True)

        assert any("17.2 GB" in title for title in seen_titles)
        assert any("18.5 GB" in title for title in seen_titles)

        loaded = load_config_yaml(rt.config_path)
        assert list(loaded.models) == ["default", "ollama_yes", "ollama_123"]
        assert loaded.models["default"].id == "123:45"
        assert loaded.models["ollama_yes"].id == "yes:latest"
        assert loaded.models["ollama_123"].id == "123:latest"

        out = capsys.readouterr().out
        assert "Setup ready" in out
        assert "Start Ollama and pull the local models" not in out
        assert "ollama pull" not in out
        assert "ollama pull" not in rt.env_path.read_text(encoding="utf-8")

    def test_custom_ollama_host_retained_in_env_even_with_zero_models(self, tmp_path: Path) -> None:
        """A configured custom `OLLAMA_HOST` is written into `.env` even when Ollama has 0 models pulled."""
        rt = _runtime(tmp_path, env={"OLLAMA_HOST": "http://gpu-box.lan:11434"})
        snapshot = _snapshot(DetectedProvider("ollama", False, "running (no models pulled)"))
        with (
            patch.object(setup_detection, "detect_available_providers", return_value=snapshot),
            patch.object(config_cli, "select", return_value="ollama"),
        ):
            config_cli.create_first_run_config(rt, provider=None, interactive=True)

        env_text = rt.env_path.read_text(encoding="utf-8")
        assert "OLLAMA_HOST=http://gpu-box.lan:11434\n" in env_text
        assert "ollama pull" in env_text


class TestInteractiveConfigInit:
    """Tests for unified interactive, piped self-hosted, overwrite-ordering, and non-interactive `config init`."""

    @pytest.mark.parametrize("matrix_server", ["mindroom.chat", "self-hosted"])
    def test_interactive_config_init_runs_smart_wizard(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        matrix_server: str,
    ) -> None:
        """Interactive `config init` runs provider detection and prints truthful next steps for both Matrix presets."""
        target = tmp_path / "config.yaml"
        snapshot = _snapshot(
            DetectedProvider("claude_code", True, "Claude Pro", cli_path="/usr/bin/claude"),
            DetectedProvider("codex", True, "ChatGPT Plus", cli_path="/usr/bin/codex"),
        )
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        with patch.object(setup_detection, "detect_available_providers", return_value=snapshot):
            result = runner.invoke(
                app,
                ["config", "init", "--path", str(target), "--matrix-server", matrix_server],
                input="\n\n",
            )

        assert result.exit_code == 0, result.output
        loaded = load_config_yaml(target)
        assert len(loaded.models) == 6
        assert loaded.models["default"].extra_kwargs == {"cli_path": "/usr/bin/claude"}
        assert loaded.router.model == "haiku"
        assert "Setup ready" in result.output
        assert "Run `claude auth login`" not in result.output
        assert ("Set your Matrix homeserver" in result.output) is (matrix_server == "self-hosted")

    def test_force_overwrite_ignores_key_from_dotenv_about_to_be_wiped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`config init --force` ignores an API key only in the old `.env` so it prompts instead of wiping silently."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text("OPENAI_API_KEY=sk-old-dotenv-key\n", encoding="utf-8")
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        seen_snapshots: list[DetectionSnapshot] = []

        def detect_using_runtime(rt: constants_module.RuntimePaths, **_kwargs: object) -> DetectionSnapshot:
            # Verify the old `.env` key is NOT visible to detection when overwriting `.env`.
            assert rt.env_value("OPENAI_API_KEY") is None
            snap = DetectionSnapshot()
            seen_snapshots.append(snap)
            return snap

        with (
            patch.object(setup_detection, "detect_available_providers", side_effect=detect_using_runtime),
            patch.object(setup_detection, "verify_provider_api_key", return_value=KeyCheckResult("valid")),
        ):
            result = runner.invoke(
                app,
                ["config", "init", "--path", str(target), "--force"],
                input="openai\nsk-fresh-key\n",
            )

        assert result.exit_code == 0, result.output
        assert len(seen_snapshots) == 1
        assert "OPENAI_API_KEY=sk-fresh-key\n" in env_path.read_text(encoding="utf-8")
        assert "sk-old-dotenv-key" not in env_path.read_text(encoding="utf-8")

    def test_piped_self_hosted_prompts_without_detection_and_no_input_defaults_openai(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Piped `config init --matrix-server self-hosted` keeps the legacy provider prompt without running probes."""
        probe_mock = Mock(side_effect=AssertionError("detection must not run when non-TTY"))
        monkeypatch.setattr(setup_detection, "detect_available_providers", probe_mock)
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: False)

        t_piped = tmp_path / "piped" / "config.yaml"
        res_piped = runner.invoke(
            app,
            ["config", "init", "--path", str(t_piped), "--matrix-server", "self-hosted"],
            input="codex\n",
        )
        assert res_piped.exit_code == 0, res_piped.output
        assert load_config_yaml(t_piped).models["default"].provider == "codex"

        t_existing_env = tmp_path / "piped_existing_env" / "config.yaml"
        t_existing_env.parent.mkdir(parents=True)
        existing_env = t_existing_env.parent / ".env"
        original_env = "KEEP_ME=preserve-existing-dotenv\nMINDROOM_API_KEY=existing-dashboard-key\n"
        existing_env.write_text(original_env, encoding="utf-8")
        res_existing_env = runner.invoke(
            app,
            ["config", "init", "--path", str(t_existing_env), "--matrix-server", "self-hosted"],
            input="codex\nn\n",
        )
        assert res_existing_env.exit_code == 0, res_existing_env.output
        assert load_config_yaml(t_existing_env).models["default"].provider == "codex"
        assert existing_env.read_text(encoding="utf-8") == original_env

        t_no_input = tmp_path / "no_input" / "config.yaml"
        res_no_input = runner.invoke(
            app,
            ["config", "init", "--path", str(t_no_input), "--matrix-server", "self-hosted", "--no-input"],
        )
        assert res_no_input.exit_code == 0, res_no_input.output
        assert load_config_yaml(t_no_input).models["default"].provider == "openai"
        probe_mock.assert_not_called()

    def test_non_interactive_hosted_and_explicit_provider_skip_detection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-interactive hosted `config init`, `--no-input`, `--print`, and `--provider` stay portable and probe-free."""
        probe_mock = Mock(side_effect=AssertionError("detection must not run"))
        monkeypatch.setattr(setup_detection, "detect_available_providers", probe_mock)

        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: False)
        t1 = tmp_path / "t1" / "config.yaml"
        res1 = runner.invoke(app, ["config", "init", "--path", str(t1)])
        assert res1.exit_code == 0, res1.output
        assert yaml.safe_load(t1.read_text(encoding="utf-8"))["models"]["default"]["provider"] == "openai"

        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        t2 = tmp_path / "t2" / "config.yaml"
        res2 = runner.invoke(app, ["config", "init", "--path", str(t2), "--provider", "claude_code"])
        assert res2.exit_code == 0, res2.output
        models2 = yaml.safe_load(t2.read_text(encoding="utf-8"))["models"]
        assert set(models2) == {"default", "opus", "haiku"}
        assert all("extra_kwargs" not in model for model in models2.values())

        t3 = tmp_path / "t3" / "config.yaml"
        assert runner.invoke(app, ["config", "init", "--path", str(t3), "--no-input"]).exit_code == 0
        assert runner.invoke(app, ["config", "init", "--path", str(t3), "--print"]).exit_code == 0
        probe_mock.assert_not_called()
