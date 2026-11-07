"""A plugin loads only on the plugin API version it needs, and a required plugin that does not load stops the load."""

from __future__ import annotations

import asyncio
import sys
import uuid
from types import ModuleType
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from mindroom.cli.main import app
from mindroom.config.main import Config
from mindroom.hooks import EVENT_MESSAGE_RECEIVED
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.startup_errors import PermanentStartupError
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.plugin_imports import PLUGIN_API_VERSION, PluginValidationError
from mindroom.tool_system.plugins import PluginGeneration, dispose_plugins, prepare_plugins
from tests.conftest import bind_runtime_paths, initialize_from_config_file, runtime_paths_for, test_runtime_paths
from tests.test_plugin_check import _write_plugin

if TYPE_CHECKING:
    from pathlib import Path

_HOOKS = "from mindroom.plugin_api import hook\n\n@hook('message:received')\nasync def audit(ctx):\n    del ctx\n"


def _prepare(tmp_path: Path, plugins: list[object]) -> PluginGeneration:
    """Load ``plugins``, a config's ``plugins:`` entries, as startup and reloads do."""
    generation = prepare_plugins(Config(plugins=plugins), test_runtime_paths(tmp_path))
    dispose_plugins(generation)
    return generation


def _demo_plugin(tmp_path: Path, needs: object) -> None:
    _write_plugin(
        tmp_path / "plugins" / "demo",
        manifest={"name": "demo", "mindroom_plugin_api": needs, "hooks_module": "hooks.py"},
        modules={"hooks.py": _HOOKS},
    )


@pytest.mark.parametrize(
    ("needs", "refusal"),
    [
        ("3.0", None),
        ("3.4", None),
        ("3.5", "Plugin 'demo' needs plugin API 3.5, but this MindRoom provides 3.4: upgrade MindRoom"),
        ("4.0", "Plugin 'demo' needs plugin API 4.0, but this MindRoom provides 3.4: upgrade MindRoom"),
        ("2.9", "Plugin 'demo' needs plugin API 2.9, but this MindRoom provides 3.4: update the plugin"),
    ],
)
def test_a_plugin_loads_only_on_the_major_it_needs_at_its_minor_or_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    needs: str,
    refusal: str | None,
) -> None:
    """A plugin needing M.m loads on M.m and any later minor of M, and is skipped with the reason otherwise."""
    monkeypatch.setattr(plugin_imports, "PLUGIN_API_VERSION", (3, 4))
    _demo_plugin(tmp_path, needs)

    generation = _prepare(tmp_path, ["./plugins/demo"])

    if refusal is None:
        assert [plugin.name for plugin in generation.plugins.values()] == ["demo"]
        assert generation.skipped == ()
    else:
        assert generation.plugins == {}
        [skipped] = generation.skipped
        assert skipped.path == "./plugins/demo"
        assert str(skipped.error).startswith(refusal)


@pytest.mark.parametrize("needs", ["1", "1.0.0", 1, 1.0, "v1.0", "1.x", " 1.0", "\u0661.\u0660", ["1", "0"]])
def test_a_plugin_whose_needed_version_is_not_major_dot_minor_is_skipped(tmp_path: Path, needs: object) -> None:
    """Only a "MAJOR.MINOR" string of ASCII digits states a version, so anything else is refused, not guessed at."""
    _demo_plugin(tmp_path, needs)

    generation = _prepare(tmp_path, ["./plugins/demo"])

    assert generation.plugins == {}
    [skipped] = generation.skipped
    assert str(skipped.error).startswith('Plugin mindroom_plugin_api must be a "MAJOR.MINOR" string such as "1.0"')


def test_an_incompatible_plugin_is_skipped_before_any_of_its_modules_runs(tmp_path: Path) -> None:
    """The reason is the version, not what the plugin's modules would have done when imported on this MindRoom."""
    major, minor = PLUGIN_API_VERSION
    _write_plugin(
        tmp_path / "plugins" / "demo",
        manifest={
            "name": "demo",
            "mindroom_plugin_api": f"{major + 1}.{minor}",
            "tools_module": "tools.py",
            "hooks_module": "hooks.py",
        },
        modules={"tools.py": "raise RuntimeError('tools ran')\n", "hooks.py": "raise RuntimeError('hooks ran')\n"},
    )

    [skipped] = _prepare(tmp_path, ["./plugins/demo"]).skipped

    assert str(skipped.error).startswith(f"Plugin 'demo' needs plugin API {major + 1}.{minor}")


def test_plugins_check_reports_a_plugin_that_needs_a_newer_plugin_api(tmp_path: Path) -> None:
    """`mindroom plugins check` fails with the same reason a load skips the plugin for."""
    major, minor = PLUGIN_API_VERSION
    plugin_root = _write_plugin(
        tmp_path / "plugin",
        manifest={"name": "demo", "mindroom_plugin_api": f"{major}.{minor + 1}"},
    )

    result = CliRunner().invoke(app, ["plugins", "check", str(plugin_root)])

    assert result.exit_code == 1
    output = " ".join(result.stdout.split())
    assert (
        f"Plugin check failed: Plugin 'demo' needs plugin API {major}.{minor + 1}, "
        f"but this MindRoom provides {major}.{minor}: upgrade MindRoom"
    ) in output


