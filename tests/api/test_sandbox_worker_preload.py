"""Tests for a worker's eager setup and the opt-in plugin preload of its forkserver template."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import mindroom.api.sandbox_forkserver as sandbox_forkserver_module
import mindroom.api.sandbox_runner as sandbox_runner_module
import mindroom.workers.backends.local as local_worker_module
from mindroom import constants
from mindroom.api import sandbox_exec, sandbox_runner_app
from mindroom.api.sandbox_forkserver import _SandboxForkserver
from mindroom.config.main import Config
from mindroom.config.plugin import PluginEntryConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.tool_system import plugin_imports
from mindroom.tool_system.plugins import worker_preload_plugin_paths
from tests.api.sandbox_runner_support import execute_call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Iterator

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
        """Return the data read at import, the plugin modules, the preload variable, and two globals of this process."""
        modules = sorted(name for name in sys.modules if name.startswith("mindroom_plugin_"))
        leaked = "!left-by-import" in yaml.SafeLoader.yaml_constructors
        shared = yaml.SafeLoader.yaml_constructors.get("!shared")
        return f"{{DATA}}|{{','.join(modules)}}|{{os.environ.get({preload_env!r})}}|{{leaked}}|{{shared}}"


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


def _warm(preloading: _SandboxForkserver) -> None:
    """Start ``preloading``'s template for the interpreter and environment that this test's requests name."""
    python_executable, template_env, _cwd = sandbox_exec.resolve_subprocess_worker_context(None)
    preloading.warm(python_executable, template_env, 30.0)


def _serve_preload(
    runtime_paths: RuntimePaths,
    plugins: list[PluginEntryConfig],
    *,
    budget_seconds: float = 60,
) -> _SandboxForkserver:
    """Preload ``plugins`` as a worker's eager setup does: start the one template, and serve from it once it is up."""
    preloading = sandbox_forkserver_module.start_preloading_forkserver(
        _preload_spec(runtime_paths, plugins, budget_seconds=budget_seconds),
    )
    try:
        _warm(preloading)
    except sandbox_forkserver_module.ForkserverError as exc:
        sandbox_forkserver_module.abandon_preload(str(exc) or "The preload outlasted the worker's setup budget.")
    else:
        preloading.serves = tuple(plugin.path for plugin in plugins)
    return preloading


def _runs(pid: int) -> bool:
    """Return whether process ``pid`` still runs; one that ended and waits to be reaped does not."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rpartition(")")[2].split()[0]
    except OSError:
        return False
    return state not in "ZX"


@pytest.fixture
def manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_SandboxForkserver]:
    """Return this test's plain forkserver, which records what every forkserver of the test does.

    ``spawned`` lists each template started, and ``dispatched`` tells for each request handed to a forkserver whether
    that was the preloading one.
    """
    spawned: list[sandbox_forkserver_module._Template] = []
    dispatched: list[bool] = []
    spawn = _SandboxForkserver._spawn_template
    execute = _SandboxForkserver.execute

    def recording_spawn(self: _SandboxForkserver, **kwargs: object) -> sandbox_forkserver_module._Template:
        template = spawn(self, **kwargs)
        spawned.append(template)
        return template

    def recording_execute(self: _SandboxForkserver, **kwargs: object) -> object:
        dispatched.append(self is not forkserver)
        return execute(self, **kwargs)

    forkserver = _SandboxForkserver()
    forkserver.spawned = spawned
    forkserver.dispatched = dispatched
    monkeypatch.setattr(_SandboxForkserver, "_spawn_template", recording_spawn)
    monkeypatch.setattr(_SandboxForkserver, "execute", recording_execute)
    monkeypatch.setattr(sandbox_forkserver_module, "get_sandbox_forkserver", lambda: forkserver)
    try:
        yield forkserver
    finally:
        forkserver.shutdown()
        if (preloading := sandbox_forkserver_module._preloading_forkserver) is not None:
            preloading.shutdown()
        sandbox_forkserver_module._preloading_forkserver = None
        # A call that imports a plugin itself leaves what that import started, as it always did.
        for pid in _started_processes(tmp_path):
            with suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        # This process plays the runner, which imports a request's plugins itself.
        for tag in ("!left-by-import", "!shared"):
            yaml.SafeLoader.yaml_constructors.pop(tag, None)


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


# Starts a process of its own at import, as a plugin that launches a helper does, and notes its PID.
# This test process plays the runner, which imports a request's plugins too, and must not leave one behind.
_STARTS_A_PROCESS = (
    "import subprocess\n"
    f"if os.getpid() != {os.getpid()}:\n"
    "    _helper = subprocess.Popen(['sleep', '600'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,"
    " stderr=subprocess.DEVNULL)\n"
    "    Path({started_log!r}).open('a').write(f'{{_helper.pid}}\\n')"
)


def _started_processes(root: Path) -> list[int]:
    """Return the PIDs of the processes that plugins written with ``_STARTS_A_PROCESS`` started."""
    log = root / "started.log"
    return [int(pid) for pid in log.read_text(encoding="utf-8").split()] if log.exists() else []


def _write_hanging_plugin(root: Path) -> PluginEntryConfig:
    """Write an opt-in plugin whose import notes each try in ``tried.log``, starts a process, and never returns."""
    notes_try = f"Path({str(root / 'tried.log')!r}).open('a').write('tried\\n')"
    starts_a_process = _STARTS_A_PROCESS.format(started_log=str(root / "started.log"))
    return _write_plugin(
        root,
        "hangs",
        worker_preload=True,
        at_import=f"{notes_try}\n{starts_a_process}\nimport time\ntime.sleep(600)",
    )


def test_only_opt_in_plugins_import_once_and_the_others_import_in_every_call(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """A plugin that sets ``worker_preload`` is imported once by the template; any other is imported by each call."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    plain = _write_plugin(tmp_path, "plain")
    _serve_preload(runtime_paths, [opted])

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
    assert manager.dispatched == [False] * 3


