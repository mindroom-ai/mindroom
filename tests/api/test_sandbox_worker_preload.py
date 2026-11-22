"""Tests for a worker's eager setup and the opt-in plugin preload of its forkserver template."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mindroom.api.sandbox_forkserver as sandbox_forkserver_module
import mindroom.api.sandbox_runner as sandbox_runner_module
import mindroom.workers.backends.local as local_worker_module
from mindroom import constants
from mindroom.api import sandbox_runner_app
from mindroom.api.sandbox_forkserver import _SandboxForkserver
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tool_system import plugin_imports
from mindroom.tool_system import plugins as plugins_module
from tests.api.sandbox_runner_support import execute_call

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.constants import RuntimePaths
    from mindroom.workers.protocol import ExecuteResponse

pytestmark = pytest.mark.skipif(
    not sandbox_forkserver_module.forkserver_supported(),
    reason="forkserver dispatch requires os.fork and unix sockets",
)

_WORKER_KEY = "v1:default:user_agent:~@alice:example.org:code:named-test"
_TOKEN = "test-token"  # noqa: S105
# How a runner hands a template what to preload; the template removes it before any tool code runs.
_PRELOAD_ENV = "MINDROOM_SANDBOX_FORKSERVER_PRELOAD"

_PLUGIN_SOURCE = '''
import os
import sys
from pathlib import Path

import yaml
{at_import}
from mindroom.plugin_api import ToolCategory, ToolFileAccess, Toolkit, register_tool_with_metadata, tool

DATA = (Path(__file__).parent / "data.txt").read_text(encoding="utf-8")
with Path({import_log!r}).open("a", encoding="utf-8") as _log:
    _log.write(f"{name}|{{os.getpid()}}|{{os.getppid()}}|{{os.getcwd()}}|{{DATA}}\\n")


class _ReportTool(Toolkit):
    def __init__(self) -> None:
        super().__init__(name="{name}_tool")

    @tool
    def report(self) -> str:
        """Return the data read at import, the plugin modules, the preload variable, and a leaked global of this process."""
        modules = sorted(name for name in sys.modules if name.startswith("mindroom_plugin_"))
        leaked = "!left-by-import" in yaml.SafeLoader.yaml_constructors
        return f"{{DATA}}|{{','.join(modules)}}|{{os.environ.get({preload_env!r})}}|{{leaked}}"


@register_tool_with_metadata(
    name="{name}_tool",
    file_access=ToolFileAccess.NONE,
    display_name="{name}",
    description="Reports how this plugin was imported",
    category=ToolCategory.DEVELOPMENT,
)
def factory():
    return _ReportTool
'''


def _write_plugin(root: Path, name: str, *, worker_preload: object = None, at_import: str = "") -> PluginEntryConfig:
    """Write a plugin that logs each import of its tools module, and return its config entry."""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True)
    manifest: dict[str, object] = {"name": name, "tools_module": "tools.py"}
    if worker_preload is not None:
        manifest["worker_preload"] = worker_preload
    (plugin_dir / "mindroom.plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / "data.txt").write_text("v1", encoding="utf-8")
    (plugin_dir / "tools.py").write_text(
        _PLUGIN_SOURCE.format(
            name=name,
            at_import=at_import,
            import_log=str(root / "imports.log"),
            preload_env=_PRELOAD_ENV,
        ),
        encoding="utf-8",
    )
    return PluginEntryConfig(path=str(plugin_dir))


def _imports(root: Path, name: str) -> list[tuple[int, str, str]]:
    """Return the (pid, cwd, data) of each import of ``name`` outside this test process, which plays the runner."""
    log = root / "imports.log"
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    records = [line.split("|") for line in lines]
    return [
        (int(pid), cwd, data)
        for plugin, pid, _parent, cwd, data in records
        if plugin == name and int(pid) != os.getpid()
    ]


def _import_parents(root: Path, name: str) -> list[int]:
    """Return the parent process of each process that imported ``name``, outside this test process."""
    lines = (root / "imports.log").read_text(encoding="utf-8").splitlines()
    records = [line.split("|") for line in lines]
    return [int(parent) for plugin, pid, parent, _cwd, _data in records if plugin == name and int(pid) != os.getpid()]


def _runtime(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={
            "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "forkserver",
            "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "30",
        },
    )


def _preload_spec(runtime_paths: RuntimePaths, plugins: list[PluginEntryConfig], *, budget_seconds: float = 60) -> str:
    return json.dumps(
        {
            "config_path": str(runtime_paths.config_path),
            "storage_root": str(runtime_paths.storage_root),
            "plugins": [plugin.path for plugin in plugins],
            "budget_seconds": budget_seconds,
        },
    )


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch) -> Iterator[_SandboxForkserver]:
    """Return this test's forkserver, with every template it spawns recorded on ``spawned``."""
    forkserver = _SandboxForkserver()
    forkserver.spawned = []
    spawn = forkserver._spawn_template

    def recording_spawn(**kwargs: object) -> sandbox_forkserver_module._Template:
        template = spawn(**kwargs)
        forkserver.spawned.append(template)
        return template

    monkeypatch.setattr(forkserver, "_spawn_template", recording_spawn)
    monkeypatch.setattr(sandbox_forkserver_module, "get_sandbox_forkserver", lambda: forkserver)
    try:
        yield forkserver
    finally:
        forkserver.shutdown()


