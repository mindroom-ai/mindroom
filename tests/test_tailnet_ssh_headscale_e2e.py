"""Tailnet SSH against a real tailnet: Headscale, the gateway image, workers that run sshd, and Tailscale clients.

Exercise real tailnet transport, WhoIs, and OpenSSH, using the production roster behind a stand-in primary connect.
Production admission is covered by ``tests/test_tailnet_ssh_links.py`` and the kind probe, which calls real connect.
Run with ``MINDROOM_TAILNET_SSH_E2E=1`` and the gateway and worker images, as in the ``headscale-e2e`` CI job.
"""

from __future__ import annotations

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
import traceback
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

import pytest
import uvicorn
from cryptography.hazmat.primitives import serialization
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import SecretStr

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.tailnet_ssh import session
from mindroom.tailnet_ssh.links import StatusReport, desired_links
from mindroom.tailnet_ssh.store import read_links
from mindroom.workers.backends.worker_security import docker_worker_security_options
from mindroom.workers.models import WORKER_SSH_PORT

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.tailnet_ssh.store import Link

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(1200), pytest.mark.usefixtures("enforce_turn_authorization")]

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
  v4: 100.64.0.0/16
  v6: fd7a:115c:a1e0:a::/64
derp:
  urls:
    - https://controlplane.tailscale.com/derpmap/default
database:
  type: sqlite
  sqlite:
    path: /var/lib/headscale/db.sqlite
dns:
  magic_dns: true
  base_domain: tailnet.test
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
write_projection(Path(sys.argv[1]), Link.model_validate(sent["link"]), sent["keys"])
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


_SECRET_ENV = {"TS_AUTHKEY", "TAILNET_SSH_AUTH_KEY", "TAILNET_SSH_GATEWAY_TOKEN"}


