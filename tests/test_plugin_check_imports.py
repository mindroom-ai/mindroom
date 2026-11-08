"""Tests for `mindroom plugins check` reporting a plugin's imports from outside the plugin API."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from mindroom.cli.main import app
from mindroom.plugin_check import imports_outside_plugin_api
from tests.test_plugin_check import _write_plugin

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def _outside_imports(plugin_root: Path) -> list[str]:
    return [f"{found.path}:{found.line}: {found.name}" for found in imports_outside_plugin_api(plugin_root)]


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
            "import mindroom.script_sdk\nfrom mindroom.script_sdk import MindRoomTools\n",
            [],
            id="script-sdk",
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
            "if TYPE_CHECKING:\n    import mindroom.config.main as config\n",
            ["module.py:2: mindroom.config.main"],
            id="type-checking-import",
        ),
    ],
)
def test_imports_outside_plugin_api_names_each_mindroom_import_outside_it(
    tmp_path: Path,
    source: str,
    expected: list[str],
) -> None:
    """Only the plugin API's modules and documented names count."""
    plugin_root = _write_plugin(tmp_path / "plugin", manifest={}, modules={"module.py": source})

    assert _outside_imports(plugin_root) == expected


def test_imports_outside_plugin_api_reads_the_plugins_own_python_files_in_order(tmp_path: Path) -> None:
    """Findings sort by path and line, skipping the plugin's tests, nested plugins, installed packages, and the like."""
    modules = {
        "hooks.py": (
            "def load():\n    from mindroom.constants import ROUTER_AGENT_NAME\n"
            + "\n" * 6
            + "import mindroom.hooks\nfrom mindroom.hooks import hook\n"
        ),
        "helpers/__init__.py": "from mindroom.logging_config import get_logger\n",
        "tests/test_hooks.py": "from mindroom.hooks.decorators import get_hook_metadata\n",
        "conftest.py": "import mindroom.constants\n",
        "test_hooks.py": "from mindroom.hooks.decorators import get_hook_metadata\n",
        "testing.py": "import mindroom.constants\n",
        "helpers/tests/test_helpers.py": "from mindroom.constants import ROUTER_AGENT_NAME\n",
        "helpers/conftest.py": "import mindroom.constants\n",
        "plugins/nested/mindroom.plugin.json": "{}",
        "plugins/nested/hooks.py": "from mindroom.hooks import hook\n",
        "plugins/shared.py": "from mindroom.logging_config import get_logger\n",
        "venv/lib/python3.13/site-packages/mindroom/hooks.py": "from mindroom.constants import ROUTER_AGENT_NAME\n",
        ".venv/bin/activate_this.py": "import mindroom.constants\n",
        "legacy.py": "print 'python 2'\nfrom mindroom.hooks import hook\n",
        # Sources too deeply nested or too long for Python's parser, which raises other errors for them.
        "long_sum.py": "from mindroom.hooks import hook\nx = " + "1+" * 10_000 + "1\n",
        "long_branches.py": "from mindroom.hooks import hook\nif x:\n    pass\n" + "elif x:\n    pass\n" * 6_000,
        "notes.txt": "from mindroom.hooks import hook\n",
    }
    plugin_root = _write_plugin(tmp_path / "plugin", manifest={}, modules=modules)
    # An editor's lock file while hooks.py has unsaved changes.
    (plugin_root / ".#hooks.py").symlink_to("user@host.1234:1700000000")

    assert _outside_imports(plugin_root) == [
        "helpers/__init__.py:1: mindroom.logging_config.get_logger",
        "helpers/conftest.py:1: mindroom.constants",
        "helpers/tests/test_helpers.py:1: mindroom.constants.ROUTER_AGENT_NAME",
        "hooks.py:2: mindroom.constants.ROUTER_AGENT_NAME",
        "hooks.py:9: mindroom.hooks",
        "hooks.py:10: mindroom.hooks.hook",
        "plugins/shared.py:1: mindroom.logging_config.get_logger",
        "testing.py:1: mindroom.constants",
    ]


