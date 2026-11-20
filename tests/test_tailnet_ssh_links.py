"""Tests for the tailnet SSH link service: what the gateway is told, what its reports change, and a connect."""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, Self, cast

import pytest
from aiohttp import web
from structlog.testing import capture_logs

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.main import Config
from mindroom.constants import RuntimePaths, resolve_runtime_paths, tracking_dir
from mindroom.file_locks import advisory_file_lock, release_file_lock, try_exclusive_file_lock
from mindroom.shared_directories.store import add_shared_directory_member, create_shared_directory
from mindroom.tailnet_ssh import eligibility, links, store
from mindroom.tailnet_ssh.links import (
    ConnectRefused,
    GatewayReport,
    GatewayStatus,
    LinkStatus,
    StatusReport,
    active_worker_keys,
    connect,
    desired_links,
    link_state,
    record_status,
    refresh_projections,
    resolve_target,
)
from mindroom.tailnet_ssh.store import Link, LinkBook, new_link, read_links, set_keys, update_links
from mindroom.tool_system.sandbox_proxy import WorkerCall
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_key
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerSshEndpoint
from mindroom.workers.protocol import ExecuteRequest
from tests.access_schema_support import membership_index
from tests.conftest import bind_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
NOW = 1_800_000_000.0
DAY = 24 * 60 * 60
LOGIN_URL = "https://login.tailscale.com/a/1c0ffee"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPVHVwHKbi4VF0jfKNiYpfwAeEgeTqB6B8NVRZvzjnDS alice@laptop"

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
    "tailnet_ssh": {"logins": {ALICE: "alice@github"}},
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


def _link(requester: str = ALICE, agent: str = "mine", *, now: float = NOW, **changes: object) -> Link:
    link = new_link(requester, agent, _key(requester, agent), agent, now=now)
    return link.model_copy(update=changes)


def _enrolled(requester: str = ALICE, agent: str = "mine", **changes: object) -> Link:
    return _link(requester, agent, enrolled_login="alice@github", enrolled_at=NOW, **changes)


def _store(paths: RuntimePaths, *stored: Link) -> None:
    update_links(paths, lambda book: book.links.extend(stored))


def _stored(paths: RuntimePaths, link: Link) -> Link | None:
    return read_links(paths).by_id(link.id)


def _store_enrollment(paths: RuntimePaths, enrollment: Literal["login", "auth_key"]) -> None:
    """Store how the gateway said it enrols its nodes."""

    def change(book: LinkBook) -> None:
        book.enrollment = enrollment

    update_links(paths, change)


def _operator_status(paths: RuntimePaths, *, operator_sync_at: float = 0.0) -> GatewayStatus:
    """Store that the gateway enrols with an operator's key, and return what is known of it in memory."""
    _store_enrollment(paths, "auth_key")
    return GatewayStatus(operator_sync_at=operator_sync_at)


def _rows(
    config: Config,
    paths: RuntimePaths,
    status: GatewayStatus | None = None,
    *,
    now: float = NOW,
    monotonic: float = 100.0,
) -> list[dict[str, object]]:
    memberships = AgentReplyMembershipIndex()
    return desired_links(config, paths, memberships, status or GatewayStatus(), now=now, monotonic=monotonic)


def _report(state: str = "running", **fields: object) -> dict[str, object]:
    report: dict[str, object] = {
        "state": state,
        "login_url": None,
        "tailnet": "tail1234.ts.net",
        "dns_name": "mine.tail1234.ts.net",
        "login_name": "bas@github",
        "key_expiry": None,
        "open_connections": 0,
        "last_error": None,
    }
    return report | fields


def _post(
    paths: RuntimePaths,
    status: GatewayStatus,
    reports: dict[str, dict[str, object]],
    *,
    enrollment: str = "login",
    now: float = NOW,
    monotonic: float = 100.0,
) -> None:
    report = StatusReport.model_validate({"enrollment": enrollment, "links": reports})
    record_status(paths, status, report, now=now, monotonic=monotonic)


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
    manager = _manager(monkeypatch, _handle(link.worker_key, tmp_path / "ssh" if ssh else None))
    calls = manager.calls

    async def start_sshd(_call: WorkerCall) -> None:
        calls.append("ssh_start")

    monkeypatch.setattr(links, "link_ineligibility", _recorded(calls, "eligibility", links.link_ineligibility))
    monkeypatch.setattr(links, "write_projection", _recorded(calls, "projection", links.write_projection))
    monkeypatch.setattr(links, "agent_terminals", lambda *_args, **_kwargs: SimpleNamespace(stand_ins=object()))
    monkeypatch.setattr(links, "worker_call", lambda *_args, **_kwargs: call or _worker_call(link.worker_key))
    monkeypatch.setattr(links, "_start_sshd", start_sshd)
    return manager


