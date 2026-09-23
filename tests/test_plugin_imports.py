"""Tests for low-level plugin import transaction helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from types import ModuleType
from typing import TYPE_CHECKING

from mindroom.tool_system import plugin_imports

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_PLUGIN_PROCESS_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import json
    import sys
    from pathlib import Path

    from agno.agent import Agent

    from mindroom.config.main import Config
    from mindroom.constants import resolve_runtime_paths
    from mindroom.tool_jobs.authorization import (
        bind_actor_authority,
        authority_snapshot,
        bind_toolkit_authority,
        function_authority,
        locally_allowed,
    )
    from mindroom.tool_jobs.provenance import function_provenance
    from mindroom.tool_jobs.runtime import BackgroundOutcome, JobSpec, ToolJobRuntime
    from mindroom.tool_system.construction import get_toolkit_construction
    from mindroom.tool_system.metadata import get_tool_by_name
    from mindroom.tool_system.plugins import load_plugins
    from mindroom.tool_system.registry_state import tool_registry_origins
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


    async def main():
        action, plugin_root_value, storage_root_value = sys.argv[1:]
        plugin_root = Path(plugin_root_value)
        storage_root = Path(storage_root_value)
        config_path = plugin_root.parent / "config.yaml"
        config = Config(
            agents={
                "lead": {
                    "display_name": "Lead",
                    "tools": [{"name": "stable_plugin", "defer": True}],
                },
            },
            plugins=[str(plugin_root)],
        )
        paths = resolve_runtime_paths(
            config_path=config_path,
            storage_path=storage_root,
            process_env={
                "MATRIX_HOMESERVER": "http://localhost:8008",
                "MINDROOM_NAMESPACE": "",
            },
        )
        load_plugins(config, paths, skip_broken_plugins=False)
        toolkit = get_tool_by_name(
            "stable_plugin",
            paths,
            runtime_config=config,
            worker_target=None,
        )
        bind_toolkit_authority(toolkit, authored_name="stable_plugin")
        function = toolkit.get_async_functions()["stable_echo"]
        function._agent = bind_actor_authority(Agent(id="lead"), authority_snapshot(config, "lead"))
        construction = get_toolkit_construction(toolkit)
        identity = {
            "factory": tool_registry_origins()["stable_plugin"],
            "construction": list(construction.factory_origin),
            "callable": function_provenance(function),
        }
        owner = ToolExecutionIdentity(
            "matrix",
            "lead",
            "@human:localhost",
            "!room:localhost",
            "$thread:localhost",
            "$thread:localhost",
            "session",
        )
        adapter = {
            "origin": function_provenance(function),
            "authority": function_authority(function),
        }

        def authorized(job):
            return locally_allowed(
                config,
                job.owner,
                tool_name=job.tool_name,
                toolkit_name=job.toolkit_name,
                origin=job.adapter["origin"],
                depth=job.depth,
                authority=job.adapter["authority"],
            )

        statuses = {}
        if action == "create":
            runtime = ToolJobRuntime(storage_root)

            async def completed():
                return BackgroundOutcome("completed", "saved")

            async def running():
                await asyncio.Event().wait()

            await runtime.start(
                JobSpec(
                    "completed-job",
                    "stable_echo",
                    0,
                    toolkit_name="stable_plugin",
                    adapter=adapter,
                ),
                owner=owner,
                operation=completed,
            )
            waited = await runtime.wait("completed-job", owner=owner, depth=0)
            await runtime.release_wait("completed-job", waited.token)
            await runtime.start(
                JobSpec(
                    "interrupted-job",
                    "stable_echo",
                    0,
                    toolkit_name="stable_plugin",
                    adapter=adapter,
                ),
                owner=owner,
                operation=running,
            )
            await asyncio.sleep(0)
            await runtime.shutdown()
        elif action == "recover":
            runtime = ToolJobRuntime(storage_root, authorize=authorized)
            await runtime.recover()
            statuses = {
                job.job_id: job.status
                for job in await runtime.list_jobs(owner=owner, depth=0)
            }
            await runtime.shutdown()

        print("PLUGIN_RESULT=" + json.dumps({"identity": identity, "statuses": statuses}, sort_keys=True))


    asyncio.run(main())
    """,
)


