"""Tests for native Wave 6 Batch 4 container & sandbox toolkits (docker, daytona, plus Batch 4 grants/migration)."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest
from daytona_api_client import Configuration

from mindroom.config.main import Config, ConfigRuntimeValidationError
from mindroom.constants import resolve_runtime_paths
from mindroom.tool_approval_grants import grant_operation
from mindroom.tool_system.registry import builtin_tool_registry

if TYPE_CHECKING:
    from pathlib import Path


class _FakeDockerContainer:
    def __init__(self, cid: str, name: str, image: object = None) -> None:
        self.id = cid
        self.name = name
        self.image = image
        self.status = "running"
        self.attrs = {"Id": cid, "Created": "2026-01-01T00:00:00Z"}
        self.ports = {"80/tcp": [{"HostPort": "8080"}]}
        self.labels = {"env": "test"}
        self.events: list[str] = []

    def logs(self, *, tail: int, stream: bool) -> bytes:
        assert not stream
        return f"last-{tail}-lines".encode()

    def start(self) -> None:
        self.events.append("start")

    def stop(self, *, timeout: int) -> None:
        self.events.append(f"stop:{timeout}")

    def remove(self, *, force: bool, v: bool) -> None:
        self.events.append(f"remove:{force}:{v}")

    def exec_run(self, command: str) -> tuple[int, bytes]:
        return (0, f"ran:{command}".encode())


def test_docker_inspect_container_resource_and_include_tools() -> None:
    """DockerTools separates inspect/container/resource cleanly, handles dangling images and detach=False, and filters include_tools."""
    docker_cls = cast("Any", builtin_tool_registry().factory("docker")())

    # include_tools filtering validates names and exposes only requested tools.
    inspect_only = docker_cls(include_tools=["docker_inspect"])
    assert set(inspect_only.functions) == {"docker_inspect"}
    with pytest.raises(ValueError, match="not present in the toolkit"):
        docker_cls(include_tools=["list_containers"])

    c1 = _FakeDockerContainer("cid-1", "web", image=None)  # dangling/deleted image
    net_events: list[str] = []
    fake_net = SimpleNamespace(
        id="net-1",
        name="app-net",
        attrs={"Driver": "bridge", "Scope": "local", "Containers": {"cid-1": {}}},
        connect=lambda cid: net_events.append(f"connect:{cid}"),
        disconnect=lambda cid: net_events.append(f"disconnect:{cid}"),
        remove=lambda: net_events.append("remove"),
    )
    fake_client = SimpleNamespace(
        containers=SimpleNamespace(
            list=lambda **_kw: [c1],
            get=lambda _cid: c1,
            run=lambda **kw: b"inline-output" if not kw["detach"] else c1,
        ),
        images=SimpleNamespace(
            list=lambda: [SimpleNamespace(id="img-1", tags=["alpine:latest"], attrs={"Size": 5}, labels={})],
            get=lambda _i: SimpleNamespace(attrs={"Id": "img-1"}, tag=lambda repo, tag=None: None),  # noqa: ARG005
            build=lambda **_kw: (SimpleNamespace(id="built-1"), []),
            remove=lambda _i, *, force=False: None,  # noqa: ARG005
        ),
        volumes=SimpleNamespace(
            list=lambda: [SimpleNamespace(name="v1", attrs={"Driver": "local"})],
            create=lambda **_kw: SimpleNamespace(name="v1"),
            get=lambda _v: SimpleNamespace(attrs={"Name": "v1"}, remove=lambda *, force=False: None),  # noqa: ARG005
        ),
        networks=SimpleNamespace(
            list=lambda: [fake_net],
            create=lambda **_kw: fake_net,
            get=lambda _n: fake_net,
        ),
        api=SimpleNamespace(pull=lambda *_a, **_kw: [{"status": "done"}]),
    )

    tool = docker_cls()
    tool._client = fake_client

    listed = json.loads(tool.docker_inspect("list_containers"))
    assert listed[0]["image"] is None
    assert tool.docker_inspect("get_container_logs", target="web", tail=25) == "last-25-lines"
    assert json.loads(tool.docker_inspect("list_images"))[0]["id"] == "img-1"

    assert tool.docker_container("run", image="alpine", detach=False) == "inline-output"
    assert "cid-1" in tool.docker_container("run", image="alpine", ports={"80/tcp": 8080})
    assert tool.docker_container("exec", container_id="web", command="whoami") == "ran:whoami"
    assert "stopped" in tool.docker_container("stop", container_id="web", timeout=5)
    assert "removed" in tool.docker_container("remove", container_id="web", force=True, remove_volumes=True)
    assert c1.events == ["stop:5", "remove:True:True"]

    assert "pulled successfully" in tool.docker_resource("pull_image", name="alpine", tag="3.20")
    assert "built-1" in tool.docker_resource("build_image", path=".", tag="app:dev")
    assert "connected" in tool.docker_resource("connect_network", name="app-net", container_id="cid-1")
    assert "disconnected" in tool.docker_resource("disconnect_network", name="app-net", container_id="cid-1")
    assert net_events == ["connect:cid-1", "disconnect:cid-1"]


def test_daytona_scoped_credentials_tls_isolation_and_safe_file_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """DaytonaTools fails closed without API key, isolates verify_ssl=False per instance, and preserves file bytes."""
    monkeypatch.setenv("DAYTONA_API_KEY", "ambient-daytona-key")
    daytona_cls = cast("Any", builtin_tool_registry().factory("daytona")())

    with pytest.raises(ValueError, match="Daytona API key is required"):
        daytona_cls()

    # Disabling TLS on one Daytona instance never mutates Configuration.__init__ globally.
    original_init = Configuration.__init__
    fake_api_client = SimpleNamespace(configuration=Configuration(host="https://daytona.invalid"), rest_client=None)
    fake_sdk_client = SimpleNamespace(_api_client=fake_api_client, _http_client=None)
    daytona_cls._disable_client_tls_verification(fake_sdk_client)
    assert fake_api_client.configuration.verify_ssl is False
    assert Configuration.__init__ is original_init
    assert Configuration().verify_ssl is True

    # Test code, shell, cwd persistence, and safe base64 file creation (quotes + EOF preserved).
    exec_calls: list[tuple[str, str | None]] = []
    code_calls: list[str] = []

    def fake_exec(cmd: str, cwd: str | None = None) -> SimpleNamespace:
        exec_calls.append((cmd, cwd))
        if cmd.startswith("test -d "):
            return SimpleNamespace(exit_code=0, result="exists")
        if cmd.startswith("cat "):
            return SimpleNamespace(exit_code=0, result="hello 'world'\nEOF\n")
        return SimpleNamespace(exit_code=0, result="ok")

    def fake_code_run(code: str) -> SimpleNamespace:
        code_calls.append(code)
        return SimpleNamespace(result="42")

    fake_sandbox = SimpleNamespace(
        id="sbx-dt-1",
        state="started",
        process=SimpleNamespace(exec=fake_exec, code_run=fake_code_run),
    )
    tool = daytona_cls(api_key="dt-scoped", sandbox_id="sbx-dt-1")
    tool._client = SimpleNamespace(get=lambda _sid: fake_sandbox)

    assert tool.daytona_run("code", code="x = true\nprint(x)") == "42"
    assert code_calls == ["x = True\nprint(x)"]

    assert tool.daytona_files("change_directory", path="project") == "Changed directory to: /home/daytona/project"
    payload = "hello 'world'\nEOF\n"
    assert tool.daytona_files("create_file", path="sub/a.txt", content=payload) == (
        "File created/updated: /home/daytona/project/sub/a.txt"
    )
    write_cmd = exec_calls[-1][0]
    assert base64.b64encode(payload.encode("utf-8")).decode("ascii") in write_cmd
    assert tool.daytona_inspect("read_file", path="sub/a.txt") == payload
    assert tool.daytona_run("shell", command="ls") == "ok"
    assert exec_calls[-1][1] == "/home/daytona/project"


def test_wave6_batch4_action_scoped_grants_and_fail_closed_migration(tmp_path: Path) -> None:
    """Batch 4 tools bind timed grants per action and fail closed on retired approval rules."""
    registry = builtin_tool_registry()
    config = Config.model_validate(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "agents": {"ops": {"display_name": "Ops", "tools": ["docker", "e2b", "daytona"]}},
        },
    )

    for toolkit, fn, a1, a2 in (
        ("docker", "docker_inspect", "list_containers", "get_container_logs"),
        ("docker", "docker_container", "stop", "remove"),
        ("docker", "docker_resource", "pull_image", "remove_image"),
        ("e2b", "e2b_run", "python", "command"),
        ("e2b", "e2b_files", "upload", "download"),
        ("e2b", "e2b_lifecycle", "set_timeout", "shutdown"),
        ("daytona", "daytona_run", "code", "shell"),
        ("daytona", "daytona_files", "create_file", "delete_file"),
    ):
        op1 = grant_operation(config, fn, {"action": a1}, tools=registry, toolkit_name=toolkit)
        op2 = grant_operation(config, fn, {"action": a2}, tools=registry, toolkit_name=toolkit)
        assert op1 is not None
        assert op2 is not None
        assert op1 != op2

    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    for toolkit, retired in (
        ("docker", "exec_in_container"),
        ("docker", "list_containers"),
        ("e2b", "run_python_code"),
        ("e2b", "upload_file"),
        ("daytona", "run_code"),
        ("daytona", "delete_file"),
    ):
        with pytest.raises(ConfigRuntimeValidationError, match=retired):
            Config.validate_with_runtime(
                {
                    "models": {"default": {"provider": "openai", "id": "test-model"}},
                    "agents": {"ops": {"display_name": "Ops", "tools": [toolkit]}},
                    "tool_approval": {
                        "default": "auto_approve",
                        "rules": [{"match": retired, "action": "require_approval"}],
                    },
                },
                runtime_paths,
            )
