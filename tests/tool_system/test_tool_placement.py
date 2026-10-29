"""Where each tool runs, pinned as a table over every sandbox setting that decides it.

Placement is the worker security boundary, so the whole decision table is pinned, not samples of it.
Regenerate the golden after an intended change with `.venv/bin/python -m tests.tool_system.test_tool_placement`.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.bootstrap import ensure_tool_registry_loaded
from mindroom.workers.backend import WorkerBackendError

_GOLDEN_PATH = Path(__file__).with_name("tool_placement_golden.txt")
_TOKEN = "placement-test-token"  # noqa: S105

_MODES = (None, "all", "selective", "off", "bogus")
_PROXY_TOOLS = (None, "*", "", "shell")
_PROXY_URLS = (None, "http://sandbox-runner:8765")
_BACKENDS = ("static_runner", "docker", "docker-misconfigured", "bogus")
_FLAGS = (False, True)
# A worker-default tool, a primary-default tool, a room-context tool, a primary-only tool, and a name no tool registers.
_TOOLS = ("shell", "calculator", "matrix_message", "claude_agent", "unregistered_tool")
_WORKER_TOOLS: tuple[tuple[str, ...] | None, ...] = (
    None,
    (),
    ("shell",),
    ("calculator", "matrix_message", "claude_agent"),
)


def _env(
    mode: str | None,
    proxy_tools: str | None,
    proxy_url: str | None,
    backend: str,
    *,
    unsafe: bool,
    runner: bool,
) -> dict[str, str]:
    env = {"MINDROOM_SANDBOX_PROXY_TOKEN": _TOKEN, "MINDROOM_SANDBOX_RUNNER_MODE": "true" if runner else "false"}
    if mode is not None:
        env["MINDROOM_SANDBOX_EXECUTION_MODE"] = mode
    if proxy_tools is not None:
        env["MINDROOM_SANDBOX_PROXY_TOOLS"] = proxy_tools
    if proxy_url is not None:
        env["MINDROOM_SANDBOX_PROXY_URL"] = proxy_url
    if backend == "bogus":
        env["MINDROOM_WORKER_BACKEND"] = "bogus"
    elif backend != "static_runner":
        env["MINDROOM_WORKER_BACKEND"] = "docker"
    if backend == "docker":
        env["MINDROOM_DOCKER_WORKER_IMAGE"] = "ghcr.io/mindroom-ai/mindroom:latest"
    if unsafe:
        env["MINDROOM_UNSAFE_ALLOW_LOCAL_EXECUTION_TOOLS"] = "true"
    return env


def _placement(runtime_paths: RuntimePaths, tool_name: str, worker_tools: tuple[str, ...] | None) -> str:
    """Return 1 for the worker, 0 for the primary, and E when the settings are rejected."""
    try:
        routed = sandbox_proxy.tool_placement(runtime_paths).runs_in_worker(tool_name, worker_tools)
    except (ValueError, WorkerBackendError):
        return "E"
    return "1" if routed else "0"


def _column_labels() -> list[str]:
    labels = []
    for worker_tools in _WORKER_TOOLS:
        listed = "unset" if worker_tools is None else "[" + ",".join(worker_tools) + "]"
        labels.extend(f"{tool}@{listed}" for tool in _TOOLS)
    return labels


def placement_table(config_dir: Path) -> list[str]:
    """Return one line per sandbox setting combination: its settings, then one 0/1 per tool and worker_tools."""
    ensure_tool_registry_loaded(resolve_runtime_paths(config_path=config_dir / "config.yaml", process_env={}))
    lines = ["# columns: " + " ".join(_column_labels())]
    for mode, proxy_tools, proxy_url, backend, unsafe, runner in itertools.product(
        _MODES,
        _PROXY_TOOLS,
        _PROXY_URLS,
        _BACKENDS,
        _FLAGS,
        _FLAGS,
    ):
        runtime_paths = resolve_runtime_paths(
            config_path=config_dir / "config.yaml",
            storage_path=config_dir / "storage",
            process_env=_env(mode, proxy_tools, proxy_url, backend, unsafe=unsafe, runner=runner),
        )
        bits = "".join(
            _placement(runtime_paths, tool, worker_tools) for worker_tools in _WORKER_TOOLS for tool in _TOOLS
        )
        settings = (
            f"mode={mode} proxy_tools={proxy_tools!r} url={int(proxy_url is not None)} "
            f"backend={backend} unsafe={int(unsafe)} runner={int(runner)}"
        )
        lines.append(f"{settings}: {bits}")
    return lines


def test_every_sandbox_setting_places_each_tool_as_the_golden_table_says(tmp_path: Path) -> None:
    """A changed placement shows as a changed line; an intended change regenerates the golden."""
    expected = _GOLDEN_PATH.read_text().splitlines()
    actual = placement_table(tmp_path)
    changed = [f"- {old}\n+ {new}" for old, new in zip(expected, actual, strict=True) if old != new]
    assert not changed, "Tool placement changed:\n" + "\n".join(changed)


@pytest.mark.parametrize("tool_name", ["matrix_message", "claude_agent"])
def test_primary_only_and_room_tools_never_run_in_a_worker_even_when_listed(tmp_path: Path, tool_name: str) -> None:
    """The two kinds of tools a worker cannot run stay in the primary whatever the settings say."""
    lines = placement_table(tmp_path)[1:]
    labels = _column_labels()
    columns = [index for index, label in enumerate(labels) if label.startswith(f"{tool_name}@")]
    assert all(line.split(": ")[1][index] != "1" for line in lines for index in columns)


def test_a_misconfigured_dedicated_backend_still_routes_so_the_call_fails(tmp_path: Path) -> None:
    """Without a usable Docker config, worker-default tools stay routed and fail at call time instead of running here."""
    lines = placement_table(tmp_path)[1:]
    shell_unset = _column_labels().index("shell@unset")
    # Settings that ask for the worker by default: no mode, or mode all; selective and off pick tools explicitly.
    misconfigured = [
        line
        for line in lines
        if "backend=docker-misconfigured" in line
        and "runner=0" in line
        and (line.startswith(("mode=None", "mode=all")))
    ]
    assert misconfigured
    assert all(line.split(": ")[1][shell_unset] == "1" for line in misconfigured)


def test_the_unsafe_local_flag_only_frees_worker_default_tools_on_a_static_runner_without_url(tmp_path: Path) -> None:
    """The unsafe flag never moves a tool off a dedicated backend or a configured proxy URL."""
    lines = {line.split(": ")[0]: line.split(": ")[1] for line in placement_table(tmp_path)[1:]}
    for settings, bits in lines.items():
        if "unsafe=1" not in settings:
            continue
        safe = lines[settings.replace("unsafe=1", "unsafe=0")]
        if "backend=static_runner" not in settings or "url=1" in settings:
            assert bits == safe, settings
        else:
            labels = _column_labels()
            for index, (unsafe_bit, safe_bit) in enumerate(zip(bits, safe, strict=True)):
                if unsafe_bit != safe_bit:
                    assert labels[index].startswith("shell@"), (settings, labels[index])
                    assert unsafe_bit == "0"


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as config_dir:
        _GOLDEN_PATH.write_text("\n".join(placement_table(Path(config_dir))) + "\n")
    sys.stdout.write(f"wrote {_GOLDEN_PATH}\n")
