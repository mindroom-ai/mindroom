"""The tools one config can build stay that config's, whatever other configs a process also serves."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.tool_system.bootstrap import ensure_tool_registry_loaded
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tool_system.plugins import load_plugins
from tests.conftest import runtime_paths_for
from tests.test_plugins import _bind_runtime_paths, _preserved_plugin_loader_state, _write_working_tool_plugin

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.xfail(strict=True, reason="the process-wide registry follows the last config synced")
def test_a_config_without_a_plugin_leaves_another_config_s_plugin_tools_buildable(tmp_path: Path) -> None:
    """While the API already serves a config that removed plugin ``p``, a bot still on the old config builds ``p``'s tool.

    A config reload publishes the new config to the API before running replies drain, so the MCP gateway and the
    dashboard resolve tools for the new config while bots keep building for the old one.
    """
    _write_working_tool_plugin(tmp_path / "plugins" / "p", plugin_name="p", tool_name="p_tool")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}", encoding="utf-8")
    old = _bind_runtime_paths(Config(plugins=["./plugins/p"]), config_path)
    new = _bind_runtime_paths(Config(plugins=[]), config_path)
    runtime_paths = runtime_paths_for(old)

    with _preserved_plugin_loader_state():
        load_plugins(old, runtime_paths)
        ensure_tool_registry_loaded(runtime_paths, new)

        toolkit = get_tool_by_name("p_tool", runtime_paths, runtime_config=old, worker_target=None)

    assert toolkit.name == "working"