def test_a_changed_file_of_a_preloaded_plugin_takes_effect_when_the_worker_restarts(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """Opting in promises files that stay as they are while a worker runs, so no call looks for a change."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    _serve_preload(runtime_paths, [opted])
    assert _report(runtime_paths, [opted], "opted")[0] == "v1"

    data_file = tmp_path / "opted" / "data.txt"
    data_file.write_text("v2", encoding="utf-8")
    os.utime(data_file, (time.time() + 5, time.time() + 5))

    assert _report(runtime_paths, [opted], "opted")[0] == "v1"
    assert manager.dispatched == [True, True]
    assert [pid for pid, _cwd, _data in _imports(tmp_path, "opted")] == [manager.spawned[0].process.pid]

    # A worker that starts afterwards, here a new preload, imports what is there now.
    sandbox_forkserver_module._preloading_forkserver.shutdown()
    _serve_preload(runtime_paths, [opted])
    assert _report(runtime_paths, [opted], "opted")[0] == "v2"


def test_the_runner_sends_a_request_to_the_preloaded_template_only_when_its_plugins_start_with_the_preloaded_ones(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """The runner decides before it dispatches: every request the preload does not suit goes to the plain template."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    other = _write_plugin(tmp_path, "other", worker_preload=True)
    plain = _write_plugin(tmp_path, "plain")
    # The same directory under another configured path is another plugin entry.
    (tmp_path / "alias").symlink_to(tmp_path / "opted", target_is_directory=True)
    alias = PluginEntryConfig(path=str(tmp_path / "alias"))
    _serve_preload(runtime_paths, [opted])

    with_opted = _report(runtime_paths, [opted, plain], "plain")[1]
    without_opted = _report(runtime_paths, [plain], "plain")[1]
    # A plugin after the preloaded ones is imported by the call, as every plugin was, whether it opts in or not.
    _report(runtime_paths, [opted, other], "opted")
    _report(runtime_paths, [alias], "opted")
    _report(runtime_paths, [PluginEntryConfig(path=opted.path, enabled=False), plain], "plain")
    _report(runtime_paths, [PluginEntryConfig(path=plain.path, enabled=False), opted], "opted")

    # One dispatch per request, so each has the one deadline it started with.
    assert manager.dispatched == [True, False, True, False, False, True]
    assert "opted" in with_opted
    assert "opted" not in without_opted
    assert "plain" in without_opted
    # The preloading template and the plain one, neither replaced.
    assert len(manager.spawned) == 2
    assert all(template.process.poll() is None for template in manager.spawned)
    preloading_template, plain_template = (template.process.pid for template in manager.spawned)
    # No process forked from the preloading template ran a request the preload did not suit.
    assert _import_parents(tmp_path, "opted") == [os.getpid(), plain_template]
    assert _import_parents(tmp_path, "other") == [preloading_template]
    assert preloading_template not in _import_parents(tmp_path, "plain")[1:]


def test_a_request_that_imports_another_plugin_first_keeps_that_order(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """Plugins import in config order, so the preload serves only requests whose first plugins are the preloaded ones."""
    runtime_paths = _runtime(tmp_path)
    # Both plugins set the same global key at import, so the one imported last owns it.
    plain = _write_plugin(tmp_path, "plain", at_import="yaml.SafeLoader.yaml_constructors['!shared'] = 'plain'")
    opted = _write_plugin(
        tmp_path,
        "opted",
        worker_preload=True,
        at_import="yaml.SafeLoader.yaml_constructors['!shared'] = 'opted'",
    )
    _serve_preload(runtime_paths, [opted])

    assert _report(runtime_paths, [plain, opted], "plain")[4] == "opted"
    assert _report(runtime_paths, [opted, plain], "plain")[4] == "plain"

    assert manager.dispatched == [False, True]


def test_a_preloading_template_that_cannot_be_reached_hands_its_request_to_the_plain_one(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A request that could not even connect to the preloading template was not sent, so the plain one runs it."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    warnings: list[str] = []
    monkeypatch.setattr(sandbox_forkserver_module.logger, "warning", lambda event, **_fields: warnings.append(event))
    _serve_preload(runtime_paths, [opted])
    # The template still runs, so the runner finds out only when it connects.
    Path(manager.spawned[0].socket_path).unlink()

    for _ in range(2):
        assert _report(runtime_paths, [opted], "opted")[0] == "v1"

    assert manager.dispatched == [True, False, False]
    assert len(manager.spawned) == 2
    assert manager.spawned[0].process.poll() is not None
    assert warnings.count("sandbox_forkserver_plugin_preload_failed") == 1


def test_no_request_runs_in_the_preloading_template_before_the_setup_succeeded(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """Until a worker's setup ends well its calls run as without a preload, also while the template is up already."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    preloading = sandbox_forkserver_module.start_preloading_forkserver(_preload_spec(runtime_paths, [opted]))

    _report(runtime_paths, [opted], "opted")
    _warm(preloading)
    _report(runtime_paths, [opted], "opted")

    assert manager.dispatched == [False, False]

    preloading.serves = (opted.path,)
    _report(runtime_paths, [opted], "opted")

    assert manager.dispatched == [False, False, True]


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
    warnings: list[str] = []
    monkeypatch.setattr(sandbox_forkserver_module.logger, "warning", lambda event, **_fields: warnings.append(event))
    _serve_preload(runtime_paths, [opted])

    for _ in range(3):
        assert _report(runtime_paths, [opted], "opted")[0] == "v1"

    # The template that tried the preload ended, and one that never tried it serves.
    tried, serving = manager.spawned
    assert tried.process.poll() is not None
    assert serving.process.poll() is None
    assert manager.dispatched == [False] * 3
    pids = [pid for pid, _cwd, _data in _imports(tmp_path, "opted")]
    # The one attempt, then each call's own import.
    assert pids[0] == tried.process.pid
    assert len(set(pids)) == 4
    assert warnings.count("sandbox_forkserver_plugin_preload_failed") == 1


def test_a_template_has_no_control_channel_and_hides_its_preload_from_tools(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """Nothing a tool can reach changes what later calls inherit: the template listens for requests only."""
    runtime_paths = _runtime(tmp_path)
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    _serve_preload(runtime_paths, [opted])

    preload_env = _report(runtime_paths, [opted], "opted")[2]

    assert preload_env == "None"
    assert manager.dispatched == [True]
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
    _serve_preload(runtime_paths, [opted])

    assert _report(runtime_paths, [opted, plain], "plain")[3] == "True"
    assert _import_parents(tmp_path, "opted") == [os.getpid()]

    # Another set of opt-in plugins: this call has no use for the preload.
    assert _report(runtime_paths, [plain], "plain")[3] == "False"
    assert manager.spawned[0].process.poll() is None


def test_a_plugin_whose_import_hangs_is_given_up_with_all_it_started_and_other_calls_run(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """A preload has its own time limit, after which calls run in a template that never tried it."""
    runtime_paths = _runtime(tmp_path)
    hangs = _write_hanging_plugin(tmp_path)
    started_at = time.monotonic()
    preloading = _serve_preload(runtime_paths, [hangs], budget_seconds=1)

    assert _call(runtime_paths, [], "calculator", "calculate", "add", 1, 2) is not None
    assert "ok" in str(_call(runtime_paths, [], "shell", "run_shell_command", ["bash", "-c", "printf ok"]))
    assert _call(runtime_paths, [], "calculator", "calculate", "add", 1, 2) is not None

    assert time.monotonic() - started_at < 20
    # Only the first template tried the preload; the one that serves never does.
    assert (tmp_path / "tried.log").read_text(encoding="utf-8") == "tried\n"
    assert manager.spawned[0].process.poll() is not None
    assert manager.dispatched == [False] * 3
    # The timer ended only the template, so the runner ended what the import had started.
    assert _started_processes(tmp_path)
    assert not any(_runs(pid) for pid in _started_processes(tmp_path))
    # A preload that was given up starts no template again, whoever asks.
    with pytest.raises(sandbox_forkserver_module.ForkserverStartupError):
        _warm(preloading)
    assert len(manager.spawned) == 2


def test_a_preloading_template_that_ends_is_not_started_again_and_its_request_keeps_one_deadline(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The preload is tried once per worker: when its template is gone, the request runs plain in the time left."""
    runtime_paths = _runtime(tmp_path)
    starts_a_process = _STARTS_A_PROCESS.format(started_log=str(tmp_path / "started.log"))
    opted = _write_plugin(tmp_path, "opted", worker_preload=True, at_import=starts_a_process)
    warnings: list[str] = []
    monkeypatch.setattr(sandbox_forkserver_module.logger, "warning", lambda event, **_fields: warnings.append(event))
    preloading = _serve_preload(runtime_paths, [opted])
    assert _report(runtime_paths, [opted], "opted")[0] == "v1"
    (helper,) = _started_processes(tmp_path)
    assert _runs(helper)
    budgets: list[tuple[bool, float, float]] = []
    execute = _SandboxForkserver.execute

    def slow_to_notice(self: _SandboxForkserver, **kwargs: float) -> object:
        budgets.append((self is preloading, kwargs["timeout_seconds"], kwargs["startup_timeout_seconds"]))
        if self is preloading:
            time.sleep(1.0)
        return execute(self, **kwargs)

    monkeypatch.setattr(_SandboxForkserver, "execute", slow_to_notice)
    manager.spawned[0].process.kill()
    manager.spawned[0].process.wait()

    for _ in range(2):
        assert _report(runtime_paths, [opted], "opted")[0] == "v1"

    # The request that found the template gone ran once more, with the second of its 30 that was spent taken off.
    (_, timeout, startup_timeout), (_, timeout_left, startup_timeout_left), later = budgets
    assert [preloaded for preloaded, _timeout, _startup_timeout in budgets] == [True, False, False]
    assert timeout_left <= timeout - 1.0
    assert startup_timeout_left <= startup_timeout - 1.0
    assert later[1] == pytest.approx(30.0, abs=0.5)
    # No second preload: one plain template serves from here on, and what the plugin had started is gone.
    assert len(manager.spawned) == 2
    plain_template = manager.spawned[1].process.pid
    assert _import_parents(tmp_path, "opted") == [os.getpid(), plain_template, plain_template]
    assert not _runs(helper)
    assert warnings.count("sandbox_forkserver_plugin_preload_failed") == 1


@pytest.mark.usefixtures("manager")
@pytest.mark.parametrize("execution_mode", ["forkserver", "subprocess"])
def test_each_call_logs_its_stage_timing_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    execution_mode: str,
) -> None:
    """One debug record per call tells where its child spent its time."""
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": execution_mode},
    )
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
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
    assert set(timing) == {"config_ms", "plugins_ms", "tool_ms"}
    assert all(isinstance(stage_ms, float) for stage_ms in timing.values())


def test_only_the_opt_in_plugins_that_lead_the_plugin_list_are_preloaded(tmp_path: Path) -> None:
    """Plugins import in config order, so a worker preloads the opt-in plugins that no other enabled plugin precedes."""
    runtime_paths = _runtime(tmp_path)
    first = _write_plugin(tmp_path, "first", worker_preload=True)
    second = _write_plugin(tmp_path, "second", worker_preload=True)
    late = _write_plugin(tmp_path, "late", worker_preload=True)
    plain = _write_plugin(tmp_path, "plain")
    disabled = PluginEntryConfig(path=_write_plugin(tmp_path, "disabled").path, enabled=False)
    missing = PluginEntryConfig(path=str(tmp_path / "missing"))

    def preloaded(*plugins: PluginEntryConfig) -> tuple[str, ...]:
        return worker_preload_plugin_paths(Config(plugins=list(plugins)), runtime_paths)

    assert preloaded(first, second, plain, late) == (first.path, second.path)
    assert preloaded(plain, first) == ()
    assert preloaded(disabled, first, plain) == (first.path,)
    assert preloaded(first, missing, second) == (first.path,)
    assert preloaded(missing, first) == ()


@pytest.mark.parametrize(
    ("path", "is_preloaded"),
    [
        ("./plugins/named", True),
        ("plugins/named", True),
        ("../beside/plugins/named", True),
        ("${MINDROOM_STORAGE_PATH}/plugins/named", True),
        ("python:packaged", True),
        ("packaged", True),
        ("~/plugins/named", False),
        ("./~/plugins/named", False),
    ],
)
def test_a_plugin_is_preloaded_however_it_is_configured_except_under_the_home_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
    is_preloaded: bool,
) -> None:
    """Only ``~`` depends on the HOME of the process that reads it; every other path names one directory."""
    config_dir = tmp_path / "beside"
    runtime_paths = _runtime(config_dir)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    absolute = _write_plugin(tmp_path, "absolute", worker_preload=True)
    for parent in (config_dir, runtime_paths.storage_root, home):
        _write_plugin(parent / "plugins", "named", worker_preload=True)
    _write_plugin(tmp_path / "packages", "packaged", worker_preload=True)
    (tmp_path / "packages" / "packaged" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "packages"))

    # The entry resolves and opts in, so only how it is configured can keep it out.
    root = plugin_imports._resolve_plugin_root(path, runtime_paths)
    assert plugin_imports._load_plugin_base(root).worker_preload is True
    config = Config(plugins=[absolute, PluginEntryConfig(path=path)])
    assert worker_preload_plugin_paths(config, runtime_paths) == (
        (absolute.path, path) if is_preloaded else (absolute.path,)
    )


