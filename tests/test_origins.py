"""Tests for shared browser origin helpers."""

import pytest

from mindroom.origins import normalized_origin, parse_browser_origin


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
    ],
)
def test_parse_browser_origin_accepts(value: str, expected: str) -> None:
    """Explicit HTTPS and loopback HTTP origins pass."""
    assert parse_browser_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://192.168.1.10:8080",
        "http://app.example",
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
