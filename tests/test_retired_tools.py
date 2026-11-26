"""Retired tool names fail config loading rather than granting a replacement."""

from pathlib import Path

import pytest

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.registry import builtin_tool_registry


@pytest.mark.parametrize(
    ("name", "guidance"),
    [
        ("web_browser_tools", r"`browser`"),
        ("jira", r"`atlassian`"),
        ("confluence", r"`atlassian`"),
        ("python", r"`shell`.*`filesystem`"),
    ],
)
@pytest.mark.parametrize("scope", ["defaults", "agent", "worker"])
def test_retired_tool_points_to_explicit_replacement(tmp_path: Path, name: str, guidance: str, scope: str) -> None:
    """Removed names, including entries with overrides, explain how to replace the toolkit."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    agent: dict[str, object] = {"display_name": "Helper"}
    defaults: dict[str, object] = {}
    if scope == "defaults":
        defaults["tools"] = [name]
    elif scope == "agent":
        agent["tools"] = [{name: {"all": True}}]
    else:
        agent["worker_tools"] = [name]

    with pytest.raises(ConfigRuntimeValidationError, match=rf"Tool '{name}' was removed; .*{guidance}"):
        Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "fake-model"}},
                "defaults": defaults,
                "agents": {"helper": agent},
            },
            paths,
        )
    assert name not in builtin_tool_registry().entries