def _call(
    runtime_paths: RuntimePaths,
    plugins: list[PluginEntryConfig],
    tool_name: str,
    function_name: str,
    *args: object,
) -> object:
    """Run one tool call under a config of ``plugins``, as the runner dispatches it, and return its result."""
    response: ExecuteResponse = sandbox_runner_module._execute_request_subprocess_sync(
        execute_call(
            runtime_paths,
            Config(plugins=plugins),
            tool_name=tool_name,
            function_name=function_name,
            args=list(args),
            kwargs={},
        ),
    )
    assert response.ok is True, response.error
    return response.result


def _report(runtime_paths: RuntimePaths, plugins: list[PluginEntryConfig], name: str) -> list[str]:
    """Call ``name``'s tool and return its data, plugin modules, preload variable, and whether a global leaked."""
    return str(_call(runtime_paths, plugins, f"{name}_tool", "report")).split("|")


def _replace_keeping_size_and_mtime(path: Path, *, in_place: bool) -> None:
    """Change a file's content the way a timestamp-preserving copy or sync does."""
    before = path.stat()
    content = path.read_text(encoding="utf-8").swapcase()
    # Coarse filesystem clocks give two writes within a few milliseconds one change time.
    time.sleep(0.05)
    if in_place:
        path.write_text(content, encoding="utf-8")
    else:
        replacement = path.with_name(f"{path.name}.new")
        replacement.write_text(content, encoding="utf-8")
        replacement.replace(path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


def test_only_opt_in_plugins_import_once_and_the_others_import_in_every_call(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """A plugin that sets ``worker_preload`` is imported once by the template; any other is imported by each call."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    plain = _write_plugin(tmp_path, "plain")
    manager.template_preload = _preload_spec(runtime_paths, [opted, plain])

    for _ in range(3):
        assert _report(runtime_paths, [opted, plain], "opted")[0] == "v1"
        assert _report(runtime_paths, [opted, plain], "plain")[0] == "v1"

    assert len(manager.spawned) == 1
    template_pid = manager.spawned[0].process.pid
    assert [pid for pid, _cwd, _data in _imports(tmp_path, "opted")] == [template_pid]
    plain_pids = [pid for pid, _cwd, _data in _imports(tmp_path, "plain")]
    assert len(set(plain_pids)) == 6
    assert template_pid not in plain_pids


def test_a_runner_without_eager_setup_preloads_nothing(manager: _SandboxForkserver, tmp_path: Path) -> None:
    """Docker workers and shared runners get no preload, so even an opt-in plugin is imported by each call."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)

    for _ in range(3):
        _report(runtime_paths, [opted], "opted")

    template_pid = manager.spawned[0].process.pid
    pids = [pid for pid, _cwd, _data in _imports(tmp_path, "opted")]
    assert len(set(pids)) == 3
    assert template_pid not in pids


def test_a_changed_file_of_a_preloaded_plugin_loads_it_in_each_call_and_keeps_the_template(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """Any changed file under an opt-in plugin's root, not only Python source, ends the use of its preload."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    manager.template_preload = _preload_spec(runtime_paths, [opted])

    assert _report(runtime_paths, [opted], "opted")[0] == "v1"
    assert _report(runtime_paths, [opted], "opted")[0] == "v1"
    assert len(_imports(tmp_path, "opted")) == 1

    data_file = tmp_path / "opted" / "data.txt"
    data_file.write_text("v2", encoding="utf-8")
    os.utime(data_file, (time.time() + 5, time.time() + 5))

    assert _report(runtime_paths, [opted], "opted")[0] == "v2"
    assert _report(runtime_paths, [opted], "opted")[0] == "v2"

    template_pid = manager.spawned[0].process.pid
    assert [(pid == template_pid, data) for pid, _cwd, data in _imports(tmp_path, "opted")] == [
        (True, "v1"),
        (False, "v2"),
        (False, "v2"),
    ]
    assert len(manager.spawned) == 1
    assert manager.spawned[0].process.poll() is None


def test_a_request_for_other_plugins_never_sees_the_preloaded_ones_and_keeps_the_template(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """Requests whose configs differ share one template: none recycles it, and none sees another's plugins."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    plain = _write_plugin(tmp_path, "plain")
    manager.template_preload = _preload_spec(runtime_paths, [opted])

    with_opted = _report(runtime_paths, [opted, plain], "plain")[1]
    without_opted = _report(runtime_paths, [plain], "plain")[1]
    _report(runtime_paths, [opted], "opted")

    assert "opted" in with_opted
    assert "opted" not in without_opted
    assert "plain" in without_opted
    assert len(manager.spawned) == 1
    assert [pid for pid, _cwd, _data in _imports(tmp_path, "opted")] == [manager.spawned[0].process.pid]


@pytest.mark.parametrize(
    "at_import",
    [
        "import threading\nthreading.Thread(target=threading.Event().wait, daemon=True).start()",
        "KEPT_OPEN = open(__file__, encoding='utf-8')",
    ],
    ids=["thread", "open-file"],
)
def test_a_plugin_that_breaks_its_promise_is_never_preloaded(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    at_import: str,
) -> None:
    """A template never forks with a thread or an open file a plugin left behind, and tries the preload once."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True, at_import=at_import)
    manager.template_preload = _preload_spec(runtime_paths, [opted])
    warnings: list[str] = []
    monkeypatch.setattr(sandbox_forkserver_module.logger, "warning", lambda event, **_fields: warnings.append(event))

    for _ in range(3):
        assert _report(runtime_paths, [opted], "opted")[0] == "v1"

    assert len(manager.spawned) == 1
    template = manager.spawned[0]
    assert template.process.poll() is None
    pids = [pid for pid, _cwd, _data in _imports(tmp_path, "opted")]
    # The template's one attempt, then each call's own import.
    assert pids[0] == template.process.pid
    assert len(set(pids)) == 4
    assert warnings == ["sandbox_forkserver_plugin_preload_failed"]


def test_a_template_has_no_control_channel_and_hides_its_preload_from_tools(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """Nothing a tool can reach changes what later calls inherit: the template listens for requests only."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    manager.template_preload = _preload_spec(runtime_paths, [opted])

    preload_env = _report(runtime_paths, [opted], "opted")[2]

    assert preload_env == "None"
    assert sorted(path.name for path in manager.spawned[0].runtime_dir.iterdir()) == ["template.err", "template.sock"]
    assert not hasattr(manager, "_preload_template_in_place")


def test_a_request_the_preload_does_not_match_runs_where_those_plugins_were_never_imported(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """What a preloaded plugin registered globally at import never reaches a call that does not use that preload."""
    runtime_paths = _runtime(tmp_path)
    registers = "yaml.SafeLoader.add_constructor('!left-by-import', lambda loader, node: None)"
    opted = _write_plugin(tmp_path, "opted", worker_preload=True, at_import=registers)
    plain = _write_plugin(tmp_path, "plain")
    manager.template_preload = _preload_spec(runtime_paths, [opted])

    assert _report(runtime_paths, [opted, plain], "plain")[3] == "True"
    preloading_template = manager.spawned[0].process.pid
    assert _import_parents(tmp_path, "opted") == []

    # Another set of opt-in plugins: this call has no use for the preload.
    assert _report(runtime_paths, [plain], "plain")[3] == "False"

    # The same set with a changed file: the call imports the plugin itself, in a process that never had the old one.
    _replace_keeping_size_and_mtime(tmp_path / "opted" / "data.txt", in_place=True)
    assert _report(runtime_paths, [opted], "opted")[0] == "V1"
    assert preloading_template not in _import_parents(tmp_path, "opted")
    assert manager.spawned[0].process.poll() is None


@pytest.mark.parametrize("in_place", [True, False], ids=["rewritten", "renamed-over"])
@pytest.mark.parametrize("changed", ["tools.py", "data.txt", "assets/logo.txt"])
def test_the_fingerprint_sees_a_replaced_file_of_the_same_size_and_time(
    tmp_path: Path,
    changed: str,
    in_place: bool,
) -> None:
    """A file replaced without a new size or modification time, also behind a linked directory, is a changed plugin."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    shared_assets = tmp_path / "shared-assets"
    shared_assets.mkdir()
    (shared_assets / "logo.txt").write_text("logo", encoding="utf-8")
    (tmp_path / "opted" / "assets").symlink_to(shared_assets, target_is_directory=True)
    # A link back to the plugin's own directory must not make the walk endless.
    (shared_assets / "back").symlink_to(tmp_path / "opted", target_is_directory=True)
    config = Config(plugins=[opted])
    _entries, fingerprint = plugins_module._worker_preload_plugins(config, runtime_paths)
    assert plugins_module._worker_preload_plugins(config, runtime_paths)[1] == fingerprint

    _replace_keeping_size_and_mtime(tmp_path / "opted" / changed, in_place=in_place)

    assert plugins_module._worker_preload_plugins(config, runtime_paths)[1] != fingerprint


def test_a_plugin_whose_import_hangs_is_given_up_and_other_calls_run(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """A preload has its own time limit, after which calls run in a template that never tried it."""
    runtime_paths = _runtime(tmp_path)
    hangs = _write_plugin(tmp_path, "hangs", worker_preload=True, at_import="import time\ntime.sleep(600)")
    manager.template_preload = _preload_spec(runtime_paths, [hangs], budget_seconds=1)
    started_at = time.monotonic()

    assert _call(runtime_paths, [], "calculator", "add", 1, 2) is not None
    assert "ok" in str(_call(runtime_paths, [], "shell", "run_shell_command", ["bash", "-c", "printf ok"]))
    assert _call(runtime_paths, [], "calculator", "add", 1, 2) is not None

    assert time.monotonic() - started_at < 20
    # Only the first template tried the preload; the one that serves never does.
    assert len(_imports(tmp_path, "hangs")) == 1
    assert manager.spawned[0].process.poll() is not None


@pytest.mark.parametrize("execution_mode", ["forkserver", "subprocess"])
def test_each_call_logs_its_stage_timing_once(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_mode: str,
) -> None:
    """One debug record per call tells where its child spent its time and whether it reused preloaded plugins."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": execution_mode},
    )
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    manager.template_preload = _preload_spec(runtime_paths, [opted])
    records: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(sandbox_runner_module.logger, "debug", lambda event, **fields: records.append((event, fields)))
    monkeypatch.setattr(
        sandbox_forkserver_module.logger,
        "debug",
        lambda event, **fields: records.append((event, fields)),
    )

    _report(runtime_paths, [opted], "opted")

    timings = [fields["timing"] for event, fields in records if "timing" in event]
    assert len(timings) == 1
    timing = timings[0]
    assert isinstance(timing, dict)
    assert set(timing) == {"config_ms", "plugins_ms", "tool_ms", "preloaded"}
    assert timing["preloaded"] is (execution_mode == "forkserver")
    assert all(isinstance(timing[stage], float) for stage in ("config_ms", "plugins_ms", "tool_ms"))


def test_worker_preload_in_a_manifest_must_be_a_boolean(tmp_path: Path) -> None:
    """The opt-in is a promise, so a manifest must state it as true or false."""
    plain = _write_plugin(tmp_path, "plain")
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    unclear = _write_plugin(tmp_path, "unclear", worker_preload="yes")

    assert plugin_imports._load_plugin_base(Path(plain.path)).worker_preload is False
    assert plugin_imports._load_plugin_base(Path(opted.path)).worker_preload is True

    with pytest.raises(plugin_imports.PluginValidationError, match="worker_preload must be true or false"):
        plugin_imports._load_plugin_base(Path(unclear.path))


def _dedicated_worker_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    eager_setup_plugins: list[str] | None,
) -> tuple[FastAPI, Path]:
    """Return a dedicated worker's runner app and worker root, started from a manifest with ``eager_setup_plugins``."""
    worker_root = tmp_path / "worker"
    worker_root.mkdir()
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=worker_root,
        process_env={
            "MINDROOM_SANDBOX_RUNNER_MODE": "true",
            "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "forkserver",
            "MINDROOM_SANDBOX_RUNNER_SUBPROCESS_TIMEOUT_SECONDS": "40",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": _WORKER_KEY,
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(worker_root),
        },
    )
    manifest_path = constants.write_startup_manifest(worker_root, runtime_paths, public_runtime=True)
    if eager_setup_plugins is not None:
        # What the Kubernetes backend adds to the manifest it writes.
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_path.write_text(json.dumps({**manifest, "eager_setup_plugins": eager_setup_plugins}), encoding="utf-8")
    monkeypatch.setenv("MINDROOM_SANDBOX_STARTUP_MANIFEST_PATH", str(manifest_path))
    app = FastAPI(lifespan=sandbox_runner_app._lifespan)
    app.include_router(sandbox_runner_module.router)
    app.add_api_route("/healthz", sandbox_runner_app.healthz)
    sandbox_runner_module.initialize_sandbox_runner_app(app, runtime_paths, config=Config(), runner_token=_TOKEN)
    return app, worker_root