def _redact(text: str, values: set[str]) -> str:
    for value in sorted(values - {""}, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def _docker(*args: str, stdin: str | None = None) -> str:
    """Run a Docker command that has to succeed, and return what it printed."""
    secret_values = {arg.partition("=")[2] for arg in args if arg.partition("=")[0] in _SECRET_ENV}
    command = shlex.join([_redact(arg, secret_values) for arg in args])
    try:
        done = _docker_result(*args, stdin=stdin)
    except subprocess.TimeoutExpired:
        done = None  # Do not chain the exception that carries the unredacted argv.
    if done is None:
        pytest.fail(f"docker {command} timed out", pytrace=False)
    if done.returncode != 0:
        pytest.fail(
            f"docker {command} exited {done.returncode}: {_redact(done.stderr.strip(), secret_values)}",
            pytrace=False,
        )
    return done.stdout.strip()


@pytest.mark.parametrize("times_out", [False, True])
def test_docker_failure_redacts_secret_env(monkeypatch: pytest.MonkeyPatch, *, times_out: bool) -> None:
    """A Docker failure must not put secrets in the error message or its traceback."""
    env = {
        "TS_AUTHKEY": "test-client-key",
        "TAILNET_SSH_AUTH_KEY": "test-gateway-key",
        "TAILNET_SSH_GATEWAY_TOKEN": "test-token",
    }
    argv = [value for name, secret in env.items() for value in ("--env", f"{name}={secret}")]
    stderr = "docker could not start: " + " ".join(env.values())

    def fail(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if times_out:
            raise subprocess.TimeoutExpired(args, 300, stderr=stderr)
        return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(pytest.fail.Exception) as failure:
        _docker("run", *argv, "image")

    diagnostic = "".join(traceback.format_exception(failure.value))
    assert all(secret not in diagnostic for secret in env.values())
    assert ("timed out" if times_out else "docker could not start") in str(failure.value)


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


@dataclass
class _Primary:
    """Serve the real roster; substitute the Docker wake and start behind connect."""

    config: Config
    paths: RuntimePaths
    token: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    port: int = field(default_factory=_free_port)
    memberships: AgentReplyMembershipIndex = field(default_factory=AgentReplyMembershipIndex)
    connects: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    warm: Callable[[set[str]], int] | None = None
    delay: float = 0
    omissions: int = 0

    def roster(self) -> list[dict[str, Any]]:
        return desired_links(self.config, self.paths, self.memberships)

    def app(self) -> FastAPI:
        def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
            if authorization != f"Bearer {self.token}":
                raise HTTPException(401, "Unauthorized")

        app = FastAPI(dependencies=[Depends(authorized)])

        @app.get("/links")
        def links() -> list[dict[str, Any]]:
            rows = self.roster()
            self.omissions += not rows
            return rows

        @app.post("/status", status_code=204)
        def status(_report: StatusReport) -> None:
            pass

        @app.post("/links/{link_id}/connect")
        def connect(link_id: str, peer: dict[str, str]) -> dict[str, Any]:
            self.connects.append((link_id, peer))
            if self.delay:
                time.sleep(self.delay)
            logins = next((set(row["allowed_logins"]) for row in self.roster() if row["id"] == link_id), set())
            if peer["peer_login"] not in logins:
                raise HTTPException(403, "Login is no longer allowed")
            assert self.warm is not None
            return {"host": "127.0.0.1", "port": self.warm(logins)}

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
    people: dict[str, _Person]


@dataclass
class _Stack:
    """One gateway, one shared worker (initially asleep), and the primary listener."""

    tailnet: _Tailnet
    primary: _Primary
    link: Link
    gateway: str
    state: str
    root: Path


def _pre_auth_key(headscale: str, user: str) -> str:
    """Create the Headscale user and return a pre-auth key of theirs."""
    created = json.loads(_docker("exec", headscale, "headscale", "users", "create", user, "--output", "json"))
    key = _docker(
        *("exec", headscale, "headscale", "preauthkeys", "create"),
        *("--user", str(created["id"]), "--reusable", "--output", "json"),
    )
    return json.loads(key)["key"]


def _registered_node(stack: _Stack) -> tuple[int, list[str]] | None:
    nodes = json.loads(_docker("exec", stack.tailnet.headscale, "headscale", "nodes", "list", "--output", "json"))
    matches = [(node["id"], node["ip_addresses"]) for node in nodes if node["given_name"] == stack.link.hostname]
    if matches:
        [identity] = matches
        return identity
    return None


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


def _run_ssh(
    argv: list[str],
    *,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True,
        text=True,
        env=env,
        timeout=90,
        check=False,
    )


@contextmanager
def _ssh_session(argv: list[str]) -> Iterator[subprocess.Popen[str]]:
    """Open SSH and reap the client and its ProxyCommand on exit."""
    client = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
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


def _assert_turned_away(stack: _Stack, person: _Person, key: Path) -> None:
    """The peer can see the node, but the gateway refuses before any worker connect call."""
    asked = len(stack.primary.connects)
    assert stack.link.hostname in _peers(person)
    refused = _run_ssh([*_client(stack.tailnet, person, key=key), stack.link.hostname, "true"])
    assert refused.returncode == 255
    assert "Connection closed" in refused.stderr
    assert len(stack.primary.connects) == asked


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


def _worker_wake(stack: _Stack, name: str) -> Callable[[set[str]], int]:
    port = _free_port()
    lock = threading.Lock()
    started = False

    def wake(logins: set[str]) -> int:
        nonlocal started
        with lock:
            if not started:
                (stack.root / "workspace").mkdir(parents=True)
                (stack.root / "workspace" / "shared").write_text("shared worker\n", encoding="utf-8")
                _own(stack.root, "1000:1000")
            # Reproject on every admitted connect, not when the test edits config.
            keys = [
                key
                for person in stack.tailnet.people.values()
                if person.login in logins
                for key in stack.primary.config.tailnet_ssh.authorized_keys[person.requester_id]
            ]
            _docker(
                "run",
                "--rm",
                "--interactive",
                "--user",
                "1000:1000",
                "--volume",
                f"{stack.root}:{_WORKER_ROOT}",
                _WORKER_IMAGE,
                "python",
                "-c",
                _WRITE_PROJECTION,
                _PROJECTION,
                stdin=json.dumps({"link": stack.link.model_dump(mode="json"), "keys": keys}),
            )
            if not started:
                _docker(
                    "run",
                    "--detach",
                    "--name",
                    name,
                    "--user",
                    "1000:1000",
                    *_READ_ONLY_ROOT,
                    "--cap-drop",
                    "ALL",
                    *_security_options(stack.root.parent),
                    "--volume",
                    f"{stack.root}:{_WORKER_ROOT}",
                    "--volume",
                    f"{stack.root}/.runtime:{_WORKER_ROOT}/.runtime:ro",
                    "--publish",
                    f"127.0.0.1:{port}:{WORKER_SSH_PORT}",
                    _WORKER_IMAGE,
                    "python",
                    "-c",
                    _SERVE,
                    _PROJECTION,
                    str(session.LAUNCH_PATH),
                    json.dumps(_LAUNCH),
                )
                _eventually("worker sshd banner", lambda: _answers_ssh(port), seconds=60)
                started = True
        return port

    return wake


@pytest.fixture(scope="module")
def tailnet(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Tailnet]:
    """One Headscale with two authorized logins and a third login unknown to MindRoom."""
    if os.environ.get("MINDROOM_TAILNET_SSH_E2E") != "1":
        pytest.skip("needs Docker, Headscale and the built images")
    assert _GATEWAY_IMAGE, "Set MINDROOM_TAILNET_SSH_E2E_GATEWAY_IMAGE"
    assert _WORKER_IMAGE, "Set MINDROOM_TAILNET_SSH_E2E_WORKER_IMAGE"
    prefix = f"mindroom-e2e-{secrets.token_hex(4)}"
    folder = tmp_path_factory.mktemp("tailnet")
    port = _free_port()
    config = folder / "headscale.yaml"
    config.write_text(
        _HEADSCALE_CONFIG.format(port=port, metrics_port=_free_port(), grpc_port=_free_port()),
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
        for login in ("alice", "bob", "mallory"):
            key = folder / login
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", login, "-f", key], check=True)
            person = people[login] = _Person(login, f"@{login}:example.org", f"{prefix}-{login}", key)
            _docker(
                *("run", "--detach", "--name", person.device, "--network", "host"),
                *("--env", f"TS_AUTHKEY={_pre_auth_key(headscale, login)}", "--env", f"TS_HOSTNAME={login}-laptop"),
                *("--env", "TS_USERSPACE=true", "--env", f"TS_EXTRA_ARGS=--login-server={login_server}"),
                _TAILSCALE_IMAGE,
            )
        for person in people.values():
            _eventually(f"the device of {person.login} to join", lambda device=person.device: _joined(device))
        yield _Tailnet(prefix, folder, headscale, login_server, people)
    finally:
        _remove(prefix)


@pytest.fixture(scope="module")
def operator_key(tailnet: _Tailnet) -> SecretStr:
    """Reusable only in this throwaway tailnet."""
    return SecretStr(_pre_auth_key(tailnet.headscale, "operator"))


@pytest.fixture
def stack(tailnet: _Tailnet, operator_key: SecretStr, tmp_path: Path) -> Iterator[_Stack]:
    """Use a fresh shared worker key and gateway per test (their rate limits are independent)."""
    suffix = secrets.token_hex(4)
    prefix = f"{tailnet.prefix}-{suffix}"
    config_path = tmp_path / "config.yaml"
    config_path.touch()
    paths = resolve_runtime_paths(
        config_path=config_path,
        storage_path=tmp_path / "storage",
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_NAMESPACE": "",
            "MINDROOM_WORKER_BACKEND": "docker",
        },
    )
    admitted = [tailnet.people[login] for login in ("alice", "bob")]
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "gpt-6-astra"}},
            "agents": {
                f"shell_{suffix}": {
                    "display_name": "Shared shell",
                    "worker_scope": "shared",
                    "tools": ["shell"],
                    "tailnet_ssh": True,
                    "access": {"users": [person.requester_id for person in admitted]},
                },
            },
            "tailnet_ssh": {
                "logins": {person.requester_id: person.login for person in admitted},
                "authorized_keys": {person.requester_id: [person.public_key] for person in admitted},
            },
        },
        paths,
    )
    primary = _Primary(config, paths)
    [row] = primary.roster()
    assert row["allowed_logins"] == ["alice", "bob"]
    [link] = read_links(paths).links
    stack = _Stack(tailnet, primary, link, f"{prefix}-gateway", f"{prefix}-state", tailnet.folder / prefix)
    primary.warm = _worker_wake(stack, f"{prefix}-worker")
    private = serialization.load_ssh_private_key(link.host_key.encode(), password=None)
    key = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    with (tailnet.folder / "known_hosts").open("a", encoding="utf-8") as known_hosts:
        known_hosts.write(f"{link.hostname} {key}\n")
    served = uvicorn.Server(
        uvicorn.Config(primary.app(), host="127.0.0.1", port=primary.port, log_level="warning", ws="none"),
    )
    serving = threading.Thread(target=served.run, daemon=True)
    environment = {
        "TAILNET_SSH_PRIMARY_URL": f"http://127.0.0.1:{primary.port}",
        "TAILNET_SSH_GATEWAY_TOKEN": primary.token,
        "TAILNET_SSH_LOGIN_SERVER": tailnet.login_server,
        "TAILNET_SSH_AUTH_KEY": operator_key.get_secret_value(),
    }
    serving.start()
    try:
        _eventually("the primary to listen", lambda: served.started, seconds=10)
        _docker(
            "run",
            "--detach",
            "--name",
            stack.gateway,
            "--network",
            "host",
            *_READ_ONLY_ROOT,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--volume",
            f"{stack.state}:/state",
            *[arg for name, value in environment.items() for arg in ("--env", f"{name}={value}")],
            _GATEWAY_IMAGE,
        )
        for person in tailnet.people.values():
            _eventually(f"{person.login} to see {link.hostname}", lambda p=person: link.hostname in _peers(p))
        yield stack
    finally:
        for container in _docker("ps", "--all", "--filter", f"name=^{prefix}", "--format", "{{.Names}}").split():
            print(f"--- {container}\n{_redact(_logs(container), {operator_key.get_secret_value(), primary.token})}")
        _remove(prefix)
        if stack.root.exists():
            _own(stack.root, f"{os.getuid()}:{os.getgid()}")
        served.should_exit = True
        serving.join(timeout=10)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_unknown_login_is_refused_before_wake(stack: _Stack) -> None:
    """Even a key in the roster is not enough if WhoIs returns an unknown login."""
    alice, mallory = (stack.tailnet.people[login] for login in ("alice", "mallory"))
    _assert_turned_away(stack, mallory, alice.key)
    assert not stack.root.exists(), "Unknown login woke the worker"
    admitted = _run_ssh([*_client(stack.tailnet, alice), stack.link.hostname, "true"])
    assert admitted.returncode == 0, admitted.stderr


