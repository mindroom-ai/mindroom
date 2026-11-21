"""Tests for the tailnet SSH link service: what the gateway is told, what its reports change, and a connect."""

from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Self

import pytest
from aiohttp import web
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.room_agent_access import room_execution_identity
from mindroom.runtime_resolution import resolve_agent_runtime
from mindroom.tailnet_ssh import links
from mindroom.tailnet_ssh.links import (
    GatewayStatus,
    StatusReport,
    _ConnectRefused,
    active_worker_keys,
    connect,
    desired_links,
    record_status,
)
from mindroom.tailnet_ssh.store import Link, read_links, update_links
from mindroom.terminal.resolution import agent_terminals
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.sandbox_proxy import WorkerCall
from mindroom.tool_system.tool_environment import ToolEnvironment, collected_tool_environment
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    build_requester_execution_identity,
    resolve_worker_key,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerSshEndpoint
from mindroom.workers.protocol import ExecuteRequest
from tests.access_schema_support import membership_index
from tests.conftest import bind_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
NOW = 1_800_000_000.0
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPVHVwHKbi4VF0jfKNiYpfwAeEgeTqB6B8NVRZvzjnDS alice@laptop"

SECOND_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAXZ8X1OjKAkv+L/ahWQr7KohQAWKyJ/TFSXMh8y3Ksf"

# The suite's autouse bypass would admit everyone; a link's removal is about who is admitted.
pytestmark = pytest.mark.usefixtures("enforce_turn_authorization")

_DATA: dict[str, Any] = {
    "models": {"default": {"provider": "openai", "id": "m"}},
    "rooms": {"lobby": {}},
    "agents": {
        "mine": {
            "display_name": "Mine",
            "tools": ["shell"],
            "worker_scope": "user_agent",
            "access": {"users": [ALICE], "members_of_rooms": []},
            "tailnet_ssh": True,
        },
        "mind": {
            "display_name": "Mind",
            "tools": ["shell"],
            "private": {"per": "user_agent"},
            "tailnet_ssh": True,
            "access": {"users": ["@*:example.org"]},
        },
        "other": {"display_name": "Other", "worker_scope": "user_agent"},
    },
    "tailnet_ssh": {"logins": {ALICE: "alice@github"}, "authorized_keys": {ALICE: [PUBLIC_KEY]}},
}


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    """A primary with the Docker worker backend."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "mindroom_data",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_WORKER_BACKEND": "docker",
        },
    )


@pytest.fixture
def config(paths: RuntimePaths) -> Config:
    """Alice's own agent, a private agent everyone has an instance of, and an agent without tailnet SSH."""
    return bind_runtime_paths(Config.validate_with_runtime(deepcopy(_DATA), paths), paths)


def _key(requester: str, agent: str, scope: str = "user_agent") -> str:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent,
        requester_id=requester,
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
    )
    key = resolve_worker_key(scope, identity)  # type: ignore[arg-type]
    assert key is not None
    return key


def _link(paths: RuntimePaths, agent: str = "mine", requester: str = ALICE) -> Link:
    with update_links(paths) as book:
        return book.ensure(_key(requester, agent))


def _handle(worker_key: str, ssh_dir: Path | None, *, status: str = "ready") -> WorkerHandle:
    return WorkerHandle(
        worker_id=f"worker-{worker_key}",
        worker_key=worker_key,
        endpoint="http://10.0.0.5:8766/api/sandbox-runner/execute",
        auth_token="runner-token",  # noqa: S106
        status=status,  # type: ignore[arg-type]
        backend_name="docker",
        last_used_at=NOW,
        created_at=NOW,
        ssh_endpoint=None if ssh_dir is None else WorkerSshEndpoint("10.0.0.5", 2222, ssh_dir),
    )


class _Manager:
    """A worker manager, and its own lease, that records what it is asked."""

    def __init__(self, calls: list[str], *handles: WorkerHandle) -> None:
        self.calls = calls
        self.handles = list(handles)
        self.specs: list[WorkerSpec] = []
        self.error: WorkerBackendError | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def touch_worker(self, key: str) -> WorkerHandle:
        self.calls.append("touch_worker")
        return next(handle for handle in self.handles if handle.worker_key == key)

    def ensure_worker(self, spec: WorkerSpec) -> WorkerHandle:
        self.calls.append("ensure_worker")
        self.specs.append(spec)
        if self.error is not None:
            raise self.error
        return next(handle for handle in self.handles if handle.worker_key == spec.worker_key)


