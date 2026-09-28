"""Tests for CLI connect helper functions."""

from __future__ import annotations

import io
import stat
from datetime import UTC, datetime, timedelta
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

    from mindroom.constants import RuntimePaths


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
    "expires_at": "2099-09-26T12:10:00Z",
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
    """Interactive pairing fails fast and surfaces service errors with their detail."""
    sleeps: list[float] = []
    post = _fake_transport([httpx.Response(429, json={"detail": "Rate limit exceeded"})], [])

    with pytest.raises(ValueError, match=r"Pairing failed \(429\): Rate limit exceeded"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=sleeps.append,
            renew_expired=False,
        )

    assert sleeps == []


def test_run_device_pairing_retries_transient_start_failures_when_renewing() -> None:
    """Unattended runs retry an unreachable, rate-limited, or failing service with bounded backoff."""
    sleeps: list[float] = []
    retries: list[str] = []
    calls: list[tuple[str, dict[str, object]]] = []
    responses = [
        httpx.Response(503, json={"detail": "Service unavailable"}),
        httpx.Response(429, json={"detail": "Rate limit exceeded"}),
        *[httpx.Response(502, json={"detail": "Bad gateway"}) for _ in range(4)],
        httpx.Response(200, json=_START),
        httpx.Response(200, json=_CONNECTED),
    ]

    def _post(url: str, *, json: dict[str, object], **_kwargs: object) -> httpx.Response:
        calls.append((url, json))
        if len(calls) == 1:
            msg = "Network is unreachable"
            raise httpx.ConnectError(msg)
        return responses.pop(0)

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=_post,
        sleep=sleeps.append,
        warn=retries.append,
    )

    assert result is not None
    assert result.client_id == "client-123"
    assert sleeps == [3, 6, 12, 24, 30, 30, 30, 3]
    assert len(retries) == 7
    assert retries[0] == "Could not reach provisioning service: Network is unreachable (retrying in 3s)"


def test_run_device_pairing_does_not_retry_permanent_start_failures() -> None:
    """A non-transient start failure still exits even when renewing."""
    post = _fake_transport([httpx.Response(400, json={"detail": "Bad request"})], [])

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


def test_run_device_pairing_fails_fast_on_unreachable_service_without_renewal() -> None:
    """Interactive pairing does not retry an unreachable service."""

    def _post(_url: str, **_kwargs: object) -> httpx.Response:
        msg = "Network is unreachable"
        raise httpx.ConnectError(msg)

    with pytest.raises(ValueError, match="Could not reach provisioning service"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=_post,
            sleep=lambda _seconds: None,
            renew_expired=False,
        )


def test_run_device_pairing_stops_when_another_process_paired() -> None:
    """A waiting run stops polling once credentials appear from elsewhere."""
    calls: list[tuple[str, dict[str, object]]] = []
    checks: list[bool] = []
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(200, json={"status": "pending"})],
        calls,
    )

    def _stop_waiting() -> bool:
        checks.append(True)
        # Checked before the start, then before each poll: stop before the second poll.
        return len(checks) > 2

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=post,
        sleep=lambda _seconds: None,
        stop_waiting=_stop_waiting,
    )

    assert result is None
    assert len(calls) == 2
    assert len(checks) == 3


def test_run_device_pairing_does_not_announce_a_new_code_after_another_process_paired() -> None:
    """An expired code is not renewed once credentials appeared from elsewhere."""
    announced: list[cli_connect.DevicePairSession] = []
    paired_elsewhere: list[bool] = [False]
    responses = [httpx.Response(200, json=_START), httpx.Response(200, json={"status": "expired"})]

    def _post(url: str, **_kwargs: object) -> httpx.Response:
        # Another process pairs while this code expires.
        paired_elsewhere[0] = url.endswith("/poll")
        return responses.pop(0)

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=announced.append,
        post_request=_post,
        sleep=lambda _seconds: None,
        stop_waiting=lambda: paired_elsewhere[0],
    )

    assert result is None
    assert len(announced) == 1


def test_run_device_pairing_stops_retrying_start_after_another_process_paired() -> None:
    """Start retries end once credentials appeared from elsewhere."""
    calls: list[tuple[str, dict[str, object]]] = []
    sleeps: list[float] = []
    post = _fake_transport([httpx.Response(503, json={"detail": "Service unavailable"})], calls)

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=post,
        sleep=sleeps.append,
        stop_waiting=lambda: bool(sleeps),
    )

    assert result is None
    assert len(calls) == 1
    assert sleeps == [3]


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