def test_relative_plugin_paths_are_resolved_against_the_config_for_preloading_and_calls(
    manager: _SandboxForkserver,
    tmp_path: Path,
) -> None:
    """A relative path is read against the config's directory by the template and by each call it serves."""
    runtime_paths = _runtime(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    for name in ("dotted", "bare"):
        _write_plugin(tmp_path / "plugins", name, worker_preload=True)
        # A different root can hold another plugin under the same relative path.
        _write_plugin(elsewhere / "plugins", name, worker_preload=True)
        (elsewhere / "plugins" / name / "data.txt").write_text("elsewhere", encoding="utf-8")
    plugins = [PluginEntryConfig(path="./plugins/dotted"), PluginEntryConfig(path="plugins/bare")]
    assert worker_preload_plugin_paths(Config(plugins=plugins), runtime_paths) == ("./plugins/dotted", "plugins/bare")
    _serve_preload(runtime_paths, plugins)

    assert _report(runtime_paths, plugins, "dotted")[0] == "v1"
    assert _report(runtime_paths, plugins, "bare")[0] == "v1"
    assert manager.dispatched == [True, True]
    template_pid = manager.spawned[0].process.pid
    for name in ("dotted", "bare"):
        assert [pid for pid, _cwd, _data in _imports(tmp_path / "plugins", name)] == [template_pid]
        assert _imports(elsewhere / "plugins", name) == []


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
    app, worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[opted.path])
    venv_may_start = threading.Event()
    ensure_state = local_worker_module._ensure_local_worker_state

    def slow_ensure_state(*args: object) -> None:
        # A burst of starting workers shares its nodes and storage, so one venv can take longer than the ready timeout.
        venv_may_start.wait(timeout=10)
        ensure_state(*args)

    monkeypatch.setattr(local_worker_module, "_ensure_local_worker_state", slow_ensure_state)

    with TestClient(app) as client:
        health = client.get("/healthz").json()
        assert health["status"] == "ok"
        assert health["setup"] == "preparing"
        assert not (worker_root / "venv").exists()
        # No template preloads plugins into a venv that does not exist yet.
        assert manager.spawned == []

        venv_may_start.set()
        _wait_for_setup(client, "ready")
        assert (worker_root / "venv" / "bin" / "python").exists()
        assert [template.ready for template in manager.spawned] == [True]

        for _ in range(2):
            for name in ("opted", "plain"):
                body = _http_report(client, [opted, plain], name)
                assert body["ok"] is True, body

    assert len(manager.spawned) == 1
    assert manager.dispatched == [True] * 4
    template_pid = manager.spawned[0].process.pid
    workspace = str((worker_root / "workspace").resolve())
    assert [pid for pid, _cwd, _data in _imports(tmp_path, "opted")] == [template_pid]
    plain_imports = _imports(tmp_path, "plain")
    # Each call imports the other plugin itself, after it moved to the request's working directory.
    assert len({pid for pid, _cwd, _data in plain_imports}) == 4
    assert {cwd for _pid, cwd, _data in plain_imports} == {workspace}