@pytest.mark.parametrize("field", ["tools_module", "hooks_module", "oauth_module", "routes_module", "lifecycle_module"])
def test_imports_outside_plugin_api_reads_a_root_test_file_the_manifest_names(tmp_path: Path, field: str) -> None:
    """A root-level test file that the manifest names is a module MindRoom loads, so its imports count."""
    _write_plugin(
        tmp_path / "plugin",
        manifest={"name": "imports-demo", field: "./test_module.py"},
        modules={"test_module.py": "import mindroom.constants\n", "conftest.py": "import mindroom.constants\n"},
    )
    # Through a symlink, so the scan's paths differ from the manifest's resolved module paths.
    plugin_link = tmp_path / "plugin-link"
    plugin_link.symlink_to(tmp_path / "plugin")

    assert _outside_imports(plugin_link) == ["test_module.py:1: mindroom.constants"]


@pytest.mark.parametrize(
    "manifest",
    [
        pytest.param(b'{"name": "caf\xe9"}', id="not-utf-8"),
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="nested-too-deeply"),
    ],
)
def test_imports_outside_plugin_api_lists_imports_of_a_plugin_whose_manifest_does_not_load(
    tmp_path: Path,
    manifest: bytes,
) -> None:
    """A manifest that fails to load, which `check_plugin` then reports, still gets the other files' imports listed."""
    plugin_root = _hooks_plugin(tmp_path / "plugin", "from mindroom.hooks import hook\n")
    (plugin_root / "mindroom.plugin.json").write_bytes(manifest)
    (plugin_root / "test_hooks.py").write_text("import mindroom.constants\n", encoding="utf-8")

    assert _outside_imports(plugin_root) == ["hooks.py:1: mindroom.hooks.hook"]


def test_imports_outside_plugin_api_skips_a_directory_it_cannot_enter(tmp_path: Path) -> None:
    """A directory the user cannot enter, such as a container's data volume, is skipped instead of failing the scan."""
    plugin_root = _hooks_plugin(tmp_path / "plugin", "from mindroom.hooks import hook\n")
    locked = plugin_root / "data" / "pgdata"
    locked.mkdir(parents=True)
    locked.chmod(0)
    try:
        assert _outside_imports(plugin_root) == ["hooks.py:1: mindroom.hooks.hook"]
    finally:
        locked.chmod(0o700)


@pytest.mark.parametrize(("options", "exit_code", "label"), [([], 0, "Warning"), (["--strict"], 1, "Error")])
def test_plugins_check_cli_lists_each_import_outside_the_plugin_api_whole_on_its_own_line(
    tmp_path: Path,
    options: list[str],
    exit_code: int,
    label: str,
) -> None:
    """A plugin that loads passes with warnings, or fails with `--strict`, listing each import outside the plugin API."""
    plugin_root = _hooks_plugin(
        tmp_path / "plugin",
        "from mindroom.plugin_api import MessageTarget\nfrom mindroom.hooks import hook\n",
    )
    # A path that reads as console markup, a line number that reads as an emoji code, in a line wider than the console.
    (plugin_root / "helpers").mkdir()
    (plugin_root / "helpers" / "[slow]_runtime_context.py").write_text(
        "\n" * 99 + "from mindroom.tool_system.runtime_context import ToolRuntimeContext\n",
        encoding="utf-8",
    )

    # The console is as wide as a CI log's, whatever terminal runs the tests.
    result = runner.invoke(app, ["plugins", "check", *options, str(plugin_root)], env={"COLUMNS": "80"})

    assert result.exit_code == exit_code
    assert "Plugin is compatible: imports-demo" in result.stdout
    assert (
        f"{label}: imports from outside the plugin API can break in any MindRoom release:\n"
        "  helpers/[slow]_runtime_context.py:100: mindroom.tool_system.runtime_context.ToolRuntimeContext\n"
        "  hooks.py:2: mindroom.hooks.hook\n"
    ) in result.stdout
    assert "MessageTarget" not in result.stdout


def test_plugins_check_cli_lists_imports_outside_the_plugin_api_of_a_plugin_that_fails_to_load(tmp_path: Path) -> None:
    """A plugin broken by a MindRoom module it imports still gets every import outside the plugin API listed."""
    plugin_root = _hooks_plugin(
        tmp_path / "plugin",
        "from mindroom.constants import ROUTER_AGENT_NAME\nfrom mindroom.retired_module import helper\n",
    )

    result = runner.invoke(app, ["plugins", "check", str(plugin_root)], env={"COLUMNS": "80"})

    assert result.exit_code == 1
    assert "Plugin check failed:" in result.stdout
    assert "No module named 'mindroom.retired_module'" in result.stdout
    assert (
        "Warning: imports from outside the plugin API can break in any MindRoom release:\n"
        "  hooks.py:1: mindroom.constants.ROUTER_AGENT_NAME\n"
        "  hooks.py:2: mindroom.retired_module.helper\n"
    ) in result.stdout