def _manager(monkeypatch: pytest.MonkeyPatch, *handles: WorkerHandle) -> _Manager:
    manager = _Manager([], *handles)
    monkeypatch.setattr(links, "lease_configured_primary_worker_manager", lambda _paths: manager)
    return manager


def _recorded[**P, T](calls: list[str], name: str, function: Callable[P, T]) -> Callable[P, T]:
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
        calls.append(name)
        return function(*args, **kwargs)

    return wrapper


def _worker_call(worker_key: str, api_root: str = "http://10.0.0.5:8766/api/sandbox-runner") -> WorkerCall:
    request = ExecuteRequest(protocol=1, worker_key=worker_key, tool_name="shell", function_name="run_shell_command")
    return WorkerCall(
        request=request,
        api_root=api_root,
        headers={"x-mindroom-sandbox-token": "runner-token"},
        handle=None,
    )


def _wire_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    link: Link,
    *,
    ssh: bool = True,
    call: WorkerCall | None = None,
) -> _Manager:
    """Stand in for the worker of ``link``, its agent's shell, and its runner, recording the order of their use."""
    manager = _manager(monkeypatch, _handle(link.worker_key, tmp_path / "ssh" if ssh else None))
    calls = manager.calls

    async def start_sshd(_call: WorkerCall) -> None:
        calls.append("ssh_start")

    monkeypatch.setattr(
        links,
        "is_sender_allowed_for_responder",
        _recorded(calls, "authorization", links.is_sender_allowed_for_responder),
    )
    monkeypatch.setattr(links, "write_projection", _recorded(calls, "projection", links.write_projection))
    monkeypatch.setattr(links, "agent_terminals", lambda *_args, **_kwargs: SimpleNamespace(stand_ins=object()))
    monkeypatch.setattr(
        links,
        "worker_call",
        lambda *_args, **_kwargs: replace(call or _worker_call(link.worker_key), handle=manager.handles[0]),
    )
    monkeypatch.setattr(links, "_start_sshd", start_sshd)
    return manager


async def _connect(
    config: Config,
    paths: RuntimePaths,
    link_id: str,
    peer_login: str = "alice@github",
) -> tuple[str, int]:
    return await connect(
        lambda: (config, paths),
        AgentReplyMembershipIndex(),
        link_id,
        peer_login,
        "alice-laptop",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent", "private_names"), [("mind", frozenset({"mind"})), ("mine", frozenset())])
async def test_connect_order(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
    private_names: frozenset[str],
) -> None:
    """A connect checks the link, wakes its worker, writes sshd's files, starts sshd, and only then names the port."""
    link = _link(paths, agent)
    config.agents["mine" if agent == "mind" else "mind"].tailnet_ssh = False
    manager = _wire_connect(monkeypatch, tmp_path, link)

    assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)

    assert manager.calls == ["authorization", "ensure_worker", "authorization", "projection", "ssh_start"]
    assert manager.specs == [WorkerSpec(link.worker_key, private_agent_names=private_names)]
    assert (tmp_path / "ssh" / "authorized_keys").read_text() == f"restrict,pty {PUBLIC_KEY}\n"
    assert (tmp_path / "ssh" / "ssh_host_ed25519_key").read_text() == link.host_key