def test_two_authorized_logins_exec_on_one_shared_worker(stack: _Stack) -> None:
    """Both login/key holders reach the same stable link and its read-only SSH projection."""
    host = stack.link.hostname
    for login in ("alice", "bob"):
        person = stack.tailnet.people[login]
        done = _run_ssh([*_client(stack.tailnet, person), host, "id -u; pwd; cat shared"])
        assert (done.returncode, done.stdout.splitlines()) == (0, ["1000", _WORKSPACE, "shared worker"]), done.stderr
        assert stack.primary.connects[-1] == (stack.link.id, {"peer_login": login, "peer_node": f"{login}-laptop"})
    done = _run_ssh([*_client(stack.tailnet, stack.tailnet.people["alice"]), host, f"touch {_PROJECTION}/planted"])
    assert (done.returncode, "Read-only file system" in done.stderr) == (1, True), done.stderr


def test_pty_scp_sftp_rsync(stack: _Stack, tmp_path: Path) -> None:
    """A login gets a terminal, and a file comes back as it was sent by scp, sftp and rsync."""
    alice = stack.tailnet.people["alice"]
    host = stack.link.hostname
    ssh = _client(stack.tailnet, alice)

    terminal = _run_ssh([*ssh, "-tt", host, "tty"])
    assert terminal.returncode == 0, terminal.stderr
    assert terminal.stdout.strip().startswith("/dev/pts/")

    sent = tmp_path / "sent" / "notes.bin"
    sent.parent.mkdir(mode=0o755)
    sent.write_bytes(secrets.token_bytes(1 << 20))
    sent.chmod(0o644)
    scp = _client(stack.tailnet, alice, "scp")
    rsync = ["rsync", "--archive", "--rsh", shlex.join(ssh)]
    transfers = [
        ([*scp, str(sent), f"{host}:scp.bin"], None),
        ([*scp, f"{host}:scp.bin", str(tmp_path / "scp.bin")], None),
        (
            [*_client(stack.tailnet, alice, "sftp"), "-b", "-", host],
            f"put {sent} sftp.bin\nget sftp.bin {tmp_path / 'sftp.bin'}\n",
        ),
        ([*rsync, f"{sent.parent}/", f"{host}:rsync/"], None),
        ([*rsync, f"{host}:rsync/", f"{tmp_path / 'rsync'}/"], None),
    ]
    for argv, stdin in transfers:
        done = _run_ssh(argv, stdin=stdin)
        assert done.returncode == 0, done.stderr
    returned = [tmp_path / "scp.bin", tmp_path / "sftp.bin", tmp_path / "rsync" / "notes.bin"]
    assert [_sha256(path) for path in returned] == [_sha256(sent)] * 3
    # What was sent is in the workspace, which is the session's home.
    landed = [stack.root / "workspace" / name for name in ("scp.bin", "sftp.bin", "rsync/notes.bin")]
    assert [_sha256(path) for path in landed] == [_sha256(sent)] * 3


