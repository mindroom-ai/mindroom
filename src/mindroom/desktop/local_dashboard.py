"""Private native-helper handoff for the standalone local dashboard."""

# Stable private-pipe errors are intentionally user-facing.
# ruff: noqa: EM101, TRY003

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from mindroom.constants import DEFAULT_MINDROOM_URL, resolve_runtime_paths
from mindroom.desktop.native_protocol import NativeProtocolError

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


_LOCAL_URL = re.compile(r"http://(localhost|127\.0\.0\.1|\[::1\]):([0-9]{1,5})/?\Z", re.ASCII)


def _canonical_loopback_url(raw_url: str) -> str:
    match = _LOCAL_URL.fullmatch(raw_url)
    if match is None:
        raise ValueError("invalid dashboard URL")
    port = int(match.group(2))
    if not 1 <= port <= 65535:
        raise ValueError("invalid dashboard port")
    host = "127.0.0.1" if match.group(1) == "localhost" else match.group(1)
    return f"http://{host}:{port}"


def local_dashboard_configuration(runtime_paths: RuntimePaths) -> dict[str, object]:
    """Read current config-adjacent credentials and return only a safe loopback target."""
    try:
        current = resolve_runtime_paths(
            config_path=runtime_paths.config_path,
            process_env=dict(runtime_paths.process_env),
        )
        raw_url = current.env_value("MINDROOM_URL", default=DEFAULT_MINDROOM_URL)
        return {"url": _canonical_loopback_url(raw_url or ""), "api_key": current.env_value("MINDROOM_API_KEY") or None}
    except Exception as exc:
        # Neither parser errors nor URLs may carry a credential into status or wire errors.
        raise NativeProtocolError(
            "dashboard_configuration_invalid",
            "Local dashboard configuration is invalid. Check MINDROOM_URL in the config-adjacent .env.",
            recovery="Use an HTTP URL with a literal loopback host and port, then retry.",
        ) from exc