def _write_registered_plugin(plugin_root: Path) -> None:
    plugin_root.mkdir(parents=True)
    (plugin_root.parent / "config.yaml").write_text("agents: {}\n", encoding="utf-8")
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "stable-plugin", "tools_module": "tools.py", "skills": []}),
        encoding="utf-8",
    )
    (plugin_root / "tools.py").write_text(
        "from agno.tools import Toolkit\n"
        "from mindroom.tool_system.declarations import ToolCategory\n"
        "from mindroom.tool_system.registration import register_tool_with_metadata\n"
        "\n"
        "class StableTools(Toolkit):\n"
        "    def __init__(self):\n"
        "        super().__init__(name='stable_plugin', tools=[self.stable_echo])\n"
        "\n"
        "    def stable_echo(self, value: str = 'saved') -> str:\n"
        "        return value\n"
        "\n"
        "@register_tool_with_metadata(\n"
        "    name='stable_plugin',\n"
        "    display_name='Stable Plugin',\n"
        "    description='Plugin identity regression fixture',\n"
        "    category=ToolCategory.DEVELOPMENT,\n"
        ")\n"
        "def stable_plugin_tools():\n"
        "    return StableTools\n",
        encoding="utf-8",
    )


def _run_plugin_process(action: str, plugin_root: Path, storage_root: Path, *, hash_seed: int) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "-c", _PLUGIN_PROCESS_SCRIPT, action, str(plugin_root), str(storage_root)],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": str(hash_seed)},
        timeout=30,
    )
    payload = next(
        line.removeprefix("PLUGIN_RESULT=")
        for line in completed.stdout.splitlines()
        if line.startswith("PLUGIN_RESULT=")
    )
    return json.loads(payload)


def test_plugin_job_identity_survives_hash_seed_restart_and_distinguishes_roots(tmp_path: Path) -> None:
    """Real plugin job authority must survive process hash randomization without merging roots."""
    first_root = tmp_path / "first" / "plugin"
    second_root = tmp_path / "second" / "plugin"
    storage_root = tmp_path / "storage"
    _write_registered_plugin(first_root)
    _write_registered_plugin(second_root)

    created = _run_plugin_process("create", first_root, storage_root, hash_seed=1001)
    recovered = _run_plugin_process("recover", first_root, storage_root, hash_seed=1002)
    other_root = _run_plugin_process("identity", second_root, storage_root, hash_seed=1002)

    assert created["identity"] == recovered["identity"]
    assert recovered["statuses"] == {
        "completed-job": "completed",
        "interrupted-job": "interrupted",
    }
    assert created["identity"] != other_root["identity"]
    identity = created["identity"]
    assert identity["factory"] == identity["construction"]
    assert identity["factory"][1] == "stable_plugin_tools"
    assert identity["callable"]["qualname"] == "StableTools.stable_echo"


def test_prepare_module_installs_package_chain(tmp_path: Path) -> None:
    """Prepared plugin module execution should install packages and expose the module."""
    plugin_root = tmp_path / "plugins" / "demo"
    package_dir = plugin_root / "nested"
    package_dir.mkdir(parents=True)
    module_path = package_dir / "tools.py"
    module_path.write_text("VALUE = 42\n", encoding="utf-8")

    module_name = plugin_imports._module_name("demo", plugin_root, module_path)
    package_names = [
        package_name for package_name, _ in plugin_imports._package_chain_names("demo", plugin_root, module_path)
    ]

    try:
        module, loader, _ = plugin_imports._prepare_module(
            "demo",
            plugin_root,
            module_path,
            module_name,
        )
        loader.exec_module(module)

        assert module.VALUE == 42
        assert sys.modules[module_name] is module
        for package_name in package_names:
            assert package_name in sys.modules
    finally:
        sys.modules.pop(module_name, None)
        for package_name in package_names:
            sys.modules.pop(package_name, None)


def test_prepare_module_restores_package_chain_on_spec_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec failures should restore pre-existing packages and remove synthetic ones."""
    plugin_root = tmp_path / "plugins" / "demo"
    package_dir = plugin_root / "nested"
    package_dir.mkdir(parents=True)
    module_path = package_dir / "tools.py"
    module_path.write_text("VALUE = 42\n", encoding="utf-8")

    package_names = [
        package_name for package_name, _ in plugin_imports._package_chain_names("demo", plugin_root, module_path)
    ]
    existing_package = ModuleType(package_names[0])
    sys.modules[package_names[0]] = existing_package
    monkeypatch.setattr(plugin_imports.util, "spec_from_file_location", lambda *_args, **_kwargs: None)

    try:
        module_execution = plugin_imports._prepare_module(
            "demo",
            plugin_root,
            module_path,
            plugin_imports._module_name("demo", plugin_root, module_path),
        )

        assert module_execution is None
        assert sys.modules[package_names[0]] is existing_package
        for package_name in package_names[1:]:
            assert package_name not in sys.modules
    finally:
        for package_name in package_names:
            sys.modules.pop(package_name, None)
