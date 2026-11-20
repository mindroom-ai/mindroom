"""Tests for the Connections portal's tailnet SSH routes: keys, links, and what each person sees of them."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal, Self

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.api import config_lifecycle, main
from mindroom.api import tailnet_ssh as tailnet_ssh_api
from mindroom.api.connection_agents import resolve_connection_agent, resolve_connection_user
from mindroom.shared_directories.store import add_shared_directory_member, create_shared_directory
from mindroom.tailnet_ssh import links
from mindroom.tailnet_ssh.links import GatewayReport, GatewayStatus, LinkReport, LinkStatus, StatusReport
from mindroom.tailnet_ssh.store import Link, new_link, read_links, update_links
from mindroom.workers.models import WorkerHandle, WorkerSpec, WorkerSshEndpoint
from tests.api.conftest import api_snapshot, publish_api_payload, trusted_upstream_env
from tests.api.test_egress_credentials_api import _signed_headers, _trust_signed_users
from tests.api.test_github_requests import connect as connect_github
from tests.api.test_oauth_api import _runtime_paths
from tests.runtime_fakes import bind_test_embedded_runtime
from tests.test_tailnet_ssh_links import _worker_call

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tailnet_ssh.store import LinkBook

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
LOGIN_URL = "https://login.tailscale.com/a/1c0ffee"
BASE = "/api/connections/tailnet-ssh"


def _user_agent(display_name: str, *users: str, **extra: object) -> dict[str, object]:
    """An agent with one worker per person and agent that only ``users`` reach."""
    return {
        "display_name": display_name,
        "role": "Test agent",
        "worker_scope": "user_agent",
        "access": {"users": list(users), "members_of_rooms": []},
        **extra,
    }


def _payload(tmp_path: Path) -> dict[str, Any]:
    return {
        "models": {"default": {"provider": "ollama", "id": "test-model"}},
        "knowledge_bases": {"allpurpose": {"path": str(tmp_path / "kb")}},
        "agents": {
            "mind": _user_agent("Mind", ALICE, tailnet_ssh=True, knowledge_bases=["allpurpose"]),
            "team_mind": {
                "display_name": "Team Mind",
                "role": "Private per person and agent",
                "private": {"per": "user_agent"},
                "access": {"users": [ALICE, BOB]},
                "tailnet_ssh": True,
            },
            "bob_mind": _user_agent("Bob's Mind", BOB, tailnet_ssh=True),
            "plain": _user_agent("Plain", ALICE),
            "researcher": _user_agent("Researcher", ALICE, tailnet_ssh=True),
            "other": {"display_name": "Other", "role": "Owns a shared directory", "access": {"users": [ALICE]}},
        },
    }


class _Manager:
    """A worker manager, and its own lease, that lists ``handles``."""

    def __init__(self) -> None:
        self.handles: list[WorkerHandle] = []
        self.listed = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def ensure_worker(self, spec: WorkerSpec) -> WorkerHandle:
        return next(handle for handle in self.handles if handle.worker_key == spec.worker_key)

    def list_workers(self) -> list[WorkerHandle]:
        self.listed += 1
        return list(self.handles)


@dataclass
class _Portal:
    client: TestClient
    headers: dict[str, dict[str, str]]
    paths: RuntimePaths
    status: GatewayStatus
    manager: _Manager
    tmp_path: Path

    def get(self, user: str = "alice") -> dict[str, Any]:
        response = self.client.get(BASE, headers=self.headers[user])
        assert response.status_code == 200, response.text
        return response.json()

    def agent(self, name: str, user: str = "alice") -> dict[str, Any]:
        return next(agent for agent in self.get(user)["agents"] if agent["name"] == name)

    def put_keys(self, *keys: str, user: str = "alice") -> httpx.Response:
        return self.client.put(f"{BASE}/keys", json={"keys": list(keys)}, headers=self.headers[user])

    def connect(self, agent: str, hostname: str, user: str = "alice") -> httpx.Response:
        return self.client.put(f"{BASE}/agents/{agent}", json={"hostname": hostname}, headers=self.headers[user])

    def link(self, requester_id: str, agent: str) -> Link:
        link = read_links(self.paths).link(requester_id, agent)
        assert link is not None
        return link

    def change_link(self, requester_id: str, agent: str, **fields: object) -> None:
        def change(book: LinkBook) -> None:
            link = book.link(requester_id, agent)
            assert link is not None
            for name, value in fields.items():
                setattr(link, name, value)

        update_links(self.paths, change)

    def report(
        self,
        link_id: str,
        *,
        enrollment: Literal["login", "auth_key"] = "login",
        age: float = 0.0,
        **fields: object,
    ) -> None:
        """Have the gateway report one link's node ``age`` seconds ago, by default running and enrolled."""
        report: dict[str, object] = {
            "state": "running",
            "login_url": None,
            "tailnet": "tail1234.ts.net",
            "dns_name": "mind.tail1234.ts.net",
            "login_name": "alice@github",
            "key_expiry": None,
            "open_connections": 0,
            "last_error": None,
            "worker_key": self.link_by_id(link_id).worker_key,
        }
        status = LinkStatus(**(report | fields))  # type: ignore[arg-type]
        self.status.report = GatewayReport(time.monotonic() - age, {link_id: status})

        def store(book: LinkBook) -> None:
            book.enrollment = enrollment

        update_links(self.paths, store)

    def link_by_id(self, link_id: str) -> Link:
        link = read_links(self.paths).by_id(link_id)
        assert link is not None
        return link


