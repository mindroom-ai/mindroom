"""Tests for shared browser origin helpers."""

from pathlib import Path

import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.origins import (
    allowed_client_origins,
    connections_client_origin_allowed,
    connections_sign_in_enabled,
    normalized_origin,
    parse_browser_origin,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Host.Example:443/", "https://host.example"),
        ("HTTP://Host.Example:80/a/b?q=1#f", "http://host.example"),
        ("https://host.example:8443/x", "https://host.example:8443"),
        ("http://host.example:443", "http://host.example:443"),
        ("  https://host.example  ", "https://host.example"),
        ("http://[::1]:8080/", "http://[::1]:8080"),
        ("https://[::1]:443", "https://[::1]"),
        ("http://127.0.0.1:80", "http://127.0.0.1"),
        ("http://[0:0:0:0:0:0:0:1]:8080", "http://[::1]:8080"),
        ("https://xn--fa-hia.de", "https://xn--fa-hia.de"),
    ],
)
def test_normalized_origin(url: str, expected: str) -> None:
    """Origins match what a browser sends."""
    assert normalized_origin(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "host.example",
        "ftp://host.example",
        "capacitor://localhost",
        "https://",
        "https://user@host.example",
        "https://user:pw@host.example",
        "https://host.example:abc",
        "https://host.example:99999",
        "https://host.example:",
        "https://bücher.example",
        "https://faß.de",
    ],
)
def test_normalized_origin_none(url: str | None) -> None:
    """Inputs without a browser origin return None."""
    assert normalized_origin(url) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://App.Example", "https://app.example"),
        ("https://app.example/", "https://app.example"),
        ("https://app.example:443", "https://app.example"),
        ("https://app.example:8443", "https://app.example:8443"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ("http://[::1]:8080", "http://[::1]:8080"),
        ("http://192.168.1.10:8080", "http://192.168.1.10:8080"),
        ("http://10.0.0.2", "http://10.0.0.2"),
        ("http://[fd00::1]:8080", "http://[fd00::1]:8080"),
    ],
)
def test_parse_browser_origin_accepts(value: str, expected: str) -> None:
    """Explicit HTTPS origins and HTTP origins on local addresses pass."""
    assert parse_browser_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://app.example",
        "http://100.64.0.1",
        "http://8.8.8.8",
        "ftp://app.example",
        "capacitor://localhost",
        "https://app.example/path",
        "https://app.example?x=1",
        "https://app.example/?x=1",
        "https://app.example#frag",
        "https://app.example:",
        "https://app.example:abc",
        "https://user@app.example",
        "https://*.example.com",
        "https://app.example *",
        "https://app.example\n",
        " https://app.example",
        "https://app\t.example",
    ],
)
def test_parse_browser_origin_rejects(value: str) -> None:
    """Everything else raises."""
    with pytest.raises(ValueError, match="rowser origin"):
        parse_browser_origin(value)


def test_allowed_client_origins_reads_named_env(tmp_path: Path) -> None:
    """Reads exact origins from the named env var."""
    paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": '["https://chat.example.org", "capacitor://localhost"]',
        },
    )
    assert allowed_client_origins(paths, "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS") == (
        "https://chat.example.org",
        "capacitor://localhost",
    )
    # Computer env unset returns empty tuple
    assert allowed_client_origins(paths, "MINDROOM_COMPUTER_ALLOWED_ORIGINS") == ()


def test_allowed_client_origins_fails_closed(tmp_path: Path) -> None:
    """Invalid configuration fails closed with empty tuple."""
    # Wildcard rejected
    wildcard_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": '["https://ok.example.org", "https://*.example.org"]',
        },
    )
    assert allowed_client_origins(wildcard_paths, "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS") == ()
    # Non-list rejected
    non_list_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path,
        process_env={
            "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": '"https://chat.example.org"',
        },
    )
    assert allowed_client_origins(non_list_paths, "MINDROOM_CONNECTIONS_ALLOWED_ORIGINS") == ()


def _paths(tmp_path: Path, **env: str) -> RuntimePaths:
    return resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path, process_env=env)


PUBLIC = "https://assistant.example.org"
LAN = "http://192.168.1.10:8765"


@pytest.mark.parametrize(
    ("client_origin", "server_origin", "allowed"),
    [
        ("https://chat.mindroom.chat", PUBLIC, True),
        ("https://chat.mindroom.chat", LAN, True),
        ("http://192.168.1.10:8080", LAN, True),
        ("http://localhost:8080", "http://localhost:8765", True),
        ("http://192.168.1.5:8080", PUBLIC, False),
        ("http://localhost:8080", PUBLIC, False),
        ("http://192.168.1.10:8080", None, False),
        ("https://chat.example.org", PUBLIC, False),
        ("https://chat.example.org", LAN, False),
        ("http://192.168.1.10:8080/", LAN, False),
    ],
)
def test_connections_client_origins_default(
    tmp_path: Path,
    client_origin: str,
    server_origin: str | None,
    allowed: bool,
) -> None:
    """Unset, MindRoom's hosted Chat is accepted everywhere, and a local Chat only by a server on a local address."""
    for env in ({}, {"MINDROOM_CONNECTIONS_ALLOWED_ORIGINS": "  "}):
        paths = _paths(tmp_path, **env)
        assert connections_client_origin_allowed(paths, client_origin, server_origin) is allowed
        assert connections_sign_in_enabled(paths)


def test_configured_connections_client_origins_replace_the_defaults(tmp_path: Path) -> None:
    """A configured list is exact, and an empty or invalid list turns Matrix sign-in off."""
    paths = _paths(tmp_path, MINDROOM_CONNECTIONS_ALLOWED_ORIGINS='["https://chat.example.org"]')
    assert connections_client_origin_allowed(paths, "https://chat.example.org", PUBLIC)
    assert not connections_client_origin_allowed(paths, "https://chat.mindroom.chat", PUBLIC)
    assert not connections_client_origin_allowed(paths, "http://192.168.1.10:8080", LAN)
    assert connections_sign_in_enabled(paths)
    for configured in ("[]", '["https://*.example.org"]'):
        off = _paths(tmp_path, MINDROOM_CONNECTIONS_ALLOWED_ORIGINS=configured)
        assert not connections_client_origin_allowed(off, "https://chat.mindroom.chat", PUBLIC)
        assert not connections_client_origin_allowed(off, "http://192.168.1.10:8080", LAN)
        assert not connections_sign_in_enabled(off)
