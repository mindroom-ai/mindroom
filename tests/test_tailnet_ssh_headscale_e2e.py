"""Tailnet SSH against a real tailnet: Headscale, the gateway image, workers that run sshd, and Tailscale clients.

A fake of the three routes the gateway asks the primary stands in for MindRoom, so no model or Matrix server takes
part. The cases run only with ``MINDROOM_TAILNET_SSH_E2E=1`` and the two images, as the ``headscale-e2e`` CI job does.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import select
import shlex
import signal
import socket
import subprocess
import threading
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

import pytest
import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException

from mindroom.config.main import Config
from mindroom.tailnet_ssh import session
from mindroom.tailnet_ssh.projection import authorized_key_lines
from mindroom.tailnet_ssh.store import Link, LinkBook, new_link, set_keys
from mindroom.workers.backends.worker_security import docker_worker_security_options
from mindroom.workers.models import WORKER_SSH_PORT

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.timeout(1200),
    pytest.mark.skipif(
        os.environ.get("MINDROOM_TAILNET_SSH_E2E") != "1",
        reason="needs Docker, Headscale and the built images",
    ),
]

_GATEWAY_IMAGE = os.environ.get("MINDROOM_TAILNET_SSH_E2E_GATEWAY_IMAGE", "")
_WORKER_IMAGE = os.environ.get("MINDROOM_TAILNET_SSH_E2E_WORKER_IMAGE", "")
_HEADSCALE_IMAGE = "headscale/headscale:0.29.7"
# The release the gateway's tsnet is of (tailnet-gateway/go.mod).
_TAILSCALE_IMAGE = "tailscale/tailscale:v1.104.7"
_HEADSCALE_CONFIG = """\
server_url: http://127.0.0.1:{port}
listen_addr: 127.0.0.1:{port}
metrics_listen_addr: 127.0.0.1:{metrics_port}
grpc_listen_addr: 127.0.0.1:{grpc_port}
noise:
  private_key_path: /var/lib/headscale/noise_private.key
prefixes:
  v4: {v4}
  v6: {v6}
derp:
  urls:
    - https://controlplane.tailscale.com/derpmap/default
database:
  type: sqlite
  sqlite:
    path: /var/lib/headscale/db.sqlite
dns:
  magic_dns: true
  base_domain: {base_domain}
  override_local_dns: false
