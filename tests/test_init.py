"""Tests for package-level import side effects."""

import importlib
import importlib.machinery
import importlib.metadata
import json
import os
import subprocess
import sys
import tomllib
import types
from itertools import pairwise
from pathlib import Path

import pytest

import mindroom
from mindroom import constants, vendor_telemetry
from mindroom.runtime_env_policy import VENDOR_TELEMETRY_ENV_VALUES
from mindroom.tool_system.authoring import Toolkit
from mindroom.tools.composio import composio_tools
from mindroom.vendor_telemetry import disable_vendor_telemetry


def test_package_init_disables_vendor_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """MindRoom should force vendor telemetry off at import time."""
    for name in VENDOR_TELEMETRY_ENV_VALUES:
        monkeypatch.setenv(name, "enabled")

    importlib.reload(mindroom)

    for name, value in VENDOR_TELEMETRY_ENV_VALUES.items():
        assert os.environ[name] == value


def test_disable_vendor_telemetry_updates_supplied_env() -> None:
    """Telemetry opt-outs should be reusable for subprocess env construction."""
    env = {"DO_NOT_TRACK": "0"}

    disable_vendor_telemetry(env)

    assert env == dict(VENDOR_TELEMETRY_ENV_VALUES)


def test_disable_vendor_telemetry_unregisters_loaded_composio_atexit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Composio's Sentry DSN fetch should be removed when the module is already loaded."""
    sentry_module = types.ModuleType("composio.utils.sentry")

    def update_dsn() -> None:
        pass

    sentry_module.update_dsn = update_dsn
    unregistered: list[object] = []
    monkeypatch.setitem(sys.modules, "composio.utils.sentry", sentry_module)
    monkeypatch.setattr(vendor_telemetry.atexit, "unregister", unregistered.append)

    disable_vendor_telemetry()

    assert unregistered == [update_dsn]


def test_composio_tools_reapplies_vendor_telemetry_after_lazy_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lazy Composio toolkit imports should still unregister import-time atexit callbacks."""

    class FakeComposioToolSet:
        pass

    class FakeAction:
        def __init__(self, value: str) -> None:
            self.value = value

    sentry_module = types.ModuleType("composio.utils.sentry")

    def update_dsn() -> None:
        pass

    sentry_module.update_dsn = update_dsn
    composio_module = types.ModuleType("composio")
    composio_module.__path__ = []
    composio_module.Action = FakeAction
    composio_module.ComposioToolSet = FakeComposioToolSet
    constants_module = types.ModuleType("composio.constants")
    constants_module.DEFAULT_BASE_URL = "https://backend.composio.dev/api"
    unregistered: list[object] = []
    for module in (
        composio_module,
        constants_module,
        sentry_module,
    ):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(vendor_telemetry.atexit, "unregister", unregistered.append)

    assert issubclass(composio_tools(), Toolkit)
    assert unregistered == [update_dsn]


def test_cli_import_disables_vendor_telemetry_before_cli_dependencies() -> None:
    """The ``mindroom run`` import path should disable telemetry before CLI dependencies."""
    expected_json = json.dumps(dict(VENDOR_TELEMETRY_ENV_VALUES))
    script = f"""
import importlib.machinery
import json
import os
import sys

expected = json.loads({expected_json!r})
targets = {{"typer"}}
seen = set()


class GuardFinder:
    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in targets and root not in seen:
            seen.add(root)
            bad = {{name: os.environ.get(name) for name, value in expected.items() if os.environ.get(name) != value}}
            if bad:
                raise RuntimeError(f"{{root}} imported before telemetry opt-outs were applied: {{bad}}")
        return importlib.machinery.PathFinder.find_spec(fullname, path)


for target in targets:
    if target in sys.modules:
        raise RuntimeError(f"{{target}} was imported before the guard was installed")

sys.meta_path.insert(0, GuardFinder())
import mindroom.cli.main

missing = targets - seen
if missing:
    raise RuntimeError(f"Guard did not observe expected CLI dependency imports: {{sorted(missing)}}")
"""
    env = os.environ.copy()
    env.update(dict.fromkeys(VENDOR_TELEMETRY_ENV_VALUES, "enabled"))

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_default_pytest_options_disable_tach_plugin() -> None:
    """Plain pytest should not run Tach impact analysis unless explicitly requested."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())

    addopts = data["tool"]["pytest"]["ini_options"]["addopts"].split()
    addopts_pairs = pairwise(addopts)

    assert ("-p", "no:tach") in addopts_pairs


def test_package_init_avoids_heavy_imports_on_startup() -> None:
    """Importing ``mindroom`` must not eagerly pull in constants, pydantic, or importlib.metadata."""
    script = (
        "import sys, mindroom; "
        "bad = [m for m in sys.modules if m == 'importlib.metadata' or m.startswith(('mindroom.constants', 'pydantic', 'pydantic_settings'))]; "
        "assert not bad, bad; assert '__version__' not in vars(mindroom)"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, check=False, text=True)
    assert result.returncode == 0, result.stderr


def test_package_version_is_lazily_resolved_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mindroom.__version__`` should resolve lazily once and reject unknown attributes."""
    calls: list[str] = []
    monkeypatch.setattr(importlib.metadata, "version", lambda name: (calls.append(name), "9.9.9-test")[1])
    previous = mindroom.__dict__.pop("__version__", None)
    try:
        assert "__version__" in dir(mindroom)
        assert mindroom.__version__ == "9.9.9-test"
        from mindroom import __version__ as imported_version  # noqa: PLC0415

        assert (imported_version, calls) == ("9.9.9-test", ["mindroom"])
        with pytest.raises(AttributeError):
            _ = mindroom.definitely_not_a_real_attribute
    finally:
        if previous is not None:
            mindroom.__dict__["__version__"] = previous
        else:
            mindroom.__dict__.pop("__version__", None)


def test_chromadb_py314_hook_runs_before_chromadb_loads_and_deduplicates_on_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Python 3.14+, the meta_path hook patches lazily before chromadb loads and never duplicates."""
    events: list[str] = []
    monkeypatch.setattr(
        sys,
        "meta_path",
        [finder for finder in sys.meta_path if not getattr(finder, "_mindroom_chromadb_py314_hook", False)],
    )
    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))
    monkeypatch.delitem(sys.modules, "chromadb", raising=False)
    monkeypatch.setattr(constants, "_CHROMADB_PY314_PATCHED", False)
    monkeypatch.setattr(constants, "patch_chromadb_for_python314", lambda: events.append("patched"))

    def hook_count() -> int:
        return sum(1 for finder in sys.meta_path if getattr(finder, "_mindroom_chromadb_py314_hook", False))

    importlib.reload(mindroom)
    importlib.reload(mindroom)
    assert hook_count() == 1

    class FakeChromaFinder:
        def create_module(self, _spec: object) -> None:
            return None

        def exec_module(self, _module: types.ModuleType) -> None:
            events.append("loaded")

        def find_spec(self, fullname: str, _path: object = None, _target: object = None) -> object:
            return importlib.machinery.ModuleSpec("chromadb", self) if fullname == "chromadb" else None

    sys.meta_path.insert(1, FakeChromaFinder())
    try:
        importlib.import_module("chromadb")
        assert (events, hook_count()) == (["patched", "loaded"], 0)
        importlib.reload(mindroom)
        assert hook_count() == 0
    finally:
        sys.modules.pop("chromadb", None)