def _end_the_setup_budget_once(monkeypatch: pytest.MonkeyPatch, happened: Callable[[], bool]) -> None:
    """Make a worker's setup budget run out as soon as ``happened()``, and not after a time a busy machine outlasts."""
    budget_seconds = 200.0
    monkeypatch.setattr(sandbox_runner_module, "_EAGER_SETUP_BUDGET_SECONDS", budget_seconds)

    @asynccontextmanager
    async def timeout(delay: float | None) -> AsyncIterator[asyncio.Timeout]:
        async with asyncio.timeout(delay) as budget:

            async def end_the_budget() -> None:
                while not happened():  # noqa: ASYNC110 - what happens is in another process, which sets no event.
                    await asyncio.sleep(0.05)
                budget.reschedule(asyncio.get_running_loop().time())

            ending = asyncio.create_task(end_the_budget()) if delay == budget_seconds else None
            try:
                yield budget
            finally:
                if ending is not None:
                    ending.cancel()

    class _AsyncioWithThatTimeout:
        def __getattr__(self, name: str) -> object:
            return timeout if name == "timeout" else getattr(asyncio, name)

    monkeypatch.setattr(sandbox_runner_module, "asyncio", _AsyncioWithThatTimeout())


def test_a_setup_that_outlasts_its_budget_ends_the_preload_and_calls_run(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A preload still running when the setup budget ends is killed with all it started, and calls run without it."""
    hangs = _write_hanging_plugin(tmp_path)
    app, _worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[hangs.path])
    # The budget ends once the plugin's import has started its process, however long this machine took to get there.
    _end_the_setup_budget_once(monkeypatch, lambda: bool(_started_processes(tmp_path)))

    with TestClient(app) as client:
        _wait_for_setup(client, "failed")
        started_at = time.monotonic()

        assert _http_call(client, [], "calculator", "calculate", "add", 1, 2)["ok"] is True
        shell = _http_call(client, [], "shell", "run_shell_command", ["bash", "-c", "printf ok"])
        assert shell["ok"] is True, shell
        assert "ok" in str(shell["result"])

        assert time.monotonic() - started_at < 25
        assert manager.spawned[0].process.poll() is not None
        assert manager.dispatched == [False, False]
        assert _started_processes(tmp_path)
        assert not any(_runs(pid) for pid in _started_processes(tmp_path))
    assert (tmp_path / "tried.log").read_text(encoding="utf-8") == "tried\n"


@pytest.mark.parametrize("worker_copy", ["does-not-opt-in", "missing"])
def test_a_worker_imports_nothing_ahead_unless_its_own_manifests_opt_in(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    worker_copy: str,
) -> None:
    """The primary names the plugins to preload, and the worker's own copies decide before any of them is imported."""
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    plain = _write_plugin(tmp_path, "plain")
    listed = plain.path if worker_copy == "does-not-opt-in" else str(tmp_path / "missing")
    app, _worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[opted.path, listed])
    warnings: list[str] = []
    monkeypatch.setattr(sandbox_forkserver_module.logger, "warning", lambda event, **_fields: warnings.append(event))

    with TestClient(app) as client:
        _wait_for_setup(client, "ready")
        # Not even the plugin that does opt in was imported.
        assert _imports(tmp_path, "opted") == []
        assert _imports(tmp_path, "plain") == []

        for _ in range(2):
            assert _http_report(client, [opted, plain], "opted")["ok"] is True
        assert manager.dispatched == [False, False]

    tried, serving = manager.spawned
    assert tried.process.poll() is not None
    assert (
        len({pid for pid, _cwd, _data in _imports(tmp_path, "opted")} - {tried.process.pid, serving.process.pid}) == 2
    )
    assert warnings.count("sandbox_forkserver_plugin_preload_failed") == 1


