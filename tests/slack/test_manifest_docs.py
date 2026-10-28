"""The Slack setup page's manifest asks Slack for what the code uses."""

from __future__ import annotations

import re
import textwrap
from pathlib import Path
from typing import Any

import yaml

from mindroom.api.slack_events import router
from mindroom.slack.oauth import SLACK_USER_SCOPES, slack_oauth_provider

_PAGE = Path(__file__).parents[2] / "docs" / "tools" / "slack.md"
_ORIGIN = "https://mindroom.example.com"


def _manifest() -> dict[str, Any]:
    """Return the page's one YAML block that holds `oauth_config`."""
    blocks = re.findall(r"```yaml\n(.*?)```", _PAGE.read_text(encoding="utf-8"), re.DOTALL)
    manifests = [yaml.safe_load(textwrap.dedent(block)) for block in blocks if "oauth_config" in block]
    assert len(manifests) == 1
    return manifests[0]


def test_manifest_requests_the_user_scopes_the_provider_asks_for() -> None:
    """People approve exactly the scopes the app's manifest grants, and the bot can map authors by email."""
    scopes = _manifest()["oauth_config"]["scopes"]

    assert scopes["user"] == list(SLACK_USER_SCOPES)
    assert "users:read.email" in scopes["bot"]


def test_manifest_points_slack_at_the_routes_mindroom_serves() -> None:
    """The redirect and events URLs are the OAuth callback and the public events route."""
    manifest = _manifest()
    event_paths = {route.path for route in router.routes}

    assert manifest["oauth_config"]["redirect_urls"] == [f"{_ORIGIN}{slack_oauth_provider().default_redirect_path}"]
    assert manifest["settings"]["event_subscriptions"]["request_url"].removeprefix(_ORIGIN) in event_paths


def test_manifest_keeps_rotation_and_socket_mode_off() -> None:
    """Rotating user tokens would never be refreshed, and Socket Mode delivers no events over HTTP."""
    settings = _manifest()["settings"]

    assert settings["token_rotation_enabled"] is False
    assert settings["socket_mode_enabled"] is False