@pytest.mark.parametrize(("options", "exit_code", "status"), [([], 0, "compatible"), (["--strict"], 1, "failed")])
def test_plugins_check_cli_json_lists_imports_outside_the_plugin_api(
    tmp_path: Path,
    options: list[str],
    exit_code: int,
    status: str,
) -> None:
    """`--json` lists each import outside the plugin API, and `--strict` fails the document and the command."""
    plugin_root = _hooks_plugin(
        tmp_path / "plugin",
        "from mindroom.plugin_api import hook\nfrom mindroom.hooks import hook\n",
    )

    result = runner.invoke(app, ["plugins", "check", "--json", *options, str(plugin_root)])

    assert result.exit_code == exit_code
    payload = json.loads(result.stdout)
    assert payload["status"] == status
    assert payload["name"] == "imports-demo"
    assert payload["internal_imports"] == [{"path": "hooks.py", "line": 2, "name": "mindroom.hooks.hook"}]
    if status == "failed":
        assert payload["detail"] == "imports from outside the plugin API can break in any MindRoom release"


def test_plugins_check_cli_json_lists_imports_outside_the_plugin_api_of_a_plugin_that_fails_to_load(
    tmp_path: Path,
) -> None:
    """A `--json` failure carries the import list too."""
    plugin_root = _hooks_plugin(tmp_path / "plugin", "from mindroom.retired_module import helper\n")

    result = runner.invoke(app, ["plugins", "check", "--json", str(plugin_root)])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "failed"
    assert "No module named 'mindroom.retired_module'" in payload["detail"]
    assert payload["internal_imports"] == [{"path": "hooks.py", "line": 1, "name": "mindroom.retired_module.helper"}]


def test_plugins_check_cli_strict_accepts_a_plugin_that_imports_only_the_plugin_api(tmp_path: Path) -> None:
    """Every documented public import loads and passes `--strict`."""
    plugin_root = _hooks_plugin(
        tmp_path / "plugin",
        "from mindroom.custom_tools.google_service import GoogleCloudToolkit\n"
        "from mindroom.memory import refresh_agent_memory_search, write_scope_markdown_file\n"
        "from mindroom.plugin_api import hook\n"
        "from mindroom.plugin_http import ClientUser, require_client_user, require_room_member\n",
    )

    result = runner.invoke(app, ["plugins", "check", "--strict", str(plugin_root)])

    assert result.exit_code == 0, result.output
    assert "Plugin is compatible: imports-demo" in result.stdout
    assert "plugin API" not in result.stdout


def test_plugins_check_cli_strict_accepts_a_plugin_whose_tests_beside_its_modules_import_internals(
    tmp_path: Path,
) -> None:
    """Root-level `conftest.py` and `test_*.py`, which MindRoom never loads, do not fail `--strict`."""
    plugin_root = _hooks_plugin(tmp_path / "plugin", "from mindroom.plugin_api import hook\n")
    (plugin_root / "conftest.py").write_text("import mindroom.constants\n", encoding="utf-8")
    (plugin_root / "test_hooks.py").write_text("from mindroom.hooks import hook\n", encoding="utf-8")

    result = runner.invoke(app, ["plugins", "check", "--strict", str(plugin_root)])

    assert result.exit_code == 0, result.output
    assert "Plugin is compatible: imports-demo" in result.stdout
    assert "plugin API" not in result.stdout


def test_plugins_check_cli_fails_on_a_directory_without_a_manifest_without_reading_its_files(tmp_path: Path) -> None:
    """A directory that is not a plugin fails on its missing manifest, and none of its Python files are scanned."""
    directory = tmp_path / "not-a-plugin"
    (directory / "package").mkdir(parents=True)
    (directory / "package" / "module.py").write_text("from mindroom.hooks import hook\n", encoding="utf-8")

    result = runner.invoke(app, ["plugins", "check", str(directory)])

    assert result.exit_code == 1
    assert "Plugin check failed: Plugin manifest missing:" in result.stdout
    assert "plugin API" not in result.stdout