@pytest.mark.parametrize(
    ("expires_at", "error"),
    [(None, r"Provisioning response missing expires_at"), ("soon", r"Pairing response has invalid expires_at")],
)
def test_run_device_pairing_requires_a_valid_expires_at(expires_at: str | None, error: str) -> None:
    """The outage deadline depends on the session expiry, so a missing or malformed one fails the start."""
    post = _fake_transport([httpx.Response(200, json={**_START, "expires_at": expires_at})], [])

    with pytest.raises(ValueError, match=error):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )


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


def _runtime_with_config(tmp_path: Path) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("models: {}\nagents: {}\nrouter:\n  model: default\n")
    return resolve_primary_runtime_paths(config_path=config_path, process_env={})


def test_pair_local_install_continues_without_saving_when_paired_elsewhere(tmp_path: Path) -> None:
    """Credentials written by another process end the wait without persisting anything."""
    runtime_paths = _runtime_with_config(tmp_path)
    out = io.StringIO()
    post = _fake_transport([httpx.Response(200, json=_START)], [])

    result = cli_connect.pair_local_install(
        runtime_paths,
        console=Console(file=out, width=200),
        provisioning_url="https://provisioning.example",
        post_request=post,
        sleep=lambda _seconds: None,
        stop_waiting=lambda: True,
    )

    assert result is None
    assert "This machine was paired by another MindRoom process; continuing." in out.getvalue()
    assert not (tmp_path / ".env").exists()


def test_pair_local_install_prints_credentials_when_env_cannot_be_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One-time credentials are shown for manual saving when .env is not writable."""
    runtime_paths = _runtime_with_config(tmp_path)
    out = io.StringIO()
    post = _fake_transport([httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)], [])

    def _read_only(*_args: object, **_kwargs: object) -> Path:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(cli_connect, "upsert_env_values", _read_only)

    with pytest.raises(ValueError, match=r"Could not save credentials to .*\.env: \[Errno 30\] Read-only file system"):
        cli_connect.pair_local_install(
            runtime_paths,
            console=Console(file=out, width=200),
            provisioning_url="https://provisioning.example",
            post_request=post,
            sleep=lambda _seconds: None,
        )

    output = out.getvalue()
    assert "export MINDROOM_LOCAL_CLIENT_ID=client-123" in output
    assert "export MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in output
    assert "export MINDROOM_NAMESPACE=a1b2c3d4" in output


def test_pair_local_install_prints_credentials_when_env_write_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused write such as a symlinked or undecodable .env also shows the one-time credentials."""
    runtime_paths = _runtime_with_config(tmp_path)
    out = io.StringIO()
    post = _fake_transport([httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)], [])

    def _refuse(*_args: object, **_kwargs: object) -> Path:
        msg = "Refusing to write .env through a symlink"
        raise ValueError(msg)

    monkeypatch.setattr(cli_connect, "upsert_env_values", _refuse)

    with pytest.raises(
        ValueError,
        match=r"Could not save credentials to .*: Refusing to write \.env through a symlink",
    ):
        cli_connect.pair_local_install(
            runtime_paths,
            console=Console(file=out, width=200),
            provisioning_url="https://provisioning.example",
            post_request=post,
            sleep=lambda _seconds: None,
        )

    assert "export MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in out.getvalue()


