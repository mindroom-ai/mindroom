"""Tests for interactive Matrix homeserver discovery, provisioning probing, and CLI onboarding."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock, patch

import httpx
import pytest
import yaml
from dotenv import dotenv_values
from rich.console import Console
from typer.testing import CliRunner

from mindroom.cli import config as config_cli
from mindroom.cli import connect as cli_connect
from mindroom.cli import setup_detection, setup_matrix
from mindroom.cli.main import app
from mindroom.cli.setup_detection import DetectionSnapshot, KeyCheckResult
from mindroom.cli.setup_matrix import (
    _PROVISIONING_CONNECTIONS_PATH,
    _PROVISIONING_MISSING_OPENID_DETAIL,
    _MatrixServerProbeResult,
    _parse_matrix_server_input,
    _ParsedMatrixServerInput,
    _probe_matrix_server,
    _probe_provisioning_service,
)
from mindroom.constants import OWNER_MATRIX_USER_ID_ENV, OWNER_MATRIX_USER_ID_PLACEHOLDER
from mindroom.matrix.provisioning_env import local_pairing_required
from tests.conftest import load_config_yaml

if TYPE_CHECKING:
    from mindroom import constants as constants_module

runner = CliRunner()
_P = _ParsedMatrixServerInput
_R = _MatrixServerProbeResult
_CLEAN_ENV_VARS = (
    "MATRIX_HOMESERVER",
    "MATRIX_SERVER_NAME",
    "MATRIX_REGISTRATION_TOKEN",
    "MATRIX_REGISTRATION_SHARED_SECRET",
    "MATRIX_REGISTRATION_SHARED_SECRET_FILE",
    "MINDROOM_PROVISIONING_URL",
    "MINDROOM_LOCAL_CLIENT_ID",
    "MINDROOM_LOCAL_CLIENT_SECRET",
    "MINDROOM_NAMESPACE",
    "MINDROOM_OWNER_USER_ID",
    "MINDROOM_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def _json_resp(status: int, data: object) -> httpx.Response:
    return httpx.Response(status, json=data)


class _FakeClient:
    """Minimal `httpx.Client` stand-in recording request URLs and redirect policy."""

    def __init__(
        self,
        responses: dict[str, httpx.Response | Exception],
        calls: list[tuple[str, bool]],
        *,
        follow_redirects: bool,
    ) -> None:
        self._responses, self._calls, self._follow_redirects = responses, calls, follow_redirects

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str) -> httpx.Response:
        self._calls.append((url, self._follow_redirects))
        outcome = self._responses.get(url)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if outcome is not None else httpx.Response(404)


def _patch_http(responses: dict[str, httpx.Response | Exception]) -> tuple[Any, list[tuple[str, bool]]]:
    calls: list[tuple[str, bool]] = []

    def client_factory(*, timeout: float, follow_redirects: bool) -> _FakeClient:  # noqa: ARG001
        return _FakeClient(responses, calls, follow_redirects=follow_redirects)

    return patch.object(httpx, "Client", side_effect=client_factory), calls


class TestMatrixServerParsingAndProbing:
    """Unit tests for URL/domain normalization, `.well-known` discovery, and provisioning detection."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("acme.example", _P("https://acme.example", "acme.example", True)),
            ("ACME.EXAMPLE/", _P("https://acme.example", "acme.example", True)),
            ("example.org:8448", _P("https://example.org:8448", "example.org:8448", True)),
            ("example.org:443", _P("https://example.org", "example.org:443", True)),
            ("localhost:8008", _P("https://localhost:8008", "localhost:8008", True)),
            ("[::1]:8448", _P("https://[::1]:8448", "[::1]:8448", True)),
            (
                "https://mindroom.acme.example/",
                _P("https://mindroom.acme.example", "mindroom.acme.example", False),
            ),
            ("https://matrix.example.com:443", _P("https://matrix.example.com", "matrix.example.com", False)),
            ("https://matrix.example.com:8448", _P("https://matrix.example.com:8448", "matrix.example.com", False)),
            ("http://localhost:8008", _P("http://localhost:8008", "localhost", False)),
            ("http://[::1]:8008/", _P("http://[::1]:8008", "[::1]", False)),
            ("https://example.com/matrix/", _P("https://example.com/matrix", "example.com", False)),
        ],
    )
    def test_parse_matrix_server_input_accepts_valid_domains_and_urls(
        self,
        raw: str,
        expected: _ParsedMatrixServerInput,
    ) -> None:
        """Bare server names default to https:// and preserve explicit ports; full URLs normalize cleanly."""
        assert _parse_matrix_server_input(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "ftp://matrix.example.com",
            "https://",
            "http://",
            "https://user:pass@matrix.example.com",
            "https://matrix.example.com?foo=bar",
            "https://matrix.example.com#frag",
            "https://matrix.example.com:99999",
            "https://matrix.example.com:0",
            "https://matrix.example.com:abc",
            "not a url",
            "example.com/subpath",
            "https://..bad",
            "https://-bad.example.com",
            "https://example.com/${SECRET}",
            "@alice:acme.example",
        ],
    )
    def test_parse_matrix_server_input_rejects_invalid_values(self, raw: str) -> None:
        """Empty, non-http(s), malformed, or `.env`-unsafe inputs return None."""
        assert _parse_matrix_server_input(raw) is None

    @pytest.mark.parametrize(
        ("outcome", "expected"),
        [
            (_json_resp(401, {"detail": _PROVISIONING_MISSING_OPENID_DETAIL}), True),
            (_json_resp(401, {"detail": "Unauthorized"}), False),
            (_json_resp(401, {"errcode": "M_UNKNOWN_TOKEN"}), False),
            (httpx.Response(401, text="<html>Unauthorized</html>"), False),
            (_json_resp(401, [_PROVISIONING_MISSING_OPENID_DETAIL]), False),
            (httpx.Response(404), False),
            (httpx.Response(405), False),
            (_json_resp(200, {"connections": []}), False),
            (httpx.ConnectError("connection refused"), False),
            (httpx.ReadTimeout("timed out"), False),
        ],
    )
    def test_probe_provisioning_service_variants(self, outcome: httpx.Response | Exception, expected: bool) -> None:
        """Only HTTP 401 with the exact missing-OpenID detail marks local provisioning as supported."""
        url = f"https://mindroom.acme.example{_PROVISIONING_CONNECTIONS_PATH}"
        ctx, calls = _patch_http({url: outcome})
        with ctx:
            assert _probe_provisioning_service("https://mindroom.acme.example/") is expected
        assert calls == [(url, False)]

    def test_probe_matrix_server_well_known_resolution_and_port_preservation(self) -> None:
        """Bare domains (including explicit ports) resolve `.well-known/matrix/client` and detect provisioning."""
        parsed = _parse_matrix_server_input("acme.example")
        assert parsed is not None
        ctx, calls = _patch_http(
            {
                "https://acme.example/.well-known/matrix/client": _json_resp(
                    200,
                    {"m.homeserver": {"base_url": "https://mindroom.acme.example/"}},
                ),
                "https://mindroom.acme.example/_matrix/client/versions": _json_resp(200, {"versions": ["v1.11"]}),
                f"https://mindroom.acme.example{_PROVISIONING_CONNECTIONS_PATH}": _json_resp(
                    401,
                    {"detail": _PROVISIONING_MISSING_OPENID_DETAIL},
                ),
            },
        )
        with ctx:
            assert _probe_matrix_server(parsed) == _R("https://mindroom.acme.example", True, True, True)
        assert calls == [
            ("https://acme.example/.well-known/matrix/client", False),
            ("https://mindroom.acme.example/_matrix/client/versions", False),
            (f"https://mindroom.acme.example{_PROVISIONING_CONNECTIONS_PATH}", False),
        ]

        parsed_port = _parse_matrix_server_input("example.org:8448")
        assert parsed_port == _P("https://example.org:8448", "example.org:8448", True)
        ctx_port, calls_port = _patch_http(
            {
                "https://example.org:8448/.well-known/matrix/client": _json_resp(
                    200,
                    {"m.homeserver": {"base_url": "https://matrix.example.org/"}},
                ),
                "https://matrix.example.org/_matrix/client/versions": _json_resp(200, {"versions": ["v1.11"]}),
                f"https://matrix.example.org{_PROVISIONING_CONNECTIONS_PATH}": httpx.Response(404),
            },
        )
        with ctx_port:
            assert _probe_matrix_server(parsed_port) == _R("https://matrix.example.org", True, True, False)
        assert calls_port[0] == ("https://example.org:8448/.well-known/matrix/client", False)

    def test_probe_matrix_server_blocks_https_to_http_downgrade_in_well_known_and_redirects(self) -> None:
        """HTTPS `.well-known` lookups reject `http://` base_url values and never follow redirects to `http://`."""
        for domain, wk_resp in (
            ("downgrade.example", _json_resp(200, {"m.homeserver": {"base_url": "http://evil.example"}})),
            (
                "redirect.example",
                httpx.Response(302, headers={"Location": "http://evil.example/.well-known/matrix/client"}),
            ),
        ):
            parsed = _parse_matrix_server_input(domain)
            assert parsed is not None
            ctx, calls = _patch_http(
                {
                    f"https://{domain}/.well-known/matrix/client": wk_resp,
                    f"https://{domain}/_matrix/client/versions": _json_resp(200, {"versions": ["v1.11"]}),
                },
            )
            with ctx:
                res = _probe_matrix_server(parsed)
            assert res.homeserver == f"https://{domain}"
            assert res.discovered_via_well_known is False
            assert all(follow is False for _, follow in calls)
            assert not any("evil.example" in url for url, _ in calls)

    def test_probe_matrix_server_full_url_fallback_subpath_and_fail_open(self) -> None:
        """Full URLs fall back when `.well-known` is absent, skip `.well-known` on subpaths, and fail open."""
        for raw, responses, expected, expected_urls in (
            (
                "http://localhost:8008",
                {
                    "http://localhost:8008/.well-known/matrix/client": httpx.Response(404),
                    "http://localhost:8008/_matrix/client/versions": _json_resp(200, {"versions": ["v1.11"]}),
                    f"http://localhost:8008{_PROVISIONING_CONNECTIONS_PATH}": httpx.Response(404),
                },
                _R("http://localhost:8008", False, True, False),
                None,
            ),
            (
                "https://example.com/matrix",
                {
                    "https://example.com/matrix/_matrix/client/versions": _json_resp(200, {"versions": ["v1.11"]}),
                    f"https://example.com/matrix{_PROVISIONING_CONNECTIONS_PATH}": httpx.Response(404),
                },
                _R("https://example.com/matrix", False, True, False),
                [
                    "https://example.com/matrix/_matrix/client/versions",
                    f"https://example.com/matrix{_PROVISIONING_CONNECTIONS_PATH}",
                ],
            ),
            (
                "offline.example",
                {
                    "https://offline.example/.well-known/matrix/client": httpx.ConnectError("refused"),
                    "https://offline.example/_matrix/client/versions": httpx.ConnectError("refused"),
                },
                _R("https://offline.example"),
                None,
            ),
        ):
            parsed = _parse_matrix_server_input(raw)
            assert parsed is not None
            ctx, calls = _patch_http(responses)
            with ctx:
                assert _probe_matrix_server(parsed) == expected
            if expected_urls is not None:
                assert [url for url, _ in calls] == expected_urls


