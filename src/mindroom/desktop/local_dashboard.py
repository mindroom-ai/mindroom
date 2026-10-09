"""Private native-helper handoff for the standalone local dashboard."""

# Stable private-pipe errors are intentionally user-facing.
# ruff: noqa: EM101, TRY003

from __future__ import annotations

import os
import re
import subprocess
from typing import TYPE_CHECKING

from mindroom.constants import DEFAULT_MINDROOM_URL, resolve_runtime_paths
from mindroom.desktop.native_protocol import NativeProtocolError
from mindroom.services.launchd import manager as launchd_manager

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


_LOCAL_URL = re.compile(r"http://(localhost|127\.0\.0\.1|\[::1\]):([0-9]{1,5})/?\Z", re.ASCII)
_LSOF = "/usr/sbin/lsof"


def _canonical_loopback_url(raw_url: str) -> str:
    match = _LOCAL_URL.fullmatch(raw_url)
    if match is None:
        raise ValueError("invalid dashboard URL")
    port = int(match.group(2))
    if not 1 <= port <= 65535:
        raise ValueError("invalid dashboard port")
    host = "127.0.0.1" if match.group(1) == "localhost" else match.group(1)
    return f"http://{host}:{port}"


def _service_owns_port(port: int) -> bool:
    """Return whether every visible listener on the port is this user's launchd MindRoom job or its runtime child."""
    # `uv tool run` keeps the launchd job alive as the parent of the Python runtime that listens.
    service_pid = launchd_manager.get_service_status().pid
    try:
        listed = subprocess.run(
            [_LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-FpRu"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    listeners: list[dict[str, str]] = []
    for line in listed.stdout.splitlines():
        if line.startswith("p"):
            listeners.append({})
        if listeners and line[:1] in {"p", "R", "u"}:
            listeners[-1][line[0]] = line[1:]
    # Another user's listener is invisible here, but it also keeps the service from binding the port at all.
    return (
        service_pid is not None
        and bool(listeners)
        and all(
            listener.get("u") == str(os.getuid()) and str(service_pid) in {listener.get("p"), listener.get("R")}
            for listener in listeners
        )
    )


def local_dashboard_configuration(runtime_paths: RuntimePaths) -> dict[str, object]:
    """Read current config-adjacent credentials and release the key only to the user's own MindRoom service."""
    try:
        current = resolve_runtime_paths(
            config_path=runtime_paths.config_path,
            process_env=dict(runtime_paths.process_env),
        )
        raw_url = current.env_value("MINDROOM_URL", default=DEFAULT_MINDROOM_URL)
        url = _canonical_loopback_url(raw_url or "")
        api_key = current.env_value("MINDROOM_API_KEY") or None
    except Exception as exc:
        # Neither parser errors nor URLs may carry a credential into status or wire errors.
        raise NativeProtocolError(
            "dashboard_configuration_invalid",
            "Local dashboard configuration is invalid. Check MINDROOM_URL in the config-adjacent .env.",
            recovery="Use an HTTP URL with a literal loopback host and port, then retry.",
        ) from exc
    # Another local user or app can bind the loopback port before the service does.
    if api_key is not None and not _service_owns_port(int(url.rsplit(":", 1)[1])):
        raise NativeProtocolError(
            "dashboard_unavailable",
            "The local dashboard port is not served by your MindRoom service yet.",
            recovery="Start local agents and retry once they are running. Quit any other program using that port.",
        )
    return {"url": url, "api_key": api_key}
