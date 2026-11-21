"""Tests for the tailnet SSH link service: what the gateway is told, what its reports change, and a connect."""

from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Never, Self

import pytest
import pytest_asyncio
from aiohttp import web
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths, tracking_dir
from mindroom.file_locks import try_exclusive_file_lock
from mindroom.shared_directories.store import add_shared_directory_member, create_shared_directory
from mindroom.tailnet_ssh import links
from mindroom.tailnet_ssh.links import (
    ConnectRefused,
    GatewayStatus,
    StatusReport,
    _resolve_target,
    active_worker_keys,
    connect,
    desired_links,
    record_status,
)
from mindroom.tailnet_ssh.store import Link, new_link, read_links, update_links
from mindroom.tool_system.sandbox_proxy import WorkerCall
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key, subagent_worker_key, worker_log_id
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerSshEndpoint
from mindroom.workers.protocol import ExecuteRequest
from tests.access_schema_support import membership_index
from tests.conftest import bind_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
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
    "agents": {
        "mine": {
            "display_name": "Mine",
            "worker_scope": "user_agent",
            "access": {"users": [ALICE], "members_of_rooms": []},
            "tailnet_ssh": True,
        },
        "mind": {
            "display_name": "Mind",
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
    return Config.validate_with_runtime(deepcopy(_DATA), paths)


@pytest.fixture
def room_config(paths: RuntimePaths) -> Config:
    """Beside the usual agents, a private agent whose people are the members of the lobby room."""
    data = deepcopy(_DATA)
    data["agents"]["research"] = {
        "display_name": "Research",
        "private": {"per": "user_agent"},
        "tailnet_ssh": True,
        "rooms": ["lobby"],
    }
    return bind_runtime_paths(Config.validate_with_runtime(data, paths), paths)


def _pending_index(config: Config, index: str) -> AgentReplyMembershipIndex:
    """Room membership not known yet: as at a primary's start, or after a reload invalidated it."""
    memberships = AgentReplyMembershipIndex()
    if index == "invalidated":
        memberships.invalidate(config, reason="config_reload")
    return memberships


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


def _link(requester: str = ALICE, agent: str = "mine") -> Link:
    return new_link(requester, agent)


def _link_key(link: Link) -> str:
    return _key(link.requester_id, link.agent_name)


def _store(paths: RuntimePaths, *stored: Link) -> None:
    update_links(paths, lambda book: book.links.extend(stored))


async def _rows(
    config: Config, paths: RuntimePaths, status: GatewayStatus | None = None, *,
    memberships: AgentReplyMembershipIndex | None = None, monotonic: float = NOW,
) -> list[dict[str, object]]:
    return await desired_links(lambda: (config, paths), paths, memberships or AgentReplyMembershipIndex(), status or GatewayStatus(), monotonic=monotonic)


def _post(
    paths: RuntimePaths, status: GatewayStatus, counts: dict[str, int] | None = None, *,
    monotonic: float = NOW, removed: list[str] | None = None,
) -> None:
    record_status(paths, status, StatusReport(open_connections=counts or {}, removed=removed or []), monotonic=monotonic)


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

    def ensure_worker(self, spec: WorkerSpec) -> WorkerHandle:
        self.calls.append("ensure_worker")
        self.specs.append(spec)
        if self.error is not None:
            raise self.error
        return next(handle for handle in self.handles if handle.worker_key == spec.worker_key)

    def list_workers(self) -> list[WorkerHandle]:
        self.calls.append("list_workers")
        return list(self.handles)


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
    return WorkerCall(request=request, api_root=api_root, headers={"x-mindroom-sandbox-token": "runner-token"})


def _wire_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    link: Link,
    *,
    ssh: bool = True,
    call: WorkerCall | None = None,
) -> _Manager:
    """Stand in for the worker of ``link``, its agent's shell, and its runner, recording the order of their use."""
    manager = _manager(monkeypatch, _handle(_link_key(link), tmp_path / "ssh" if ssh else None))
    calls = manager.calls

    async def start_sshd(_call: WorkerCall) -> None:
        calls.append("ssh_start")

    monkeypatch.setattr(links, "link_ineligibility", _recorded(calls, "eligibility", links.link_ineligibility))
    monkeypatch.setattr(links, "write_projection", _recorded(calls, "projection", links.write_projection))
    monkeypatch.setattr(links, "agent_terminals", lambda *_args, **_kwargs: SimpleNamespace(stand_ins=object()))
    monkeypatch.setattr(links, "worker_call", lambda *_args, **_kwargs: call or _worker_call(_link_key(link)))
    monkeypatch.setattr(links, "_start_sshd", start_sshd)
    return manager


class FakeRunner:
    """A worker's sandbox runner that records each ``POST`` it gets, with its runner token, and answers ``status``.

    It answers once ``release`` is set, which it is unless a test holds the answers back.
    """

    def __init__(self) -> None:
        self.url = ""
        self.status = 204
        self.posts: list[tuple[str, str | None]] = []
        self.release = asyncio.Event()
        self.release.set()

    def handle(self, worker_key: str, ssh_dir: Path | None, *, token: str = "runner-token") -> WorkerHandle:  # noqa: S107
        """Return a listed worker whose runner this is."""
        endpoint = f"{self.url}/api/sandbox-runner/execute"
        return replace(_handle(worker_key, ssh_dir), endpoint=endpoint, auth_token=token)

    async def answer(self, request: web.Request) -> web.Response:
        """Record a post, and answer it once released."""
        self.posts.append((request.path, request.headers.get("x-mindroom-sandbox-token")))
        await self.release.wait()
        return web.Response(status=self.status)


@pytest_asyncio.fixture
async def fake_runner() -> AsyncIterator[FakeRunner]:
    """Serve a ``FakeRunner`` on a local port."""
    runner = FakeRunner()
    app = web.Application()
    app.router.add_post("/api/sandbox-runner/ssh/stop", runner.answer)
    app.router.add_post("/api/sandbox-runner/ssh/start", runner.answer)
    served = web.AppRunner(app, access_log=None)
    await served.setup()
    try:
        site = web.TCPSite(served, "127.0.0.1", 0)
        await site.start()
        runner.url = f"http://127.0.0.1:{served.addresses[0][1]}"
        yield runner
    finally:
        runner.release.set()
        await served.cleanup()


_STOP = ("/api/sandbox-runner/ssh/stop", "runner-token")


def _projected_keys(ssh_dir: Path, link: Link) -> None:
    """Write the files a connect of ``link`` would have left in its worker, Alice's key admitted."""
    links.write_projection(ssh_dir, link, [f"restrict,pty {PUBLIC_KEY}"])


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


def test_resolve_target_is_the_persons_own_worker(config: Config, paths: RuntimePaths) -> None:
    """A link's worker is the one a turn of the agent for that person runs in."""
    assert _resolve_target(config, paths, ALICE, "mine").worker_key == _key(ALICE, "mine")
    private = _resolve_target(config, paths, ALICE, "mind")
    assert private.worker_key == _key(ALICE, "mind")
    assert private.private_agent_names == frozenset({"mind"})


@pytest.mark.asyncio
async def test_discovery_uses_exact_marked_worker_keys_without_a_status_report(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sorted eligible workers get stable claims; credentials alone never create a node."""
    config.tailnet_ssh.logins[BOB] = "bob@github"
    config.tailnet_ssh.authorized_keys[BOB] = [PUBLIC_KEY]
    status = GatewayStatus()
    ssh = tmp_path / "ssh"
    manager = _manager(
        monkeypatch,
        _handle(_key(BOB, "mind"), ssh),
        _handle(_key(ALICE, "mind"), ssh),
        _handle(_key(BOB, "mine"), ssh),  # Unauthorized.
        _handle(_key(ALICE, "mine"), None),  # Unmarked.
        _handle(_key(ALICE, "other"), ssh),  # Not opted in.
        _handle(_key(ALICE, "mine").replace(":default:", ":other:"), ssh),
        _handle(subagent_worker_key(_key(ALICE, "mine"), "a" * 32), ssh),
    )
    rows = await _rows(config, paths, status, monotonic=0)
    assert [row["hostname"] for row in rows] == ["mind", "mind-bob"]
    assert [row["allowed_login"] for row in rows] == ["alice@github", "bob@github"]
    assert all(set(row) == {"id", "hostname", "desired", "allowed_login", "access_revision"} for row in rows)
    assert [link.requester_id for link in read_links(paths).links] == [ALICE, BOB]
    assert manager.calls == ["list_workers"]

    manager.handles += [_handle(_key(ALICE, "mine"), ssh)]
    assert await _rows(config, paths, status, monotonic=59) == rows
    assert [row["hostname"] for row in await _rows(config, paths, status, monotonic=60)] == ["mind", "mind-bob", "mine"]


@pytest.mark.asyncio
async def test_a_worker_discovered_without_credentials_reuses_its_link(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials gate advertisement, not the durable identity; reuse that identity even without a live worker."""
    manager = _manager(monkeypatch, _handle(_key(BOB, "mind"), tmp_path / "ssh"))
    assert await _rows(config, paths) == []
    [link] = read_links(paths).links
    assert link.requester_id == BOB
    manager.handles.clear()
    config.tailnet_ssh.logins[BOB] = "bob@github"
    config.tailnet_ssh.authorized_keys[BOB] = [PUBLIC_KEY]
    assert [row["id"] for row in await _rows(config, paths)] == [link.id]
    assert read_links(paths).by_id(link.id) == link


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["disabled", "scope", "denied", "login", "keys"])
async def test_ineligible_links_are_omitted_then_reused(config: Config, paths: RuntimePaths, reason: str) -> None:
    """A temporary eligibility loss does not retire or rename an asleep worker's link."""
    link = _link()
    _store(paths, link)
    changed = config.model_copy(deep=True)
    if reason == "disabled":
        changed.agents["mine"].tailnet_ssh = False
    elif reason == "scope":
        changed.agents["mine"].worker_scope = "user"
    elif reason == "denied":
        changed.agents["mine"].access.users = [BOB]
    elif reason == "login":
        changed.tailnet_ssh.logins.clear()
    else:
        changed.tailnet_ssh.authorized_keys.clear()
    assert await _rows(changed, paths) == []
    assert read_links(paths).retiring_ids == set()
    assert [row["id"] for row in await _rows(config, paths)] == [link.id]
    assert read_links(paths).by_id(link.id) == link


@pytest.mark.asyncio
async def test_a_worker_key_shared_by_two_agents_is_not_discovered(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name collisions in worker keys fail even though DNS collisions can be resolved."""
    config.agents["mine_"] = config.agents["mine"].model_copy(update={"tailnet_ssh": False})
    _manager(monkeypatch, _handle(_key(ALICE, "mine"), tmp_path / "ssh"))
    assert await _rows(config, paths) == []
    assert not read_links(paths).links


@pytest.mark.asyncio
async def test_restoring_a_deleted_agent_can_never_reuse_a_retiring_id(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete, restore, and delete again; a delayed acknowledgement of the first ID cannot delete the second."""
    link = _link()
    _store(paths, link)
    manager = _manager(monkeypatch)
    deleted = config.model_copy(deep=True)
    del deleted.agents["mine"]
    [retirement] = await _rows(deleted, paths)
    assert retirement == {
        "id": link.id, "hostname": link.hostname, "desired": "removing", "allowed_login": None, "access_revision": "",
    }
    manager.handles = [_handle(_link_key(link), tmp_path / "ssh")]
    assert await _rows(config, paths) == [retirement]
    _post(paths, GatewayStatus(), removed=[link.id])

    [row] = await _rows(config, paths)
    [restored] = read_links(paths).links
    assert row["id"] == restored.id != link.id
    assert restored.host_key != link.host_key
    assert restored.hostname == link.hostname
    _post(paths, GatewayStatus(), removed=[restored.id])  # An acknowledgement cannot drop an active ID.
    assert read_links(paths).by_id(restored.id) == restored
    assert [row["id"] for row in await _rows(deleted, paths)] == [restored.id]
    _post(paths, GatewayStatus(), removed=[link.id])
    assert read_links(paths).retiring_ids == {restored.id}
    _post(paths, GatewayStatus(), removed=[restored.id])
    assert read_links(paths).links == []


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", ["list", "clear"])
async def test_retirement_retries_before_ack_and_stops_only_its_own_worker(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner, retry: str,
) -> None:
    """Retry a failed list or projection clear; a removal neither stops early nor clears a different worker."""
    link, other = _link(), _link(agent="mind")
    _store(paths, link, other)
    ssh, other_ssh = tmp_path / "ssh", tmp_path / "other"
    _projected_keys(ssh, link)
    _projected_keys(other_ssh, other)
    manager = _manager(monkeypatch, fake_runner.handle(_link_key(link), ssh), fake_runner.handle(_link_key(other), other_ssh))
    clear = links.clear_authorized_keys
    listed = manager.list_workers

    def fail(*_args: object) -> Never:
        if retry == "list":
            msg = "list failed"
            raise WorkerBackendError(msg)
        msg = "write failed"
        raise FileNotFoundError(msg)

    if retry == "list":
        monkeypatch.setattr(manager, "list_workers", fail)
    else:
        monkeypatch.setattr(links, "clear_authorized_keys", fail)
    del config.agents["mine"]
    status = GatewayStatus()
    assert [row["id"] for row in await _rows(config, paths, status)] == [other.id]
    assert (ssh / "authorized_keys").read_text()
    assert read_links(paths).retiring_ids == {link.id}
    assert fake_runner.posts == []

    monkeypatch.setattr(links, "clear_authorized_keys", clear)
    monkeypatch.setattr(manager, "list_workers", listed)
    rows = await _rows(config, paths, status)
    assert [(row["id"], row["desired"]) for row in rows] == [(link.id, "removing"), (other.id, "on")]
    assert (ssh / "authorized_keys").read_text() == ""
    assert (other_ssh / "authorized_keys").read_text() == f"restrict,pty {PUBLIC_KEY}\n"
    await asyncio.gather(*links._STOPPING)
    assert fake_runner.posts == [_STOP]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["keys", "login", "scope", "disabled"])
async def test_omission_clears_running_projection_and_stops_sshd(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner,
    trigger: str,
) -> None:
    """The unpinned key still identifies the old worker when its agent no longer has eligible scope."""
    link = _link()
    _store(paths, link)
    _projected_keys(tmp_path / "ssh", link)
    _manager(monkeypatch, fake_runner.handle(_link_key(link), tmp_path / "ssh"))
    if trigger == "keys":
        config.tailnet_ssh.authorized_keys.clear()
    elif trigger == "login":
        config.tailnet_ssh.logins.clear()
    elif trigger == "scope":
        config.agents["mine"].worker_scope = "user"
    else:
        config.agents["mine"].tailnet_ssh = False
    assert await _rows(config, paths) == []
    assert (tmp_path / "ssh" / "authorized_keys").read_text() == ""
    assert read_links(paths).by_id(link.id) == link
    await asyncio.gather(*links._STOPPING)
    assert fake_runner.posts == [_STOP]


@pytest.mark.asyncio
async def test_stops_run_together_and_apart_from_the_caller(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner,
) -> None:
    """A slow or failed stop neither delays the link list nor serializes other workers' stops."""
    one, two = _link(), _link(agent="mind")
    _store(paths, one, two)
    handles = []
    for link in (one, two):
        ssh = tmp_path / link.agent_name
        _projected_keys(ssh, link)
        handles.append(fake_runner.handle(_link_key(link), ssh))
    _manager(monkeypatch, *handles)
    fake_runner.release.clear()
    fake_runner.status = 503
    config.tailnet_ssh.logins.clear()
    with capture_logs() as logs:
        try:
            async with asyncio.timeout(5):
                assert await _rows(config, paths) == []
                while len(fake_runner.posts) != 2:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            assert all((tmp_path / link.agent_name / "authorized_keys").read_text() == "" for link in (one, two))
        finally:
            fake_runner.release.set()
            await asyncio.gather(*links._STOPPING)
    assert fake_runner.posts == [_STOP, _STOP]
    assert {log["worker"] for log in logs if log["event"] == "tailnet_ssh_sshd_not_stopped"} == {
        worker_log_id(_link_key(one)), worker_log_id(_link_key(two)),
    }


@pytest.mark.asyncio
async def test_changed_key_revision_refreshes_only_live_projections(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Comments and duplicates do not revoke sessions; changing the key identity or login does."""
    link, idle = _link(), _link(agent="mind")
    _store(paths, link, idle)
    ssh = tmp_path / "ssh"
    _projected_keys(ssh, link)
    _manager(monkeypatch, _handle(_link_key(link), ssh))
    status = GatewayStatus()
    before = await _rows(config, paths, status)
    bare = PUBLIC_KEY.rsplit(" ", 1)[0]
    config.tailnet_ssh.authorized_keys[ALICE] = [f"{bare} renamed", PUBLIC_KEY]
    assert await _rows(config, paths, status) == before
    assert (ssh / "authorized_keys").read_text() == f"restrict,pty {bare} renamed\n"

    config.tailnet_ssh.authorized_keys[ALICE] = [SECOND_KEY]
    after = await _rows(config, paths, status)
    assert [row["id"] for row in after] == [link.id, idle.id]
    assert all(old["access_revision"] != new["access_revision"] for old, new in zip(before, after, strict=True))
    assert (ssh / "authorized_keys").read_text() == f"restrict,pty {SECOND_KEY}\n"
    assert (ssh / "ssh_host_ed25519_key").read_text() == link.host_key
    config.tailnet_ssh.logins[ALICE] = "new@login"
    assert (await _rows(config, paths, status))[0]["access_revision"] != after[0]["access_revision"]


@pytest.mark.asyncio
async def test_shared_directories_are_read_once_per_reconcile(
    config: Config, paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All links are checked against the same directory list."""
    _store(paths, _link(), _link(agent="mind"))
    reads: list[str] = []
    monkeypatch.setattr(links, "load_shared_directories", _recorded(reads, "directories", links.load_shared_directories))
    assert len(await _rows(config, paths)) == 2
    assert reads == ["directories"]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", ["fresh", "invalidated", "denied"])
async def test_unresolved_or_denied_room_membership_omits_without_retirement(
    room_config: Config, paths: RuntimePaths, index: str,
) -> None:
    """Membership loss is reversible and retains the ID and host key."""
    link = _link(agent="research")
    _store(paths, link)
    memberships = _pending_index(room_config, index)
    if index == "denied":
        memberships = await membership_index(room_config, {"lobby": {BOB}})
    assert await _rows(room_config, paths, memberships=memberships) == []
    assert read_links(paths).by_id(link.id) == link
    assert not read_links(paths).retiring_ids
    joined = await membership_index(room_config, {"lobby": {ALICE}})
    assert [row["id"] for row in await _rows(room_config, paths, memberships=joined)] == [link.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", ["fresh", "invalidated"])
async def test_connect_refuses_while_room_membership_is_pending(
    room_config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index: str,
) -> None:
    """A connect is refused, for a retry, while it is not known whether the person may use the agent."""
    link = _link(agent="research")
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)

    with pytest.raises(ConnectRefused) as refused:
        await connect(
            lambda: (room_config, paths),
            _pending_index(room_config, index),
            link.id,
            "alice@github",
            "alice-laptop",
        )

    assert refused.value.status_code == 503
    assert "ensure_worker" not in manager.calls
    assert read_links(paths).by_id(link.id) == link


@pytest.mark.asyncio
@pytest.mark.parametrize("login", [None, "", "   "])
async def test_missing_or_empty_configured_login_admits_nobody(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: str | None,
) -> None:
    """An unknown or tagged peer cannot wake a worker even if config validation was bypassed."""
    link = _link()
    _store(paths, link)
    if login is None:
        config.tailnet_ssh.logins.clear()
    else:
        config.tailnet_ssh.logins[ALICE] = login
    manager = _wire_connect(monkeypatch, tmp_path, link)
    for peer in ("", "alice@github"):
        with pytest.raises(ConnectRefused) as refused:
            await _connect(config, paths, link.id, peer)
        assert refused.value.status_code == 403
    assert not manager.calls


@pytest.mark.asyncio
async def test_connect_order(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect checks the link, wakes its worker, writes sshd's files, starts sshd, and only then names the port."""
    link = _link(agent="mind")
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)

    assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)

    assert manager.calls == ["eligibility", "ensure_worker", "eligibility", "projection", "ssh_start"]
    assert manager.specs == [WorkerSpec(_link_key(link), private_agent_names=frozenset({"mind"}))]
    assert (tmp_path / "ssh" / "authorized_keys").read_text() == f"restrict,pty {PUBLIC_KEY}\n"
    assert (tmp_path / "ssh" / "ssh_host_ed25519_key").read_text() == link.host_key


@pytest.mark.asyncio
async def test_connect_without_ssh_endpoint_calls_no_runner(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker launched without the SSH mark is never asked to start sshd."""
    link = _link()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link, ssh=False)

    with pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 409
    assert refused.value.detail == "This worker has no marked SSH endpoint."
    assert manager.calls == ["eligibility", "ensure_worker"]


@pytest.mark.asyncio
async def test_connect_refuses_wrong_peer_unknown_ineligible_and_retiring(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peer must match the exact link owner; revoked or retiring links cannot wake their worker."""
    link, ineligible, retiring = _link(agent="mind"), _link(), _link(BOB, "mind")
    _store(paths, link, ineligible, retiring)
    update_links(paths, lambda book: book.retiring_ids.add(retiring.id))
    create_shared_directory(paths, owner="other", name="specs")
    add_shared_directory_member(paths, owner="other", name="specs", member="mine")
    manager = _wire_connect(monkeypatch, tmp_path, link)
    for link_id, peer, code in (
        (link.id, "bob@github", 403), ("0" * 16, "alice@github", 404),
        (ineligible.id, "alice@github", 409), (retiring.id, "bob@github", 409),
    ):
        with pytest.raises(ConnectRefused) as refused:
            await _connect(config, paths, link_id, peer)
        assert refused.value.status_code == code
    assert "ensure_worker" not in manager.calls


@pytest.mark.asyncio
async def test_connect_refuses_while_the_worker_is_not_ready(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that fails, or a worker that did not come up, is a refusal the next attempt may not meet."""
    link = _link()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)

    manager.handles = [_handle(_link_key(link), tmp_path / "ssh", status="failed")]
    with pytest.raises(ConnectRefused) as not_ready:
        await _connect(config, paths, link.id)
    manager.error = WorkerBackendError("image pull failed for registry.example/secret")
    with pytest.raises(ConnectRefused) as failed:
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
    link = _link()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link, call=_worker_call(_key(BOB, "mind")))
    if shell == "no_terminals":
        monkeypatch.setattr(links, "agent_terminals", lambda *_args, **_kwargs: None)
    elif shell == "no_call":
        monkeypatch.setattr(links, "worker_call", lambda *_args, **_kwargs: None)

    with pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 409
    assert (refused.value.detail == "This agent has no shell tool.") == (shell != "other_worker")
    assert "ssh_start" not in manager.calls


@pytest.mark.asyncio
async def test_a_connect_whose_keys_admit_nobody_starts_no_sshd(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No keys for this requester means no wake, projection, or runner call."""
    link = _link()
    _store(paths, link)
    config.tailnet_ssh.authorized_keys = {BOB: [PUBLIC_KEY]}
    manager = _wire_connect(monkeypatch, tmp_path, link)
    with pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)
    assert refused.value.status_code == 409
    assert manager.calls == ["eligibility"]
    assert not (tmp_path / "ssh").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["keys", "runtime", "same-backend-new-keys"])
async def test_connect_admission_uses_config_and_runtime_from_after_wake(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    """A worker may wake either under a revoked admission or with a new key set."""
    link = _link()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)
    committed = config, paths
    ensure = manager.ensure_worker

    def wake(spec: WorkerSpec) -> WorkerHandle:
        nonlocal committed
        handle = ensure(spec)
        current = config.model_copy(deep=True)
        current_paths = paths
        if change == "keys":
            current.tailnet_ssh.authorized_keys.clear()
        elif change == "runtime":
            current_paths = replace(paths, storage_root=tmp_path / "new-storage")
        else:
            current.tailnet_ssh.authorized_keys[ALICE] = [SECOND_KEY]
        committed = current, current_paths
        return handle

    monkeypatch.setattr(manager, "ensure_worker", wake)
    admission = connect(lambda: committed, AgentReplyMembershipIndex(), link.id, "alice@github", "laptop")
    if change == "same-backend-new-keys":
        assert await admission == ("10.0.0.5", 2222)
        assert (tmp_path / "ssh" / "authorized_keys").read_text() == f"restrict,pty {SECOND_KEY}\n"
    else:
        with pytest.raises(ConnectRefused) as refused:
            await admission
        assert refused.value.status_code == 409
        assert "projection" not in manager.calls
        assert "ssh_start" not in manager.calls
        assert not (tmp_path / "ssh").exists()


@pytest.mark.asyncio
async def test_key_revocation_serializes_with_projection_write(
    config: Config, paths: RuntimePaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_runner: FakeRunner,
) -> None:
    """A connect paused after revalidation cannot write a removed key after reconciliation clears it."""
    link = _link()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)
    manager.handles = [fake_runner.handle(_link_key(link), tmp_path / "ssh")]
    write = links.write_projection
    projecting, release = threading.Event(), threading.Event()

    def paused(*args: Any) -> None:  # noqa: ANN401
        projecting.set()
        assert release.wait(10)
        write(*args)

    monkeypatch.setattr(links, "write_projection", paused)
    connecting = asyncio.create_task(_connect(config, paths, link.id))
    try:
        assert await asyncio.to_thread(projecting.wait, 5)
        lock_path = tracking_dir(paths) / "tailnet_ssh.lock"
        assert try_exclusive_file_lock(lock_path) is None
        revoked = config.model_copy(deep=True)
        revoked.tailnet_ssh.authorized_keys.clear()
        reconcile = asyncio.create_task(_rows(revoked, paths))
    finally:
        release.set()
        await connecting
    assert await reconcile == []
    assert (tmp_path / "ssh" / "authorized_keys").read_text() == ""
    await asyncio.gather(*links._STOPPING)
    assert fake_runner.posts == [_STOP]


