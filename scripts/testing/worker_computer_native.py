"""Small native MCP and noVNC helpers for the real worker acceptance driver."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page


async def connect_viewer(page: Page, session: dict[str, Any]) -> None:
    """Wait for noVNC's asynchronous module before using the public fixture hook."""
    await page.wait_for_function("typeof window.connectComputer === 'function'", timeout=30000)
    await page.evaluate("session=>window.connectComputer(session)", session)
    await page.wait_for_function("window.probe.connected || window.probe.disconnected")
    assert await page.evaluate("window.probe.connected && !window.probe.disconnected")


def native_json(text: str) -> Any:  # noqa: ANN401 - native page evaluation returns arbitrary JSON
    """Parse the server's result section without interpreting code or snapshots."""
    return json.loads(text.split("### Result\n", 1)[1].split("\n### ", 1)[0].strip())


def native_tabs(text: str) -> list[dict[str, Any]]:
    """Read current native tab indices; they are intentionally not stable targets."""
    return [
        {"index": int(index), "current": bool(current), "title": title, "url": url}
        for index, current, title, url in re.findall(
            r"^- (\d+): (\(current\) )?\[(.*?)\]\((.*?)\)$", text, re.MULTILINE
        )
    ]