class TestInteractiveFirstRunMatrixSetup:
    """End-to-end tests for `mindroom run` first-run interactive Matrix server selection."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("mindroom.cli.main.terminal_is_interactive", lambda: True)
        monkeypatch.setattr(
            "mindroom.cli.setup_detection.detect_available_providers",
            lambda _rt, **_kw: DetectionSnapshot(),
        )
        monkeypatch.setattr(
            "mindroom.cli.setup_detection.verify_provider_api_key",
            lambda _p, _k, **_kw: KeyCheckResult("valid"),
        )
        monkeypatch.setattr(
            "mindroom.cli.service._get_service_manager",
            Mock(side_effect=RuntimeError("Unsupported platform")),
        )
        for name in _CLEAN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    @staticmethod
    def _invoke_run(
        config_path: Path,
        answers: str,
        *,
        extra_args: tuple[str, ...] = (),
    ) -> tuple[Any, list[constants_module.RuntimePaths], list[constants_module.RuntimePaths]]:
        paired: list[constants_module.RuntimePaths] = []
        started: list[constants_module.RuntimePaths] = []

        async def fake_run(*, config_path: Path | None, storage_path: Path | None, **_kw: object) -> None:
            started.append(config_cli.activate_cli_runtime(config_path, storage_path=storage_path))

        with (
            patch("mindroom.cli.connect.pair_local_install", side_effect=lambda rt, **_kw: paired.append(rt)),
            patch("mindroom.api.startup_probes.serve_startup_probes", return_value=nullcontext()),
            patch("mindroom.cli.main._run", side_effect=fake_run),
        ):
            result = runner.invoke(
                app,
                [
                    "run",
                    "--no-api",
                    "--config",
                    str(config_path),
                    "--storage-path",
                    str(config_path.parent / "data"),
                    *extra_args,
                ],
                input=answers,
            )
        return result, paired, started

    def test_first_run_default_enter_selects_mindroom_chat(self, tmp_path: Path) -> None:
        """Pressing Enter once picks MindRoom Chat (mindroom.chat) with hosted output and QR pairing."""
        config_path = tmp_path / "config.yaml"
        result, paired, started = self._invoke_run(config_path, "\nopenai\nsk-hosted-key\n")

        assert result.exit_code == 0, result.output
        assert "Own Matrix server? Press Ctrl+C" not in result.output
        assert "Matrix: MindRoom Chat (mindroom.chat)" in result.output
        env_vals = dotenv_values(tmp_path / ".env")
        assert env_vals["MATRIX_HOMESERVER"] == "https://mindroom.chat"
        assert env_vals["MINDROOM_PROVISIONING_URL"] == "https://mindroom.chat"
        assert "mindroom_user" not in yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert len(paired) == 1
        assert len(started) == 1

    def test_first_run_custom_server_with_provisioning_pairs_and_fills_owner(self, tmp_path: Path) -> None:
        """Custom server with auto-detected provisioning writes provisioning `.env`, omits `mindroom_user`, and pairs."""
        config_path = tmp_path / "config.yaml"
        probe = _R("https://mindroom.acme.example", True, True, True)
        with patch.object(setup_matrix, "_probe_matrix_server", return_value=probe):
            result, paired, started = self._invoke_run(config_path, "custom\nacme.example\nopenai\nsk-custom-key\n")

        assert result.exit_code == 0, result.output
        assert "Discovered homeserver https://mindroom.acme.example (server name: acme.example)" in result.output
        assert "Local pairing supported on https://mindroom.acme.example" in result.output
        env_vals = dotenv_values(tmp_path / ".env")
        assert env_vals["MATRIX_HOMESERVER"] == "https://mindroom.acme.example"
        assert env_vals["MATRIX_SERVER_NAME"] == "acme.example"
        assert env_vals["MINDROOM_PROVISIONING_URL"] == "https://mindroom.acme.example"
        assert "mindroom_user" not in yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert load_config_yaml(config_path)
        assert len(paired) == 1
        assert len(started) == 1
        rt = paired[0]
        assert local_pairing_required(rt) is True
        assert cli_connect.self_hosted_pairing_error(rt) is None

        expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        seen_urls: list[str] = []

        def fake_post(url: str, *, json: dict[str, object], timeout: float) -> httpx.Response:  # noqa: ARG001
            seen_urls.append(url)
            if url.endswith("/start"):
                return _json_resp(
                    200,
                    {
                        "pair_code": "PAIR-1234",
                        "device_secret": "dev-secret",
                        "approve_url": "https://chat.acme.example/connect?code=PAIR-1234",
                        "poll_interval_seconds": 1,
                        "expires_at": expires_at,
                    },
                )
            return _json_resp(
                200,
                {
                    "status": "connected",
                    "client_id": "client-ap",
                    "client_secret": "secret-ap",
                    "namespace": "ap01",
                    "owner_user_id": "@alice:acme.example",
                },
            )

        cli_connect.pair_local_install(
            rt,
            console=Console(force_terminal=False, color_system=None),
            post_request=fake_post,
            sleep=lambda _s: None,
            renew_expired=False,
        )
        assert seen_urls == [
            "https://mindroom.acme.example/v1/local-mindroom/pair/device/start",
            "https://mindroom.acme.example/v1/local-mindroom/pair/device/poll",
        ]
        assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["administrators"] == ["@alice:acme.example"]
        assert dotenv_values(tmp_path / ".env")[OWNER_MATRIX_USER_ID_ENV] == "@alice:acme.example"

    def test_first_run_custom_server_with_registration_token_and_owner_skips_pairing(self, tmp_path: Path) -> None:
        """Custom server with registration token + owner MXID fills owner placeholders, keeps `mindroom_user`, and skips pairing."""
        config_path = tmp_path / "config.yaml"
        answers = (
            "custom\nnot-a-valid url\nhttps://matrix.company.com\ncompany.com\n"
            "token\nreg-token-#with=special\nnot-an-mxid\n@alice:company.com\ncodex\n"
        )
        with patch.object(
            setup_matrix,
            "_probe_matrix_server",
            return_value=_R("https://matrix.company.com", False, True, False),
        ):
            result, paired, started = self._invoke_run(config_path, answers)

        assert result.exit_code == 0, result.output
        assert "Enter a valid domain or http(s) URL" in result.output
        assert "Invalid Matrix user ID" in result.output
        assert "reg-token-#with=special" not in result.output
        assert paired == []
        assert len(started) == 1
        assert local_pairing_required(started[0]) is False
        env_vals = dotenv_values(tmp_path / ".env")
        assert env_vals["MATRIX_HOMESERVER"] == "https://matrix.company.com"
        assert env_vals["MATRIX_SERVER_NAME"] == "company.com"
        assert env_vals["MATRIX_REGISTRATION_TOKEN"] == "reg-token-#with=special"  # noqa: S105
        assert env_vals[OWNER_MATRIX_USER_ID_ENV] == "@alice:company.com"
        assert "MINDROOM_PROVISIONING_URL" not in env_vals
        raw_cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert raw_cfg["mindroom_user"]["username"] == "mindroom_user"
        assert raw_cfg["administrators"] == ["@alice:company.com"]
        assert load_config_yaml(config_path)

    def test_first_run_entering_mindroom_chat_in_custom_flow_uses_hosted_defaults(self, tmp_path: Path) -> None:
        """Entering `mindroom.chat` in the custom server prompt routes cleanly to hosted MindRoom Chat defaults."""
        config_path = tmp_path / "config.yaml"
        probe_mock = Mock(side_effect=AssertionError("hosted mindroom.chat needs no custom probe"))
        with patch.object(setup_matrix, "_probe_matrix_server", probe_mock):
            result, paired, started = self._invoke_run(config_path, "custom\nmindroom.chat\ncodex\n")
        assert result.exit_code == 0, result.output
        probe_mock.assert_not_called()
        assert dotenv_values(tmp_path / ".env")["MATRIX_HOMESERVER"] == "https://mindroom.chat"
        assert len(paired) == 1
        assert len(started) == 1

    def test_hosted_recovery_with_matrix_state_and_missing_env_succeeds(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Hosted `config init --no-input` and `mindroom run --provider openai` succeed when `matrix_state.yaml` exists without `.env`."""
        init_dir = tmp_path / "init_recovery"
        (init_dir / "mindroom_data").mkdir(parents=True)
        (init_dir / "mindroom_data" / "matrix_state.yaml").write_text("accounts: {}\n", encoding="utf-8")
        res_init = runner.invoke(app, ["config", "init", "--path", str(init_dir / "config.yaml"), "--no-input"])
        assert res_init.exit_code == 0, res_init.output
        assert dotenv_values(init_dir / ".env")["MATRIX_HOMESERVER"] == "https://mindroom.chat"

        run_dir = tmp_path / "run_recovery"
        (run_dir / "data").mkdir(parents=True)
        (run_dir / "data" / "matrix_state.yaml").write_text("accounts: {}\n", encoding="utf-8")
        monkeypatch.setattr("mindroom.cli.main.terminal_is_interactive", lambda: False)
        res_run, paired, started = self._invoke_run(run_dir / "config.yaml", "", extra_args=("--provider", "openai"))
        assert res_run.exit_code == 0, res_run.output
        assert dotenv_values(run_dir / ".env")["MATRIX_HOMESERVER"] == "https://mindroom.chat"
        assert len(paired) == 1
        assert len(started) == 1