def test_forwarding_is_refused(stack: _Stack, tmp_path: Path) -> None:
    """A session forwards no port, no stdio and no agent."""
    alice = stack.tailnet.people["alice"]
    host = stack.link.hostname
    ssh = _client(stack.tailnet, alice)
    sshd = f"127.0.0.1:{WORKER_SSH_PORT}"

    # A local forward's listener is the client's own, so the worker refuses when something connects to it.
    port = _free_port()
    with _ssh_session([*ssh, "-L", f"{port}:{sshd}", host, "echo up; sleep 60"]) as local:
        _await_up(local)
        with socket.create_connection(("127.0.0.1", port), timeout=30) as through:
            assert through.recv(8) == b""
        assert "open failed" in _end(local)[1]

    remote = _run_ssh([*ssh, "-o", "ExitOnForwardFailure=yes", "-R", f"0:{sshd}", host, "true"])
    assert remote.returncode == 255
    assert "remote port forwarding failed" in remote.stderr
    stdio = _run_ssh([*ssh, "-W", sshd, host])
    assert stdio.returncode == 255
    assert "stdio forwarding failed" in stdio.stderr

    # The session's program drops what sshd says of an agent, so the session looks for the socket sshd makes for a
    # forwarded one, in the folders the worker's user can write.
    socket_path = tmp_path / "agent"
    agent = subprocess.Popen(["ssh-agent", "-D", "-a", socket_path], stdout=subprocess.DEVNULL)
    try:
        _eventually("the SSH agent's socket", socket_path.exists, seconds=10)
        forwarded = _run_ssh(
            [*ssh, "-A", host, f"find /tmp {_WORKER_ROOT} -type s && echo end"],
            env={**os.environ, "SSH_AUTH_SOCK": str(socket_path)},
        )
    finally:
        agent.terminate()
        agent.wait(timeout=10)
    assert (forwarded.returncode, forwarded.stdout.split()) == (0, ["end"]), forwarded.stderr