@pytest.mark.asyncio
async def test_connect_refuses_when_sshds_files_cannot_be_written(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready worker whose SSH folder the primary cannot write is a refusal, and its runner is not asked."""
    link = _link()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)
    (tmp_path / "blocked").write_text("")
    manager.handles = [_handle(_link_key(link), tmp_path / "blocked" / "ssh")]

    with capture_logs() as logs, pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 503
    assert str(tmp_path) not in refused.value.detail
    assert manager.calls == ["eligibility", "ensure_worker", "eligibility", "projection"]
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
    link = _link()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)
    threads: list[str] = []
    ensure_worker = manager.ensure_worker

    def recorded(spec: WorkerSpec) -> WorkerHandle:
        threads.append(threading.current_thread().name)
        return ensure_worker(spec)

    monkeypatch.setattr(manager, "ensure_worker", recorded)

    await _connect(config, paths, link.id)

    assert [name.startswith("mindroom-tailnet-ssh-connect") for name in threads] == [True]


@pytest.mark.parametrize("status", [200, 400, 503])
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
        link = _link()
        _store(paths, link)
        call = _worker_call(_link_key(link), f"http://127.0.0.1:{port}/api/sandbox-runner")
        start_sshd = links._start_sshd
        _wire_connect(monkeypatch, tmp_path, link, call=call)
        monkeypatch.setattr(links, "_start_sshd", start_sshd)

        if status == 200:
            assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)
        else:
            with pytest.raises(ConnectRefused) as refused:
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
    link = _link()
    _store(paths, link)
    start_sshd = links._start_sshd
    _wire_connect(monkeypatch, tmp_path, link, call=_worker_call(_link_key(link), "http://127.0.0.1:1/api"))
    monkeypatch.setattr(links, "_start_sshd", start_sshd)

    with pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 503
    assert "127.0.0.1" not in refused.value.detail


def test_active_worker_keys_window(paths: RuntimePaths) -> None:
    """Only fresh positive counts for known non-retiring IDs keep workers active."""
    busy, quiet, retiring = _link(), _link(agent="mind"), _link(BOB, "mind")
    _store(paths, busy, quiet, retiring)
    update_links(paths, lambda book: book.retiring_ids.add(retiring.id))
    status = GatewayStatus()
    assert active_worker_keys(status, monotonic=5.0) == frozenset()
    _post(paths, status, {busy.id: 1, quiet.id: 0, retiring.id: 2, "unknown": 7}, monotonic=1000.0)
    assert active_worker_keys(status, monotonic=1090.0) == {_link_key(busy)}
    assert not active_worker_keys(status, monotonic=1091.0)
    previous = status.report
    _post(paths, status, {quiet.id: 1}, monotonic=1090.0)
    assert previous == (1000.0, {_link_key(busy)})
    assert active_worker_keys(status, monotonic=1090.0) == {_link_key(quiet)}