unix_socket: /var/run/headscale/headscale.sock
"""

# How the chart runs the gateway and the Docker backend a worker: nothing but the mounts is writable.
_READ_ONLY_ROOT = ("--read-only", "--tmpfs", "/tmp")  # noqa: S108
# Where the Docker backend mounts a worker's root, and in it the files the primary writes for sshd and a workspace.
_WORKER_ROOT = "/app/worker"
_PROJECTION = f"{_WORKER_ROOT}/.runtime/ssh"
_WORKSPACE = f"{_WORKER_ROOT}/workspace"
# Run in the worker image as uid 1000, the primary's uid and the worker's: write sshd's files for the link on stdin,
# as the primary does for a connect it passed.
_WRITE_PROJECTION = """
import json, sys
from pathlib import Path
from mindroom.tailnet_ssh.projection import write_projection
from mindroom.tailnet_ssh.store import Link
sent = json.load(sys.stdin)
write_projection(Path(sys.argv[1]), Link.model_validate(sent["link"]), sent["key_lines"])
"""
# The worker's command, which does what the runner's /ssh/start does: keep the session's launch, written here by hand,
# then run sshd on the files the primary wrote.
_SERVE = """
import os, sys
from pathlib import Path
from mindroom.api.worker_ssh import _sshd_argv
projection, launch_path, launch = sys.argv[1:]
Path(launch_path).parent.mkdir(mode=0o700)
with open(os.open(launch_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as file:
    file.write(launch)
argv = _sshd_argv(Path(projection))
os.execv(argv[0], argv)
"""
_LAUNCH = {"argv": ["bash"], "env": {"PATH": "/app/.venv/bin:/usr/local/bin:/usr/bin:/bin"}, "cwd": _WORKSPACE}


def _docker_result(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=stdin, capture_output=True, text=True, timeout=300, check=False)


def _docker(*args: str, stdin: str | None = None) -> str:
    """Run a Docker command that has to succeed, and return what it printed."""
    done = _docker_result(*args, stdin=stdin)
    assert done.returncode == 0, f"docker {shlex.join(args)} exited {done.returncode}: {done.stderr.strip()}"
    return done.stdout.strip()


def _logs(container: str) -> str:
    done = _docker_result("logs", container)
    return done.stdout + done.stderr


def _remove(prefix: str) -> None:
    """Remove every container and volume whose name starts with ``prefix``."""
    if containers := _docker("ps", "-aq", "--filter", f"name=^{prefix}").split():
        _docker("rm", "-f", "-v", *containers)
    if volumes := _docker("volume", "ls", "-q", "--filter", f"name={prefix}").split():
        _docker("volume", "rm", *volumes)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _eventually[T](what: str, check: Callable[[], T | None], *, seconds: float = 60) -> T:
    """Return the first truthy result of ``check``, asked until ``seconds`` have passed."""
    deadline = time.monotonic() + seconds
    while True:
        if result := check():
            return result
        assert time.monotonic() < deadline, f"Waited {seconds:g} s for {what}"
        time.sleep(0.2)


@dataclass(frozen=True)
class _Connect:
    """One connect call of the gateway."""

    link_id: str
    peer: dict[str, str]
    at: float


@dataclass
class _Primary:
    """The primary's internal listener as the gateway sees it: its three routes, over rows a test edits."""

    token: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    port: int = field(default_factory=_free_port)
    # What GET /links answers.
    rows: list[dict[str, Any]] = field(default_factory=list)
    # The host port each link's worker publishes its sshd on, and how long each link's connect takes.
    workers: dict[str, int] = field(default_factory=dict)
    delays: dict[str, float] = field(default_factory=dict)
    # Every POST /status body and every connect call, in the order they came.
    reports: list[dict[str, Any]] = field(default_factory=list)
    connects: list[_Connect] = field(default_factory=list)

    def status(self, link_id: str) -> dict[str, Any] | None:
        """Return what the gateway reported last of the link's node."""
        return self.reports[-1]["links"].get(link_id) if self.reports else None

    def app(self) -> FastAPI:
        def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
            if authorization != f"Bearer {self.token}":
                raise HTTPException(401, "Unauthorized")

        app = FastAPI(dependencies=[Depends(authorized)])

        @app.get("/links")
        def links() -> list[dict[str, Any]]:
            return self.rows

        @app.post("/status", status_code=204)
        def status(report: dict[str, Any]) -> None:
            self.reports.append(report)

        @app.post("/links/{link_id}/connect")
        async def connect(link_id: str, peer: dict[str, str]) -> dict[str, Any]:
            self.connects.append(_Connect(link_id, peer, time.monotonic()))
            await asyncio.sleep(self.delays.get(link_id, 0))
            return {"host": "127.0.0.1", "port": self.workers[link_id]}

        return app


@dataclass(frozen=True)
class _Person:
    """Someone with a device in the tailnet and an SSH key pair on the test host."""

    # Their Headscale user, which is their Tailscale login.
    login: str
    requester_id: str
    # Their device: a Tailscale client container.
    device: str
    key: Path

    @property
    def public_key(self) -> str:
        return self.key.with_suffix(".pub").read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class _Tailnet:
    """One Headscale, which is one tailnet, and the people in it."""

    # What the name of every container and volume of this tailnet starts with.
    prefix: str
    # On the test host: the SSH keys, ``known_hosts``, Headscale's config, and the workers' roots.
    folder: Path
    headscale: str
    login_server: str
    base_domain: str
    people: dict[str, _Person]


@dataclass
class _Node:
    """A link under test: the stored link, its row for the gateway, and its worker's root on the test host."""

    link: Link
    owner: _Person
    row: dict[str, Any]
    root: Path | None = None
    # What the gateway reported while the node waited for its login.
    pending: dict[str, Any] | None = None


@dataclass
class _Stack:
    """One gateway and the fake primary it asks, in the module's tailnet."""

    tailnet: _Tailnet
    prefix: str
    primary: _Primary
    gateway: str
    # The gateway's volume of node keys.
    state: str
    config: Config
    book: LinkBook = field(default_factory=LinkBook)
    nodes: dict[str, _Node] = field(default_factory=dict)
    # How many connections the tests opened that each link's connect call is for, by link id.
    opened: dict[str, int] = field(default_factory=dict)


def _pre_auth_key(headscale: str, user: str) -> str:
    """Create the Headscale user and return a pre-auth key of theirs."""
    created = json.loads(_docker("exec", headscale, "headscale", "users", "create", user, "--output", "json"))
    key = _docker(
        *("exec", headscale, "headscale", "preauthkeys", "create"),
        *("--user", str(created["id"]), "--reusable", "--output", "json"),
    )
    return json.loads(key)["key"]


def _signed_in(tailnet: _Tailnet) -> set[str]:
    """Return the names of the machines signed in to the tailnet.

    Headscale keeps a machine that signed out in its list, as one whose key has expired, which no peer sees.
    """
    listed = json.loads(_docker("exec", tailnet.headscale, "headscale", "nodes", "list", "--output", "json"))
    return {node["given_name"] for node in listed if "expiry" not in node or node["expiry"]["seconds"] > time.time()}


def _joined(device: str) -> bool:
    status = _docker_result("exec", device, "tailscale", "status", "--json")
    return status.returncode == 0 and json.loads(status.stdout)["BackendState"] == "Running"


def _client(tailnet: _Tailnet, person: _Person, tool: str = "ssh", *, key: Path | None = None) -> list[str]:
    """Return ``ssh``, ``scp`` or ``sftp`` with the options that reach a link's node from the person's device."""
    options = {
        "User": "mindroom",
        "IdentityFile": key or person.key,
        "IdentitiesOnly": "yes",
        "BatchMode": "yes",
        "StrictHostKeyChecking": "yes",
        "UserKnownHostsFile": tailnet.folder / "known_hosts",
        "GlobalKnownHostsFile": "/dev/null",
        "ProxyCommand": f"docker exec -i {person.device} tailscale nc %h %p",
    }
    return [tool, "-F", "none", *[arg for name, value in options.items() for arg in ("-o", f"{name}={value}")]]


def _await_connect_slot(stack: _Stack, node: _Node) -> None:
    """Wait until the link may have one more connect call: the gateway makes six a minute and closes what is over.

    Every connection the link's own login opens waits here first, so each one is one connect call.
    """
    link_id = node.link.id

    def asked() -> list[float]:
        return [connect.at for connect in stack.primary.connects if connect.link_id == link_id]

    opened = stack.opened.get(link_id, 0)
    _eventually("the link's earlier connections to reach the primary", lambda: len(asked()) >= opened, seconds=30)
    while len(recent := [at for at in asked() if time.monotonic() - at < 61]) >= 6:
        time.sleep(max(0.1, 61 - (time.monotonic() - min(recent))))
    stack.opened[link_id] = opened + 1


def _run_ssh(
    stack: _Stack,
    node: _Node,
    argv: list[str],
    *,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a client command that opens one connection to the node as its link's login."""
    _await_connect_slot(stack, node)
    return subprocess.run(
        argv,
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


@contextmanager
def _ssh_session(stack: _Stack, node: _Node, argv: list[str]) -> Iterator[subprocess.Popen[str]]:
    """Start a client command that opens one connection to the node as its link's login, and end it on the way out."""
    _await_connect_slot(stack, node)
    # In a process group of its own, so that the client ends together with its ProxyCommand.
    client = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        yield client
    finally:
        _end(client)


def _end(client: subprocess.Popen[str]) -> tuple[str, str]:
    """Kill the client and what it started, and return what they wrote, waited for ten seconds at most."""
    # A client that is over was waited for, so its id may be another process's by now.
    if client.poll() is None:
        os.killpg(client.pid, signal.SIGKILL)
    try:
        return client.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        # Something the client started is in a group of its own and holds a pipe open; its container's removal ends it.
        for pipe in (client.stdout, client.stderr):
            assert pipe is not None
            pipe.close()
        return "", ""


def _await_up(client: subprocess.Popen[str]) -> None:
    """Wait a minute at most for ``up``, the line the session's command prints first."""
    assert client.stdout is not None
    deadline = time.monotonic() + 60
    line = b""
    # Nothing is read through the client's own buffer, so no read waits longer than the deadline for a whole line.
    while not line.endswith(b"\n") and (left := deadline - time.monotonic()) > 0:
        ready, _, _ = select.select([client.stdout], [], [], left)
        if ready:
            byte = os.read(client.stdout.fileno(), 1)
            if not byte:
                break
            line += byte
    if line != b"up\n":
        pytest.fail(f"The session did not start: it wrote {line!r}, and then {_end(client)}")


def _peers(person: _Person) -> set[str]:
    """Return the names of the machines the person's device sees in its tailnet."""
    status = json.loads(_docker("exec", person.device, "tailscale", "status", "--json"))
    return {peer["HostName"] for peer in (status["Peer"] or {}).values()}


def _assert_turned_away(stack: _Stack, person: _Person, node: _Node) -> None:
    """Check that the gateway closes the person's connection to the node as not its login's, asking no primary."""
    refusal = f"link {node.link.id}: closed a connection, its peer is not the link's login"
    refused, asked = _logs(stack.gateway).count(refusal), len(stack.primary.connects)
    done = subprocess.run(
        [*_client(stack.tailnet, person), node.link.hostname, "true"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 255
    assert "Connection closed" in done.stderr
    assert _logs(stack.gateway).count(refusal) == refused + 1
    assert len(stack.primary.connects) == asked


def _in_state_volume(stack: _Stack, *command: str) -> subprocess.CompletedProcess[str]:
    """Run a command of the worker image, as root, over the gateway's volume of node keys."""
    return _docker_result(
        *("run", "--rm", "--user", "0:0", "--entrypoint", command[0], "--volume", f"{stack.state}:/state:ro"),
        *(_WORKER_IMAGE, *command[1:]),
    )


def _security_options(folder: Path) -> list[str]:
    """Return the Docker backend's security options as the CLI takes them, which reads a seccomp profile from a file."""
    options = []
    for option in docker_worker_security_options():
        name, _, value = option.partition("=")
        if name == "seccomp":
            profile = folder / "seccomp.json"
            profile.write_text(value, encoding="utf-8")
            options += ["--security-opt", f"seccomp={profile}"]
        else:
            options += ["--security-opt", option]
    return options


def _answers_ssh(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=5) as probe:
            return probe.recv(8).startswith(b"SSH-2.0-")
    except OSError:
        return False


def _own(folder: Path, owner: str) -> None:
    """Give the folder and what it holds to ``owner``, a ``uid:gid``, as root."""
    _docker(
        *("run", "--rm", "--user", "0:0", "--entrypoint", "chown", "--volume", f"{folder}:/owned"),
        *(_WORKER_IMAGE, "--recursive", owner, "/owned"),
    )


def _project(stack: _Stack, node: _Node) -> None:
    """Write sshd's files for the link with its owner's keys into its worker's root, as the primary and its uid."""
    key_lines = authorized_key_lines(stack.config, stack.book, node.owner.requester_id)
    _docker(
        *("run", "--rm", "--interactive", "--user", "1000:1000", "--volume", f"{node.root}:{_WORKER_ROOT}"),
        *(_WORKER_IMAGE, "python", "-c", _WRITE_PROJECTION, _PROJECTION),
        stdin=json.dumps({"link": node.link.model_dump(mode="json"), "key_lines": key_lines}),
    )


def _start_worker(stack: _Stack, node: _Node) -> None:
    """Run the link's worker as the Docker backend runs one, with sshd up, as after a connect the primary passed.

    Its root is a folder of the test host, mounted as the backend mounts one: writable, with ``.runtime``, which the
    primary writes, read-only over it. The primary and the worker are uid 1000, whoever runs the test.
    """
    name = f"{stack.prefix}-{node.link.hostname}"
    node.root = stack.tailnet.folder / name
    (node.root / "workspace").mkdir(parents=True)
    for folder in (node.root, node.root / "workspace"):
        folder.chmod(0o755)
    _own(node.root, "1000:1000")
    _project(stack, node)
    port = _free_port()
    _docker(
        *("run", "--detach", "--name", name, "--user", "1000:1000", *_READ_ONLY_ROOT),
        *("--cap-drop", "ALL", *_security_options(stack.tailnet.folder)),
        *("--volume", f"{node.root}:{_WORKER_ROOT}", "--volume", f"{node.root}/.runtime:{_WORKER_ROOT}/.runtime:ro"),
        *("--publish", f"127.0.0.1:{port}:{WORKER_SSH_PORT}"),
        *(_WORKER_IMAGE, "python", "-c", _SERVE, _PROJECTION, str(session.LAUNCH_PATH), json.dumps(_LAUNCH)),
    )
    _eventually(f"the sshd of {name}", lambda: _answers_ssh(port), seconds=90)
    stack.primary.workers[node.link.id] = port


def _add_link(
    stack: _Stack,
    owner: _Person,
    hostname: str,
    *,
    allowed_login: str | None = None,
    worker: bool = True,
) -> _Node:
    """Add a link that is on, as the portal's connect does, with a worker that runs sshd unless ``worker`` is false."""
    link = new_link(owner.requester_id, hostname.replace("-", "_"), f"worker-{hostname}", hostname, now=time.time())
    row = {
        "id": link.id,
        "hostname": hostname,
        "desired": "on",
        "allowed_login": allowed_login,
        "closed_before": link.closed_before,
        "login_after": link.login_after,
    }
    node = stack.nodes[hostname] = _Node(link, owner, row)
    stack.book.links.append(link)
    with (stack.tailnet.folder / "known_hosts").open("a", encoding="utf-8") as known_hosts:
        known_hosts.write(f"{hostname} {link.host_public_key}\n")
    if worker:
        _start_worker(stack, node)
    stack.primary.rows.append(row)
    return node


def _await_admitted(stack: _Stack, node: _Node) -> None:
    """Connect to the node as its link's login, again and again, until the gateway takes a connection to the worker.

    The gateway admits the login once a pass of it has read the row that names the login, and it reports no pass.
    Until then it closes the login's connections and makes no connect call, so this costs the link one connect call.
    """
    link_id = node.link.id
    argv = [*_client(stack.tailnet, node.owner), "-o", "ConnectTimeout=30", node.link.hostname, "true"]

    def asked() -> int:
        return sum(connect.link_id == link_id for connect in stack.primary.connects)

    def admitted() -> bool:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=60, check=False)
        return done.returncode == 0

    before = asked()
    _eventually(f"the gateway to admit the login of {node.link.hostname}", admitted, seconds=90)
    assert asked() == before + 1
    stack.opened[link_id] = stack.opened.get(link_id, 0) + 1


def _enrol(stack: _Stack, node: _Node) -> None:
    """Sign the link's node in as its owner, by the login URL the gateway reports, and admit that login from then on.

    A node with a worker is returned once the gateway took a connection of that login to it.
    """
    link_id = node.link.id

    def reported(state: str, told: str) -> dict[str, Any] | None:
        status = stack.primary.status(link_id)
        return status if status and status["state"] == state and status[told] else None

    node.pending = _eventually(f"the login URL of {node.link.hostname}", lambda: reported("needs_login", "login_url"))
    auth_id = node.pending["login_url"].rsplit("/", 1)[1]
    _docker(
        *("exec", stack.tailnet.headscale, "headscale", "auth", "register"),
        *("--auth-id", auth_id, "--user", node.owner.login),
    )
    running = _eventually(f"{node.link.hostname} to run", lambda: reported("running", "login_name"))
    node.row["allowed_login"] = running["login_name"]
    if node.root is not None:
        _await_admitted(stack, node)


def _set_keys(stack: _Stack, person: _Person, lines: list[str]) -> None:
    """Replace the person's keys as the portal does: store them, rewrite their workers' files, close their sessions."""
    set_keys(stack.book, person.requester_id, lines, now=time.time())
    for node in stack.nodes.values():
        if node.owner == person:
            if node.root is not None:
                _project(stack, node)
            node.row["closed_before"] = node.link.closed_before


@contextmanager
def _gateway(tailnet: _Tailnet, name: str, config: Config, *, auth_key: str | None = None) -> Iterator[_Stack]:
    """Run the gateway image as the chart runs it, against a fake primary of its own."""
    primary = _Primary()
    listener = uvicorn.Config(primary.app(), host="127.0.0.1", port=primary.port, log_level="warning", ws="none")
    served = uvicorn.Server(listener)
    serving = threading.Thread(target=served.run, daemon=True)
    serving.start()
    prefix = f"{tailnet.prefix}-{name}"
    stack = _Stack(tailnet, prefix, primary, f"{prefix}-gateway", f"{prefix}-state", config)
    environment = {
        "TAILNET_SSH_PRIMARY_URL": f"http://127.0.0.1:{primary.port}",
        "TAILNET_SSH_GATEWAY_TOKEN": primary.token,
        "TAILNET_SSH_LOGIN_SERVER": tailnet.login_server,
        **({"TAILNET_SSH_AUTH_KEY": auth_key} if auth_key else {}),
    }
    try:
        _eventually("the fake primary to listen", lambda: served.started, seconds=10)
        _docker(
            *("run", "--detach", "--name", stack.gateway, "--network", "host", *_READ_ONLY_ROOT),
            *("--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--volume", f"{stack.state}:/state"),
            *[arg for name, value in environment.items() for arg in ("--env", f"{name}={value}")],
            _GATEWAY_IMAGE,
        )
        yield stack
    finally:
        # Shown with a failed case: what the gateway and the workers' sshd logged.
        for container in _docker("ps", "--all", "--filter", f"name=^{prefix}", "--format", "{{.Names}}").split():
            print(f"--- {container}\n{_logs(container)}")
        _remove(prefix)
        # The test host removes its temp folders as its own user.
        for node in stack.nodes.values():
            if node.root is not None:
                _own(node.root, f"{os.getuid()}:{os.getgid()}")
        served.should_exit = True
        serving.join(timeout=10)


@contextmanager
def _headscale(folder: Path, logins: Sequence[str], *, base_domain: str, v4: str, v6: str) -> Iterator[_Tailnet]:
    """Run a Headscale of its own with a user and a Tailscale client, joined by pre-auth key, for each login."""
    prefix = f"mindroom-e2e-{secrets.token_hex(4)}"
    port = _free_port()
    config = folder / "headscale.yaml"
    config.write_text(
        _HEADSCALE_CONFIG.format(
            port=port,
            metrics_port=_free_port(),
            grpc_port=_free_port(),
            base_domain=base_domain,
            v4=v4,
            v6=v6,
        ),
        encoding="utf-8",
    )
    headscale, login_server = f"{prefix}-headscale", f"http://127.0.0.1:{port}"
    try:
        _docker(
            *("run", "--detach", "--name", headscale, "--network", "host"),
            *("--volume", f"{config}:/etc/headscale/config.yaml:ro", _HEADSCALE_IMAGE, "serve"),
        )
        _eventually(
            "Headscale to start",
            lambda: _docker_result("exec", headscale, "headscale", "users", "list").returncode == 0,
        )
        people = {}
        for login in logins:
            key = folder / login
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", login, "-f", key], check=True)
            people[login] = _Person(login, f"@{login}:example.org", f"{prefix}-{login}", key)
            _docker(
                *("run", "--detach", "--name", people[login].device, "--network", "host"),
                *("--env", f"TS_AUTHKEY={_pre_auth_key(headscale, login)}", "--env", f"TS_HOSTNAME={login}-laptop"),
                *("--env", "TS_USERSPACE=true", "--env", f"TS_EXTRA_ARGS=--login-server={login_server}"),
                _TAILSCALE_IMAGE,
            )
        for person in people.values():
            _eventually(f"the device of {person.login} to join", lambda device=person.device: _joined(device))
        yield _Tailnet(prefix, folder, headscale, login_server, base_domain, people)
    finally:
        _remove(prefix)


@contextmanager
def _login_stack(tailnet: _Tailnet, login: str) -> Iterator[_Stack]:
    """Run a gateway that enrols by login URL, with a link and a worker for the person, who signed its node in."""
    person = tailnet.people[login]
    with _gateway(tailnet, "login", Config()) as stack:
        set_keys(stack.book, person.requester_id, [person.public_key], now=time.time())
        _enrol(stack, _add_link(stack, person, f"mind-{login}"))
        yield stack


@pytest.fixture(scope="module")
def tailnet(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Tailnet]:
    """A tailnet with a device each of alice and bob."""
    folder = tmp_path_factory.mktemp("tailnet")
    domain, v4, v6 = "tailnet.test", "100.64.0.0/16", "fd7a:115c:a1e0:a::/64"
    with _headscale(folder, ("alice", "bob"), base_domain=domain, v4=v4, v6=v6) as tailnet:
        yield tailnet


@pytest.fixture(scope="module")
def other_tailnet(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Tailnet]:
    """A second tailnet, of a Headscale that shares nothing with the first, with another device of bob."""
    folder = tmp_path_factory.mktemp("other-tailnet")
    domain, v4, v6 = "other.test", "100.96.0.0/16", "fd7a:115c:a1e0:b::/64"
    with _headscale(folder, ("bob",), base_domain=domain, v4=v4, v6=v6) as tailnet:
        yield tailnet


@pytest.fixture(scope="module")
def stack(tailnet: _Tailnet) -> Iterator[_Stack]:
    """A gateway that enrols by login URL, with a link and a worker for alice, who signed its node in."""
    with _login_stack(tailnet, "alice") as stack:
        yield stack


@pytest.fixture(scope="module")
def stack_other_tailnet(other_tailnet: _Tailnet) -> Iterator[_Stack]:
    """A gateway in the second tailnet, with a link and a worker for bob, who signed its node in there."""
    with _login_stack(other_tailnet, "bob") as stack:
        yield stack


@pytest.fixture(scope="module")
def stack_auth_key(tailnet: _Tailnet) -> Iterator[_Stack]:
    """A gateway that enrols with an operator's pre-auth key, with a link and a worker for alice."""
    alice = tailnet.people["alice"]
    # With an operator key the config names the login a person's link admits, and may name their SSH keys.
    operated = {
        "logins": {alice.requester_id: alice.login},
        "authorized_keys": {alice.requester_id: [alice.public_key]},
    }
    config = Config(tailnet_ssh=operated)
    with _gateway(tailnet, "key", config, auth_key=_pre_auth_key(tailnet.headscale, "operator")) as stack:
        _add_link(stack, alice, "mind-key", allowed_login=config.tailnet_ssh.logins[alice.requester_id])
        yield stack


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_enrol_by_login_url_then_exec(stack: _Stack) -> None:
    """A node signs in by the login URL its gateway reports, and its login's command runs in the worker."""
    alice = stack.tailnet.people["alice"]
    node = stack.nodes["mind-alice"]
    assert node.pending is not None
    assert node.pending["state"] == "needs_login"
    login_url = node.pending["login_url"]
    assert login_url.startswith(f"{stack.tailnet.login_server}/register/")
    status = stack.primary.status(node.link.id)
    assert status is not None
    assert (status["state"], status["login_url"], status["login_name"]) == ("running", None, "alice")
    domain = stack.tailnet.base_domain
    assert (status["dns_name"], status["tailnet"]) == (f"mind-alice.{domain}", domain)
    assert stack.primary.reports[-1]["enrollment"] == "login"

    # The login URL signs a machine in to the tailnet of whoever opens it, so it is kept nowhere: not in the gateway's
    # log, and not in its volume, which does hold the node's keys.
    auth_id = login_url.rsplit("/", 1)[1]
    assert auth_id not in _logs(stack.gateway)
    assert _in_state_volume(stack, "test", "-s", f"/state/{node.link.id}/tailscaled.state").returncode == 0
    naming = _in_state_volume(stack, "grep", "-rlF", "--", auth_id, "/state")
    assert (naming.returncode, naming.stdout) == (1, ""), naming.stderr

    # The session is the worker's user in the launch's folder, and sshd's files are that user's, who cannot write them.
    seen = f"stat --format %u:%a {_PROJECTION} {_PROJECTION}/ssh_host_ed25519_key; touch {_PROJECTION}/planted"
    done = _run_ssh(stack, node, [*_client(stack.tailnet, alice), "mind-alice", f"id -u; pwd; {seen}"])
    assert done.stdout.split() == ["1000", _WORKSPACE, "1000:755", "1000:600"], done.stderr
    assert (done.returncode, "Read-only file system" in done.stderr) == (1, True), done.stderr
    asked = stack.primary.connects[-1]
    assert (asked.link_id, asked.peer) == (node.link.id, {"peer_login": "alice", "peer_node": "alice-laptop"})


def test_enrol_by_pre_auth_key(stack_auth_key: _Stack) -> None:
    """With an operator's key a node runs without a login URL, and admits the login the config names."""
    stack = stack_auth_key
    node = stack.nodes["mind-key"]

    def running() -> bool:
        status = stack.primary.status(node.link.id)
        return status is not None and status["state"] == "running" and status["login_name"] is not None

    _eventually("the node to run", running)
    reports = [report for report in stack.primary.reports if node.link.id in report["links"]]
    assert {report["enrollment"] for report in reports} == {"auth_key"}
    assert {report["links"][node.link.id]["login_url"] for report in reports} == {None}
    assert reports[-1]["links"][node.link.id]["login_name"] == "operator"

    done = _run_ssh(stack, node, [*_client(stack.tailnet, stack.tailnet.people["alice"]), "mind-key", "id -u"])
    assert (done.returncode, done.stdout.strip()) == (0, "1000"), done.stderr


def test_pty_scp_sftp_rsync(stack: _Stack, tmp_path: Path) -> None:
    """A login gets a terminal, and a file comes back as it was sent by scp, sftp and rsync."""
    alice = stack.tailnet.people["alice"]
    node = stack.nodes["mind-alice"]
    ssh = _client(stack.tailnet, alice)

    terminal = _run_ssh(stack, node, [*ssh, "-tt", "mind-alice", "tty"])
    assert terminal.returncode == 0, terminal.stderr
    assert terminal.stdout.strip().startswith("/dev/pts/")

    sent = tmp_path / "sent" / "notes.bin"
    sent.parent.mkdir(mode=0o755)
    sent.write_bytes(secrets.token_bytes(1 << 20))
    sent.chmod(0o644)
    scp = _client(stack.tailnet, alice, "scp")
    rsync = ["rsync", "--archive", "--rsh", shlex.join(ssh)]
    transfers = [
        ([*scp, str(sent), "mind-alice:scp.bin"], None),
        ([*scp, "mind-alice:scp.bin", str(tmp_path / "scp.bin")], None),
        (
            [*_client(stack.tailnet, alice, "sftp"), "-b", "-", "mind-alice"],
            f"put {sent} sftp.bin\nget sftp.bin {tmp_path / 'sftp.bin'}\n",
        ),
        ([*rsync, f"{sent.parent}/", "mind-alice:rsync/"], None),
        ([*rsync, "mind-alice:rsync/", f"{tmp_path / 'rsync'}/"], None),
    ]
    for argv, stdin in transfers:
        done = _run_ssh(stack, node, argv, stdin=stdin)
        assert done.returncode == 0, done.stderr
    returned = [tmp_path / "scp.bin", tmp_path / "sftp.bin", tmp_path / "rsync" / "notes.bin"]
    assert [_sha256(path) for path in returned] == [_sha256(sent)] * 3
    # What was sent is in the workspace, which is the session's home.
    assert node.root is not None
    landed = [node.root / "workspace" / name for name in ("scp.bin", "sftp.bin", "rsync/notes.bin")]
    assert [_sha256(path) for path in landed] == [_sha256(sent)] * 3


def test_forwarding_is_refused(stack: _Stack, tmp_path: Path) -> None:
    """A session forwards no port, no stdio and no agent."""
    alice = stack.tailnet.people["alice"]
    node = stack.nodes["mind-alice"]
    ssh = _client(stack.tailnet, alice)
    sshd = f"127.0.0.1:{WORKER_SSH_PORT}"

    # A local forward's listener is the client's own, so the worker refuses when something connects to it.
    port = _free_port()
    with _ssh_session(stack, node, [*ssh, "-L", f"{port}:{sshd}", "mind-alice", "echo up; sleep 60"]) as local:
        _await_up(local)
        with socket.create_connection(("127.0.0.1", port), timeout=30) as through:
            assert through.recv(8) == b""
        assert "open failed" in _end(local)[1]

    remote = _run_ssh(stack, node, [*ssh, "-o", "ExitOnForwardFailure=yes", "-R", f"0:{sshd}", "mind-alice", "true"])
    assert remote.returncode == 255
    assert "remote port forwarding failed" in remote.stderr
    stdio = _run_ssh(stack, node, [*ssh, "-W", sshd, "mind-alice"])
    assert stdio.returncode == 255
    assert "stdio forwarding failed" in stdio.stderr

    # The session's program drops what sshd says of an agent, so the session looks for the socket sshd makes for a
    # forwarded one, in the folders the worker's user can write.
    socket_path = tmp_path / "agent"
    agent = subprocess.Popen(["ssh-agent", "-D", "-a", socket_path], stdout=subprocess.DEVNULL)
    try:
        _eventually("the SSH agent's socket", socket_path.exists, seconds=10)
        forwarded = _run_ssh(
            stack,
            node,
            [*ssh, "-A", "mind-alice", f"find /tmp {_WORKER_ROOT} -type s && echo end"],
            env={**os.environ, "SSH_AUTH_SOCK": str(socket_path)},
        )
    finally:
        agent.terminate()
        agent.wait(timeout=10)
    assert (forwarded.returncode, forwarded.stdout.split()) == (0, ["end"]), forwarded.stderr


def test_wrong_key_and_password_are_refused(stack: _Stack, tmp_path: Path) -> None:
    """Only a key of the link's owner signs in: the worker offers no other way."""
    alice = stack.tailnet.people["alice"]
    node = stack.nodes["mind-alice"]
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other], check=True)

    wrong_key = _run_ssh(stack, node, [*_client(stack.tailnet, alice, key=other), "mind-alice", "true"])
    password = _run_ssh(
        stack,
        node,
        [*_client(stack.tailnet, alice), "-o", "PubkeyAuthentication=no", "mind-alice", "true"],
    )
    for refused in (wrong_key, password):
        assert refused.returncode == 255
        # The worker names the ways it offers, and a password is not one.
        assert "Permission denied (publickey)." in refused.stderr


def test_second_user_in_same_tailnet_is_refused_before_connect(stack: _Stack) -> None:
    """Another login of the same tailnet, which its policy lets reach the node, gets no connect call."""
    _assert_turned_away(stack, stack.tailnet.people["bob"], stack.nodes["mind-alice"])


def test_two_links_two_users_cannot_cross(stack: _Stack, stack_other_tailnet: _Stack) -> None:
    """Each of two people, in a tailnet of their own, reaches the node they signed in, and neither the other's."""
    alice, bob = stack.tailnet.people["alice"], stack_other_tailnet.tailnet.people["bob"]
    for own, person, other, theirs in (
        (stack, alice, stack_other_tailnet, bob),
        (stack_other_tailnet, bob, stack, alice),
    ):
        node, elsewhere = own.nodes[f"mind-{person.login}"], other.nodes[f"mind-{theirs.login}"]
        done = _run_ssh(own, node, [*_client(own.tailnet, person), node.link.hostname, "id -u"])
        assert (done.returncode, done.stdout.strip()) == (0, "1000"), done.stderr
        assert own.primary.connects[-1].peer["peer_login"] == person.login

        # The other tailnet's node is no machine of this one, so its name leads nowhere and its gateway sees nobody.
        peers = _peers(person)
        assert node.link.hostname in peers
        assert elsewhere.link.hostname not in peers
        seen = _logs(other.gateway), len(other.primary.connects)
        missed = subprocess.run(
            [*_client(own.tailnet, person), "-o", "ConnectTimeout=30", elsewhere.link.hostname, "true"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert missed.returncode == 255
        assert (_logs(other.gateway), len(other.primary.connects)) == seen


def test_key_removal_cuts_an_open_session(stack: _Stack) -> None:
    """Removing a person's key ends their open session within the gateway's next passes, and admits no new one."""
    alice = stack.tailnet.people["alice"]
    node = stack.nodes["mind-alice"]
    ssh = _client(stack.tailnet, alice)
    with _ssh_session(stack, node, [*ssh, "mind-alice", "echo up; sleep 300"]) as held:
        _await_up(held)
        try:
            _set_keys(stack, alice, [])
            assert held.wait(timeout=15) == 255
            refused = _run_ssh(stack, node, [*ssh, "mind-alice", "true"])
            assert refused.returncode == 255
            assert "Permission denied (publickey)." in refused.stderr
        finally:
            _set_keys(stack, alice, [alice.public_key])


def test_connection_held_through_a_120s_start(stack: _Stack) -> None:
    """A client with no setting of its own waits for a worker that takes two minutes to start, told to wait."""
    alice = stack.tailnet.people["alice"]
    node = stack.nodes["mind-alice"]
    ssh = _client(stack.tailnet, alice)
    stack.primary.delays[node.link.id] = 120
    try:
        with ExitStack() as held:
            clients = [
                held.enter_context(_ssh_session(stack, node, [*ssh, *verbose, "mind-alice", "true"]))
                for verbose in ([], ["-v"])
            ]
            outputs = [client.communicate(timeout=300) for client in clients]
    finally:
        del stack.primary.delays[node.link.id]
    assert [client.returncode for client in clients] == [0, 0], outputs
    # Both were held: the first one's connect call came this long ago.
    assert time.monotonic() - stack.primary.connects[-2].at >= 120
    assert "MindRoom: waking worker" in outputs[1][1]


def test_removing_logs_out_and_gateway_restart_keeps_others(stack: _Stack) -> None:
    """A removed link's node leaves the tailnet, also when the gateway was down, and a restart signs no node out."""
    alice = stack.tailnet.people["alice"]
    kept = stack.nodes["mind-alice"]
    gone, late = (_add_link(stack, alice, hostname, worker=False) for hostname in ("mind-gone", "mind-late"))
    for node in (gone, late):
        _enrol(stack, node)
    assert {"mind-gone", "mind-late"} <= _signed_in(stack.tailnet)

    def removed(node: _Node) -> bool:
        status = stack.primary.status(node.link.id)
        return status is not None and status["state"] == "removed"

    gone.row["desired"] = "removing"
    _eventually("the removed link's node to be gone", lambda: removed(gone))
    assert "mind-gone" not in _signed_in(stack.tailnet)
    assert _in_state_volume(stack, "test", "-e", f"/state/{gone.link.id}").returncode == 1
    stack.primary.rows.remove(gone.row)

    # A restart, with a link removed while the gateway is down: its node is signed out from the keys in the volume.
    _docker("stop", stack.gateway)
    late.row["desired"] = "removing"
    before = len(stack.primary.reports)
    _docker("start", stack.gateway)
    _eventually("the link removed meanwhile to be gone", lambda: removed(late))
    assert "mind-late" not in _signed_in(stack.tailnet)
    assert _in_state_volume(stack, "test", "-e", f"/state/{late.link.id}").returncode == 1
    stack.primary.rows.remove(late.row)

    def running() -> bool:
        status = stack.primary.status(kept.link.id)
        return len(stack.primary.reports) > before and status is not None and status["state"] == "running"

    _eventually("the kept link's node to run again", running)
    since = [
        report["links"][kept.link.id] for report in stack.primary.reports[before:] if kept.link.id in report["links"]
    ]
    assert {status["login_url"] for status in since} == {None}
    done = _run_ssh(stack, kept, [*_client(stack.tailnet, alice), "mind-alice", "id -u"])
    assert (done.returncode, done.stdout.strip()) == (0, "1000"), done.stderr