def test_wrong_key_and_password_are_refused(stack: _Stack, tmp_path: Path) -> None:
    """The worker accepts only a configured SSH public key, not a password."""
    alice = stack.tailnet.people["alice"]
    host = stack.link.hostname
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", other], check=True)

    wrong_key = _run_ssh([*_client(stack.tailnet, alice, key=other), host, "true"])
    password = _run_ssh(
        [*_client(stack.tailnet, alice), "-o", "PubkeyAuthentication=no", host, "true"],
    )
    for refused in (wrong_key, password):
        assert refused.returncode == 255
        # The worker names the ways it offers, and a password is not one.
        assert "Permission denied (publickey)." in refused.stderr


def _assert_session_still_works(held: subprocess.Popen[str]) -> None:
    assert held.stdin is not None
    assert held.stdout is not None
    held.stdin.write("still here\n")
    held.stdin.flush()
    ready, _, _ = select.select([held.stdout], [], [], 30)
    assert ready, "No reply from the already-open session"
    assert held.stdout.readline() == "still here\n"
    assert held.wait(timeout=15) == 0


_ROUND_TRIP = 'echo up; IFS= read -r line; printf "%s\\n" "$line"'


def test_key_removal_refuses_new_connection_without_cutting_session(stack: _Stack) -> None:
    """Alice still has a valid login, but a newly projected allowlist no longer accepts her old key."""
    alice, bob = (stack.tailnet.people[login] for login in ("alice", "bob"))
    ssh = [*_client(stack.tailnet, alice), stack.link.hostname]
    with _ssh_session([*ssh, _ROUND_TRIP]) as held:
        _await_up(held)
        # Keep Alice's login present so the new connection must reach OpenSSH's key check.
        stack.primary.config.tailnet_ssh.authorized_keys[alice.requester_id] = [bob.public_key]
        done = _run_ssh([*ssh, "true"])
        assert done.returncode == 255
        assert "Permission denied (publickey)." in done.stderr
        _assert_session_still_works(held)


