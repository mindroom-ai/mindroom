"""Tests for CLI connect helper functions."""

from __future__ import annotations

import io
import stat
from typing import TYPE_CHECKING

import httpx
import pytest
import yaml
from rich.console import Console

import mindroom.cli.connect as cli_connect
from mindroom.constants import OWNER_MATRIX_USER_ID_ENV, OWNER_MATRIX_USER_ID_PLACEHOLDER, resolve_primary_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _fake_transport(
    responses: list[httpx.Response],
    calls: list[tuple[str, dict[str, object]]],
) -> Callable[..., httpx.Response]:
    def _post(url: str, *, json: dict[str, object], **_kwargs: object) -> httpx.Response:
        calls.append((url, json))
        return responses.pop(0)

    return _post


_START = {
    "pair_code": "ABCD-EFGH",
    "device_secret": "device-secret",
    "approve_url": "https://chat.example/connect?code=ABCD-EFGH",
    "expires_at": "2026-09-26T12:10:00Z",
    "poll_interval_seconds": 3,
}
_CONNECTED = {
    "status": "connected",
    "client_id": "client-123",
    "client_secret": "secret-123",
    "namespace": "a1b2c3d4",
    "owner_user_id": "@alice:mindroom.chat",
    "connection": {
        "id": "client-123",
        "client_name": "devbox",
        "fingerprint": "sha256:test",
        "namespace": "a1b2c3d4",
        "created_at": "2026-09-26T12:01:00Z",
        "last_seen_at": "2026-09-26T12:01:00Z",
    },
}


def test_run_device_pairing_polls_until_connected() -> None:
    """The flow announces the link, polls at the server interval, and returns credentials."""
    calls: list[tuple[str, dict[str, object]]] = []
    announced: list[cli_connect.DevicePairSession] = []
    sleeps: list[float] = []
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(200, json={"status": "pending"}),
            httpx.Response(200, json=_CONNECTED),
        ],
        calls,
    )

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example/",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=announced.append,
        post_request=post,
        sleep=sleeps.append,
    )

    assert [url for url, _ in calls] == [
        "https://provisioning.example/v1/local-mindroom/pair/device/start",
        "https://provisioning.example/v1/local-mindroom/pair/device/poll",
        "https://provisioning.example/v1/local-mindroom/pair/device/poll",
    ]
    assert calls[0][1] == {"client_name": "devbox", "client_pubkey_or_fingerprint": "sha256:test"}
    assert calls[1][1] == {"device_secret": "device-secret"}
    assert announced[0].approve_url == "https://chat.example/connect?code=ABCD-EFGH"
    assert sleeps == [3, 3]
    assert result.client_id == "client-123"
    assert result.owner_user_id == "@alice:mindroom.chat"


def test_run_device_pairing_starts_a_new_code_after_expiry() -> None:
    """Unattended runs keep waiting: an expired code is replaced and announced again."""
    calls: list[tuple[str, dict[str, object]]] = []
    announced: list[cli_connect.DevicePairSession] = []
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(200, json={"status": "expired"}),
            httpx.Response(200, json={**_START, "pair_code": "WXYZ-2345", "device_secret": "second"}),
            httpx.Response(200, json=_CONNECTED),
        ],
        calls,
    )

    cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=announced.append,
        post_request=post,
        sleep=lambda _seconds: None,
    )

    assert [session.pair_code for session in announced] == ["ABCD-EFGH", "WXYZ-2345"]
    assert calls[-1][1] == {"device_secret": "second"}


def test_run_device_pairing_reports_service_errors() -> None:
    """Service errors surface with their detail."""
    post = _fake_transport([httpx.Response(429, json={"detail": "Rate limit exceeded"})], [])

    with pytest.raises(ValueError, match=r"Pairing failed \(429\): Rate limit exceeded"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )


def test_run_device_pairing_flags_malformed_owner_user_id() -> None:
    """A malformed owner from the service is flagged instead of persisted."""
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(200, json={**_CONNECTED, "owner_user_id": "alice"})],
        [],
    )

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=post,
        sleep=lambda _seconds: None,
    )

    assert result.owner_user_id is None
    assert result.owner_user_id_invalid is True


def test_render_qr_draws_the_link_with_block_characters() -> None:
    """The QR code is text so it works over SSH and in logs."""
    qr = cli_connect.render_qr("https://chat.example/connect?code=ABCD-EFGH")

    assert qr.count("\n") > 10
    assert set(qr) <= {"█", "▀", "▄", " ", "\n"}