def test_a_call_that_arrives_during_the_setup_leaves_the_health_check_answering(
    manager: _SandboxForkserver,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A call waits for the venv the setup is still creating without holding up what else the worker answers."""
    app, _worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[])
    venv_may_start = threading.Event()
    ensure_state = local_worker_module._ensure_local_worker_state

    def slow_ensure_state(*args: object) -> None:
        venv_may_start.wait(timeout=3)
        ensure_state(*args)

    monkeypatch.setattr(local_worker_module, "_ensure_local_worker_state", slow_ensure_state)

    with TestClient(app) as client:
        assert client.get("/healthz").json()["setup"] == "preparing"
        answers: list[dict[str, object]] = []
        call = threading.Thread(
            target=lambda: answers.append(_http_call(client, [], "calculator", "calculate", "add", 1, 2)),
        )
        call.start()
        # Long enough for the call to reach the lock that the setup holds.
        time.sleep(0.5)

        started_at = time.monotonic()
        health = client.get("/healthz").json()
        answered_in = time.monotonic() - started_at
        venv_may_start.set()
        call.join(timeout=60)

    assert health["status"] == "ok"
    assert answered_in < 1.5
    assert answers[0]["ok"] is True, answers


def test_a_worker_that_shuts_down_during_its_setup_ends_the_preload_at_once(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A worker told to stop does not wait for a preload to finish or to run into its time limit."""
    hangs = _write_hanging_plugin(tmp_path)
    app, _worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[hangs.path])
    monkeypatch.setattr(sandbox_runner_module, "_PRELOAD_BUDGET_SECONDS", 20.0)
    warnings: list[str] = []
    monkeypatch.setattr(sandbox_forkserver_module.logger, "warning", lambda event, **_fields: warnings.append(event))

    with TestClient(app) as client:
        deadline = time.monotonic() + 60
        while not _started_processes(tmp_path):
            assert time.monotonic() < deadline, client.get("/healthz").json()
            time.sleep(0.1)
        stopped_at = time.monotonic()

    # Leaving the block stops the worker, which waits for the thread its setup runs in.
    assert time.monotonic() - stopped_at < 10
    assert manager.spawned[0].process.poll() is not None
    assert not any(_runs(pid) for pid in _started_processes(tmp_path))
    # Only the preloading template ever started, and stopping is no failed preload.
    assert len(manager.spawned) == 1
    assert "sandbox_forkserver_plugin_preload_failed" not in warnings