@pytest.fixture
def portal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enforce_turn_authorization: None) -> _Portal:  # noqa: ARG001
    """Serve the real API with tailnet SSH on, signed users Alice and Bob, and a worker backend that lists workers."""
    key = _trust_signed_users(monkeypatch)
    env = {
        **trusted_upstream_env(),
        "MINDROOM_PUBLIC_URL": "https://portal.example.org",
        "MINDROOM_WORKER_BACKEND": "docker",
    }
    paths = _runtime_paths(tmp_path, env)
    main.initialize_api_app(main.app, paths)
    bind_test_embedded_runtime(main.app)
    publish_api_payload(main.app, _payload(tmp_path))
    status = GatewayStatus()
    monkeypatch.setattr(config_lifecycle.app_state(main.app), "tailnet_ssh_status", status)
    manager = _Manager()
    monkeypatch.setattr(links, "lease_configured_primary_worker_manager", lambda _paths: manager)
    return _Portal(
        TestClient(main.app, base_url="https://portal.example.org"),
        {name: _signed_headers(key, name) for name in ("alice", "bob")},
        paths,
        status,
        manager,
        tmp_path,
    )


def _public_key(comment: str = "") -> str:
    key = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.OpenSSH, PublicFormat.OpenSSH).decode()
    return f"{key} {comment}".strip()


def _handle(worker_key: str, *, status: str = "ready", ssh_dir: Path | None = None) -> WorkerHandle:
    return WorkerHandle(
        worker_id=f"worker-{worker_key}",
        worker_key=worker_key,
        endpoint="http://10.0.0.5:8766/api/sandbox-runner/execute",
        auth_token="runner-token",  # noqa: S106
        status=status,  # type: ignore[arg-type]
        backend_name="docker",
        last_used_at=time.time(),
        created_at=time.time(),
        ssh_endpoint=None if ssh_dir is None else WorkerSshEndpoint("10.0.0.5", 2222, ssh_dir),
    )


def _connected(portal: _Portal, agent: str = "mind", hostname: str = "mind", user: str = "alice") -> Link:
    """Give the person a key and a link to ``agent``, and return the link."""
    assert portal.put_keys(_public_key("laptop"), user=user).status_code == 204
    response = portal.connect(agent, hostname, user=user)
    assert response.status_code == 204, response.text
    return portal.link(f"@{user}:example.org", agent)


def _mutations(agent: str = "mind") -> list[tuple[str, str, dict[str, object] | None]]:
    return [
        ("PUT", f"{BASE}/keys", {"keys": [_public_key()]}),
        ("POST", f"{BASE}/keys/github", None),
        ("PUT", f"{BASE}/agents/{agent}", {"hostname": "mind"}),
        ("POST", f"{BASE}/agents/{agent}/login", None),
        ("DELETE", f"{BASE}/agents/{agent}", None),
    ]