@pytest.mark.asyncio
async def test_first_private_connect_creates_workspace_before_worker_mounts(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold worker must see the private workspace when it plans its mounts."""
    config.agents["mine"].tailnet_ssh = False
    link = _link(paths, "mind")
    identity = build_requester_execution_identity(agent_name="mind", runtime_paths=paths, requester_id=ALICE)
    workspace = resolve_agent_runtime("mind", config, paths, identity, create=False).workspace.root
    assert not workspace.exists()
    manager = _wire_connect(monkeypatch, tmp_path, link)
    monkeypatch.setattr(links, "agent_terminals", agent_terminals)
    ensure_worker = manager.ensure_worker

    def ensure_with_workspace(spec: WorkerSpec) -> WorkerHandle:
        assert workspace.is_dir()
        return ensure_worker(spec)

    monkeypatch.setattr(manager, "ensure_worker", ensure_with_workspace)
    assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [True, False])
async def test_connect_refuses_replacement_between_wake_and_runner_lookup(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    marked: bool,
) -> None:
    """Refuse a changed handle before projecting keys or starting a runner."""
    link = _link(paths)
    config.agents["mine"].tools = ["shell"]
    config.agents["mine"].worker_tools = ["shell"]
    handle = _handle(link.worker_key, tmp_path / "ssh")
    assert handle.ssh_endpoint is not None
    replacement = replace(
        handle,
        endpoint=handle.endpoint.replace(":8766/", ":18766/"),
        ssh_endpoint=replace(handle.ssh_endpoint, port=12222) if marked else None,
    )
    manager = _manager(monkeypatch, handle)

    def touch_worker(key: str) -> WorkerHandle:
        assert key == handle.worker_key
        manager.calls.append("touch_worker")
        return replacement

    async def start_sshd(_call: WorkerCall) -> None:
        manager.calls.append("ssh_start")

    monkeypatch.setattr(manager, "touch_worker", touch_worker, raising=False)
    monkeypatch.setattr(sandbox_proxy, "lease_worker_manager", lambda *_: manager)
    monkeypatch.setattr(links, "_start_sshd", start_sshd)

    with pytest.raises(_ConnectRefused, match="worker changed") as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 409
    assert manager.calls == ["ensure_worker", "touch_worker"]
    assert not (tmp_path / "ssh" / "authorized_keys").exists()


@pytest.mark.asyncio
async def test_list_can_finish_while_connect_touches_worker(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow worker lookup must not hold up link reconciliation."""
    config.agents["mind"].tailnet_ssh = False
    link = _link(paths)
    handle = _handle(link.worker_key, tmp_path / "ssh")
    manager = _manager(monkeypatch, handle)
    touching = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def slow_touch(key: str) -> WorkerHandle:
        assert key == link.worker_key
        loop.call_soon_threadsafe(touching.set)
        assert release.wait(10)
        return handle

    async def start_sshd(_call: WorkerCall) -> None:
        pass

    monkeypatch.setattr(manager, "touch_worker", slow_touch)
    monkeypatch.setattr(sandbox_proxy, "lease_worker_manager", lambda *_: manager)
    monkeypatch.setattr(links, "_start_sshd", start_sshd)
    serving = asyncio.create_task(_connect(config, paths, link.id))
    try:
        async with asyncio.timeout(2):
            await touching.wait()
            [row] = await asyncio.to_thread(desired_links, config, paths, AgentReplyMembershipIndex())
            assert row["id"] == link.id
    finally:
        release.set()
        assert await serving == ("10.0.0.5", 2222)


@pytest.mark.asyncio
async def test_connect_without_ssh_endpoint_calls_no_runner(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker launched without the SSH mark is never asked to start sshd."""
    link = _link(paths)
    config.agents["mind"].tailnet_ssh = False
    manager = _wire_connect(monkeypatch, tmp_path, link, ssh=False)

    with pytest.raises(_ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 409
    assert refused.value.detail == "This worker has no marked SSH endpoint."
    assert manager.calls == ["authorization", "ensure_worker"]


@pytest.mark.asyncio
async def test_connect_refuses_while_the_worker_is_not_ready(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that fails, or a worker that did not come up, is a refusal the next attempt may not meet."""
    link = _link(paths)
    manager = _wire_connect(monkeypatch, tmp_path, link)

    manager.handles = [_handle(link.worker_key, tmp_path / "ssh", status="failed")]
    with pytest.raises(_ConnectRefused) as not_ready:
        await _connect(config, paths, link.id)
    manager.error = WorkerBackendError("image pull failed for registry.example/secret")
    with pytest.raises(_ConnectRefused) as failed:
        await _connect(config, paths, link.id)

    assert (not_ready.value.status_code, failed.value.status_code) == (503, 503)
    assert "secret" not in failed.value.detail
    assert "projection" not in manager.calls
    assert "ssh_start" not in manager.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("shell", ["no_terminals", "no_call", "other_worker"])
async def test_connect_refuses_without_the_agents_own_shell(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shell: str,
) -> None:
    """The session is the agent's shell in the link's worker; without that shell there is no session."""
    link = _link(paths)
    manager = _wire_connect(monkeypatch, tmp_path, link, call=_worker_call(_key(BOB, "mind")))
    if shell == "no_terminals":
        monkeypatch.setattr(links, "agent_terminals", lambda *_args, **_kwargs: None)
    elif shell == "no_call":
        monkeypatch.setattr(links, "worker_call", lambda *_args, **_kwargs: None)

    with pytest.raises(_ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 409
    assert (refused.value.detail == "This agent has no shell tool.") == (shell != "other_worker")
    assert "ssh_start" not in manager.calls
    if shell == "no_terminals":
        assert "ensure_worker" not in manager.calls


@pytest.mark.asyncio
async def test_connect_refuses_when_sshds_files_cannot_be_written(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready worker whose SSH folder the primary cannot write is a refusal, and its runner is not asked."""
    link = _link(paths)
    config.agents["mind"].tailnet_ssh = False
    manager = _wire_connect(monkeypatch, tmp_path, link)
    (tmp_path / "blocked").write_text("")
    manager.handles = [_handle(link.worker_key, tmp_path / "blocked" / "ssh")]

    with capture_logs() as logs, pytest.raises(_ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 503
    assert str(tmp_path) not in refused.value.detail
    assert manager.calls == ["authorization", "ensure_worker", "authorization", "projection"]
    assert [(log["event"], log["link_id"]) for log in logs] == [
        ("tailnet_ssh_projection_not_written", link.id),
        ("tailnet_ssh_connect_refused", link.id),
    ]


@pytest.mark.asyncio
async def test_a_connect_waits_for_its_worker_on_a_pool_of_its_own(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that takes minutes to start holds a connect thread, never one of the pool the primary shares."""
    link = _link(paths)
    manager = _wire_connect(monkeypatch, tmp_path, link)
    threads: list[str] = []
    ensure_worker = manager.ensure_worker

    def recorded(spec: WorkerSpec) -> WorkerHandle:
        threads.append(threading.current_thread().name)
        return ensure_worker(spec)

    monkeypatch.setattr(manager, "ensure_worker", recorded)

    await _connect(config, paths, link.id)

    assert [name.startswith("mindroom-tailnet-ssh-connect") for name in threads] == [True]


@pytest.mark.parametrize("status", [200, 204, 400, 503])
@pytest.mark.asyncio
async def test_runner_receives_the_shell_call_and_returns_generic_refusal(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """Pass the shell launch to the authenticated runner, not its untrusted response to the peer."""
    received: list[tuple[str | None, object]] = []

    async def start(request: web.Request) -> web.Response:
        received.append((request.headers.get("x-mindroom-sandbox-token"), await request.json()))
        return web.json_response({"detail": "internal runner detail"}, status=status)

    app = web.Application()
    app.router.add_post("/api/sandbox-runner/ssh/start", start)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        link = _link(paths)
        call = _worker_call(link.worker_key, f"http://127.0.0.1:{port}/api/sandbox-runner")
        start_sshd = links._start_sshd
        _wire_connect(monkeypatch, tmp_path, link, call=call)
        monkeypatch.setattr(links, "_start_sshd", start_sshd)

        if status in {200, 204}:
            assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)
        else:
            with pytest.raises(_ConnectRefused) as refused:
                await _connect(config, paths, link.id)
            assert (refused.value.status_code, refused.value.detail) == (503, "The worker's SSH server did not start.")
    finally:
        await runner.cleanup()

    assert received == [("runner-token", {"request": call.request.model_dump(mode="json")})]


@pytest.mark.asyncio
async def test_an_unreachable_runner_refuses_the_connect(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that does not answer is a refusal that names the kind of failure, not the worker's address."""
    link = _link(paths)
    start_sshd = links._start_sshd
    _wire_connect(monkeypatch, tmp_path, link, call=_worker_call(link.worker_key, "http://127.0.0.1:1/api"))
    monkeypatch.setattr(links, "_start_sshd", start_sshd)

    with pytest.raises(_ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 503
    assert "127.0.0.1" not in refused.value.detail


@pytest.mark.parametrize("decision", ["allowed", "denied", "pending", "current_room_only"])
@pytest.mark.asyncio
async def test_list_and_connect_use_roomless_shell_authorization(
    config: Config,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    """Use the same responder predicate, including resolved membership and fail-closed pending grants."""
    config.agents["mind"].tailnet_ssh = False
    link = _link(paths)
    access = config.agents["mine"].access
    assert access is not None
    access.users = []
    access.members_of_rooms = [] if decision == "current_room_only" else ["lobby"]
    if decision == "current_room_only":
        access.current_room_members = True
    memberships = (
        AgentReplyMembershipIndex()
        if decision == "pending"
        else await membership_index(config, {"lobby": {ALICE} if decision != "denied" else {BOB}})
    )
    args = (ALICE, "mine", None, config, paths, memberships)
    if decision == "pending":
        with pytest.raises(ReplyMembershipPendingError):
            is_sender_allowed_for_responder(*args, require_resolved_membership=True)
    else:
        assert is_sender_allowed_for_responder(*args, require_resolved_membership=True) is (decision == "allowed")
    assert bool(desired_links(config, paths, memberships)) is (decision == "allowed")
    assert read_links(paths).by_id(link.id) == link
    if decision != "allowed":
        manager = _manager(monkeypatch)
        with pytest.raises(_ConnectRefused, match="not authorized"):
            await connect(lambda: (config, paths), memberships, link.id, "alice@github", "laptop")
        assert manager.calls == []


@pytest.mark.parametrize("scope", ["shared", "user", "user_agent", None])
@pytest.mark.asyncio
async def test_connect_resolves_the_exact_shell_worker(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str | None,
) -> None:
    """Listing never consults inventory, and wake and sshd start use the ordinary shell's key."""
    config.agents["mind"].tailnet_ssh = False
    config.agents["mine"].worker_scope = scope  # type: ignore[assignment]
    identity = build_requester_execution_identity(agent_name="mine", runtime_paths=paths, requester_id=ALICE)
    terminals = agent_terminals("mine", config, paths, identity, create=True)
    assert terminals is not None
    worker_key = "v1:default:unscoped:mine" if scope is None else _key(ALICE, "mine", scope)
    manager = _manager(monkeypatch, _handle(worker_key, tmp_path / "ssh"))
    monkeypatch.setattr(sandbox_proxy, "lease_worker_manager", lambda *_: manager)
    [row] = desired_links(config, paths, AgentReplyMembershipIndex())
    [link] = read_links(paths).links
    assert row == {"id": link.id, "hostname": link.hostname, "allowed_logins": ["alice@github"]}
    assert manager.calls == []
    starts: list[WorkerCall] = []

    async def start_sshd(call: WorkerCall) -> None:
        starts.append(call)

    monkeypatch.setattr(links, "_start_sshd", start_sshd)
    with collected_tool_environment(ToolEnvironment(values={"REQUEST_ONLY": "do-not-export"})):
        shell_call = sandbox_proxy.worker_call(terminals.stand_ins, "run_shell_command", start=False)
        assert shell_call is not None
        assert shell_call.request.execution_env["REQUEST_ONLY"] == "do-not-export"
        assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)
    [ssh_call] = starts
    assert (
        manager.specs[0].worker_key == ssh_call.request.worker_key == shell_call.request.worker_key == link.worker_key
    )
    assert ssh_call.request.execution_env == {}
    assert ssh_call.request.environment.set_names == []


@pytest.mark.parametrize("scope", ["user", "user_agent"])
@pytest.mark.asyncio
async def test_alias_and_canonical_login_share_the_shell_worker(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    """An alias uses the turn's execution identity but keeps its configured login and public key."""
    alias = "@bridge_alice:example.org"
    config.authorization.aliases = {ALICE: [alias]}
    config.agents["mind"].tailnet_ssh = False
    config.agents["mine"].worker_scope = scope  # type: ignore[assignment]
    config.tailnet_ssh.logins[alias] = "alice-bridge@github"
    config.tailnet_ssh.authorized_keys[alias] = [SECOND_KEY]
    identity = room_execution_identity("mine", alias, "!lobby:example.org", config, paths)
    terminals = agent_terminals("mine", config, paths, identity, create=True)
    assert terminals is not None
    worker_key = _key(ALICE, "mine", scope)
    manager = _manager(monkeypatch, _handle(worker_key, tmp_path / "ssh"))
    monkeypatch.setattr(sandbox_proxy, "lease_worker_manager", lambda *_: manager)
    starts: list[WorkerCall] = []

    async def start_sshd(call: WorkerCall) -> None:
        starts.append(call)

    monkeypatch.setattr(links, "_start_sshd", start_sshd)
    [row] = desired_links(config, paths, AgentReplyMembershipIndex())
    [link] = read_links(paths).links
    assert row["allowed_logins"] == ["alice-bridge@github", "alice@github"]
    assert row["id"] == link.id
    shell_call = sandbox_proxy.worker_call(terminals.stand_ins, "run_shell_command", start=False)
    assert shell_call is not None
    assert await _connect(config, paths, link.id, "alice-bridge@github") == ("10.0.0.5", 2222)
    [ssh_call] = starts
    assert (
        manager.specs[0].worker_key == ssh_call.request.worker_key == shell_call.request.worker_key == link.worker_key
    )
    assert (tmp_path / "ssh" / "authorized_keys").read_text().splitlines() == [
        f"restrict,pty {PUBLIC_KEY}",
        f"restrict,pty {SECOND_KEY}",
    ]


@pytest.mark.asyncio
async def test_shared_worker_admits_multiple_logins_and_reprojects_on_connect(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale gateway admissions cannot wake the worker and omission does not clear its last projection."""
    config.agents["mind"].tailnet_ssh = False
    config.agents["mine"].worker_scope = "shared"
    access = config.agents["mine"].access
    assert access is not None
    access.users = [ALICE, BOB]
    config.tailnet_ssh.logins[BOB] = "bob@github"
    config.tailnet_ssh.authorized_keys[BOB] = [SECOND_KEY]
    memberships = AgentReplyMembershipIndex()
    [row] = desired_links(config, paths, memberships)
    assert row["allowed_logins"] == ["alice@github", "bob@github"]
    [link] = read_links(paths).links
    manager = _wire_connect(monkeypatch, tmp_path, link)
    projected = tmp_path / "ssh" / "authorized_keys"
    for login in ("alice@github", "bob@github"):
        assert await _connect(config, paths, link.id, login) == ("10.0.0.5", 2222)
        assert projected.read_text().splitlines() == [f"restrict,pty {PUBLIC_KEY}", f"restrict,pty {SECOND_KEY}"]

    access.users = [ALICE]
    assert desired_links(config, paths, memberships) == [row | {"allowed_logins": ["alice@github"]}]
    wakes = len(manager.specs)
    with pytest.raises(_ConnectRefused, match="not authorized"):
        await _connect(config, paths, link.id, "bob@github")
    assert len(manager.specs) == wakes
    assert SECOND_KEY in projected.read_text()
    await _connect(config, paths, link.id)
    assert projected.read_text() == f"restrict,pty {PUBLIC_KEY}\n"

    config.tailnet_ssh.authorized_keys.clear()
    assert desired_links(config, paths, memberships) == []
    assert projected.read_text() == f"restrict,pty {PUBLIC_KEY}\n"
    assert read_links(paths).links == [link]
    config.tailnet_ssh.authorized_keys[ALICE] = [PUBLIC_KEY]
    assert desired_links(config, paths, memberships) == [row | {"allowed_logins": ["alice@github"]}]


@pytest.mark.parametrize("working_route", [False, True])
@pytest.mark.asyncio
async def test_disabled_shell_routes_cannot_list_or_mask_a_working_shell(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    working_route: bool,
) -> None:
    """Only agents offering run_shell_command contribute to a worker's SSH routes."""
    config.agents["mind"].tailnet_ssh = False
    config.agents["mine"].worker_scope = "user"
    config.agents["mine"].tools = [{"shell": {"enable_run_shell_command": False}}]
    other = config.agents["other"]
    other.worker_scope = "user"
    other.access = config.agents["mine"].access
    other.tools = ["shell"]
    other.tailnet_ssh = working_route
    key = _key(ALICE, "mine", "user")
    with update_links(paths) as book:
        link = book.ensure(key)
    manager = _manager(monkeypatch, _handle(key, tmp_path / "ssh"))
    monkeypatch.setattr(sandbox_proxy, "lease_worker_manager", lambda *_: manager)
    rows = desired_links(config, paths, AgentReplyMembershipIndex())
    if not working_route:
        assert rows == []
        with pytest.raises(_ConnectRefused, match="not authorized"):
            await _connect(config, paths, link.id)
        assert manager.calls == []
        return

    async def start_sshd(_call: WorkerCall) -> None:
        pass

    monkeypatch.setattr(links, "_start_sshd", start_sshd)
    assert rows == [{"id": link.id, "hostname": link.hostname, "allowed_logins": ["alice@github"]}]
    assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)


@pytest.mark.parametrize("field", ["enable_run_shell_command", "include_tools"])
@pytest.mark.asyncio
async def test_malformed_stored_shell_settings_only_refuse_their_own_route(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """A broken agent's settings cannot prevent another worker's link from listing or connecting."""
    broken = _link(paths, "mine")
    healthy = _link(paths, "mind")
    manager = _wire_connect(monkeypatch, tmp_path, healthy)
    credentials = get_runtime_credentials_manager(paths).for_primary_runtime_scope(ALICE, "mine")
    stored_value = {"invalid": "private-setting-value"}
    credentials.save_credentials("shell", {field: stored_value})

    with capture_logs() as logs:
        rows = desired_links(config, paths, AgentReplyMembershipIndex())
    assert rows == [{"id": healthy.id, "hostname": healthy.hostname, "allowed_logins": ["alice@github"]}]
    assert any(log.get("agent") == "mine" and log.get("worker_key") == broken.worker_key for log in logs)
    assert "private-setting-value" not in str(logs)
    with pytest.raises(_ConnectRefused, match="not authorized"):
        await _connect(config, paths, broken.id)
    assert manager.specs == []
    assert await _connect(config, paths, healthy.id) == ("10.0.0.5", 2222)
    assert [spec.worker_key for spec in manager.specs] == [healthy.worker_key]


@pytest.mark.parametrize("backend", ["static_runner", "local", "docker_with_local_shell"])
@pytest.mark.asyncio
async def test_unsupported_shell_placement_never_wakes(
    config: Config,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    """A static runner and shells running locally have no dedicated SSH target."""
    overrides = {"MINDROOM_WORKER_BACKEND": "static_runner", "MINDROOM_SANDBOX_PROXY_URL": "http://runner:8766"}
    if backend != "static_runner":
        overrides = {
            "MINDROOM_WORKER_BACKEND": "docker" if backend == "docker_with_local_shell" else "",
            "MINDROOM_SANDBOX_EXECUTION_MODE": "local",
        }
    link = _link(paths)
    paths = replace(paths, process_env={**paths.process_env, **overrides})
    manager = _manager(monkeypatch)
    assert desired_links(config, paths, AgentReplyMembershipIndex()) == []
    with pytest.raises(_ConnectRefused, match="not authorized"):
        await _connect(config, paths, link.id)
    assert manager.calls == []


def test_active_worker_keys_window(paths: RuntimePaths) -> None:
    """Fresh positive counts for known IDs hold workers even if their links are no longer listed."""
    busy = _link(paths)
    quiet = _link(paths, "mind")
    status = GatewayStatus()
    assert active_worker_keys(status, monotonic=5.0) == frozenset()
    record_status(paths, status, StatusReport(open_connections={busy.id: 1, quiet.id: 0, "unknown": 7}), monotonic=1000)
    assert active_worker_keys(status, monotonic=1090) == {busy.worker_key}
    assert not active_worker_keys(status, monotonic=1091)
    record_status(paths, status, StatusReport(open_connections={quiet.id: 1}), monotonic=1091)
    assert active_worker_keys(status, monotonic=1091) == {quiet.worker_key}
