"""Tests for package-level import side effects."""

import importlib
import json
import os
import subprocess
import sys

import pytest

import mindroom
from mindroom.constants import VENDOR_TELEMETRY_ENV_VALUES
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
    env = {"AGNO_TELEMETRY": "true"}

    disable_vendor_telemetry(env)

    assert env == dict(VENDOR_TELEMETRY_ENV_VALUES)


def test_cli_import_disables_vendor_telemetry_before_cli_dependencies() -> None:
    """The ``mindroom run`` import path should disable telemetry before CLI dependencies."""
    expected_json = json.dumps(dict(VENDOR_TELEMETRY_ENV_VALUES))
    script = f"""
import importlib.machinery
import json
import os
import sys

expected = json.loads({expected_json!r})
targets = {{"httpx", "typer"}}
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