def test_persist_local_provisioning_env_writes_credentials_only(tmp_path: Path) -> None:
    """Persisted .env should contain provisioning credentials but not owner-user config."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\nrouter:\n  model: default\n")

    env_path = cli_connect.persist_local_provisioning_env(
        provisioning_url="https://provisioning.example",
        client_id="client-123",
        client_secret="secret-123",  # noqa: S106
        namespace="a1b2c3d4",
        config_path=config_path,
    )

    assert env_path == tmp_path / ".env"
    content = env_path.read_text()
    assert "MINDROOM_PROVISIONING_URL=https://provisioning.example" in content
    assert "MINDROOM_LOCAL_CLIENT_ID=client-123" in content
    assert "MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in content
    assert "MINDROOM_NAMESPACE=a1b2c3d4" in content
    assert "MINDROOM_OWNER_USER_ID=" not in content
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600


def test_persist_local_provisioning_env_writes_owner_when_available(tmp_path: Path) -> None:
    """Persisted owner MXID lets a later config init replace owner access placeholders."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\nrouter:\n  model: default\n")

    env_path = cli_connect.persist_local_provisioning_env(
        provisioning_url="https://provisioning.example",
        client_id="client-123",
        client_secret="secret-123",  # noqa: S106
        namespace="a1b2c3d4",
        owner_user_id="@alice:mindroom.chat",
        config_path=config_path,
    )

    content = env_path.read_text()
    assert f"{OWNER_MATRIX_USER_ID_ENV}=@alice:mindroom.chat" in content


def test_replace_owner_placeholders_in_config_accepts_server_port(tmp_path: Path) -> None:
    """Placeholder replacement should quote MXIDs so '@' doesn't break YAML."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "authorization:\n"
        "  global_users:\n"
        f"    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n"
        "  agent_reply_permissions:\n"
        '    "*":\n'
        "      - __PLACEHOLDER__\n",
    )

    replaced = cli_connect.replace_owner_placeholders_in_config(
        config_path=config_path,
        owner_user_id="@alice:mindroom.chat:8448",
    )

    assert replaced is True
    updated = config_path.read_text()
    assert OWNER_MATRIX_USER_ID_PLACEHOLDER not in updated
    assert "__PLACEHOLDER__" not in updated
    # Value must be YAML-quoted so the leading '@' doesn't break parsing
    assert '"@alice:mindroom.chat:8448"' in updated

    # Verify the result is valid YAML
    parsed = yaml.safe_load(updated)
    assert parsed["authorization"]["global_users"] == ["@alice:mindroom.chat:8448"]
    assert parsed["authorization"]["agent_reply_permissions"]["*"] == ["@alice:mindroom.chat:8448"]


def test_replace_owner_placeholders_reaches_included_files(tmp_path: Path) -> None:
    """Placeholders living in !include files are replaced too."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("authorization: !include auth.yaml\n", encoding="utf-8")
    auth_path = tmp_path / "auth.yaml"
    auth_path.write_text(
        f"global_users:\n  - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n",
        encoding="utf-8",
    )

    replaced = cli_connect.replace_owner_placeholders_in_config(
        config_path=config_path,
        owner_user_id="@alice:mindroom.chat",
    )

    assert replaced is True
    assert config_path.read_text(encoding="utf-8") == "authorization: !include auth.yaml\n"
    updated = auth_path.read_text(encoding="utf-8")
    assert OWNER_MATRIX_USER_ID_PLACEHOLDER not in updated
    assert yaml.safe_load(updated)["global_users"] == ["@alice:mindroom.chat"]


def test_run_device_pairing_renews_after_404_from_poll() -> None:
    """A 404 from poll (pruned session) triggers renewal like explicit expired status."""
    calls: list[tuple[str, dict[str, object]]] = []
    announced: list[cli_connect.DevicePairSession] = []
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(404, json={"detail": "Pair session not found"}),
            httpx.Response(200, json={**_START, "pair_code": "WXYZ-2345", "device_secret": "second"}),
            httpx.Response(200, json=_CONNECTED),
        ],
        calls,
    )

    cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=announced.append,
        post_request=post,
        sleep=lambda _seconds: None,
    )

    assert [session.pair_code for session in announced] == ["ABCD-EFGH", "WXYZ-2345"]


def test_run_device_pairing_retries_transient_poll_failures() -> None:
    """Polling retries on 503, sleeps the poll interval, then continues."""
    calls: list[tuple[str, dict[str, object]]] = []
    sleeps: list[float] = []
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(503, json={"detail": "Service unavailable"}),
            httpx.Response(200, json=_CONNECTED),
        ],
        calls,
    )

    cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=post,
        sleep=sleeps.append,
    )

    assert sleeps == [3, 3]


def test_run_device_pairing_retries_network_errors_during_polling() -> None:
    """Network errors during polling sleep and retry."""
    calls: list[tuple[str, dict[str, object]]] = []
    sleeps: list[float] = []

    def _post(url: str, *, json: dict[str, object], **_kwargs: object) -> httpx.Response:
        calls.append((url, json))
        if len(calls) == 1:
            return httpx.Response(200, json=_START)
        if len(calls) == 2:
            msg = "Connection refused"
            raise httpx.ConnectError(msg)
        return httpx.Response(200, json=_CONNECTED)

    cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=_post,
        sleep=sleeps.append,
    )

    assert len(calls) == 3
    assert sleeps == [3, 3]


def test_run_device_pairing_fails_on_non_transient_poll_errors() -> None:
    """A 400 from poll is fatal, not retried."""
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(400, json={"detail": "Bad request"}),
        ],
        [],
    )

    with pytest.raises(ValueError, match=r"Pairing failed \(400\): Bad request"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )


