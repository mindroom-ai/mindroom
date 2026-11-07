"""Tests for `mindroom plugins check` reporting a plugin's imports from outside the plugin API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from mindroom.cli.main import app
from mindroom.plugin_check import imports_outside_plugin_api
from tests.test_plugin_check import _write_plugin

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()

_HOOK = "@hook(event='message:received', name='imports-demo-hook')\nasync def imports_demo_hook(ctx):\n    del ctx\n"


def _hooks_plugin(root: Path, hooks_module: str) -> Path:
    return _write_plugin(
        root,
        manifest={"name": "imports-demo", "hooks_module": "hooks.py"},
        modules={"hooks.py": hooks_module},
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import mindroom.plugin_api as api\nfrom mindroom.plugin_api import *\n", [], id="plugin-api"),
        pytest.param(
            "from mindroom import plugin_api, plugin_http\nfrom mindroom.plugin_http import ClientUser\n",
            [],
            id="plugin-http",
        ),
        pytest.param(
            "from mindroom.memory import refresh_agent_memory_search, write_scope_markdown_file\n"
            "from mindroom.custom_tools.google_service import GoogleCloudToolkit\n",
            [],
            id="documented-names",
        ),
        pytest.param(
            "from . import helpers\nfrom .mindroom import hooks\nimport mindroomx\nfrom mindroom_extra import hooks\n",
            [],
            id="not-mindroom",
        ),
        pytest.param(
            "import mindroom\nimport mindroom.memory\n",
            ["module.py:1: mindroom", "module.py:2: mindroom.memory"],
            id="whole-modules",
        ),
        pytest.param("from mindroom import hooks, plugin_api\n", ["module.py:1: mindroom.hooks"], id="submodule-names"),
        pytest.param(
            "from mindroom.memory import (\n    MemoryNotFound,\n    write_scope_markdown_file,\n)\n"
            "import mindroom.plugin_api, \\\n    mindroom.hooks\n",
            ["module.py:2: mindroom.memory.MemoryNotFound", "module.py:6: mindroom.hooks"],
            id="each-name-on-its-line",
        ),
        pytest.param(
            "from mindroom.hooks import *\nfrom mindroom.plugin_apix import hook\n",
            ["module.py:1: mindroom.hooks.*", "module.py:2: mindroom.plugin_apix.hook"],
            id="star-and-lookalike",
        ),
        pytest.param(
            "def load():\n    from mindroom.constants import ROUTER_AGENT_NAME\n"
            "if TYPE_CHECKING:\n    import mindroom.config.main as config\n",
            ["module.py:2: mindroom.constants.ROUTER_AGENT_NAME", "module.py:4: mindroom.config.main"],
            id="nested-imports",
        ),
    ],
)
def test_imports_outside_plugin_api_names_each_mindroom_import_outside_it(
    tmp_path: Path,
    source: str,
    expected: list[str],
) -> None:
    """Only `mindroom.plugin_api`, `mindroom.plugin_http`, and the documented names count as the plugin API."""
    (tmp_path / "module.py").write_text(source, encoding="utf-8")

    assert imports_outside_plugin_api(tmp_path) == expected


def test_imports_outside_plugin_api_reads_the_plugins_own_python_files_in_order(tmp_path: Path) -> None:
    """Findings sort by path and line, and installed packages, hidden directories, and unreadable files are skipped."""
    for relative_path, source in {
        "hooks.py": (
            "def load():\n    from mindroom.constants import ROUTER_AGENT_NAME\n"
            + "\n" * 6
            + "import mindroom.hooks\nfrom mindroom.hooks import hook\n"
        ),
        "helpers/__init__.py": "from mindroom.logging_config import get_logger\n",
        "tests/test_hooks.py": "from mindroom.hooks.decorators import get_hook_metadata\n",
        "venv/lib/python3.13/site-packages/mindroom/hooks.py": "from mindroom.constants import ROUTER_AGENT_NAME\n",
        ".venv/bin/activate_this.py": "import mindroom.constants\n",
        "legacy.py": "print 'python 2'\nfrom mindroom.hooks import hook\n",
        "notes.txt": "from mindroom.hooks import hook\n",
    }.items():
        path = tmp_path / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    # An editor's lock file while hooks.py has unsaved changes.
    (tmp_path / ".#hooks.py").symlink_to("user@host.1234:1700000000")

    assert imports_outside_plugin_api(tmp_path) == [
        "helpers/__init__.py:1: mindroom.logging_config.get_logger",
        "hooks.py:2: mindroom.constants.ROUTER_AGENT_NAME",
        "hooks.py:9: mindroom.hooks",
        "hooks.py:10: mindroom.hooks.hook",
        "tests/test_hooks.py:1: mindroom.hooks.decorators.get_hook_metadata",
    ]


def test_plugins_check_cli_warns_about_imports_outside_the_plugin_api(tmp_path: Path) -> None:
    """A compatible plugin passes, and each import from outside the plugin API is listed whole on its own line."""
    plugin_root = _hooks_plugin(
        tmp_path / "plugin",
        "from mindroom.plugin_api import MessageTarget\nfrom mindroom.hooks import hook\n" + _HOOK,
    )
    # A path that reads as console markup, a line number that reads as an emoji code, in a line wider than the console.
    (plugin_root / "tests").mkdir()
    (plugin_root / "tests" / "test_[slow]_runtime_context_integration.py").write_text(
        "\n" * 99 + "from mindroom.tool_system.runtime_context import ToolRuntimeContext\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["plugins", "check", str(plugin_root)])

    assert result.exit_code == 0
    assert "Plugin is compatible: imports-demo" in result.stdout
    assert (
        "Warning: imports from outside the plugin API can break in any MindRoom release:\n"
        "  hooks.py:2: mindroom.hooks.hook\n"
        "  tests/test_[slow]_runtime_context_integration.py:100: mindroom.tool_system.runtime_context.ToolRuntimeContext\n"
    ) in result.stdout
    assert "MessageTarget" not in result.stdout


def test_plugins_check_cli_strict_fails_on_imports_outside_the_plugin_api(tmp_path: Path) -> None:
    """`--strict` turns the warnings into errors and exits nonzero."""
    plugin_root = _hooks_plugin(tmp_path / "plugin", "from mindroom.hooks import hook\n" + _HOOK)

    result = runner.invoke(app, ["plugins", "check", "--strict", str(plugin_root)])

    assert result.exit_code == 1
    assert (
        "Error: imports from outside the plugin API can break in any MindRoom release:\n"
        "  hooks.py:1: mindroom.hooks.hook\n"
    ) in result.stdout


def test_plugins_check_cli_strict_accepts_a_plugin_that_imports_only_the_plugin_api(tmp_path: Path) -> None:
    """Every documented public import loads and passes `--strict`."""
    plugin_root = _hooks_plugin(
        tmp_path / "plugin",
        "from mindroom.custom_tools.google_service import GoogleCloudToolkit\n"
        "from mindroom.memory import refresh_agent_memory_search, write_scope_markdown_file\n"
        "from mindroom.plugin_api import hook\n"
        "from mindroom.plugin_http import ClientUser, require_client_user, require_room_member\n" + _HOOK,
    )

    result = runner.invoke(app, ["plugins", "check", "--strict", str(plugin_root)])

    assert result.exit_code == 0, result.output
    assert "Plugin is compatible: imports-demo" in result.stdout
    assert "plugin API" not in result.stdout