def test_pair_local_install_warns_when_owner_placeholders_cannot_be_updated(tmp_path: Path) -> None:
    """Saved credentials are kept and a read-only config only produces a warning."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"authorization:\n  global_users:\n    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n")
    config_path.chmod(0o400)
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, process_env={})
    out = io.StringIO()
    post = _fake_transport([httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)], [])

    result = cli_connect.pair_local_install(
        runtime_paths,
        console=Console(file=out, width=200),
        provisioning_url="https://provisioning.example",
        post_request=post,
        sleep=lambda _seconds: None,
    )

    assert result is not None
    output = out.getvalue()
    assert "Saved credentials to" in output
    assert "Could not update owner placeholder(s)" in output
    assert "MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in (tmp_path / ".env").read_text()
    assert OWNER_MATRIX_USER_ID_PLACEHOLDER in config_path.read_text()


def _approval_install(tmp_path: Path) -> tuple[RuntimePaths, Path]:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"authorization:\n  global_users:\n    - {OWNER_MATRIX_USER_ID_PLACEHOLDER}\n")
    return resolve_primary_runtime_paths(config_path=config_path, process_env={}), config_path


def test_pair_local_install_discards_credentials_when_the_approver_is_not_this_user(tmp_path: Path) -> None:
    """Declining the approving account saves nothing and explains that the connection is unusable."""
    runtime_paths, config_path = _approval_install(tmp_path)
    out = io.StringIO()
    post = _fake_transport([httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)], [])

    with pytest.raises(
        ValueError,
        match=r"Credentials discarded.*revoke it in MindRoom Chat → Settings → Local MindRoom",
    ):
        cli_connect.pair_local_install(
            runtime_paths,
            console=Console(file=out, width=200),
            provisioning_url="https://provisioning.example",
            post_request=post,
            sleep=lambda _seconds: None,
            confirm_approver=lambda: False,
        )

    assert "Approved by @alice:mindroom.chat." in out.getvalue()
    assert "secret-123" not in out.getvalue()
    assert not (tmp_path / ".env").exists()
    assert OWNER_MATRIX_USER_ID_PLACEHOLDER in config_path.read_text()


def test_pair_local_install_asks_about_the_approver_before_saving(tmp_path: Path) -> None:
    """The approving account is confirmed before any credential or config change is written."""
    runtime_paths, config_path = _approval_install(tmp_path)
    out = io.StringIO()
    post = _fake_transport([httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)], [])
    env_existed_when_asked: list[bool] = []

    def _confirm() -> bool:
        env_existed_when_asked.append((tmp_path / ".env").exists())
        return True

    result = cli_connect.pair_local_install(
        runtime_paths,
        console=Console(file=out, width=200),
        provisioning_url="https://provisioning.example",
        post_request=post,
        sleep=lambda _seconds: None,
        confirm_approver=_confirm,
    )

    assert result is not None
    assert env_existed_when_asked == [False]
    assert "Approved by @alice:mindroom.chat." in out.getvalue()
    assert "MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in (tmp_path / ".env").read_text()
    assert "@alice:mindroom.chat" in config_path.read_text()


def test_pair_local_install_names_the_approver_without_a_terminal(tmp_path: Path) -> None:
    """Services and the macOS app cannot answer a prompt, so the approving account is shown with how to revoke it."""
    runtime_paths, _config_path = _approval_install(tmp_path)
    out = io.StringIO()
    post = _fake_transport([httpx.Response(200, json=_START), httpx.Response(200, json=_CONNECTED)], [])

    cli_connect.pair_local_install(
        runtime_paths,
        console=Console(file=out, width=200),
        provisioning_url="https://provisioning.example",
        post_request=post,
        sleep=lambda _seconds: None,
    )

    output = out.getvalue()
    assert "Approved by @alice:mindroom.chat." in output
    assert "If this is not your account" in output
    assert "MindRoom Chat → Settings → Local MindRoom" in output
    assert "MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in (tmp_path / ".env").read_text()


_CLAIMED = httpx.Response(410, json={"detail": "Pair session already claimed"})


def test_run_device_pairing_starts_over_when_the_approval_was_lost() -> None:
    """A lost `connected` response leaves the session claimed, so an unattended run warns and pairs again."""
    announced: list[cli_connect.DevicePairSession] = []
    warnings: list[str] = []
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            _CLAIMED,
            httpx.Response(200, json={**_START, "pair_code": "WXYZ-2345", "device_secret": "second"}),
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
        warn=warnings.append,
    )

    assert result is not None
    assert [session.pair_code for session in announced] == ["ABCD-EFGH", "WXYZ-2345"]
    assert warnings == [
        "The previous approval could not be received; starting a new pairing. "
        "You can revoke the unused entry in MindRoom Chat → Settings → Local MindRoom.",
    ]


def test_run_device_pairing_explains_a_lost_approval_without_renewal() -> None:
    """`mindroom connect` exits with an explanation instead of a generic 410 error."""
    post = _fake_transport([httpx.Response(200, json=_START), _CLAIMED], [])

    with pytest.raises(ValueError, match="approval could not be received") as exc_info:
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

    message = str(exc_info.value)
    assert "410" not in message
    assert "revoke the unused entry in MindRoom Chat → Settings → Local MindRoom." in message
    assert "Run the command again" in message


def test_run_device_pairing_backs_off_while_rate_limited() -> None:
    """Many installs behind one NAT share a poll budget: 429 doubles the wait up to 30s and success resets it."""
    sleeps: list[float] = []
    rate_limited = httpx.Response(429, json={"detail": "Rate limit exceeded"})
    post = _fake_transport(
        [
            httpx.Response(200, json=_START),
            *[rate_limited] * 5,
            httpx.Response(200, json={"status": "pending"}),
            httpx.Response(200, json=_CONNECTED),
        ],
        [],
    )

    result = cli_connect.run_device_pairing(
        provisioning_url="https://provisioning.example",
        client_name="devbox",
        client_fingerprint="sha256:test",
        matrix_ssl_verify=True,
        announce=lambda _session: None,
        post_request=post,
        sleep=sleeps.append,
    )

    assert result is not None
    assert sleeps == [3, 6, 12, 24, 30, 30, 3]


def test_run_device_pairing_times_out_at_expiry_while_the_service_is_unreachable() -> None:
    """`mindroom connect` stops a grace period after the session's expiry even if no poll gets through."""
    expires_at = datetime(2026, 9, 26, 12, 10, tzinfo=UTC)
    clock = [expires_at - timedelta(minutes=10)]
    polls: list[str] = []

    def _sleep(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)

    def _post(url: str, **_kwargs: object) -> httpx.Response:
        if url.endswith("/start"):
            return httpx.Response(200, json={**_START, "expires_at": "2026-09-26T12:10:00Z"})
        polls.append(url)
        msg = "Network is unreachable"
        raise httpx.ConnectError(msg)

    with pytest.raises(ValueError, match=r"Approval timed out\. Run the command again to get a new link\."):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=_post,
            sleep=_sleep,
            renew_expired=False,
            now=lambda: clock[0],
        )

    assert clock[0] == expires_at + timedelta(seconds=60)
    assert len(polls) == 220