@pytest.mark.parametrize(
    ("manifest", "hooks", "reason"),
    [
        (None, _HOOKS, "Configured plugin path does not exist"),
        ({"mindroom_plugin_api": f"{PLUGIN_API_VERSION[0] + 1}.0"}, _HOOKS, "Plugin 'needed' needs plugin API"),
        ({}, "raise RuntimeError('missing dependency')\n", "missing dependency"),
    ],
    ids=["missing-path", "incompatible", "import-error"],
)
@pytest.mark.parametrize("required", [True, False])
def test_a_plugin_skipped_for_any_reason_fails_the_load_only_when_its_entry_requires_it(
    tmp_path: Path,
    manifest: dict[str, str] | None,
    hooks: str,
    reason: str,
    required: bool,
) -> None:
    """A required plugin that would be skipped refuses the whole load, with the reason; any other is skipped."""
    if manifest is not None:
        _write_plugin(
            tmp_path / "plugins" / "needed",
            manifest={"name": "needed", "hooks_module": "hooks.py", **manifest},
            modules={"hooks.py": hooks},
        )
    entries = [{"path": "./plugins/needed", "required": required}]

    if required:
        refusal = rf"^Required plugin \./plugins/needed failed to load: .*{reason}"
        with pytest.raises(PluginValidationError, match=refusal):
            _prepare(tmp_path, entries)
    else:
        [skipped] = _prepare(tmp_path, entries).skipped
        assert reason in str(skipped.error)


def test_a_disabled_required_entry_requires_nothing(tmp_path: Path) -> None:
    """A disabled entry loads nothing, so its `required` does not make another entry for the same path required."""
    entries = [{"path": "./plugins/missing", "enabled": False, "required": True}, "./plugins/missing"]

    [skipped] = _prepare(tmp_path, entries).skipped

    assert "Configured plugin path does not exist" in str(skipped.error)


def test_a_refused_load_names_every_required_plugin_that_did_not_load(tmp_path: Path) -> None:
    """Each required plugin that would be skipped is named with its reason, not only the first."""
    entries = [{"path": "./plugins/first", "required": True}, {"path": "./plugins/second", "required": True}]
    refusal = (
        r"^Required plugin \./plugins/first failed to load: Configured plugin path does not exist: .*; "
        r"Required plugin \./plugins/second failed to load: Configured plugin path does not exist: "
    )

    with pytest.raises(PluginValidationError, match=refusal):
        _prepare(tmp_path, entries)


@pytest.mark.asyncio
async def test_a_refused_load_ends_the_tasks_its_loaded_plugins_started(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing a load refused for a required plugin started keeps running."""
    probe = ModuleType(f"mindroom_test_probe_{uuid.uuid4().hex}")
    probe.tasks = []
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    _write_plugin(
        tmp_path / "plugins" / "starts-task",
        manifest={"name": "starts-task", "hooks_module": "hooks.py"},
        modules={
            "hooks.py": (
                f"import asyncio\nimport {probe.__name__} as probe\n"
                "_POLLER = asyncio.get_running_loop().create_task(asyncio.Event().wait())\n"
                "probe.tasks.append(_POLLER)\n"
            ),
        },
    )
    entries = ["./plugins/starts-task", {"path": "./plugins/missing", "required": True}]

    try:
        with pytest.raises(PluginValidationError, match=r"^Required plugin \./plugins/missing failed to load"):
            _prepare(tmp_path, entries)
        await asyncio.sleep(0)

        [task] = probe.tasks
        assert task.cancelled()
    finally:
        for task in probe.tasks:
            task.cancel()
        await asyncio.gather(*probe.tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_startup_that_cannot_load_a_required_plugin_fails_with_the_reason(tmp_path: Path) -> None:
    """Startup refuses a required plugin that does not load as a permanent error, which `mindroom run` prints."""
    runtime_paths = test_runtime_paths(tmp_path)
    runtime_paths.config_path.write_text(
        "plugins:\n  - path: ./plugins/missing\n    required: true\n",
        encoding="utf-8",
    )

    with pytest.raises(PermanentStartupError, match=r"^Required plugin \./plugins/missing failed to load"):
        await initialize_from_config_file(runtime_paths)


@pytest.mark.asyncio
async def test_a_reload_that_cannot_load_a_required_plugin_keeps_the_live_plugins(tmp_path: Path) -> None:
    """A reload refused for a required plugin publishes nothing, so the running plugins keep their hooks."""
    plugin_root = _write_plugin(
        tmp_path / "plugins" / "needed",
        manifest={"name": "needed", "hooks_module": "hooks.py"},
        modules={"hooks.py": _HOOKS},
    )
    config = bind_runtime_paths(
        Config(plugins=[{"path": "./plugins/needed", "required": True}]),
        test_runtime_paths(tmp_path),
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True
    await orchestrator.reload_plugins_now(source="test")
    live_hooks = orchestrator.hook_registry
    (plugin_root / "hooks.py").write_text("raise RuntimeError('broken by an edit')\n", encoding="utf-8")

    with pytest.raises(PluginValidationError, match=r"^Required plugin \./plugins/needed failed to load: .*broken by"):
        await orchestrator.reload_plugins_now(source="test")

    assert orchestrator.hook_registry is live_hooks
    assert [hook.plugin_name for hook in live_hooks.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["needed"]