async def _connect(
    config: Config,
    paths: RuntimePaths,
    link_id: str,
    peer_login: str = "alice@github",
) -> tuple[str, int]:
    return await connect(
        config,
        paths,
        AgentReplyMembershipIndex(),
        link_id,
        peer_login,
        "alice-laptop",
    )


def test_resolve_target_is_the_persons_own_worker(config: Config, paths: RuntimePaths) -> None:
    """A link's worker is the one a turn of the agent for that person runs in."""
    assert resolve_target(config, paths, ALICE, "mine").worker_key == _key(ALICE, "mine")
    private = resolve_target(config, paths, ALICE, "mind")
    assert private.worker_key == _key(ALICE, "mind")
    assert private.private_agent_names == frozenset({"mind"})


def test_desired_rows_have_the_contract_keys(config: Config, paths: RuntimePaths) -> None:
    """A row carries exactly what the gateway reads, and never the host key."""
    link = _enrolled(closed_before=NOW - 5, login_after=NOW - 7)
    _store(paths, link)
    assert _rows(config, paths) == [
        {
            "id": link.id,
            "hostname": "mine",
            "desired": "on",
            "allowed_login": "alice@github",
            "closed_before": NOW - 5,
            "login_after": NOW - 7,
        },
    ]


def test_off_links_are_not_listed_and_suspended_is_computed(config: Config, paths: RuntimePaths) -> None:
    """A parked link is not the gateway's business, and a link that is not eligible now is suspended, not stored so."""
    off, joined = _enrolled(agent="mind", desired="off"), _enrolled()
    _store(paths, off, joined)
    create_shared_directory(paths, owner="other", name="specs")
    add_shared_directory_member(paths, owner="other", name="specs", member="mine")

    rows = _rows(config, paths)

    assert [(row["id"], row["desired"]) for row in rows] == [(joined.id, "suspended")]
    assert _stored(paths, joined) == joined
    assert _stored(paths, off) == off


@pytest.mark.parametrize("index", ["fresh", "invalidated"])
def test_pending_room_membership_suspends_and_never_removes(
    room_config: Config,
    paths: RuntimePaths,
    index: str,
) -> None:
    """While room membership is not known yet a room-granted link is suspended: only a known denial removes it."""
    link = _enrolled(agent="research", closed_before=NOW - 5)
    _store(paths, link)
    memberships = _pending_index(room_config, index)

    rows = desired_links(room_config, paths, memberships, GatewayStatus(), now=NOW, monotonic=100.0)

    assert [(row["id"], row["desired"], row["closed_before"]) for row in rows] == [(link.id, "suspended", NOW - 5)]
    assert _stored(paths, link) == link


@pytest.mark.asyncio
async def test_a_known_room_denial_removes_a_room_granted_link(room_config: Config, paths: RuntimePaths) -> None:
    """Once the lobby's members are known, Alice's link stays while she is one of them and is removed once she is not."""
    link = _enrolled(agent="research")
    _store(paths, link)

    member = desired_links(
        room_config,
        paths,
        await membership_index(room_config, {"lobby": {ALICE}}),
        GatewayStatus(),
        now=NOW + 1,
        monotonic=100.0,
    )
    left = desired_links(
        room_config,
        paths,
        await membership_index(room_config, {"lobby": {BOB}}),
        GatewayStatus(),
        now=NOW + 2,
        monotonic=100.0,
    )

    assert [(row["id"], row["desired"]) for row in member] == [(link.id, "on")]
    assert [(row["id"], row["desired"], row["closed_before"]) for row in left] == [(link.id, "removing", NOW + 2)]


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
    link = _enrolled(agent="research")
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)

    with pytest.raises(ConnectRefused) as refused:
        await connect(
            room_config,
            paths,
            _pending_index(room_config, index),
            link.id,
            "alice@github",
            "alice-laptop",
        )

    assert refused.value.status_code == 503
    assert "ensure_worker" not in manager.calls
    assert _stored(paths, link) == link


@pytest.mark.parametrize("trigger", ["agent_removed", "not_authorized", "worker_key_changed", "never_enrolled_24h"])
def test_removal_triggers_set_removing(config: Config, paths: RuntimePaths, trigger: str) -> None:
    """A link whose agent, person, or worker is no longer the one it was made for is removed, as is one never used."""
    link = {
        "agent_removed": _enrolled(agent="gone"),
        "not_authorized": _enrolled(BOB),
        "worker_key_changed": _enrolled(worker_key="v1:elsewhere:user_agent:~@alice:example.org:mine"),
        "never_enrolled_24h": _link(now=NOW - DAY),
    }[trigger]
    kept = [_enrolled(agent="mind", created_at=NOW - 2 * DAY), _link(BOB, "mind", now=NOW - DAY + 1)]
    _store(paths, link, *kept)

    rows = _rows(config, paths)

    assert [(row["id"], row["desired"], row["closed_before"]) for row in rows] == [
        (link.id, "removing", NOW),
        (kept[0].id, "on", NOW),
        (kept[1].id, "on", NOW - DAY + 1),
    ]
    removed = _stored(paths, link)
    assert removed is not None
    assert (removed.desired, removed.closed_before) == ("removing", NOW)
    assert [_stored(paths, other) for other in kept] == kept

    # A removal already under way is not started again.
    _rows(config, paths, now=NOW + 5)
    assert _stored(paths, link) == removed