@pytest.mark.parametrize(
    ("env", "refused_homeserver"),
    [
        ({"MATRIX_HOMESERVER": "https://matrix.example.org"}, "https://matrix.example.org"),
        ({}, "http://localhost:8008"),
        ({"MATRIX_HOMESERVER": "https://matrix.example.org", "MINDROOM_PROVISIONING_URL": "https://p.example"}, None),
        ({"MATRIX_HOMESERVER": "https://mindroom.chat"}, None),
        ({"MATRIX_HOMESERVER": "https://matrix.mindroom.chat/"}, None),
    ],
)
def test_self_hosted_pairing_error(tmp_path: Path, env: dict[str, str], refused_homeserver: str | None) -> None:
    """Pairing is refused for any effective non-hosted homeserver, including the localhost default, without a provisioning URL."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n")
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, process_env=env)

    error = cli_connect.self_hosted_pairing_error(runtime_paths)

    if refused_homeserver is None:
        assert error is None
    else:
        assert error is not None
        assert refused_homeserver in error
        assert "MATRIX_REGISTRATION_TOKEN" in error
        assert "--provisioning-url" in error


def test_run_device_pairing_treats_other_410s_as_permanent_errors() -> None:
    """Only the service's exact already-claimed detail means a lost approval; other 410s are not retried."""
    post = _fake_transport(
        [httpx.Response(200, json=_START), httpx.Response(410, json={"detail": "Gone"})],
        [],
    )

    with pytest.raises(ValueError, match=r"Pairing failed \(410\): Gone"):
        cli_connect.run_device_pairing(
            provisioning_url="https://provisioning.example",
            client_name="devbox",
            client_fingerprint="sha256:test",
            matrix_ssl_verify=True,
            announce=lambda _session: None,
            post_request=post,
            sleep=lambda _seconds: None,
        )


def test_pair_local_install_does_not_ask_about_an_unidentified_approver(tmp_path: Path) -> None:
    """Nobody can recognize an account the service did not name, so the prompt is skipped and the revoke hint shown."""
    runtime_paths = _runtime_with_config(tmp_path)
    out = io.StringIO()
    connected = {key: value for key, value in _CONNECTED.items() if key != "owner_user_id"}
    post = _fake_transport([httpx.Response(200, json=_START), httpx.Response(200, json=connected)], [])

    def _unexpected_prompt() -> bool:
        msg = "confirm_approver must not be called without an identified approver"
        raise AssertionError(msg)

    result = cli_connect.pair_local_install(
        runtime_paths,
        console=Console(file=out, width=200),
        provisioning_url="https://provisioning.example",
        post_request=post,
        sleep=lambda _seconds: None,
        confirm_approver=_unexpected_prompt,
    )

    assert result is not None
    output = out.getvalue()
    assert "Approved by an account the provisioning service did not identify." in output
    assert "If this is not your account, revoke this connection in MindRoom Chat → Settings → Local MindRoom." in output
    assert "MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in (tmp_path / ".env").read_text()