def test_run_device_pairing_validates_poll_interval() -> None:
    """Only positive int poll intervals are accepted; others fall back to 3."""
    for invalid in [None, 0, -5, True, False, "3"]:
        started = {**_START, "poll_interval_seconds": invalid}
        post = _fake_transport(
            [httpx.Response(200, json=started), httpx.Response(200, json=_CONNECTED)],
            [],
        )
        result = cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )
        assert result.client_id == "client-123"


def test_run_device_pairing_fails_on_unknown_poll_status() -> None:
    """Unknown poll status raises ValueError instead of polling forever."""
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(200, json={"status": "unknown"}),
        ],
        [],
    )

    with pytest.raises(ValueError, match=r"Unexpected poll status: unknown"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )


def test_run_device_pairing_fails_on_missing_poll_status() -> None:
    """Missing poll status raises ValueError."""
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(200, json={}),
        ],
        [],
    )

    with pytest.raises(ValueError, match=r"Poll response missing status"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )


def test_run_device_pairing_rejects_invalid_json() -> None:
    """Invalid JSON from connected poll raises ValueError."""
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(200, text="not-json")],
        [],
    )

    with pytest.raises(ValueError, match="invalid JSON"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )


def test_run_device_pairing_rejects_non_object_json() -> None:
    """Non-object JSON from connected poll raises TypeError."""
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(200, json=["not", "an", "object"])],
        [],
    )

    with pytest.raises(TypeError, match="unexpected response"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )


def test_run_device_pairing_uses_empty_namespace_when_missing() -> None:
    """Missing namespace preserves the unnamespaced install default."""
    connected = {**_CONNECTED}
    del connected["namespace"]
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(200, json=connected)],
        [],
    )

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=post,
        sleep=lambda _seconds: None,
    )

    assert result.namespace == ""
    assert result.namespace_invalid is False


@pytest.mark.parametrize("namespace", [None, 123, ["a1b2c3d4"], {"value": "a1b2c3d4"}])
def test_run_device_pairing_uses_empty_namespace_when_non_string(namespace: object) -> None:
    """Non-string namespaces fall back to unnamespaced default."""
    connected = {**_CONNECTED, "namespace": namespace}
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(200, json=connected)],
        [],
    )

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=post,
        sleep=lambda _seconds: None,
    )

    assert result.namespace == ""
    assert result.namespace_invalid is False


def test_pair_local_install_shows_qr_only_when_terminal(tmp_path: Path) -> None:
    """QR code appears only when console.is_terminal is True."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\nrouter:\n  model: default\n")
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, process_env={})

    # Terminal: QR shown
    terminal_out = io.StringIO()
    terminal_console = Console(file=terminal_out, force_terminal=True)
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)],
        [],
    )

    cli_connect.pair_local_install(
        runtime_paths,
        console=terminal_console,
        provisioning_url="https://provisioning.example",
        persist_env=False,
        post_request=post,
        sleep=lambda _seconds: None,
    )

    terminal_output = terminal_out.getvalue()
    assert "or enter code ABCD-EFGH" in terminal_output
    assert "█" in terminal_output or "▀" in terminal_output  # QR present

    # Non-terminal: QR hidden
    non_terminal_out = io.StringIO()
    non_terminal_console = Console(file=non_terminal_out, force_terminal=False)
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)],
        [],
    )

    cli_connect.pair_local_install(
        runtime_paths,
        console=non_terminal_console,
        provisioning_url="https://provisioning.example",
        persist_env=False,
        post_request=post,
        sleep=lambda _seconds: None,
    )

    non_terminal_output = non_terminal_out.getvalue()
    assert "or enter code ABCD-EFGH" in non_terminal_output
    assert "█" not in non_terminal_output
    assert "▀" not in non_terminal_output


def test_run_device_pairing_raises_on_expired_when_no_renew() -> None:
    """With renew_expired=False, an expired status raises instead of renewing."""
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(200, json={"status": "expired"}),
        ],
        [],
    )

    with pytest.raises(ValueError, match=r"Approval timed out\. Run the command again to get a new link\."):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
            renew_expired=False,
        )


def test_run_device_pairing_raises_on_404_when_no_renew() -> None:
    """With renew_expired=False, a 404 from poll raises instead of renewing."""
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(404, json={"detail": "Pair session not found"}),
        ],
        [],
    )

    with pytest.raises(ValueError, match=r"Approval timed out\. Run the command again to get a new link\."):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
            renew_expired=False,
        )


def test_run_device_pairing_renews_when_allowed() -> None:
    """With renew_expired=True (default), expired sessions renew."""
    announced: list[cli_connect.DevicePairSession] = []
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            httpx.Response(200, json={"status": "expired"}),
            httpx.Response(200, json={**_START, "pair_code": "WXYZ-2345"}),
            httpx.Response(200, json=_CONNECTED),
        ],
        [],
    )

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=announced.append,
        post_request=post,
        sleep=lambda _seconds: None,
        renew_expired=True,
    )

    assert [session.pair_code for session in announced] == ["ABCD-EFGH", "WXYZ-2345"]
    assert result.client_id == "client-123"