class TestInteractiveConfigInitMatrixSetup:
    """Tests for `mindroom config init` interactive Matrix server flows, kept-`.env` checks, and flag compatibility."""

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(setup_detection, "detect_available_providers", lambda _rt, **_kw: DetectionSnapshot())
        for name in _CLEAN_ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_interactive_config_init_default_and_explicit_mindroom_chat(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Default interactive `config init` offers the Matrix menu; `--matrix-server mindroom.chat` skips it."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        t_default = tmp_path / "default" / "config.yaml"
        res_default = runner.invoke(app, ["config", "init", "--path", str(t_default)], input="\ncodex\n")
        assert res_default.exit_code == 0, res_default.output
        assert "Which Matrix server should your agents connect to?" in res_default.output
        assert "Matrix: MindRoom Chat (mindroom.chat)" in res_default.output
        assert dotenv_values(t_default.parent / ".env")["MINDROOM_PROVISIONING_URL"] == "https://mindroom.chat"

        t_explicit = tmp_path / "explicit" / "config.yaml"
        res_explicit = runner.invoke(
            app,
            ["config", "init", "--path", str(t_explicit), "--matrix-server", "mindroom.chat"],
            input="codex\n",
        )
        assert res_explicit.exit_code == 0, res_explicit.output
        assert "Which Matrix server should your agents connect to?" not in res_explicit.output
        assert dotenv_values(t_explicit.parent / ".env")["MATRIX_HOMESERVER"] == "https://mindroom.chat"

    @pytest.mark.parametrize(
        ("auth_choice", "extra_answers", "expected_env_key", "expected_env_val"),
        [
            (
                "shared_secret",
                "super-secret-value\n@bob:local.test\n",
                "MATRIX_REGISTRATION_SHARED_SECRET",
                "super-secret-value",
            ),
            ("open", "\n", None, None),
            (
                "provisioning_url",
                "https://provision.local.test\n",
                "MINDROOM_PROVISIONING_URL",
                "https://provision.local.test",
            ),
        ],
    )
    def test_interactive_config_init_self_hosted_auth_modes_always_persist_server_name(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        auth_choice: str,
        extra_answers: str,
        expected_env_key: str | None,
        expected_env_val: str | None,
    ) -> None:
        """Interactive `--matrix-server self-hosted` supports all auth modes and always writes `MATRIX_SERVER_NAME`."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        target = tmp_path / "config.yaml"
        with (
            patch.object(setup_matrix, "_probe_matrix_server", return_value=_R("http://localhost:8008")),
            patch.object(setup_matrix, "_probe_provisioning_service", return_value=False),
        ):
            result = runner.invoke(
                app,
                ["config", "init", "--path", str(target), "--matrix-server", "self-hosted", "--provider", "anthropic"],
                input=f"http://localhost:8008\n\n{auth_choice}\n{extra_answers}",
            )
        assert result.exit_code == 0, result.output
        assert "Which Matrix server should your agents connect to?" not in result.output
        assert "Could not reach Matrix homeserver at http://localhost:8008; saving anyway." in result.output
        env_vals = dotenv_values(tmp_path / ".env")
        assert env_vals["MATRIX_HOMESERVER"] == "http://localhost:8008"
        assert env_vals["MATRIX_SERVER_NAME"] == "localhost"
        if expected_env_key is not None:
            assert env_vals[expected_env_key] == expected_env_val
        cfg = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert ("mindroom_user" not in cfg) == (auth_choice == "provisioning_url")
        assert cfg["administrators"] == [
            "@bob:local.test" if auth_choice == "shared_secret" else OWNER_MATRIX_USER_ID_PLACEHOLDER,
        ]

    def test_bare_server_name_with_explicit_port_preserves_port_in_delegated_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Entering bare `example.org:8448` with `.well-known` delegation saves `MATRIX_SERVER_NAME=example.org:8448`."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        target = tmp_path / "config.yaml"
        with patch.object(
            setup_matrix,
            "_probe_matrix_server",
            return_value=_R("https://matrix.example.org", True, True, False),
        ):
            result = runner.invoke(
                app,
                ["config", "init", "--path", str(target), "--matrix-server", "self-hosted", "--provider", "codex"],
                input="example.org:8448\nopen\n\n",
            )
        assert result.exit_code == 0, result.output
        assert "Discovered homeserver https://matrix.example.org (server name: example.org:8448)" in result.output
        env_vals = dotenv_values(tmp_path / ".env")
        assert env_vals["MATRIX_HOMESERVER"] == "https://matrix.example.org"
        assert env_vals["MATRIX_SERVER_NAME"] == "example.org:8448"

    @pytest.mark.parametrize(
        ("existing_env", "extra_args", "answers", "probe"),
        [
            (
                "MATRIX_HOMESERVER=https://mindroom.chat\nMINDROOM_PROVISIONING_URL=https://mindroom.chat\n",
                (),
                "n\ncustom\nhttps://matrix.open.example\n\nopen\n\n",
                _R("https://matrix.open.example", False, True, False),
            ),
            (
                "MINDROOM_LOCAL_CLIENT_ID=hosted-client\nMINDROOM_LOCAL_CLIENT_SECRET=hosted-secret\n",
                (),
                "n\ncustom\nacme.example\n",
                _R("https://mindroom.acme.example", True, True, True),
            ),
            (
                "MATRIX_HOMESERVER=https://matrix.company.com\nMATRIX_SSL_VERIFY=false\nMATRIX_REGISTRATION_SHARED_SECRET_FILE=/etc/mindroom/secret\n",
                (),
                "n\n\n",
                None,
            ),
            ("MATRIX_HOMESERVER=https://matrix.company.com\n", ("--provider", "openai"), "n\n", None),
            (
                "MINDROOM_PROVISIONING_URL=https://mindroom.acme.example\nMINDROOM_LOCAL_CLIENT_ID=acme-client\n",
                (),
                "n\n\n",
                None,
            ),
            (
                "MATRIX_HOMESERVER=https://matrix.a.example\n",
                (),
                "n\ncustom\nhttps://matrix.b.example\n\nopen\n\n",
                _R("https://matrix.b.example", False, True, False),
            ),
        ],
    )
    def test_refuses_cross_server_merge_when_keeping_existing_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        existing_env: str,
        extra_args: tuple[str, ...],
        answers: str,
        probe: _MatrixServerProbeResult | None,
    ) -> None:
        """Keeping an existing `.env` refuses switching servers in either direction before writing anything."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text(existing_env, encoding="utf-8")
        probe_ctx = patch.object(setup_matrix, "_probe_matrix_server", return_value=probe) if probe else nullcontext()
        with probe_ctx:
            result = runner.invoke(app, ["config", "init", "--path", str(target), *extra_args], input=answers)
        assert result.exit_code == 1
        output = " ".join(result.output.split())
        assert "targets another Matrix server" in output
        assert "--force" in output
        assert env_path.read_text(encoding="utf-8") == existing_env
        assert not target.exists()

    def test_no_input_on_existing_custom_install_is_idempotent_unless_matrix_server_explicit(
        self,
        tmp_path: Path,
    ) -> None:
        """`config init --no-input` keeps custom Matrix settings while still adding a missing dashboard key, and explicit hosted still refuses."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        orig_cfg = "agents: {}\nmodels: {}\nrouter:\n  model: default\n"
        orig_env = "MATRIX_HOMESERVER=https://matrix.company.com\nMATRIX_SERVER_NAME=company.com\n"
        target.write_text(orig_cfg, encoding="utf-8")
        env_path.write_text(orig_env, encoding="utf-8")

        res_first = runner.invoke(app, ["config", "init", "--path", str(target), "--no-input"])
        assert res_first.exit_code == 0, res_first.output
        assert target.read_text(encoding="utf-8") == orig_cfg
        after_first = env_path.read_text(encoding="utf-8")
        env_vals = dotenv_values(env_path)
        assert env_vals["MATRIX_HOMESERVER"] == "https://matrix.company.com"
        assert env_vals["MATRIX_SERVER_NAME"] == "company.com"
        assert "MINDROOM_PROVISIONING_URL" not in env_vals
        assert env_vals["MINDROOM_API_KEY"]

        # Second run is byte-for-byte idempotent now that MINDROOM_API_KEY is present.
        res_second = runner.invoke(app, ["config", "init", "--path", str(target), "--no-input"])
        assert res_second.exit_code == 0, res_second.output
        assert env_path.read_text(encoding="utf-8") == after_first

        res_refused = runner.invoke(
            app,
            ["config", "init", "--path", str(target), "--matrix-server", "mindroom.chat", "--no-input"],
        )
        assert res_refused.exit_code == 1
        assert "targets another Matrix server" in " ".join(res_refused.output.split())
        assert env_path.read_text(encoding="utf-8") == after_first

    def test_no_input_on_existing_hosted_pairing_env_still_appends_defaults_and_api_key(
        self,
        tmp_path: Path,
    ) -> None:
        """Bare `config init --no-input` on an existing hosted `.env` still appends missing hosted defaults and `MINDROOM_API_KEY`."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        target.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        env_path.write_text(
            "MINDROOM_PROVISIONING_URL=https://mindroom.chat\nMINDROOM_LOCAL_CLIENT_ID=cid\nMINDROOM_LOCAL_CLIENT_SECRET=csec\n",
            encoding="utf-8",
        )
        res = runner.invoke(app, ["config", "init", "--path", str(target), "--no-input"])
        assert res.exit_code == 0, res.output
        env_vals = dotenv_values(env_path)
        assert env_vals["MATRIX_HOMESERVER"] == "https://mindroom.chat"
        assert env_vals["MINDROOM_API_KEY"]
        assert env_vals["MINDROOM_LOCAL_CLIENT_ID"] == "cid"

    def test_force_overwrite_drops_discarded_env_owner_on_server_switch(self, tmp_path: Path) -> None:
        """`config init --force --provider openai` overwrites a custom `.env` without baking its old owner into `config.yaml`."""
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text(
            f"MATRIX_HOMESERVER=https://matrix.company.com\nMATRIX_SSL_VERIFY=false\n{OWNER_MATRIX_USER_ID_ENV}=@old:company.com\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["config", "init", "--path", str(target), "--force", "--provider", "openai"])
        assert result.exit_code == 0, result.output
        env_vals = dotenv_values(env_path)
        assert env_vals["MATRIX_HOMESERVER"] == "https://mindroom.chat"
        assert "MATRIX_SSL_VERIFY" not in env_vals
        assert OWNER_MATRIX_USER_ID_ENV not in env_vals
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["administrators"] == [
            OWNER_MATRIX_USER_ID_PLACEHOLDER,
        ]

    def test_hosted_selection_replaces_self_hosted_placeholder_in_kept_env(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Choosing MindRoom Chat while keeping a placeholder self-hosted `.env` replaces `https://matrix.example.com`."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text("KEEP_ME=1\nMATRIX_HOMESERVER=https://matrix.example.com\n", encoding="utf-8")

        result = runner.invoke(app, ["config", "init", "--path", str(target)], input="n\n\ncodex\n")
        assert result.exit_code == 0, result.output
        env_vals = dotenv_values(env_path)
        assert env_vals["KEEP_ME"] == "1"
        assert env_vals["MATRIX_HOMESERVER"] == "https://mindroom.chat"
        assert env_vals["MINDROOM_PROVISIONING_URL"] == "https://mindroom.chat"
        assert "matrix.example.com" not in env_path.read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        ("answers", "expected_preserved", "expected_cleared"),
        [
            (
                "n\ncustom\nhttps://matrix.company.com\n\ntoken\n\n\ncodex\n",
                {
                    "MATRIX_REGISTRATION_TOKEN": "tok-keep",
                    "MINDROOM_NAMESPACE": "ns-keep",
                    OWNER_MATRIX_USER_ID_ENV: "@alice:company.com",
                    "MATRIX_SSL_VERIFY": "false",
                },
                (
                    "MATRIX_REGISTRATION_SHARED_SECRET",
                    "MATRIX_REGISTRATION_SHARED_SECRET_FILE",
                    "MINDROOM_PROVISIONING_URL",
                    "MINDROOM_LOCAL_CLIENT_ID",
                    "MINDROOM_LOCAL_CLIENT_SECRET",
                ),
            ),
            (
                "n\ncustom\nhttps://matrix.company.com\n\nshared_secret\n\n\ncodex\n",
                {
                    "MATRIX_REGISTRATION_SHARED_SECRET": "sec-keep",
                    "MATRIX_REGISTRATION_SHARED_SECRET_FILE": "/etc/mindroom/secret",
                    "MINDROOM_NAMESPACE": "ns-keep",
                    OWNER_MATRIX_USER_ID_ENV: "@alice:company.com",
                },
                ("MATRIX_REGISTRATION_TOKEN", "MINDROOM_PROVISIONING_URL", "MINDROOM_LOCAL_CLIENT_ID"),
            ),
            (
                "n\ncustom\nhttps://matrix.company.com\n\nopen\n\ncodex\n",
                {"MINDROOM_NAMESPACE": "ns-keep", OWNER_MATRIX_USER_ID_ENV: "@alice:company.com"},
                (
                    "MATRIX_REGISTRATION_TOKEN",
                    "MATRIX_REGISTRATION_SHARED_SECRET",
                    "MATRIX_REGISTRATION_SHARED_SECRET_FILE",
                    "MINDROOM_PROVISIONING_URL",
                ),
            ),
        ],
    )
    def test_same_server_reinit_preserves_skipped_secrets_owner_and_namespace(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        answers: str,
        expected_preserved: dict[str, str],
        expected_cleared: tuple[str, ...],
    ) -> None:
        """Same-server re-init preserves skipped secrets, owner, and `MINDROOM_NAMESPACE` while clearing switched-away auth."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        target = tmp_path / "config.yaml"
        env_path = tmp_path / ".env"
        env_path.write_text(
            "KEEP_ME=still-here\nMATRIX_HOMESERVER=https://Matrix.Company.com:443/\nMATRIX_SERVER_NAME=company.com\n"
            "MATRIX_SSL_VERIFY=false\nMATRIX_REGISTRATION_TOKEN=tok-keep\nMATRIX_REGISTRATION_SHARED_SECRET=sec-keep\n"
            "MATRIX_REGISTRATION_SHARED_SECRET_FILE=/etc/mindroom/secret\nMINDROOM_PROVISIONING_URL=https://matrix.company.com\n"
            f"MINDROOM_LOCAL_CLIENT_ID=old-cid\nMINDROOM_LOCAL_CLIENT_SECRET=old-csec\nMINDROOM_NAMESPACE=ns-keep\n{OWNER_MATRIX_USER_ID_ENV}=@alice:company.com\n",
            encoding="utf-8",
        )
        with patch.object(
            setup_matrix,
            "_probe_matrix_server",
            return_value=_R("https://matrix.company.com", False, True, False),
        ):
            result = runner.invoke(app, ["config", "init", "--path", str(target)], input=answers)
        assert result.exit_code == 0, result.output
        env_vals = dotenv_values(env_path)
        assert env_vals["KEEP_ME"] == "still-here"
        for key, value in expected_preserved.items():
            assert env_vals[key] == value
        for key in expected_cleared:
            assert env_vals.get(key) == ""
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["administrators"] == ["@alice:company.com"]

    def test_merging_same_custom_provisioning_server_preserves_existing_pairing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Keeping a pre-paired `.env` on the same custom provisioning URL preserves pairing credentials and owner."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        target = tmp_path / "config.yaml"
        (tmp_path / ".env").write_text(
            "MINDROOM_PROVISIONING_URL=https://MINDROOM.ACME.EXAMPLE:443/\n"
            "MINDROOM_LOCAL_CLIENT_ID=keep-client\nMINDROOM_LOCAL_CLIENT_SECRET=keep-secret\n"
            "MINDROOM_NAMESPACE=keepns\nMINDROOM_OWNER_USER_ID=@alice:acme.example\n",
            encoding="utf-8",
        )
        with patch.object(
            setup_matrix,
            "_probe_matrix_server",
            return_value=_R("https://mindroom.acme.example", True, True, True),
        ):
            res = runner.invoke(
                app,
                ["config", "init", "--path", str(target)],
                input="n\ncustom\nacme.example\ncodex\n",
            )
        assert res.exit_code == 0, res.output
        env_same = dotenv_values(tmp_path / ".env")
        assert env_same["MINDROOM_LOCAL_CLIENT_ID"] == "keep-client"
        assert env_same["MINDROOM_LOCAL_CLIENT_SECRET"] == "keep-secret"  # noqa: S105
        assert env_same["MINDROOM_NAMESPACE"] == "keepns"
        assert env_same[OWNER_MATRIX_USER_ID_ENV] == "@alice:acme.example"
        assert local_pairing_required(config_cli.activate_cli_runtime(target)) is False
        assert yaml.safe_load(target.read_text(encoding="utf-8"))["administrators"] == ["@alice:acme.example"]

    def test_exported_matrix_homeserver_warns_without_crashing_or_leaking_value(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A conflicting shell-exported `MATRIX_HOMESERVER` prints a warning naming only the variable and completes setup."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        exported_url = "https://internal-secret-host.corp.example"
        monkeypatch.setenv("MATRIX_HOMESERVER", exported_url)

        # Hosted selection never echoes the exported URL anywhere.
        t_hosted = tmp_path / "hosted" / "config.yaml"
        res_hosted = runner.invoke(app, ["config", "init", "--path", str(t_hosted)], input="\ncodex\n")
        assert res_hosted.exit_code == 0, res_hosted.output
        assert "Warning: MATRIX_HOMESERVER is exported in your shell environment and will override .env." in " ".join(
            res_hosted.output.split(),
        )
        assert exported_url not in res_hosted.output

        # Custom selection targeting another server warns (without echoing value in the warning) and saves `.env`.
        t_custom = tmp_path / "custom" / "config.yaml"
        with patch.object(
            setup_matrix,
            "_probe_matrix_server",
            return_value=_R("https://matrix.other.example", False, True, False),
        ):
            res_custom = runner.invoke(
                app,
                ["config", "init", "--path", str(t_custom), "--matrix-server", "self-hosted", "--provider", "codex"],
                input="https://matrix.other.example\n\nopen\n\n",
            )
        assert res_custom.exit_code == 0, res_custom.output
        warn_text = "Warning: MATRIX_HOMESERVER is exported in your shell environment and will override .env."
        assert warn_text in " ".join(res_custom.output.split())
        # The warning appears after the prompt and does not echo the exported URL.
        assert exported_url not in res_custom.output.split("Warning:", 1)[1]
        assert dotenv_values(t_custom.parent / ".env")["MATRIX_HOMESERVER"] == "https://matrix.other.example"

    def test_no_input_and_print_skip_all_matrix_prompts_and_probes(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """`--no-input` and `--print` stay non-interactive and probe-free even in a TTY."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        probe_mock = Mock(side_effect=AssertionError("matrix probe must not run"))
        monkeypatch.setattr(setup_matrix, "_probe_matrix_server", probe_mock)

        t_default = tmp_path / "default" / "config.yaml"
        assert runner.invoke(app, ["config", "init", "--path", str(t_default), "--no-input"]).exit_code == 0
        assert dotenv_values(t_default.parent / ".env")["MATRIX_HOMESERVER"] == "https://mindroom.chat"

        t_self = tmp_path / "self" / "config.yaml"
        res_self = runner.invoke(
            app,
            ["config", "init", "--path", str(t_self), "--matrix-server", "self-hosted", "--no-input"],
        )
        assert res_self.exit_code == 0, res_self.output
        assert dotenv_values(t_self.parent / ".env")["MATRIX_HOMESERVER"] == "https://matrix.example.com"
        assert "Set your API keys and Matrix homeserver" in res_self.output

        res_print = runner.invoke(
            app,
            [
                "config",
                "init",
                "--path",
                str(tmp_path / "print" / "config.yaml"),
                "--matrix-server",
                "self-hosted",
                "--print",
            ],
        )
        assert res_print.exit_code == 0, res_print.output
        assert "mindroom_user" in yaml.safe_load(res_print.output)
        probe_mock.assert_not_called()

    def test_custom_server_prefills_existing_env_and_reprompts_invalid_server_name_and_secret(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Existing non-default MATRIX_HOMESERVER/MATRIX_SERVER_NAME prefill defaults; invalid server names or `${...}` tokens re-prompt."""
        monkeypatch.setattr(config_cli, "terminal_is_interactive", lambda: True)
        monkeypatch.setenv("MATRIX_HOMESERVER", "https://matrix.prefill.example")
        monkeypatch.setenv("MATRIX_SERVER_NAME", "prefill.example")
        target = tmp_path / "config.yaml"
        with patch.object(
            setup_matrix,
            "_probe_matrix_server",
            return_value=_R("https://matrix.prefill.example", False, True, False),
        ):
            result = runner.invoke(
                app,
                ["config", "init", "--path", str(target), "--matrix-server", "self-hosted", "--provider", "openai"],
                input="\n@not-a-domain\n\ntoken\n${BAD}\ntok-valid\n\n",
            )
        assert result.exit_code == 0, result.output
        output = " ".join(result.output.split())
        assert "Invalid Matrix server name." in output
        assert "python-dotenv would not read its value back unchanged" in output
        env_vals = dotenv_values(tmp_path / ".env")
        assert env_vals["MATRIX_HOMESERVER"] == "https://matrix.prefill.example"
        assert env_vals["MATRIX_SERVER_NAME"] == "prefill.example"
        assert env_vals["MATRIX_REGISTRATION_TOKEN"] == "tok-valid"  # noqa: S105

    def test_provisioning_probe_detail_matches_service_contract(self) -> None:
        """Keep `PROVISIONING_MISSING_OPENID_DETAIL` aligned with `scripts/local_mindroom_provisioning_service.py`."""
        import scripts.local_mindroom_provisioning_service as service  # noqa: PLC0415

        assert service.OPENID_TOKEN_HEADER == "X-Matrix-OpenID-Token"  # noqa: S105
        source = service.__file__
        assert source is not None
        assert f'detail="{_PROVISIONING_MISSING_OPENID_DETAIL}"' in Path(source).read_text(encoding="utf-8")
