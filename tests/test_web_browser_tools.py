"""The host browser opener hands only http(s) URLs to the operating system."""

from __future__ import annotations

import webbrowser

import pytest

from mindroom.tools.web_browser_tools import web_browser_tools


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/hosts",
        "smb://example/share",
        "vscode://vscode.git/clone?url=x",
        "javascript:alert(1)",
        "example.com",
        " https://example.com",
    ],
)
def test_open_page_opens_only_http_urls(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """Local files, application schemes, and scheme-less strings never reach the OS opener."""
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open_new_tab", opened.append)
    monkeypatch.setattr(webbrowser, "open_new", opened.append)
    tool = web_browser_tools()()

    refusal = tool.open_page(url)
    tool.open_page("HTTPS://example.com/page", new_window=True)

    assert refusal is not None
    assert refusal.startswith("Refused to open")
    assert opened == ["HTTPS://example.com/page"]
