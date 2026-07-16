"""Tests for standalone external-plugin compatibility checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mindroom.cli.main import app
from mindroom.plugin_check import check_plugin
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.registry_state import TOOL_REGISTRY
from mindroom.tool_system.skills import get_plugin_skill_roots

runner = CliRunner()


def _write_plugin(
    root: Path,
    *,
    manifest: dict[str, object],
    modules: dict[str, str] | None = None,
) -> Path:
    root.mkdir(parents=True)
    (root / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    for relative_path, content in (modules or {}).items():
        module_path = root / relative_path
        module_path.parent.mkdir(parents=True, exist_ok=True)
        module_path.write_text(content, encoding="utf-8")
    return root


def _valid_plugin(root: Path) -> Path:
    return _write_plugin(
        root,
        manifest={
            "name": "compat-demo",
            "tools_module": "tools.py",
            "hooks_module": "hooks.py",
            "oauth_module": "oauth.py",
            "skills": ["skills"],
        },
        modules={
            "tools.py": (
                "from agno.tools import Toolkit\n"
                "from mindroom.tool_system.declarations import ToolCategory\n"
                "from mindroom.tool_system.registration import register_tool_with_metadata\n"
                "class DemoTool(Toolkit):\n"
                "    def __init__(self):\n"
                "        super().__init__(name='demo', tools=[])\n"
                "@register_tool_with_metadata(\n"
                "    name='compat_demo_tool',\n"
                "    display_name='Compatibility demo',\n"
                "    description='Compatibility test tool',\n"
                "    category=ToolCategory.DEVELOPMENT,\n"
                "    auth_provider='compat_demo_oauth',\n"
                ")\n"
                "def compat_demo_tools():\n"
                "    return DemoTool\n"
            ),
            "hooks.py": (
                "from mindroom.hooks import hook\n"
                "@hook(event='message:received', name='compat-demo-hook')\n"
                "async def compat_demo_hook(ctx):\n"
                "    del ctx\n"
            ),
            "oauth.py": (
                "from mindroom.oauth import OAuthProvider\n"
                "def register_oauth_providers(settings, runtime_paths):\n"
                "    assert settings == {}\n"
                "    assert runtime_paths.process_env == {}\n"
                "    return [OAuthProvider(\n"
                "        id='compat_demo_oauth',\n"
                "        display_name='Compatibility demo',\n"
                "        authorization_url='https://example.com/authorize',\n"
                "        token_url='https://example.com/token',\n"
                "        scopes=('read',),\n"
                "        credential_service='compat_demo_credentials',\n"
                "        tool_config_service='compat_demo_tool',\n"
                "        client_config_services=('compat_demo_oauth_client',),\n"
                "    )]\n"
            ),
            "skills/demo/SKILL.md": "# Demo\n",
        },
    )


def test_check_plugin_validates_all_registration_surfaces_and_restores_state(tmp_path: Path) -> None:
    """One check should exercise tools, hooks, OAuth, and skills without leaking state."""
    plugin_root = _valid_plugin(tmp_path / "plugin")
    original_tool_metadata = TOOL_METADATA.copy()
    original_skill_roots = tuple(get_plugin_skill_roots())
    original_module_import_cache = plugin_imports._MODULE_IMPORT_CACHE.copy()
    original_synthetic_modules = {name for name in sys.modules if name.startswith("mindroom_plugin_")}

    result = check_plugin(plugin_root)

    assert result.name == "compat-demo"
    assert result.tool_names == ("compat_demo_tool",)
    assert result.hook_names == ("compat-demo-hook",)
    assert result.skill_directories == ("skills",)
    assert original_tool_metadata == TOOL_METADATA
    assert tuple(get_plugin_skill_roots()) == original_skill_roots
    assert original_module_import_cache == plugin_imports._MODULE_IMPORT_CACHE
    assert {name for name in sys.modules if name.startswith("mindroom_plugin_")} == original_synthetic_modules


def test_check_plugin_rejects_invalid_oauth_registration_and_restores_state(tmp_path: Path) -> None:
    """Strict checks should include OAuth callbacks and remain transactional on failure."""
    plugin_root = _write_plugin(
        tmp_path / "plugin",
        manifest={"name": "broken-oauth", "oauth_module": "oauth.py"},
        modules={"oauth.py": "def register_oauth_providers(settings, runtime_paths):\n    return ['invalid']\n"},
    )
    original_tool_metadata = TOOL_METADATA.copy()
    original_skill_roots = tuple(get_plugin_skill_roots())
    original_synthetic_modules = {name for name in sys.modules if name.startswith("mindroom_plugin_")}

    with pytest.raises(ValueError, match="non-OAuthProvider"):
        check_plugin(plugin_root)

    assert original_tool_metadata == TOOL_METADATA
    assert tuple(get_plugin_skill_roots()) == original_skill_roots
    assert {name for name in sys.modules if name.startswith("mindroom_plugin_")} == original_synthetic_modules


def test_check_plugin_rejects_oauth_module_without_registration_callback(tmp_path: Path) -> None:
    """An OAuth manifest entry should require the documented registration callback."""
    plugin_root = _write_plugin(
        tmp_path / "plugin",
        manifest={"name": "missing-oauth-callback", "oauth_module": "oauth.py"},
        modules={"oauth.py": "PROVIDER_NAME = 'missing'\n"},
    )

    with pytest.raises(ValueError, match="must define callable register_oauth_providers"):
        check_plugin(plugin_root)


def test_check_plugin_rejects_oauth_tool_provider_mismatch(tmp_path: Path) -> None:
    """A provider must own the tool service that names it as auth provider."""
    plugin_root = _valid_plugin(tmp_path / "plugin")
    oauth_path = plugin_root / "oauth.py"
    oauth_path.write_text(
        oauth_path.read_text(encoding="utf-8").replace("id='compat_demo_oauth'", "id='different_provider'"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="overlap existing tool service"):
        check_plugin(plugin_root)


def test_check_plugin_rejects_broken_module_import(tmp_path: Path) -> None:
    """Import failures should fail instead of degrading to skipped plugins."""
    plugin_root = _write_plugin(
        tmp_path / "plugin",
        manifest={"name": "broken-import", "hooks_module": "hooks.py"},
        modules={"hooks.py": "from package_that_does_not_exist import missing\n"},
    )

    with pytest.raises(ValueError, match="package_that_does_not_exist"):
        check_plugin(plugin_root)


def test_check_plugin_accepts_skill_only_plugin_and_keeps_builtin_tools(tmp_path: Path) -> None:
    """Plugins need no tool or hook minimum, and checks must retain built-in tools."""
    plugin_root = _write_plugin(
        tmp_path / "plugin",
        manifest={"name": "skill-only", "skills": ["skills"]},
        modules={"skills/demo/SKILL.md": "# Demo\n"},
    )

    result = check_plugin(plugin_root)

    assert result.tool_names == ()
    assert result.hook_names == ()
    assert result.skill_directories == ("skills",)
    assert "calculator" in TOOL_METADATA
    assert "calculator" in TOOL_REGISTRY


def test_plugin_fleet_registry_is_sorted_and_unique() -> None:
    """The checked-in fleet registry should remain deterministic and collision-free."""
    registry_path = Path(__file__).resolve().parents[1] / ".github" / "plugin-fleet.json"
    repositories = json.loads(registry_path.read_text(encoding="utf-8"))

    assert repositories == sorted(repositories)
    assert len(repositories) == len(set(repositories))
    assert all(repository.count("/") == 1 for repository in repositories)


def test_plugins_check_cli_reports_compatibility(tmp_path: Path) -> None:
    """CLI should expose a stable compatibility-check entrypoint for CI."""
    plugin_root = _valid_plugin(tmp_path / "plugin")

    result = runner.invoke(app, ["plugins", "check", str(plugin_root)])

    assert result.exit_code == 0
    assert "Plugin is compatible: compat-demo" in result.stdout
    assert "compat_demo_tool" in result.stdout
    assert "compat-demo-hook" in result.stdout
    assert "Skills: skills" in result.stdout


def test_plugins_check_cli_reports_failure_without_traceback(tmp_path: Path) -> None:
    """CI output should preserve the plugin error without a Typer traceback."""
    plugin_root = _write_plugin(
        tmp_path / "plugin",
        manifest={"name": "broken-import", "hooks_module": "hooks.py"},
        modules={"hooks.py": "raise RuntimeError('plugin exploded')\n"},
    )

    result = runner.invoke(app, ["plugins", "check", str(plugin_root)])

    assert result.exit_code == 1
    assert "Plugin check failed:" in result.stdout
    assert "plugin exploded" in result.stdout
    assert "Traceback (most recent call last)" not in result.output
    assert result.exception is not None


def test_plugins_check_cli_rejects_nonexistent_directory(tmp_path: Path) -> None:
    """Typer should reject a missing plugin path before runtime loading."""
    missing_plugin = tmp_path / "missing-plugin"

    result = runner.invoke(app, ["plugins", "check", str(missing_plugin)])

    assert result.exit_code == 2
    assert "does not exist" in result.output
    assert "Traceback (most recent call last)" not in result.output