def test_a_node_waiting_for_approval_is_not_dropped_at_a_day(config: Config, paths: RuntimePaths) -> None:
    """An admin may take more than a day to approve a node, so its link waits; without a fresh report it is dropped."""
    link = _link(now=NOW - DAY)
    _store(paths, link)
    status = GatewayStatus()
    _post(paths, status, {link.id: _report("needs_approval", login_name=None)}, now=NOW - 10, monotonic=100.0)

    assert [(row["id"], row["desired"]) for row in _rows(config, paths, status, monotonic=150.0)] == [(link.id, "on")]
    assert [(row["id"], row["desired"]) for row in _rows(config, paths, status, monotonic=191.0)] == [
        (link.id, "removing"),
    ]


def test_shared_directories_are_read_once_per_reconcile(
    config: Config,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """However many links there are, one reconcile reads the stored shared directories once."""
    _store(paths, _enrolled(), _enrolled(agent="mind"), _link(BOB, "mind"))
    reads: list[RuntimePaths] = []
    load = eligibility.load_shared_directories

    def counted(runtime_paths: RuntimePaths) -> object:
        reads.append(runtime_paths)
        return load(runtime_paths)

    monkeypatch.setattr(eligibility, "load_shared_directories", counted)
    monkeypatch.setattr(links, "load_shared_directories", counted)

    _rows(config, paths)

    assert reads == [paths]


def test_removed_report_drops_or_parks(config: Config, paths: RuntimePaths) -> None:
    """Once the gateway has logged a node out, its link is gone, or parked when the person disconnected it."""
    dropped = _enrolled(desired="removing")
    parked = _enrolled(agent="mind", desired="removing", park_when_removed=True)
    _store(paths, dropped, parked)
    status = GatewayStatus()

    _post(paths, status, {dropped.id: _report("removed"), parked.id: _report("removed")})

    book = read_links(paths)
    assert [(link.id, link.desired, link.enrolled_login) for link in book.links] == [(parked.id, "off", None)]
    assert status.report.links == {}
    assert _rows(config, paths, status) == []


def test_first_running_report_pins_enrolled_login(paths: RuntimePaths) -> None:
    """The login that first enrolled a node is the one it admits, whoever signs in to it later."""
    link = _link()
    _store(paths, link)
    status = GatewayStatus()

    _post(paths, status, {link.id: _report("needs_login", login_name=None, login_url=LOGIN_URL)}, now=NOW + 1)
    assert _stored(paths, link) == link

    _post(paths, status, {link.id: _report()}, now=NOW + 2)
    pinned = _stored(paths, link)
    assert pinned is not None
    assert (pinned.enrolled_login, pinned.enrolled_at) == ("bas@github", NOW + 2)

    _post(paths, status, {link.id: _report(login_name="eve@github")}, now=NOW + 3)
    assert _stored(paths, link) == pinned
    assert status.report.links[link.id].login_name == "eve@github"


def test_operator_key_enrolment_pins_no_login(config: Config, paths: RuntimePaths) -> None:
    """A node an operator's key enrolled admits who the config names, not whoever the gateway saw sign in."""
    link = _link()
    _store(paths, link)
    assert [row["allowed_login"] for row in _rows(config, paths)] == [None]

    _post(paths, GatewayStatus(), {link.id: _report(login_name="operator@github")}, enrollment="auth_key")

    stored = _stored(paths, link)
    assert stored is not None
    assert (stored.enrolled_login, stored.enrolled_at) == (None, NOW)
    assert [row["allowed_login"] for row in _rows(config, paths)] == ["alice@github"]


@pytest.mark.asyncio
async def test_operator_mode_survives_a_primary_restart(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a restart the gateway's last mode is known before it reports again, so operator links admit their login."""
    link = _link()
    _store(paths, link)
    _post(paths, GatewayStatus(), {link.id: _report()}, enrollment="auth_key")
    _wire_connect(monkeypatch, tmp_path, link)

    assert [row["allowed_login"] for row in _rows(config, paths, GatewayStatus())] == ["alice@github"]
    assert await _connect(config, paths, link.id, "alice@github") == ("10.0.0.5", 2222)


def test_status_records_the_workers_of_stored_links_only(paths: RuntimePaths) -> None:
    """A report is kept with its link's worker key, and one for a link the store does not hold is ignored."""
    link = _enrolled()
    _store(paths, link)
    stale = GatewayReport(links={"stale": LinkStatus("running", None, None, None, None, None, 1, None, "key")})
    status = GatewayStatus(stale)

    _post(paths, status, {link.id: _report(open_connections=2), "f" * 16: _report()}, monotonic=42.0)

    assert (read_links(paths).enrollment, status.report.received_at) == ("login", 42.0)
    assert status.report.links == {
        link.id: LinkStatus(
            state="running",
            login_url=None,
            tailnet="tail1234.ts.net",
            dns_name="mine.tail1234.ts.net",
            login_name="bas@github",
            key_expiry=None,
            open_connections=2,
            last_error=None,
            worker_key=link.worker_key,
        ),
    }


def test_enrolled_link_without_status_stays_listed_and_enrolled(config: Config, paths: RuntimePaths) -> None:
    """After a primary restart nothing is known of the gateway yet, which must not change a link."""
    link = _enrolled()
    _store(paths, link)
    status = GatewayStatus()

    assert [(row["id"], row["desired"]) for row in _rows(config, paths, status)] == [(link.id, "on")]
    assert link_state(link, status.report.links.get(link.id), suspended=False) == "enrolled"
    assert _stored(paths, link) == link


def test_link_state_names_what_the_card_shows() -> None:
    """The card's state is the stored link's, then whether it is eligible, then what the gateway last reported."""

    def reported(state: str) -> LinkStatus:
        return LinkStatus(state, None, None, None, None, None, 0, None, "key")  # type: ignore[arg-type]

    new, enrolled = _link(), _enrolled()
    assert link_state(new, None, suspended=False) == "pending_login"
    assert link_state(new, reported("needs_login"), suspended=False) == "pending_login"
    assert link_state(new, reported("needs_approval"), suspended=False) == "needs_approval"
    assert link_state(new, reported("running"), suspended=False) == "enrolled"
    assert link_state(enrolled, reported("needs_login"), suspended=False) == "needs_login"
    assert link_state(enrolled, reported("running"), suspended=True) == "suspended"
    assert link_state(_enrolled(desired="removing"), reported("running"), suspended=True) == "removing"


def test_operator_mode_adds_links_once_a_minute(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an operator's key every worker that serves SSH gets a link by itself, except one its person parked."""
    parked = _link(agent="mind", desired="off")
    _store(paths, parked)
    manager = _manager(
        monkeypatch,
        _handle(_key(ALICE, "mine"), tmp_path / "mine"),
        _handle(_key(ALICE, "mind"), tmp_path / "mind"),
        _handle(_key(BOB, "mind"), None),
        _handle(_key(ALICE, "mine", "user"), tmp_path / "user"),
        _handle(_key(ALICE, "gone"), tmp_path / "gone"),
        _handle(_key(BOB, "mine"), tmp_path / "bob"),
    )
    status = _operator_status(paths)

    rows = _rows(config, paths, status)

    added = read_links(paths).link(ALICE, "mine")
    assert added is not None
    assert (added.worker_key, added.desired, added.created_at) == (_key(ALICE, "mine"), "on", NOW)
    assert rows == [
        {
            "id": added.id,
            "hostname": "mine",
            "desired": "on",
            "allowed_login": "alice@github",
            "closed_before": NOW,
            "login_after": NOW,
        },
    ]
    assert read_links(paths).links == [parked, added]

    manager.handles.append(_handle(_key(BOB, "mind"), tmp_path / "bob-mind"))
    assert _rows(config, paths, status, now=NOW + 10) == rows
    assert manager.calls == ["list_workers"]

    assert len(_rows(config, paths, status, now=NOW + 61)) == 2
    assert read_links(paths).link(BOB, "mind") is not None


def test_operator_mode_adds_a_link_that_is_suspended_for_now(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that serves SSH gets its link also while the link is not eligible: suspended, not left out."""
    create_shared_directory(paths, owner="other", name="specs")
    add_shared_directory_member(paths, owner="other", name="specs", member="mine")
    _manager(monkeypatch, _handle(_key(ALICE, "mine"), tmp_path / "mine"))

    rows = _rows(config, paths, _operator_status(paths))

    added = read_links(paths).link(ALICE, "mine")
    assert added is not None
    assert added.desired == "on"
    assert [(row["id"], row["desired"]) for row in rows] == [(added.id, "suspended")]


def test_operator_mode_adds_no_link_for_a_key_two_agents_share(
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker key whose agent part two configured names give is no one agent's, so it gets no link."""
    data = deepcopy(_DATA)
    data["agents"]["mine_"] = deepcopy(data["agents"]["mine"])
    config = Config.validate_with_runtime(data, paths)
    _manager(monkeypatch, _handle(_key(ALICE, "mine"), tmp_path / "mine"))

    assert _rows(config, paths, _operator_status(paths)) == []
    assert read_links(paths).links == []


def test_an_operator_key_removed_from_the_config_closes_the_sessions(config: Config, paths: RuntimePaths) -> None:
    """A key the operator takes out of ``tailnet_ssh.authorized_keys`` closes the person's open sessions at once.

    Each reconcile here starts from a fresh status, as after a restart, so the keys last seen are stored.
    """
    link = _enrolled(closed_before=NOW - 5)
    _store(paths, link)
    config.tailnet_ssh.authorized_keys[ALICE] = [PUBLIC_KEY]
    assert [row["closed_before"] for row in _rows(config, paths)] == [NOW - 5]

    config.tailnet_ssh.authorized_keys.clear()

    assert [row["closed_before"] for row in _rows(config, paths, now=NOW + 5)] == [NOW + 5]
    assert [row["closed_before"] for row in _rows(config, paths, now=NOW + 10)] == [NOW + 5]


def test_a_key_still_in_the_portal_or_already_closed_for_closes_nothing_more(
    config: Config,
    paths: RuntimePaths,
) -> None:
    """An operator key the person also added is still theirs; a key removed in the portal closed the sessions then."""
    link = _enrolled(closed_before=NOW - 5)
    _store(paths, link)
    update_links(paths, lambda book: set_keys(book, ALICE, [PUBLIC_KEY], now=NOW))
    config.tailnet_ssh.authorized_keys[ALICE] = [PUBLIC_KEY]
    _rows(config, paths)

    config.tailnet_ssh.authorized_keys.clear()
    assert [row["closed_before"] for row in _rows(config, paths, now=NOW + 5)] == [NOW - 5]

    update_links(paths, lambda book: set_keys(book, ALICE, [], now=NOW + 6))
    assert [row["closed_before"] for row in _rows(config, paths, now=NOW + 10)] == [NOW + 6]


def test_a_steady_gateway_rewrites_nothing(config: Config, paths: RuntimePaths) -> None:
    """A reconcile or a report that changes no link leaves the stored file untouched."""
    link = _enrolled()
    _store(paths, link)
    _store_enrollment(paths, "login")
    path = tracking_dir(paths) / "tailnet_ssh.json"
    stored = path.stat()
    status = GatewayStatus()

    _rows(config, paths, status)
    _post(paths, status, {link.id: _report(open_connections=1)})
    _rows(config, paths, status, now=NOW + 5)

    assert status.report.links[link.id].open_connections == 1
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (stored.st_ino, stored.st_mtime_ns)


def test_login_mode_adds_no_links(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an operator's key a link exists only once its person asked for it."""
    manager = _manager(monkeypatch, _handle(_key(ALICE, "mine"), tmp_path / "mine"))
    _store_enrollment(paths, "login")
    assert _rows(config, paths, GatewayStatus()) == []
    assert _rows(config, paths, GatewayStatus()) == []
    assert manager.calls == []


def test_operator_mode_lists_links_while_the_backend_fails(
    config: Config,
    paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that cannot list its workers adds no link and takes none from the gateway."""
    link = _enrolled()
    _store(paths, link)

    def fail(_paths: RuntimePaths) -> object:
        msg = "docker is down"
        raise WorkerBackendError(msg)

    monkeypatch.setattr(links, "lease_configured_primary_worker_manager", fail)
    with capture_logs():
        rows = _rows(config, paths, _operator_status(paths))
    assert [(row["id"], row["desired"]) for row in rows] == [(link.id, "on")]


@pytest.mark.asyncio
async def test_operator_link_without_login_refuses_everyone(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With an operator's key and no login named for the person, nobody in the operator's tailnet gets in."""
    config.tailnet_ssh.logins.clear()
    link = _enrolled()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)
    status = _operator_status(paths, operator_sync_at=NOW)

    assert [row["allowed_login"] for row in _rows(config, paths, status)] == [None]
    for peer_login in ("alice@github", "", "None"):
        with pytest.raises(ConnectRefused) as refused:
            await _connect(config, paths, link.id, peer_login)
        assert refused.value.status_code == 403
    assert "ensure_worker" not in manager.calls


@pytest.mark.asyncio
async def test_an_empty_operator_login_admits_no_loginless_peer(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty login is no login: a tagged device, whose login the gateway reports as empty, is refused."""
    config.tailnet_ssh.logins[ALICE] = ""
    link = _enrolled()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)
    _operator_status(paths)

    with pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id, "")

    assert refused.value.status_code == 403
    assert manager.calls == []


@pytest.mark.asyncio
async def test_connect_order(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect checks the link, wakes its worker, writes sshd's files, starts sshd, and only then names the port."""
    link = _enrolled(agent="mind")
    _store(paths, link)
    update_links(paths, lambda book: set_keys(book, ALICE, [PUBLIC_KEY], now=NOW))
    manager = _wire_connect(monkeypatch, tmp_path, link)

    assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)

    assert manager.calls == ["eligibility", "ensure_worker", "projection", "ssh_start"]
    assert manager.specs == [WorkerSpec(link.worker_key, private_agent_names=frozenset({"mind"}))]
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
    link = _enrolled()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link, ssh=False)

    with pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 409
    assert refused.value.detail == (
        "This worker has no SSH port; with Docker, MINDROOM_DOCKER_WORKER_PUBLISH_HOST must not be a wildcard address."
    )
    assert manager.calls == ["eligibility", "ensure_worker"]


@pytest.mark.asyncio
async def test_connect_refuses_wrong_peer_unknown_suspended_and_removing(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing wakes a worker for a peer who is not the link's login, or for a link that is not on and eligible."""
    link = _enrolled(agent="mind")
    suspended = _enrolled()
    removing = _enrolled(BOB, "mind", desired="removing")
    off = _enrolled(BOB, "mine", desired="off")
    _store(paths, link, suspended, removing, off)
    create_shared_directory(paths, owner="other", name="specs")
    add_shared_directory_member(paths, owner="other", name="specs", member="mine")
    manager = _wire_connect(monkeypatch, tmp_path, link)

    async def refusal(link_id: str, peer_login: str = "alice@github") -> tuple[int, str]:
        with pytest.raises(ConnectRefused) as refused:
            await _connect(config, paths, link_id, peer_login)
        return refused.value.status_code, refused.value.detail

    assert (await refusal(link.id, "eve@github"))[0] == 403
    assert (await refusal("0" * 16))[0] == 404
    assert await refusal(suspended.id) == (409, "joined_directory")
    assert (await refusal(removing.id))[0] == 409
    assert (await refusal(off.id))[0] == 409
    assert "ensure_worker" not in manager.calls


@pytest.mark.asyncio
async def test_connect_refuses_while_the_worker_is_not_ready(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A backend that fails, or a worker that did not come up, is a refusal the next attempt may not meet."""
    link = _enrolled()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)

    manager.handles = [_handle(link.worker_key, tmp_path / "ssh", status="failed")]
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
    link = _enrolled()
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
async def test_connect_writes_the_keys_stored_when_the_worker_is_up(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key removed, or a link disconnected, while the worker woke is not what sshd gets."""
    link = _enrolled()
    _store(paths, link)
    update_links(paths, lambda book: set_keys(book, ALICE, [PUBLIC_KEY], now=NOW))
    manager = _wire_connect(monkeypatch, tmp_path, link)
    ensure_worker = manager.ensure_worker

    def removes_the_key(spec: WorkerSpec) -> WorkerHandle:
        update_links(paths, lambda book: set_keys(book, ALICE, [], now=NOW + 1))
        return ensure_worker(spec)

    monkeypatch.setattr(manager, "ensure_worker", removes_the_key)
    await _connect(config, paths, link.id)
    assert (tmp_path / "ssh" / "authorized_keys").read_text() == ""

    def disconnects(spec: WorkerSpec) -> WorkerHandle:
        update_links(paths, lambda book: book.links.clear())
        return ensure_worker(spec)

    manager.calls.clear()
    monkeypatch.setattr(manager, "ensure_worker", disconnects)
    with pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)
    assert refused.value.status_code == 404
    assert manager.calls == ["eligibility", "ensure_worker"]


@pytest.mark.asyncio
async def test_a_key_removed_while_connect_writes_is_not_left_in_the_worker(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect that has read the keys writes them before a revocation's refresh reads and writes, never after.

    Connect is held where it has read the keys and not yet written them, and the revocation starts then. The
    revocation must find the store's lock taken, or it would finish first and connect would write the removed key
    last. Every step waits for the one before it, so no outcome here depends on how fast a thread runs; the
    timeouts only end a run that hangs.
    """
    link = _enrolled()
    _store(paths, link)
    update_links(paths, lambda book: set_keys(book, ALICE, [PUBLIC_KEY], now=NOW))
    _wire_connect(monkeypatch, tmp_path, link)
    write_projection = links.write_projection
    connect_holds_keys, release_connect = threading.Event(), threading.Event()
    # The revocation is settled once it has met a taken lock, where it waits, or has finished.
    met_taken_lock, revocation_settled = threading.Event(), threading.Event()
    writes: list[str] = []

    def revoke() -> None:
        try:
            update_links(paths, lambda book: set_keys(book, ALICE, [], now=NOW + 1))
            refresh_projections(config, paths, ALICE)
        finally:
            revocation_settled.set()

    revocation = threading.Thread(target=revoke)

    def watched_lock(lock_path: Path) -> AbstractContextManager[None]:
        if threading.current_thread() is revocation:
            free = try_exclusive_file_lock(lock_path)
            if free is None:
                met_taken_lock.set()
                revocation_settled.set()
            else:
                release_file_lock(free)
        return advisory_file_lock(lock_path)

    def held_write(projection_dir: Path, link: Link, key_lines: list[str]) -> None:
        writer = "revocation" if threading.current_thread() is revocation else "connect"
        if writer == "connect":
            connect_holds_keys.set()
            assert release_connect.wait(30)
        write_projection(projection_dir, link, key_lines)
        writes.append(writer)

    monkeypatch.setattr(store, "advisory_file_lock", watched_lock)
    monkeypatch.setattr(links, "write_projection", held_write)
    connecting = asyncio.create_task(_connect(config, paths, link.id))
    try:
        assert await asyncio.to_thread(connect_holds_keys.wait, 30)
        revocation.start()
        assert await asyncio.to_thread(revocation_settled.wait, 30)
        waits_for_connect = met_taken_lock.is_set() and revocation.is_alive() and writes == []
    finally:
        release_connect.set()
    await connecting
    await asyncio.to_thread(revocation.join, 30)

    assert read_links(paths).keys == {}
    assert (tmp_path / "ssh" / "authorized_keys").read_text() == ""
    assert writes == ["connect", "revocation"]
    assert waits_for_connect


@pytest.mark.asyncio
async def test_connect_refuses_when_sshds_files_cannot_be_written(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ready worker whose SSH folder the primary cannot write is a refusal, and its runner is not asked."""
    link = _enrolled()
    _store(paths, link)
    manager = _wire_connect(monkeypatch, tmp_path, link)
    (tmp_path / "blocked").write_text("")
    manager.handles = [_handle(link.worker_key, tmp_path / "blocked" / "ssh")]

    with capture_logs() as logs, pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 503
    assert str(tmp_path) not in refused.value.detail
    assert manager.calls == ["eligibility", "ensure_worker", "projection"]
    assert [(log["event"], log["link_id"]) for log in logs] == [
        ("tailnet_ssh_projection_not_written", link.id),
        ("tailnet_ssh_connect_refused", link.id),
    ]


@pytest.mark.asyncio
async def test_connect_logs_peer_but_never_a_login_url(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One line says who connected to which link, and one who was refused and how; no line holds the login URL.

    The lines are read from structlog, which this suite does not pass on to ``caplog``.
    """
    link = _enrolled()
    _store(paths, link)
    _wire_connect(monkeypatch, tmp_path, link)
    status = GatewayStatus()

    with capture_logs() as logs:
        _post(paths, status, {link.id: _report(login_url=LOGIN_URL)})
        assert status.report.links[link.id].login_url == LOGIN_URL
        _rows(config, paths, status)
        await _connect(config, paths, link.id)
        with pytest.raises(ConnectRefused):
            await _connect(config, paths, link.id, "eve@github")

    assert logs == [
        {
            "event": "tailnet_ssh_connect",
            "log_level": "info",
            "link_id": link.id,
            "peer_login": "alice@github",
            "peer_node": "alice-laptop",
        },
        {
            "event": "tailnet_ssh_connect_refused",
            "log_level": "info",
            "link_id": link.id,
            "peer_login": "eve@github",
            "peer_node": "alice-laptop",
            "status_code": 403,
        },
    ]


@pytest.mark.asyncio
async def test_a_connect_waits_for_its_worker_on_a_pool_of_its_own(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker that takes minutes to start holds a connect thread, never one of the pool the primary shares."""
    link = _enrolled()
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


@pytest.mark.parametrize(
    ("answer", "detail"),
    [
        (web.json_response({"detail": "The worker image has no SSH server."}, status=503), None),
        (
            web.json_response({"detail": "This shell has no workspace for an SSH session to start in."}, status=400),
            None,
        ),
        (web.Response(status=502, text="<html>bad gateway</html>"), "The worker's SSH server did not start."),
        (web.json_response({"port": 2222}), ""),
    ],
)
@pytest.mark.asyncio
async def test_runner_503_becomes_connect_refused_with_its_detail(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: web.Response,
    detail: str | None,
) -> None:
    """What the runner says when it cannot start sshd is what the card shows; the runner gets a shell call's request."""
    received: list[tuple[str | None, object]] = []

    async def start(request: web.Request) -> web.Response:
        received.append((request.headers.get("x-mindroom-sandbox-token"), await request.json()))
        return answer

    app = web.Application()
    app.router.add_post("/api/sandbox-runner/ssh/start", start)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        link = _enrolled()
        _store(paths, link)
        call = _worker_call(link.worker_key, f"http://127.0.0.1:{port}/api/sandbox-runner")
        start_sshd = links._start_sshd
        _wire_connect(monkeypatch, tmp_path, link, call=call)
        monkeypatch.setattr(links, "_start_sshd", start_sshd)

        if detail == "":
            assert await _connect(config, paths, link.id) == ("10.0.0.5", 2222)
        else:
            with pytest.raises(ConnectRefused) as refused:
                await _connect(config, paths, link.id)
            assert isinstance(answer.body, bytes)
            expected = detail or json.loads(answer.body)["detail"]
            assert (refused.value.status_code, refused.value.detail) == (503, expected)
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
    link = _enrolled()
    _store(paths, link)
    start_sshd = links._start_sshd
    _wire_connect(monkeypatch, tmp_path, link, call=_worker_call(link.worker_key, "http://127.0.0.1:1/api"))
    monkeypatch.setattr(links, "_start_sshd", start_sshd)

    with pytest.raises(ConnectRefused) as refused:
        await _connect(config, paths, link.id)

    assert refused.value.status_code == 503
    assert "127.0.0.1" not in refused.value.detail


def test_active_worker_keys_window(paths: RuntimePaths) -> None:
    """A worker counts as used while the gateway's last report, at most 90 seconds old, shows an open connection."""
    busy, quiet = _enrolled(), _enrolled(agent="mind")
    _store(paths, busy, quiet)
    status = GatewayStatus()
    assert active_worker_keys(status, monotonic=5.0) == frozenset()

    _post(paths, status, {busy.id: _report(open_connections=1), quiet.id: _report()}, monotonic=1000.0)

    assert active_worker_keys(status, monotonic=1089.0) == frozenset({busy.worker_key})
    assert active_worker_keys(status, monotonic=1091.0) == frozenset()


class _ChangingStatus:
    """A status whose report is replaced, as a gateway's post replaces it, after each time it is read."""

    def __init__(self, *reports: GatewayReport) -> None:
        self.reports = list(reports)
        self.reads = 0
        self.operator_sync_at = NOW

    @property
    def report(self) -> GatewayReport:
        self.reads += 1
        return self.reports[min(self.reads, len(self.reports)) - 1]


def test_a_report_is_published_whole_and_never_changed(paths: RuntimePaths) -> None:
    """A post replaces the last report in one step, so no reader finds the links of one report with the age of another."""
    link = _enrolled()
    _store(paths, link)
    assigned: list[str] = []

    class Recorded(GatewayStatus):
        def __setattr__(self, name: str, value: object) -> None:
            assigned.append(name)
            super().__setattr__(name, value)

    status = Recorded()
    _post(paths, status, {link.id: _report(open_connections=1)}, monotonic=10.0)
    first, kept = status.report, deepcopy(status.report)
    assigned.clear()

    _post(paths, status, {link.id: _report()}, enrollment="auth_key", monotonic=20.0)

    assert assigned == ["report"]
    assert first == kept
    assert status.report == GatewayReport(20.0, {link.id: replace(kept.links[link.id], open_connections=0)})
    with pytest.raises(FrozenInstanceError):
        status.report.received_at = 30.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        status.report.links[link.id].open_connections = 1  # type: ignore[misc]


def test_active_worker_keys_reads_one_report() -> None:
    """A report that comes in while the workers in use are read does not lend its age to the last one's links."""
    held = {"abc": LinkStatus("running", None, None, None, None, None, 1, None, "worker")}
    status = _ChangingStatus(GatewayReport(1000.0, {}), GatewayReport(0.0, held))

    keys = active_worker_keys(cast("GatewayStatus", status), monotonic=1001.0)

    assert (keys, status.reads) == (frozenset(), 1)


def test_key_change_rewrites_projection_of_running_workers_only(
    config: Config,
    paths: RuntimePaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """New keys reach every worker of the person that serves SSH now; a stopped one gets them at its next connect."""
    running, stopped, bobs = _enrolled(), _enrolled(agent="mind"), _enrolled(BOB, "mind")
    _store(paths, running, stopped, bobs)
    update_links(paths, lambda book: set_keys(book, ALICE, [PUBLIC_KEY], now=NOW))
    _manager(
        monkeypatch,
        _handle(running.worker_key, tmp_path / "running", status="idle"),
        _handle(stopped.worker_key, None),
        _handle(bobs.worker_key, tmp_path / "bob"),
    )

    refresh_projections(config, paths, ALICE)

    assert (tmp_path / "running" / "authorized_keys").read_text() == f"restrict,pty {PUBLIC_KEY}\n"
    assert (tmp_path / "running" / "ssh_host_ed25519_key").read_text() == running.host_key
    assert sorted(path.name for path in tmp_path.iterdir() if path.is_dir()) == ["mindroom_data", "running"]