def test_feature_off_reports_disabled_and_mutations_404(portal: _Portal, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the gateway's token there is no status object: the card is told so, and nothing can be changed."""
    _connected(portal)
    monkeypatch.setattr(config_lifecycle.app_state(main.app), "tailnet_ssh_status", None)
    book = read_links(portal.paths).model_dump()

    assert portal.get() == {"enabled": False, "enrollment": None, "keys": [], "github_login": None, "agents": []}
    for method, path, body in _mutations():
        response = portal.client.request(method, path, json=body, headers=portal.headers["alice"])
        assert response.status_code == 404, (method, path)
    assert read_links(portal.paths).model_dump() == book


def test_listing_shows_only_my_tailnet_ssh_agents(portal: _Portal) -> None:
    """Alice sees her agents that opt in to SSH, with what a shell there can read, and nobody else's."""
    create_shared_directory(portal.paths, owner="mind", name="notes")
    listing = portal.get()

    assert listing["enabled"] is True
    assert listing["enrollment"] is None
    assert listing["keys"] == []
    assert listing["github_login"] is None
    assert [agent["name"] for agent in listing["agents"]] == ["mind", "team_mind", "researcher"]
    assert listing["agents"][0] == {
        "name": "mind",
        "display_name": "Mind",
        "eligible": True,
        "reason": None,
        "detail": None,
        "default_hostname": "mind",
        "knowledge_bases": ["allpurpose"],
        "shared_directories": ["mind/notes"],
        "link": None,
    }
    assert listing["agents"][1]["default_hostname"] == "team-mind"
    assert [agent["name"] for agent in portal.get("bob")["agents"]] == ["team_mind", "bob_mind"]


def test_another_persons_link_is_invisible(portal: _Portal) -> None:
    """Alice's link to an agent both use is not Bob's: he sees no link and cannot disconnect hers."""
    link = _connected(portal, "team_mind", "team-mind")

    assert portal.agent("team_mind")["link"] is not None
    assert portal.agent("team_mind", "bob")["link"] is None
    assert portal.client.delete(f"{BASE}/agents/team_mind", headers=portal.headers["bob"]).status_code == 404
    assert portal.client.post(f"{BASE}/agents/team_mind/login", headers=portal.headers["bob"]).status_code == 404
    assert portal.link(ALICE, "team_mind") == link
    assert portal.get("bob")["keys"] == []


def test_login_url_goes_only_to_the_owner(portal: _Portal) -> None:
    """The gateway's sign-in URL reaches the link's owner only, in the listing only."""
    link = _connected(portal, "team_mind", "team-mind")
    portal.report(link.id, state="needs_login", login_url=LOGIN_URL, tailnet=None, dns_name=None, login_name=None)

    assert portal.agent("team_mind")["link"]["state"] == "pending_login"
    assert portal.agent("team_mind")["link"]["login_url"] == LOGIN_URL
    bob = portal.client.get(BASE, headers=portal.headers["bob"])
    assert bob.status_code == 200
    assert LOGIN_URL not in bob.text
    for method, path, body in _mutations("team_mind"):
        response = portal.client.request(method, path, json=body, headers=portal.headers["alice"])
        assert LOGIN_URL not in response.text, (method, path)


def test_ineligible_agent_reports_reason_and_detail(portal: _Portal) -> None:
    """An agent that joined another agent's shared directory names that directory."""
    create_shared_directory(portal.paths, owner="other", name="specs")
    add_shared_directory_member(portal.paths, owner="other", name="specs", member="researcher")

    agent = portal.agent("researcher")

    assert (agent["eligible"], agent["reason"], agent["detail"]) == (False, "joined_directory", "other/specs")
    assert portal.agent("mind")["eligible"] is True


def test_put_keys_normalizes_and_replaces(portal: _Portal) -> None:
    """Pasted lines are trimmed, split, and kept once by type and base64; a later PUT replaces them all."""
    first, second, third = _public_key("laptop"), _public_key("phone"), _public_key()
    renamed = first.rsplit(" ", 1)[0] + " renamed"

    assert portal.put_keys(f"  {first}  \n\n{renamed}", second).status_code == 204
    assert portal.get()["keys"] == [first, second]
    assert portal.put_keys(third).status_code == 204
    assert portal.get()["keys"] == [third]
    assert portal.put_keys().status_code == 204
    assert portal.get()["keys"] == []
    assert portal.put_keys(*(_public_key() for _ in range(21))).status_code == 422


def test_put_keys_refuses_private_key_with_a_sentence(portal: _Portal) -> None:
    """A pasted private key is refused with a sentence that does not repeat it, and nothing is stored."""
    kept = _public_key("laptop")
    assert portal.put_keys(kept).status_code == 204
    private = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"

    response = portal.put_keys(private)

    assert response.status_code == 422
    assert "private key" in response.json()["detail"]
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in response.text
    assert portal.get()["keys"] == [kept]


def test_removing_a_key_rewrites_projection_and_sets_closed_before(portal: _Portal) -> None:
    """Removing a key closes the person's sessions and takes it out of the files a running sshd reads."""
    kept, removed = _public_key("laptop"), _public_key("phone")
    assert portal.put_keys(kept, removed).status_code == 204
    link = _connected(portal)
    ssh_dir = portal.tmp_path / "worker-ssh"
    portal.manager.handles = [_handle(link.worker_key, ssh_dir=ssh_dir)]
    portal.change_link(ALICE, "mind", closed_before=0.0)

    assert portal.put_keys(kept).status_code == 204

    assert portal.link(ALICE, "mind").closed_before > 0.0
    assert (ssh_dir / "authorized_keys").read_text() == f"restrict,pty {kept}\n"


def test_a_failed_refresh_is_reported_and_the_next_connect_writes_the_keys(
    portal: _Portal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keys whose workers' files cannot be rewritten are stored and the failure answered; a connect writes them first.

    So a running sshd that still holds a removed key never gets a connection before its files hold the stored keys.
    """
    kept, removed = _public_key("laptop"), _public_key("phone")
    assert portal.put_keys(kept, removed).status_code == 204
    link = _connected(portal)
    portal.change_link(ALICE, "mind", enrolled_login="alice@github", enrolled_at=time.time())
    ssh_dir = portal.tmp_path / "worker-ssh"
    portal.manager.handles = [_handle(link.worker_key, ssh_dir=ssh_dir)]
    assert portal.put_keys(kept, removed).status_code == 204
    assert removed in (ssh_dir / "authorized_keys").read_text()
    write_projection = links.write_projection

    def unwritable(*_args: object) -> None:
        raise PermissionError(13, "Permission denied", "/primary/path/to/worker")

    monkeypatch.setattr(links, "write_projection", unwritable)

    response = portal.put_keys(kept)

    assert (response.status_code, response.json()["detail"]) == (503, tailnet_ssh_api._NOT_REFRESHED)
    assert "/primary/path" not in response.text
    assert portal.get()["keys"] == [kept]
    assert removed in (ssh_dir / "authorized_keys").read_text()

    served: list[str] = []

    async def start_sshd(_call: object) -> None:
        served.append((ssh_dir / "authorized_keys").read_text())

    monkeypatch.setattr(links, "write_projection", write_projection)
    monkeypatch.setattr(links, "agent_terminals", lambda *_args, **_kwargs: SimpleNamespace(stand_ins=object()))
    monkeypatch.setattr(links, "worker_call", lambda *_args, **_kwargs: _worker_call(link.worker_key))
    monkeypatch.setattr(links, "_start_sshd", start_sshd)
    config = api_snapshot(main.app).runtime_config
    assert config is not None
    memberships = config_lifecycle.app_state(main.app).runtime.agent_reply_memberships

    asyncio.run(links.connect(config, portal.paths, memberships, link.id, "alice@github", "laptop"))

    assert served == [f"restrict,pty {kept}\n"]


def test_github_import_adds_keys(portal: _Portal, monkeypatch: pytest.MonkeyPatch) -> None:
    """The keys GitHub publishes for the person's connected account are added to theirs."""
    assert portal.client.post(f"{BASE}/keys/github", headers=portal.headers["alice"]).json() == {
        "detail": "Connect a GitHub account first.",
    }
    existing, first, second = _public_key("laptop"), _public_key("gh1"), _public_key("gh2")
    assert portal.put_keys(existing).status_code == 204
    connect_github(portal.paths, ALICE, login="alice-gh")
    asked: list[str] = []

    async def github_keys(login: str) -> list[str]:
        asked.append(login)
        return [existing, first, second]

    monkeypatch.setattr(tailnet_ssh_api, "_fetch_github_keys", github_keys)

    response = portal.client.post(f"{BASE}/keys/github", headers=portal.headers["alice"])

    assert (response.status_code, response.json()) == (200, {"added": 2})
    assert asked == ["alice-gh"]
    listing = portal.get()
    assert listing["keys"] == [existing, first, second]
    assert listing["github_login"] == "alice-gh"
    assert portal.client.post(f"{BASE}/keys/github", headers=portal.headers["bob"]).status_code == 409


def test_github_import_without_a_verified_login_is_refused(portal: _Portal) -> None:
    """A GitHub connection without a verified login names no account whose keys could be read."""
    connect_github(portal.paths, ALICE, login=None)

    response = portal.client.post(f"{BASE}/keys/github", headers=portal.headers["alice"])

    assert (response.status_code, response.json()["detail"]) == (409, "Connect a GitHub account first.")


def _github(monkeypatch: pytest.MonkeyPatch, handler: Any) -> list[httpx.Request]:  # noqa: ANN401
    """Answer the import's GitHub request with ``handler``, and return the requests sent."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    def client(**kwargs: Any) -> httpx.AsyncClient:  # noqa: ANN401
        assert kwargs == {"follow_redirects": False, "timeout": 10}
        return real_client(transport=httpx.MockTransport(answer), **kwargs)

    monkeypatch.setattr(tailnet_ssh_api.httpx, "AsyncClient", client)
    return requests


@pytest.mark.asyncio
async def test_github_keys_keep_only_public_key_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Of GitHub's answer only the lines that are public keys are kept, and at most 64 KiB of it is read."""
    first, second = _public_key(), _public_key()
    body = f"{first}\nnot a key\n{second}\n".encode() + b"#" * (70 * 1024) + f"\n{_public_key()}\n".encode()
    requests = _github(monkeypatch, lambda _request: httpx.Response(200, stream=_CountedStream([body])))

    assert await tailnet_ssh_api._fetch_github_keys("alice-gh") == [first, second]
    assert [str(request.url) for request in requests] == ["https://github.com/alice-gh.keys"]
    assert requests[0].headers["Accept-Encoding"] == "identity"


class _CountedStream(httpx.AsyncByteStream):
    """A response body of ``chunks`` that counts the bytes taken from it."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.taken = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.taken += len(chunk)
            yield chunk


@pytest.mark.asyncio
async def test_github_keys_stop_reading_at_64_kib(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading GitHub's answer stops at 64 KiB: the rest of a longer answer is never taken from the connection."""
    first = _public_key()
    stream = _CountedStream([f"{first}\n".encode(), *([b"#" * 4096] * 100)])
    _github(monkeypatch, lambda _request: httpx.Response(200, stream=stream))

    assert await tailnet_ssh_api._fetch_github_keys("alice-gh") == [first]
    assert stream.taken <= 64 * 1024 + 4096


@pytest.mark.asyncio
async def test_github_keys_refuse_an_encoded_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """An answer compressed against the request's ``identity`` is a 502, never decompressed."""
    answer = httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b"\x1f\x8b")
    _github(monkeypatch, lambda _request: answer)

    with pytest.raises(HTTPException) as raised:
        await tailnet_ssh_api._fetch_github_keys("alice-gh")

    assert raised.value.status_code == 502


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(404),
        httpx.Response(302, headers={"Location": "https://example.org/keys"}),
        httpx.Response(500),
    ],
)
@pytest.mark.asyncio
async def test_github_keys_fail_as_502(monkeypatch: pytest.MonkeyPatch, answer: httpx.Response) -> None:
    """A GitHub answer other than 200, a redirect too, is a 502, and no redirect is followed."""
    requests = _github(monkeypatch, lambda _request: answer)

    with pytest.raises(HTTPException) as raised:
        await tailnet_ssh_api._fetch_github_keys("alice-gh")

    assert raised.value.status_code == 502
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_github_keys_unreachable_is_502(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub not answering is a 502."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        msg = "no route"
        raise httpx.ConnectError(msg, request=request)

    _github(monkeypatch, unreachable)

    with pytest.raises(HTTPException) as raised:
        await tailnet_ssh_api._fetch_github_keys("alice-gh")

    assert raised.value.status_code == 502


def test_connect_creates_link_with_session_worker_key(portal: _Portal) -> None:
    """The link's person and worker come from the session: the worker Alice's turns of the agent run in."""
    assert portal.put_keys(_public_key()).status_code == 204
    smuggled = {"hostname": "mind", "worker_key": "v1:x:user_agent:@bob:example.org:mind", "requester_id": BOB}
    refused = portal.client.put(f"{BASE}/agents/mind", json=smuggled, headers=portal.headers["alice"])
    assert refused.status_code == 422
    assert read_links(portal.paths).links == []

    assert portal.connect("mind", "mind-alice").status_code == 204

    link = portal.link(ALICE, "mind")
    snapshot = api_snapshot(main.app)
    memberships = config_lifecycle.app_state(main.app).runtime.agent_reply_memberships
    user = resolve_connection_user(snapshot, ALICE, membership_index=memberships)
    assert snapshot.runtime_config is not None
    session_key = resolve_connection_agent(user, "mind").worker_target.worker_key
    assert link.worker_key == session_key
    assert link.worker_key == links.resolve_target(snapshot.runtime_config, portal.paths, ALICE, "mind").worker_key
    assert (link.requester_id, link.hostname, link.desired) == (ALICE, "mind-alice", "on")
    assert portal.agent("mind")["link"] == {
        "hostname": "mind-alice",
        "state": "pending_login",
        "login_url": None,
        "tailnet": None,
        "dns_name": None,
        "login_name": None,
        "key_expiry": None,
        "worker": None,
        "host_public_key": link.host_public_key,
        "last_error": None,
    }


@pytest.mark.parametrize("hostname", ["Mind", "-a", "a_b", "a" * 64, ""])
def test_connect_refuses_bad_hostnames(portal: _Portal, hostname: str) -> None:
    """A hostname must be one lowercase DNS label."""
    assert portal.put_keys(_public_key()).status_code == 204

    assert portal.connect("mind", hostname).status_code == 422
    assert read_links(portal.paths).links == []


def test_connect_needs_a_key_and_eligibility(portal: _Portal) -> None:
    """Without a key, or for an agent that cannot serve SSH, there is no link; an agent Alice may not use is a 404."""
    no_key = portal.connect("mind", "mind")
    assert (no_key.status_code, no_key.json()["detail"]) == (409, "Add an SSH public key first.")
    assert portal.put_keys(_public_key()).status_code == 204

    not_enabled = portal.connect("plain", "plain")

    assert (not_enabled.status_code, not_enabled.json()["detail"]) == (409, "not_enabled")
    assert portal.connect("bob_mind", "bob-mind").status_code == 404
    assert portal.connect("nobody", "nobody").status_code == 404
    assert read_links(portal.paths).links == []


def test_connect_counts_an_operator_key(portal: _Portal) -> None:
    """A key the operator configured for the person is a key too."""
    payload = _payload(portal.tmp_path) | {"tailnet_ssh": {"authorized_keys": {ALICE: [_public_key()]}}}
    publish_api_payload(main.app, payload)

    assert portal.connect("mind", "mind").status_code == 204


def test_connect_turns_an_off_link_back_on(portal: _Portal) -> None:
    """A parked link comes back on as new, so the day without a sign-in starts again; then its hostname is fixed."""
    link = _connected(portal)
    portal.change_link(ALICE, "mind", desired="off", created_at=1.0, login_after=1.0)
    assert portal.agent("mind")["link"] is None
    before = time.time()

    assert portal.connect("mind", "mind-again").status_code == 204

    back = portal.link(ALICE, "mind")
    assert (back.id, back.host_key, back.desired, back.hostname) == (link.id, link.host_key, "on", "mind-again")
    assert back.created_at >= before
    assert back.login_after >= before
    assert portal.connect("mind", "mind-again").status_code == 204
    renamed = portal.connect("mind", "other-name")
    assert (renamed.status_code, renamed.json()["detail"]) == (409, "Disconnect first to change the hostname.")
    assert portal.client.delete(f"{BASE}/agents/mind", headers=portal.headers["alice"]).status_code == 204
    removing = portal.connect("mind", "mind-again")
    assert (removing.status_code, removing.json()["detail"]) == (
        409,
        "Still disconnecting; try again in a few seconds.",
    )


def test_login_moves_login_after(portal: _Portal) -> None:
    """Asking for a fresh sign-in URL moves the link's ``login_after`` to now."""
    assert portal.client.post(f"{BASE}/agents/mind/login", headers=portal.headers["alice"]).status_code == 404
    _connected(portal)
    portal.change_link(ALICE, "mind", login_after=1.0)
    before = time.time()

    assert portal.client.post(f"{BASE}/agents/mind/login", headers=portal.headers["alice"]).status_code == 204

    assert portal.link(ALICE, "mind").login_after >= before
    portal.change_link(ALICE, "mind", desired="off")
    assert portal.client.post(f"{BASE}/agents/mind/login", headers=portal.headers["alice"]).status_code == 404


def test_disconnect_sets_removing_and_parks_in_operator_mode(portal: _Portal) -> None:
    """Disconnect removes the link; with an operator key it is parked, so the gateway does not add it again."""
    assert portal.client.delete(f"{BASE}/agents/mind", headers=portal.headers["alice"]).status_code == 404
    _connected(portal)
    portal.change_link(ALICE, "mind", closed_before=0.0)

    assert portal.client.delete(f"{BASE}/agents/mind", headers=portal.headers["alice"]).status_code == 204

    removed = portal.link(ALICE, "mind")
    assert (removed.desired, removed.park_when_removed) == ("removing", False)
    assert removed.closed_before > 0.0
    assert portal.agent("mind")["link"]["state"] == "removing"

    operator = _connected(portal, "team_mind", "team-mind")
    portal.report(operator.id, enrollment="auth_key")

    assert portal.client.delete(f"{BASE}/agents/team_mind", headers=portal.headers["alice"]).status_code == 204

    parked = portal.link(ALICE, "team_mind")
    assert (parked.desired, parked.park_when_removed) == ("removing", True)


def test_a_disconnect_right_after_a_restart_stays_disconnected(portal: _Portal, tmp_path: Path) -> None:
    """Disconnecting an operator key's link before the restarted primary hears from the gateway parks it all the same.

    Else the gateway logs the node out, the link is dropped, and the next operator reconcile adds it again.
    """
    link = _connected(portal, "team_mind", "team-mind")
    # Before the restart the gateway said it enrols with an operator key; the primary's status is fresh now.
    links.record_status(
        portal.paths,
        GatewayStatus(),
        StatusReport(enrollment="auth_key", links={link.id: LinkReport(state="running")}),
        now=time.time(),
        monotonic=time.monotonic(),
    )
    assert portal.status == GatewayStatus()

    assert portal.client.delete(f"{BASE}/agents/team_mind", headers=portal.headers["alice"]).status_code == 204

    assert (portal.link(ALICE, "team_mind").desired, portal.link(ALICE, "team_mind").park_when_removed) == (
        "removing",
        True,
    )
    removed = StatusReport(enrollment="auth_key", links={link.id: LinkReport(state="removed")})
    links.record_status(portal.paths, portal.status, removed, now=time.time(), monotonic=time.monotonic())
    portal.manager.handles = [_handle(link.worker_key, ssh_dir=tmp_path / "ssh")]
    config, paths = config_lifecycle.read_app_committed_runtime_config(main.app)
    memberships = AgentReplyMembershipIndex()
    rows = links.desired_links(
        config,
        paths,
        memberships,
        portal.status,
        now=time.time() + 61,
        monotonic=time.monotonic(),
    )

    assert rows == []
    assert [(stored.id, stored.desired) for stored in read_links(portal.paths).links] == [(link.id, "off")]
    assert portal.agent("team_mind")["link"] is None


def test_mutations_refuse_cross_origin_and_query_parameters(portal: _Portal) -> None:
    """A change from another origin, or with any query parameter, is refused before it changes anything."""
    _connected(portal)
    book = read_links(portal.paths).model_dump()
    cross_origin = portal.headers["alice"] | {"Origin": "https://evil.example.org"}

    for method, path, body in _mutations():
        foreign = portal.client.request(method, path, json=body, headers=cross_origin)
        queried = portal.client.request(method, f"{path}?worker_key=x", json=body, headers=portal.headers["alice"])
        assert (foreign.status_code, queried.status_code) == (403, 400), (method, path)
    assert portal.client.get(f"{BASE}?user=bob", headers=portal.headers["alice"]).status_code == 400
    assert read_links(portal.paths).model_dump() == book


def test_link_state_and_worker_are_reported(portal: _Portal) -> None:
    """An enrolled link shows the gateway's view of its node and whether its worker is up."""
    link = _connected(portal)
    portal.report(link.id)
    portal.manager.handles = [_handle(link.worker_key, status="starting")]

    shown = portal.agent("mind")["link"]

    assert (shown["state"], shown["worker"]) == ("enrolled", "waking")
    assert (shown["tailnet"], shown["dns_name"], shown["login_name"]) == (
        "tail1234.ts.net",
        "mind.tail1234.ts.net",
        "alice@github",
    )
    assert portal.get()["enrollment"] == "login"
    portal.manager.handles = [_handle(link.worker_key, status="ready")]
    assert portal.agent("mind")["link"]["worker"] == "ready"
    portal.manager.handles = []
    assert portal.agent("mind")["link"]["worker"] == "asleep"


def test_workers_are_not_listed_without_an_enrolled_link(portal: _Portal) -> None:
    """Only an enrolled link shows its worker, so a listing without one asks the backend nothing."""
    link = _connected(portal)
    portal.report(link.id, state="needs_login", login_url=LOGIN_URL)
    listed = portal.manager.listed

    assert portal.agent("mind")["link"]["worker"] is None
    assert portal.manager.listed == listed


def test_a_link_stays_on_the_card_when_its_agent_drops_tailnet_ssh(portal: _Portal) -> None:
    """The gateway keeps the node of a suspended link in the person's tailnet, so the card keeps it, to disconnect."""
    link = _connected(portal, "researcher", "researcher")
    payload = _payload(portal.tmp_path)
    payload["agents"]["researcher"].pop("tailnet_ssh")
    publish_api_payload(main.app, payload)

    shown = portal.agent("researcher")

    assert (shown["eligible"], shown["reason"], shown["link"]["state"]) == (False, "not_enabled", "suspended")
    assert shown["link"]["host_public_key"] == link.host_public_key
    assert portal.client.delete(f"{BASE}/agents/researcher", headers=portal.headers["alice"]).status_code == 204
    assert portal.agent("researcher")["link"]["state"] == "removing"
    portal.change_link(ALICE, "researcher", desired="off")
    assert "researcher" not in [agent["name"] for agent in portal.get()["agents"]]


def test_a_room_granted_link_waits_suspended_while_membership_is_pending(portal: _Portal) -> None:
    """Before the primary knows a room's members, the person's link to an agent that room grants is shown suspended."""
    payload = _payload(portal.tmp_path)
    payload["agents"]["research"] = {
        "display_name": "Research",
        "role": "Private per person and agent, for the lobby's members",
        "private": {"per": "user_agent"},
        "tailnet_ssh": True,
        "rooms": ["lobby"],
    }
    publish_api_payload(main.app, payload)
    config = api_snapshot(main.app).runtime_config
    assert config is not None
    now = time.time()
    worker_key = links.resolve_target(config, portal.paths, ALICE, "research").worker_key
    link = new_link(ALICE, "research", worker_key, "research", now=now - 60).model_copy(
        update={"enrolled_login": "alice@github", "enrolled_at": now - 30},
    )
    update_links(portal.paths, lambda book: book.links.append(link))

    shown = portal.agent("research")

    assert (shown["eligible"], shown["reason"], shown["link"]["state"]) == (False, "not_authorized", "suspended")
    assert portal.link(ALICE, "research") == link


def test_suspended_link_shows_no_login_url(portal: _Portal) -> None:
    """A link that is not eligible now is suspended, and its node's sign-in URL is not offered."""
    link = _connected(portal, "researcher", "researcher")
    portal.report(link.id, state="needs_login", login_url=LOGIN_URL)
    create_shared_directory(portal.paths, owner="other", name="specs")
    add_shared_directory_member(portal.paths, owner="other", name="specs", member="researcher")

    shown = portal.agent("researcher")["link"]

    assert (shown["state"], shown["login_url"]) == ("suspended", None)


def test_an_expired_report_is_not_shown(portal: _Portal) -> None:
    """A gateway report over 90 seconds old says nothing of a node: no sign-in URL, and no enrolled worker."""
    link = _connected(portal)
    portal.manager.handles = [_handle(link.worker_key)]
    portal.report(link.id, state="needs_login", login_url=LOGIN_URL, age=91.0)
    listed = portal.manager.listed

    assert LOGIN_URL not in portal.client.get(BASE, headers=portal.headers["alice"]).text
    portal.report(link.id, age=91.0)

    shown = portal.agent("mind")["link"]

    assert (shown["state"], shown["worker"], shown["dns_name"], shown["tailnet"]) == ("pending_login", None, None, None)
    assert portal.manager.listed == listed
    portal.report(link.id, age=89.0)
    assert portal.agent("mind")["link"]["state"] == "enrolled"