def test_omission_drains_and_gateway_restart_reuses_node_state(stack: _Stack) -> None:
    """Omission refuses new connections, lets the live one drain, and preserves the tailnet identity."""
    alice = stack.tailnet.people["alice"]
    ssh = [*_client(stack.tailnet, alice), stack.link.hostname]
    identity = _registered_node(stack)
    assert identity is not None
    logins = stack.primary.config.tailnet_ssh.logins.copy()
    with _ssh_session([*ssh, _ROUND_TRIP]) as held:
        _await_up(held)
        stack.primary.config.tailnet_ssh.logins.clear()
        # Reconcile is serial: by a second empty GET it processed the first omission.
        _eventually("the gateway to reconcile omission", lambda: stack.primary.omissions >= 2)
        _assert_turned_away(stack, alice, alice.key)
        _assert_session_still_works(held)

    _docker("stop", stack.gateway)
    # A new process with the same volume has no in-memory tsnet state.
    stack.primary.config.tailnet_ssh.logins.update(logins)
    _docker("start", stack.gateway)
    _eventually("SSH after gateway restart", lambda: _run_ssh([*ssh, "true"]).returncode == 0)
    assert _registered_node(stack) == identity


def test_connection_held_through_a_120s_start(stack: _Stack) -> None:
    """A client with no setting of its own waits out a long wake and sees the pre-banner."""
    alice = stack.tailnet.people["alice"]
    endpoint = stack.link.hostname
    stack.primary.delay = 120
    with ExitStack() as held:
        clients = [
            held.enter_context(_ssh_session([*_client(stack.tailnet, alice), *verbose, endpoint, "true"]))
            for verbose in ([], ["-v"])
        ]
        outputs = [client.communicate(timeout=300) for client in clients]
    assert [client.returncode for client in clients] == [0, 0], outputs
    assert "MindRoom: waking worker" in outputs[1][1]
