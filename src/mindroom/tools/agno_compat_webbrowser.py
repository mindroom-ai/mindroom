"""Host browser opener limited to http(s) URLs."""

from __future__ import annotations

from typing import override

from agno.tools.webbrowser import WebBrowserTools


class MindRoomWebBrowserTools(WebBrowserTools):
    """Open only http(s) URLs, so the host never hands local files or other URI schemes to their OS handlers."""

    # AGNO_COMPAT: WebBrowserTools hands any URI scheme to the host's URL handlers.
    # Reason: Agno 3.0.9 passes the model's URL straight to webbrowser, which opens file: paths and
    # custom application schemes through xdg-open, open, or os.startfile, not only web pages.
    # Upstream issue: Tracking gap; upstream tracking has not been verified.
    # Upstream PR: None identified.
    # Remove when: WebBrowserTools opens only http and https URLs.
    # Coverage: tests/test_web_browser_tools.py.
    @override
    def open_page(self, url: str, new_window: bool = False) -> str | None:
        """Open an http or https URL in a browser window.

        Args:
            url (str): http or https URL to open
            new_window (bool): If True, open in a new window, otherwise open in a new tab. Default is False.

        Returns:
            None, or an error message when the URL is not http or https

        """
        if not url.lower().startswith(("http://", "https://")):
            return f"Refused to open '{url}': only http and https URLs can be opened."
        return super().open_page(url, new_window)