def _wait_for_setup(client: TestClient, state: str) -> None:
    deadline = time.monotonic() + 240
    while client.get("/healthz").json().get("setup") != state:
        assert time.monotonic() < deadline, client.get("/healthz").json()
        time.sleep(0.2)


def _http_call(
    client: TestClient,
    plugins: list[PluginEntryConfig],
    tool_name: str,
    function_name: str,
    *args: object,
) -> dict[str, object]:
    response = client.post(
        "/api/sandbox-runner/execute",
        headers={"X-Mindroom-Sandbox-Token": _TOKEN},
        json={
            "protocol": sandbox_runner_module.WORKER_PROTOCOL_VERSION,
            "tool_name": tool_name,
            "function_name": function_name,
            "args": list(args),
            "private_agent_names": [],
            "config_snapshot": {"plugins": [{"path": plugin.path} for plugin in plugins]},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def _http_report(client: TestClient, plugins: list[PluginEntryConfig], name: str) -> dict[str, object]:
    return _http_call(client, plugins, f"{name}_tool", "report")


def test_eager_setup_runs_behind_the_health_check_and_preloads_only_opt_in_plugins(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker answers its health check while it prepares, and then runs opt-in plugins without importing them again."""
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    plain = _write_plugin(tmp_path, "plain")
    app, worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[opted.path, plain.path])
    venv_may_start = threading.Event()
    ensure_state = local_worker_module._ensure_local_worker_state

    def slow_ensure_state(paths: object) -> None:
        # A burst of starting workers shares its nodes and storage, so one venv can take longer than the ready timeout.
        venv_may_start.wait(timeout=10)
        ensure_state(paths)

    monkeypatch.setattr(local_worker_module, "_ensure_local_worker_state", slow_ensure_state)

    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["status"] == "ok"
        assert health["setup"] == "preparing"
        assert not (worker_root / "venv").exists()

        venv_may_start.set()
        _wait_for_setup(client, "ready")
        assert (worker_root / "venv" / "bin" / "python").exists()
        assert [template.ready for template in manager.spawned] == [True]

        for _ in range(2):
            for name in ("opted", "plain"):
                body = _http_report(client, [opted, plain], name)
                assert body["ok"] is True, body

    assert len(manager.spawned) == 1
    template_pid = manager.spawned[0].process.pid
    workspace = str((worker_root / "workspace").resolve())
    assert [pid for pid, _cwd, _data in _imports(tmp_path, "opted")] == [template_pid]
    plain_imports = _imports(tmp_path, "plain")
    # Each call imports the other plugin itself, after it moved to the request's working directory.
    assert len({pid for pid, _cwd, _data in plain_imports}) == 4
    assert {cwd for _pid, cwd, _data in plain_imports} == {workspace}


def test_a_setup_that_outlasts_its_budget_ends_the_preload_and_calls_run(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A preload still running when the setup budget ends is killed, and calls run in a template without it."""
    hangs = _write_plugin(tmp_path, "hangs", worker_preload=True, at_import="import time\ntime.sleep(600)")
    app, _worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[hangs.path])
    monkeypatch.setattr(sandbox_runner_module, "_EAGER_SETUP_BUDGET_SECONDS", 6.0)

    with TestClient(app) as client:
        _wait_for_setup(client, "failed")
        started_at = time.monotonic()

        assert _http_call(client, [], "calculator", "add", 1, 2)["ok"] is True
        shell = _http_call(client, [], "shell", "run_shell_command", ["bash", "-c", "printf ok"])
        assert shell["ok"] is True, shell
        assert "ok" in str(shell["result"])

        assert time.monotonic() - started_at < 25
        assert manager.spawned[0].process.poll() is not None
    assert len(_imports(tmp_path, "hangs")) == 1


def test_a_failed_eager_setup_leaves_the_worker_healthy_and_its_first_call_reports_the_real_error(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A setup that fails never stops the worker: calls prepare it themselves, and fail with their own error."""
    plain = _write_plugin(tmp_path, "plain")
    app, worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[])
    disk_full = threading.Event()
    disk_full.set()
    ensure_state = local_worker_module._ensure_local_worker_state

    def ensure_state_on_a_full_disk(paths: object) -> None:
        if disk_full.is_set():
            msg = "No space left on device"
            raise OSError(msg)
        ensure_state(paths)

    monkeypatch.setattr(local_worker_module, "_ensure_local_worker_state", ensure_state_on_a_full_disk)

    with TestClient(app) as client:
        _wait_for_setup(client, "failed")
        assert client.get("/healthz").json()["status"] == "ok"
        assert manager.spawned == []

        failed = _http_report(client, [plain], "plain")
        assert failed["ok"] is False
        assert "No space left on device" in str(failed["error"])

        disk_full.clear()
        body = _http_report(client, [plain], "plain")
        assert body["ok"] is True, body
        assert (worker_root / "venv" / "bin" / "python").exists()


def test_a_worker_whose_manifest_asks_for_no_eager_setup_starts_as_before(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Docker workers get a manifest without eager setup, so they prepare on their first request and report no setup."""
    app, worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=None)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {
            "status": "ok",
            "mindroom_version": sandbox_runner_app.__version__,
            "worker_protocol": sandbox_runner_module.WORKER_PROTOCOL_VERSION,
        }
        time.sleep(0.5)
        assert not (worker_root / "venv").exists()
        assert manager.spawned == []
        assert manager.template_preload is None
