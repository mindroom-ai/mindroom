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
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT
from tests.conftest import load_config_yaml

if TYPE_CHECKING:
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


@pytest.fixture(autouse=True)
def _isolate_host_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent the developer's shell environment from leaking into setup detection tests."""
    for env_key in (
        *constants_module.PROVIDER_ENV_KEYS.values(),
        constants_module.OWNER_MATRIX_USER_ID_ENV,
        "CODEX_HOME",
        "KIMI_CODE_HOME",
        "OLLAMA_HOST",
    ):
        monkeypatch.delenv(env_key, raising=False)
        monkeypatch.delenv(f"{env_key}_FILE", raising=False)


class TestProviderDetection:
    """Unit and subprocess tests for local provider detection."""

    def test_codex_detection_variants(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Codex detects unexpired ChatGPT tokens with plan, refresh-only tokens, non-chatgpt auth, and installed CLI."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        rt = _runtime(tmp_path)
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        auth_file = codex_home / "auth.json"

        # 1. Nothing present -> None.
        assert detect_single_provider("codex", rt) is None

        # 2. Valid unexpired access token + id_token plan type -> ready with ChatGPT Pro.
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
        detected = detect_single_provider("codex", rt)
        assert detected == DetectedProvider("codex", True, "ChatGPT Pro", cli_path=None)

        # 3. Expired access token + non-empty refresh token (no id_token) -> ready ("signed in").
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
        detected = detect_single_provider("codex", rt)
        assert detected == DetectedProvider("codex", True, "signed in", cli_path=None)

        # 4. Non-chatgpt mode with codex binary installed -> ready=False, CLI installed.
        codex_bin = _write_fake_cli(bin_dir, "codex", "import sys; sys.exit(0)")
        auth_file.write_text(
            json.dumps({"auth_mode": "api_key", "tokens": {"access_token": "x"}}),
            encoding="utf-8",
        )
        detected = detect_single_provider("codex", rt)
        assert detected == DetectedProvider("codex", False, "CLI installed", cli_path=str(codex_bin))

    def test_claude_code_detection_uses_cli_status_and_scrubs_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Claude probe runs `claude auth status` with scrubbed env, checks firstParty claude.ai, and handles timeouts."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-scrubbed")
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        monkeypatch.setenv("CLAUDE_BG_AUTH_SNAPSHOT_PATH", str(tmp_path / "bad-snapshot"))
        rt = _runtime(tmp_path)

        # No binary -> None.
        assert detect_single_provider("claude_code", rt) is None

        # Fake claude verifies scrubbed env and returns logged-in subscription JSON.
        env_dump = tmp_path / "claude_env.json"
        claude_bin = _write_fake_cli(
            bin_dir,
            "claude",
            (
                "import json, os, sys\n"
                f"open({str(env_dump)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
                "assert sys.argv[1:] == ['auth', 'status']\n"
                "print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', "
                "'apiProvider': 'firstParty', 'subscriptionType': 'max'}))\n"
            ),
        )
        detected = detect_single_provider("claude_code", rt)
        assert detected == DetectedProvider("claude_code", True, "Claude Max", cli_path=str(claude_bin))
        child_env = json.loads(env_dump.read_text(encoding="utf-8"))
        assert "ANTHROPIC_API_KEY" not in child_env
        assert "CLAUDE_CODE_USE_BEDROCK" not in child_env
        assert "CLAUDE_BG_AUTH_SNAPSHOT_PATH" not in child_env

        # API-key authMethod is NOT a Claude subscription -> CLI installed only.
        _write_fake_cli(
            bin_dir,
            "claude",
            (
                "import json\n"
                "print(json.dumps({'loggedIn': True, 'authMethod': 'apiKey', "
                "'apiProvider': 'firstParty', 'subscriptionType': 'pro'}))\n"
            ),
        )
        assert detect_single_provider("claude_code", rt) == DetectedProvider(
            "claude_code",
            False,
            "CLI installed",
            cli_path=str(claude_bin),
        )

        # Logged out -> CLI installed only.
        _write_fake_cli(bin_dir, "claude", "import json; print(json.dumps({'loggedIn': False}))")
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
        """Kimi credentials and non-placeholder env API keys are detected cleanly."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        kimi_creds = tmp_path / "kimi-home" / "credentials" / "kimi-code.json"
        kimi_creds.parent.mkdir(parents=True)

        # Refresh-only Kimi credentials -> ready.
        kimi_creds.write_text(json.dumps({"access_token": "expired", "expires_at": 1, "refresh_token": "rt-kimi"}))
        rt = _runtime(
            tmp_path,
            env={
                "OPENAI_API_KEY": "sk-real-openai",
                "ANTHROPIC_API_KEY": "your-anthropic-key-here",
                "OPENROUTER_API_KEY": "sk-or-real",
            },
        )
        assert detect_single_provider("kimi", rt) == DetectedProvider("kimi", True, "signed in", cli_path=None)
        assert detect_single_provider("openai", rt) == DetectedProvider("openai", True, "OPENAI_API_KEY set")
        assert detect_single_provider("anthropic", rt) is None
        assert detect_single_provider("openrouter", rt) == DetectedProvider(
            "openrouter",
            True,
            "OPENROUTER_API_KEY set",
        )

        # Missing Kimi credentials + installed binary -> CLI installed.
        kimi_creds.unlink()
        kimi_bin = _write_fake_cli(bin_dir, "kimi", "import sys; sys.exit(0)")
        assert detect_single_provider("kimi", rt) == DetectedProvider(
            "kimi",
            False,
            "CLI installed",
            cli_path=str(kimi_bin),
        )

    def test_ollama_detection_models_empty_and_unreachable(self, tmp_path: Path) -> None:
        """Ollama parses `/api/tags` models + sizes, detects empty server, and fails open when unreachable."""
        rt = _runtime(tmp_path)
        tags_url = f"{OLLAMA_HOST_DEFAULT}/api/tags"

        # 4 pulled models -> concise summary (2 names + "+2 more") and formatted sizes.
        resp = httpx.Response(
            200,
            json={
                "models": [
                    {"name": "gemma4:latest", "size": 17_200_000_000},
                    {"name": "qwen3.8:27b", "size": 450_000_000},
                    {"name": "llama3.2:latest", "size": 2_100_000_000},
                    {"name": "nomic-embed-text:latest"},
                ],
            },
        )
        with patch.object(setup_detection.httpx, "get", return_value=resp) as mock_get:
            detected = detect_single_provider("ollama", rt)
        assert detected is not None
        assert detected.ready is True
        assert detected.status_detail == "gemma4:latest, qwen3.8:27b, +2 more"
        assert detected.ollama_models[0].format_size() == "17.2 GB"
        assert detected.ollama_models[1].format_size() == "450 MB"
        assert detected.ollama_models[3].format_size() is None
        assert mock_get.call_args.args == (tags_url,)
        assert mock_get.call_args.kwargs["follow_redirects"] is False

        # Empty models list -> running (no models pulled).
        empty_resp = httpx.Response(200, json={"models": []})
        with patch.object(setup_detection.httpx, "get", return_value=empty_resp):
            detected = detect_single_provider("ollama", rt)
        assert detected == DetectedProvider("ollama", False, "running (no models pulled)")

        # Explicit custom OLLAMA_HOST is probed at its configured URL.
        custom_rt = _runtime(tmp_path, env={"OLLAMA_HOST": "http://ollama.lan:11434/"})
        with patch.object(setup_detection.httpx, "get", return_value=empty_resp) as custom_get:
            assert detect_single_provider("ollama", custom_rt) == DetectedProvider(
                "ollama",
                False,
                "running (no models pulled)",
            )
        assert custom_get.call_args.args == ("http://ollama.lan:11434/api/tags",)

        # Connection timeout -> fails open to None.
        with patch.object(setup_detection.httpx, "get", side_effect=httpx.ConnectTimeout("timeout")):
            assert detect_single_provider("ollama", rt) is None

    def test_parallel_detection_snapshot_and_fail_open(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`detect_available_providers` aggregates probes in deterministic order and ignores individual probe errors."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("PATH", str(bin_dir))
        _write_fake_cli(
            bin_dir,
            "claude",
            (
                "import json\n"
                "print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', "
                "'apiProvider': 'firstParty', 'subscriptionType': 'pro'}))\n"
            ),
        )
        codex_home = tmp_path / "codex-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(
            json.dumps({"auth_mode": "chatgpt", "tokens": {"refresh_token": "rt-ok"}}),
            encoding="utf-8",
        )
        rt = _runtime(tmp_path, env={"OPENAI_API_KEY": "sk-test"})

        with patch.object(setup_detection, "_probe_ollama", side_effect=RuntimeError("boom")):
            snapshot = detect_available_providers(rt)

        assert snapshot.both_claude_and_codex_ready() is True
        assert snapshot.ready_presets() == ("claude_code", "codex", "openai")
        summary = snapshot.summary_line()
        assert summary is not None
        assert "Claude (Claude Pro)" in summary
        assert "Codex (signed in)" in summary
        assert "OpenAI (OPENAI_API_KEY set)" in summary


class TestLiveApiKeyVerification:
    """Tests for read-only provider API key checks and interactive retry/fail-open loop."""

    def test_verify_provider_api_key_statuses(self) -> None:
        """2xx -> valid, 401/403 -> rejected, 5xx/network/unknown -> inconclusive, never leaking the key."""
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

        with patch.object(setup_detection.httpx, "get", return_value=httpx.Response(503)):
            assert verify_provider_api_key("openrouter", sample_key).status == "inconclusive"

        with patch.object(setup_detection.httpx, "get", side_effect=httpx.ConnectError("offline")):
            assert verify_provider_api_key("openai", sample_key).status == "inconclusive"

        assert verify_provider_api_key("unknown_provider", sample_key).status == "inconclusive"

    def test_rejected_key_retries_until_valid_and_skip_leaves_placeholder(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Rejected keys prompt again; a subsequent valid key is saved and never echoed."""
        rt = _runtime(tmp_path)
        checks = iter([KeyCheckResult("rejected", "HTTP 401"), KeyCheckResult("valid")])
        keys = iter(["sk-bad-key", "sk-good-key"])

        with (
            patch.object(setup_detection, "detect_single_provider", return_value=None),
            patch.object(config_cli, "secret", side_effect=lambda _msg: next(keys)),
            patch.object(setup_detection, "verify_provider_api_key", side_effect=lambda _p, _k: next(checks)),
        ):
            config_cli.create_first_run_config(rt, provider="openai", interactive=True)

        out = capsys.readouterr().out
        assert "rejected that API key" in out
        assert "Verified key with OpenAI." in out
        assert "sk-bad-key" not in out
        assert "sk-good-key" not in out
        assert "OPENAI_API_KEY=sk-good-key\n" in rt.env_path.read_text(encoding="utf-8")

        # Pressing Enter after rejection skips and leaves placeholder.
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
        assert "OPENAI_API_KEY=your-openai-key-here\n" in rt2.env_path.read_text(encoding="utf-8")

    def test_inconclusive_key_fails_open_and_saves(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Network failures during verification warn and save the typed key anyway."""
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

        out = capsys.readouterr().out
        assert "Could not verify key right now; saving anyway." in out
        assert "ANTHROPIC_API_KEY=sk-offline-key\n" in rt.env_path.read_text(encoding="utf-8")


class TestCombinedClaudeCodexSetup:
    """Tests for combined Claude Code + Codex starter generation."""

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
        """When both logins are ready, `Claude + Codex` emits all 6 models cleanly, pins `cli_path`, and loads cleanly."""
        rt = _runtime(tmp_path)
        claude_cli = "/opt/homebrew/bin/claude"
        snapshot = DetectionSnapshot(
            providers={
                "claude_code": DetectedProvider("claude_code", True, "Claude Max", cli_path=claude_cli),
                "codex": DetectedProvider("codex", True, "ChatGPT Pro", cli_path="/usr/local/bin/codex"),
            },
        )
        select_calls: list[tuple[str, list[str]]] = []

        def fake_select(message: str, choices: list[Any]) -> str:
            values = [c[0] for c in choices if not isinstance(c, config_cli.Separator)]
            select_calls.append((message, values))
            if message == "Default model for Mind?":
                return default_pick
            return values[0]  # Default first choice must be "claude_code+codex".

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
            first = next(c[0] for c in choices if not isinstance(c, config_cli.Separator))
            return str(first)

        # Empty detection -> exact _PROVIDER_PRESETS order, no separator.
        with patch.object(config_cli, "select", side_effect=fake_select):
            config_cli._prompt_provider_choice(DetectionSnapshot())
        assert [c[0] for c in captured[0]] == list(config_cli._PROVIDER_PRESETS)
        assert not any(isinstance(c, config_cli.Separator) for c in captured[0])

        # Rich detection -> Claude + Codex, ready providers, CLI-installed, Separator, remaining presets.
        snapshot = DetectionSnapshot(
            providers={
                "claude_code": DetectedProvider("claude_code", True, "Claude Max", cli_path="/usr/bin/claude"),
                "codex": DetectedProvider("codex", True, "ChatGPT Pro", cli_path="/usr/bin/codex"),
                "kimi": DetectedProvider("kimi", False, "CLI installed", cli_path="/usr/bin/kimi"),
                "openai": DetectedProvider("openai", True, "OPENAI_API_KEY set"),
                "ollama": DetectedProvider("ollama", True, "gemma4:latest"),
            },
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
        below = [c[0] for c in entries[sep_idx + 1 :]]
        assert below == [p for p in config_cli._PROVIDER_PRESETS if p not in snapshot.providers]


class TestInlineCliLogin:
    """Tests for inline `codex login` / `claude auth login` during interactive setup."""

    def test_run_inline_cli_login_runs_expected_args_and_scrubs_claude_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`run_inline_cli_login` invokes the CLI login subcommand and scrubs Claude env overrides."""
        bin_dir = tmp_path / "bin"
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-scrub-me")
        claude_env_dump = tmp_path / "login_env.json"
        claude_bin = _write_fake_cli(
            bin_dir,
            "claude",
            (
                "import json, os, sys\n"
                "assert sys.argv[1:] == ['auth', 'login']\n"
                f"open({str(claude_env_dump)!r}, 'w').write(json.dumps(dict(os.environ)))\n"
            ),
        )
        codex_bin = _write_fake_cli(bin_dir, "codex", "import sys; assert sys.argv[1:] == ['login']")

        assert run_inline_cli_login("claude_code", str(claude_bin)) is True
        assert "ANTHROPIC_API_KEY" not in json.loads(claude_env_dump.read_text(encoding="utf-8"))
        assert run_inline_cli_login("codex", str(codex_bin)) is True
        assert run_inline_cli_login("kimi", str(codex_bin)) is False

    def test_inline_login_accept_marks_ready_and_decline_shows_hint(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Accepting inline login re-probes and suppresses pending hints; declining shows the login hint."""
        # Accept -> re-probes ready, pins cli_path, suppresses hint.
        rt_ok = _runtime(tmp_path / "ok")
        claude_cli = "/opt/homebrew/bin/claude"
        installed_claude = DetectionSnapshot(
            providers={"claude_code": DetectedProvider("claude_code", False, "CLI installed", cli_path=claude_cli)},
        )
        ready_claude = DetectedProvider("claude_code", True, "Claude Pro", cli_path=claude_cli)
        with (
            patch.object(setup_detection, "detect_available_providers", return_value=installed_claude),
            patch.object(config_cli, "select", return_value="claude_code"),
            patch.object(config_cli, "confirm", return_value=True),
            patch.object(setup_detection, "run_inline_cli_login", return_value=True) as mock_login,
            patch.object(setup_detection, "detect_single_provider", return_value=ready_claude),
        ):
            config_cli.create_first_run_config(rt_ok, provider=None, interactive=True)

        mock_login.assert_called_once_with("claude_code", claude_cli)
        out_ok = capsys.readouterr().out
        assert "Signed in to Claude subscription (Claude Pro)." in out_ok
        assert "MindRoom keeps starting; your agents can answer once you:" not in out_ok
        cfg_ok = yaml.safe_load(rt_ok.config_path.read_text(encoding="utf-8"))
        assert cfg_ok["models"]["default"]["extra_kwargs"]["cli_path"] == claude_cli

        # Decline -> does not run login, prints first-run login hint.
        rt_no = _runtime(tmp_path / "no")
        installed_codex = DetectionSnapshot(
            providers={"codex": DetectedProvider("codex", False, "CLI installed", cli_path="/usr/local/bin/codex")},
        )
        with (
            patch.object(setup_detection, "detect_available_providers", return_value=installed_codex),
            patch.object(config_cli, "select", return_value="codex"),
            patch.object(config_cli, "confirm", return_value=False),
            patch.object(setup_detection, "run_inline_cli_login") as mock_login_no,
        ):
            config_cli.create_first_run_config(rt_no, provider=None, interactive=True)

        mock_login_no.assert_not_called()
        out_no = capsys.readouterr().out
        assert "MindRoom keeps starting; your agents can answer once you:" in out_no
        assert "codex login" in out_no


class TestOllamaModelDiscoverySetup:
    """Tests for choosing already-pulled Ollama models in interactive setup."""

    def test_ollama_pulled_model_selection_sanitizes_keys_and_skips_pull_hints(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Picking Ollama with pulled models prompts for the default model, sanitizes keys, and skips `ollama pull`."""
        rt = _runtime(tmp_path)
        pulled = (
            OllamaModelInfo("gemma4:latest", 17_200_000_000),
            OllamaModelInfo("default:latest", 4_100_000_000),
            OllamaModelInfo("qwen3.8:27b", 18_500_000_000),
            OllamaModelInfo("hf.co/unsloth/extra.model:Q4_K_M", 9_000_000_000),
        )
        snapshot = DetectionSnapshot(
            providers={
                "ollama": DetectedProvider(
                    "ollama",
                    True,
                    "gemma4:latest, default:latest, +2 more",
                    ollama_models=pulled,
                ),
            },
        )
        seen_titles: list[str] = []

        def fake_select(message: str, choices: list[Any]) -> str:
            if message == "Default local model?":
                seen_titles.extend(c[1] for c in choices)
                return "qwen3.8:27b"
            return "ollama"

        with (
            patch.object(setup_detection, "detect_available_providers", return_value=snapshot),
            patch.object(config_cli, "select", side_effect=fake_select),
        ):
            config_cli.create_first_run_config(rt, provider=None, interactive=True)

        assert any("17.2 GB" in title for title in seen_titles)
        assert any("18.5 GB" in title for title in seen_titles)

        raw_cfg = yaml.safe_load(rt.config_path.read_text(encoding="utf-8"))
        assert list(raw_cfg["models"]) == ["default", "gemma4", "default_2"]
        assert raw_cfg["models"]["default"]["id"] == "qwen3.8:27b"
        assert raw_cfg["models"]["gemma4"]["id"] == "gemma4:latest"
        assert raw_cfg["models"]["default_2"]["id"] == "default:latest"
        loaded = load_config_yaml(rt.config_path)
        assert loaded.models["default"].provider == "ollama"

        out = capsys.readouterr().out
        assert "Setup ready" in out
        assert "Start Ollama and pull the local models" not in out
        assert "ollama pull" not in out
        assert "ollama pull" not in rt.env_path.read_text(encoding="utf-8")


class TestInteractiveConfigInit:
    """Tests for unified interactive vs non-interactive `mindroom config init`."""

    @pytest.mark.parametrize("matrix_server", ["mindroom.chat", "self-hosted"])
    def test_interactive_config_init_runs_smart_wizard(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        matrix_server: str,
    ) -> None:
        """Interactive `config init` runs provider detection and prints truthful next steps for both Matrix presets."""
        target = tmp_path / "config.yaml"
        snapshot = DetectionSnapshot(
            providers={
                "claude_code": DetectedProvider("claude_code", True, "Claude Pro", cli_path="/usr/bin/claude"),
                "codex": DetectedProvider("codex", True, "ChatGPT Plus", cli_path="/usr/bin/codex"),
            },
        )
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        with patch.object(setup_detection, "detect_available_providers", return_value=snapshot):
            # Default menu pick is claude_code+codex; default Mind model is sonnet.
            result = runner.invoke(
                app,
                ["config", "init", "--path", str(target), "--matrix-server", matrix_server],
                input="\n\n",
            )

        assert result.exit_code == 0, result.output
        raw_cfg = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert len(raw_cfg["models"]) == 6
        assert raw_cfg["models"]["default"]["extra_kwargs"]["cli_path"] == "/usr/bin/claude"
        assert load_config_yaml(target).router.model == "haiku"
        assert "Setup ready" in result.output
        assert "Run `claude auth login`" not in result.output
        assert ("Set your Matrix homeserver" in result.output) is (matrix_server == "self-hosted")

    def test_non_interactive_config_init_and_explicit_provider_skip_detection(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-interactive `config init`, `--no-input`, `--print`, and `--provider` stay fast, portable, and probe-free."""
        probe_mock = Mock(side_effect=AssertionError("detection must not run"))
        monkeypatch.setattr(setup_detection, "detect_available_providers", probe_mock)

        # 1. Non-interactive terminal defaults to openai with no probes.
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: False)
        t1 = tmp_path / "t1" / "config.yaml"
        res1 = runner.invoke(app, ["config", "init", "--path", str(t1)])
        assert res1.exit_code == 0, res1.output
        assert yaml.safe_load(t1.read_text(encoding="utf-8"))["models"]["default"]["provider"] == "openai"

        # 2. Interactive terminal with explicit `--provider claude_code` skips probes and stays portable (no cli_path).
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        t2 = tmp_path / "t2" / "config.yaml"
        res2 = runner.invoke(app, ["config", "init", "--path", str(t2), "--provider", "claude_code"])
        assert res2.exit_code == 0, res2.output
        models2 = yaml.safe_load(t2.read_text(encoding="utf-8"))["models"]
        assert set(models2) == {"default", "opus", "haiku"}
        assert all("extra_kwargs" not in model for model in models2.values())

        # 3. Interactive terminal with `--no-input` or `--print` skips probes.
        t3 = tmp_path / "t3" / "config.yaml"
        res3 = runner.invoke(app, ["config", "init", "--path", str(t3), "--no-input"])
        assert res3.exit_code == 0, res3.output
        res4 = runner.invoke(app, ["config", "init", "--path", str(t3), "--print"])
        assert res4.exit_code == 0, res4.output
        probe_mock.assert_not_called()