def test_a_failed_eager_setup_leaves_the_worker_healthy_and_its_first_call_reports_the_real_error(
    manager: _SandboxForkserver,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A setup that fails never stops the worker: calls prepare it themselves, as without any setup or preload."""
    plain = _write_plugin(tmp_path, "plain")
    opted = _write_plugin(tmp_path, "opted", worker_preload=True)
    app, worker_root = _dedicated_worker_app(monkeypatch, tmp_path, eager_setup_plugins=[opted.path])
    disk_full = threading.Event()
    disk_full.set()
    ensure_state = local_worker_module._ensure_local_worker_state

    def ensure_state_on_a_full_disk(*args: object) -> None:
        if disk_full.is_set():
            msg = "No space left on device"
            raise OSError(msg)
        ensure_state(*args)

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

        # No template preloads once the setup failed: each call imports the opt-in plugin itself.
        for _ in range(2):
            assert _http_report(client, [opted], "opted")["ok"] is True
        assert manager.dispatched == [False] * 3
        assert len(manager.spawned) == 1
        opted_pids = [pid for pid, _cwd, _data in _imports(tmp_path, "opted")]
        assert len(set(opted_pids)) == 2
        assert manager.spawned[0].process.pid not in opted_pids


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
        assert sandbox_forkserver_module._preloading_forkserver is None
