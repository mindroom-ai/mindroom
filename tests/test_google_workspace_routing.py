"""Primary-runtime routing for additional Google workspace tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.catalog import TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.plugins import isolated_plugin_runtime
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_enabled_for_tool

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _workspace_plugin(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    plugin = tmp_path / "workspace_plugin"
    plugin.mkdir()
    (plugin / "mindroom.plugin.json").write_text(
        json.dumps({"name": "workspace-example", "tools_module": "tools.py"}),
    )
    (plugin / "tools.py").write_text(
        "from mindroom.tool_system.google_workspaces import (\n"
        "    GoogleWorkspaceConfig,\n"
        "    register_google_workspace_tools,\n"
        ")\n"
        "register_google_workspace_tools(GoogleWorkspaceConfig(\n"
        "    name='secondary',\n"
        "    display_name='Secondary',\n"
        "    client_config_service='secondary_google_oauth_client',\n"
        "    allowed_hosted_domains=('secondary.example',),\n"
        "))\n",
    )
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n")
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "data",
        process_env={
            "MINDROOM_PUBLIC_URL": "https://chat.example",
            "MINDROOM_SANDBOX_EXECUTION_MODE": "all",
            "MINDROOM_SANDBOX_PROXY_URL": "https://sandbox.example",
            "MINDROOM_SANDBOX_PROXY_TOKEN": "test-token",
        },
    )
    return Config(plugins=[str(plugin)]), paths


_SERVICE_CALLS = {
    "gmail": ("secondary_get_latest_emails", {}),
    "google_calendar": ("secondary_list_calendars", {}),
    "google_drive": ("secondary_google_drive_list_files", {}),
    "google_docs": ("secondary_google_docs_get_document", {"document_id": "test"}),
    "google_sheets": (
        "secondary_read_sheet",
        {"spreadsheet_id": "test", "spreadsheet_range": "A1"},
    ),
}


@pytest.mark.parametrize("service", _SERVICE_CALLS)
@pytest.mark.parametrize("routing", ["all", "explicit"])
def test_workspace_tools_stay_in_primary_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    routing: str,
) -> None:
    """Workspace aliases must not proxy under global or explicit worker routing."""

    class _ForbiddenProxyClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            msg = "Workspace tools must not create a sandbox proxy client"
            raise AssertionError(msg)

    config, paths = _workspace_plugin(tmp_path)
    tool_name = f"secondary_{service}"
    worker_tools = None if routing == "all" else [tool_name]
    monkeypatch.setattr("mindroom.tool_system.sandbox_proxy.httpx.Client", _ForbiddenProxyClient)

    with isolated_plugin_runtime(config, paths):
        metadata = TOOL_METADATA[tool_name]
        assert metadata.requires_primary_runtime is True
        assert (
            sandbox_proxy_enabled_for_tool(
                tool_name,
                runtime_paths=paths,
                worker_tools_override=worker_tools,
            )
            is False
        )

        tool = get_tool_by_name(
            tool_name,
            paths,
            worker_tools_override=worker_tools,
            worker_target=None,
        )
        function_name, kwargs = _SERVICE_CALLS[service]
        function = tool.functions[function_name]
        result = json.loads(function.entrypoint(**kwargs))
        assert result["oauth_connection_required"] is True
