#!/usr/bin/env python3
"""Live isolation smoke test for Agent Vault multi-identity token scoping.

This proves the security properties MindRoom's worker-scoped egress brokers
rely on before real user secrets are stored behind per-worker bridges. The
identity model matches per-worker bridge provisioning: one vault per worker
identity, plus one proxy-role Agent Vault agent per worker whose token is the
bridge's upstream credential.

1. agent A's token injects only vault A's credential
2. agent B's token injects only vault B's credential
3. agent A's token gets no injection for a service only vault B configured
4. a garbage session token cannot egress through the vault proxy at all
5. a proxy request without any session token is refused
6. agent A's token cannot list or decrypt vault B's credentials via the API
7. a proxy-role agent token cannot decrypt even its own vault's credentials

Like ``agent_vault_bridge_live_smoke.py`` this intentionally uses the real
``infisical/agent-vault`` image and Docker containers, so it is not part of
normal pytest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from mindroom.egress.agent_vault_bridge import start_adapter

AGENT_VAULT_IMAGE = os.environ.get("AGENT_VAULT_IMAGE", "infisical/agent-vault:latest")
WORKER_IMAGE = os.environ.get("MINDROOM_AGENT_VAULT_SMOKE_WORKER_IMAGE", "python:3.13-alpine")
DOCKER_HOST_GATEWAY = "host.docker.internal"
SHARED_ECHO_HOST = "shared-echo.test"
ONLY_B_ECHO_HOST = "only-b-echo.test"
ECHO_PORT = 80
ECHO_SUBNET = "203.0.113.0/24"
ECHO_IPV4 = "203.0.113.10"
SECRET_A = "vault-a-isolation-secret"  # noqa: S105
SECRET_B = "vault-b-isolation-secret"  # noqa: S105
MASTER_PASSWORD = "mindroom-agent-vault-isolation-master-password"  # noqa: S105
OWNER_PASSWORD = "mindroom-agent-vault-isolation-owner-password"  # noqa: S105
OWNER_EMAIL = "owner@example.test"
VAULT_A = "worker-vault-a"
VAULT_B = "worker-vault-b"
AGENT_A = "worker-agent-a"
AGENT_B = "worker-agent-b"
OWNER_HOME = "/tmp/agent-vault-home-owner"  # noqa: S108
ECHO_SERVER_CODE = r"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class HeaderEchoHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path != "/headers":
            self.send_error(404, "Not Found")
            return
        payload = json.dumps(
            {"headers": {key: value for key, value in self.headers.items()}},
            sort_keys=True,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

ThreadingHTTPServer(("0.0.0.0", __ECHO_PORT__), HeaderEchoHandler).serve_forever()
""".replace("__ECHO_PORT__", str(ECHO_PORT))


def _run(
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
    timeout_seconds: float = 300,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            input=input_text,
            text=True,
            check=False,
            timeout=timeout_seconds,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except subprocess.TimeoutExpired as exc:
        command = " ".join(args)
        output = exc.stdout or ""
        msg = f"Command timed out after {timeout_seconds}s: {command}\n{output}"
        raise TimeoutError(msg) from exc
    if check and result.returncode != 0:
        command = " ".join(args)
        msg = f"Command failed ({result.returncode}): {command}\n{result.stdout}"
        raise RuntimeError(msg)
    return result


def _vault_cli(
    container: str,
    home: str,
    args: list[str],
    *,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the agent-vault CLI as one identity, keyed by a per-identity HOME."""
    docker_args = ["docker", "exec", "-e", f"HOME={home}"]
    if input_text is not None:
        docker_args.append("-i")
    docker_args.extend([container, "agent-vault", *args])
    return _run(docker_args, input_text=input_text, check=check)


def _docker_port(container: str, private_port: int) -> int:
    result = _run(["docker", "port", container, f"{private_port}/tcp"])
    raw = result.stdout.strip().rsplit(":", 1)[-1]
    return int(raw)


def _start_echo_container(container: str, network: str) -> None:
    _run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "--network",
            network,
            "--ip",
            ECHO_IPV4,
            "--network-alias",
            SHARED_ECHO_HOST,
            "--network-alias",
            ONLY_B_ECHO_HOST,
            WORKER_IMAGE,
            "python",
            "-c",
            ECHO_SERVER_CODE,
        ],
    )
    _wait_for_echo(container)


def _wait_for_echo(container: str) -> None:
    probe_code = f"""
import urllib.request
urllib.request.urlopen("http://127.0.0.1:{ECHO_PORT}/headers", timeout=2).read()
"""
    deadline = time.monotonic() + 45
    last_output = ""
    while time.monotonic() < deadline:
        result = _run(
            ["docker", "exec", container, "python", "-c", probe_code],
            check=False,
            timeout_seconds=5,
        )
        if result.returncode == 0:
            return
        last_output = result.stdout
        time.sleep(1)
    logs = _run(["docker", "logs", container], check=False).stdout
    msg = f"Echo container did not become healthy:\n{last_output}\n{logs}"
    raise TimeoutError(msg)


def _wait_for_health(api_port: int) -> None:
    url = f"http://127.0.0.1:{api_port}/health"
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    msg = f"Agent Vault did not become healthy at {url}"
    raise TimeoutError(msg)


def _register_identity(container: str, home: str, email: str, password: str) -> None:
    _vault_cli(
        container,
        home,
        [
            "auth",
            "register",
            "--address",
            "http://127.0.0.1:14321",
            "--email",
            email,
            "--password-stdin",
        ],
        input_text=f"{password}\n",
    )
    _vault_cli(
        container,
        home,
        [
            "auth",
            "login",
            "--address",
            "http://127.0.0.1:14321",
            "--email",
            email,
            "--password-stdin",
        ],
        input_text=f"{password}\n",
    )


def _configure_vault(
    container: str,
    vault: str,
    agent: str,
    secret: str,
    service_hosts: list[str],
) -> str:
    """Create one vault and one proxy-role agent for it; return the agent token.

    This mirrors per-worker bridge provisioning: the instance owner creates a
    vault per worker identity plus a proxy-role agent whose token becomes that
    worker bridge's upstream credential.
    """
    _vault_cli(container, OWNER_HOME, ["vault", "create", vault])
    _vault_cli(
        container,
        OWNER_HOME,
        ["vault", "credential", "set", f"SERVICE_TOKEN={secret}", "--vault", vault],
    )
    for index, host in enumerate(service_hosts):
        _vault_cli(
            container,
            OWNER_HOME,
            [
                "vault",
                "service",
                "add",
                "--vault",
                vault,
                "--name",
                f"echo-{index}",
                "--host",
                host,
                "--auth-type",
                "bearer",
                "--token-key",
                "SERVICE_TOKEN",
            ],
        )
    result = _vault_cli(
        container,
        OWNER_HOME,
        ["agent", "create", agent, "--vault", f"{vault}:proxy", "--token-only"],
    )
    return result.stdout.strip()


def _agent_cli(
    container: str,
    token: str,
    args: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the agent-vault CLI authenticated by one agent token."""
    return _run(
        [
            "docker",
            "exec",
            "-e",
            f"AGENT_VAULT_TOKEN={token}",
            "-e",
            "AGENT_VAULT_ADDR=http://127.0.0.1:14321",
            container,
            "agent-vault",
            *args,
        ],
        check=check,
    )


def _run_worker(adapter_port: int, target_host: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proxy_url = f"http://{DOCKER_HOST_GATEWAY}:{adapter_port}"
    worker_code = r"""
import json
import os
import urllib.request

leaked = {
    key: value
    for key, value in os.environ.items()
    if "AGENT_VAULT" in key or "TOKEN" in key or "SECRET" in key
}
if leaked:
    raise SystemExit(f"worker env leaked secret-like names: {sorted(leaked)}")

with urllib.request.urlopen("http://__TARGET_HOST__/headers", timeout=20) as response:
    data = json.loads(response.read().decode("utf-8"))
print(json.dumps(data["headers"], sort_keys=True))
""".replace("__TARGET_HOST__", target_host)
    return _run(
        [
            "docker",
            "run",
            "--rm",
            f"--add-host={DOCKER_HOST_GATEWAY}:host-gateway",
            "-e",
            f"HTTP_PROXY={proxy_url}",
            "-e",
            f"HTTPS_PROXY={proxy_url}",
            "-e",
            f"http_proxy={proxy_url}",
            "-e",
            f"https_proxy={proxy_url}",
            WORKER_IMAGE,
            "python",
            "-c",
            worker_code,
        ],
        check=check,
    )


def _parse_worker_headers(output: str) -> dict[str, str]:
    for line in reversed(output.splitlines()):
        raw_line = line.strip()
        if not raw_line:
            continue
        try:
            data = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        headers: dict[str, str] = {}
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                msg = f"Worker emitted non-string header data: {data}"
                raise TypeError(msg)
            headers[key] = value
        return headers
    msg = f"Worker did not emit JSON headers:\n{output}"
    raise ValueError(msg)


def _request_via_raw_proxy(proxy_port: int, target_host: str) -> tuple[int | None, str]:
    """Issue one proxied request straight at the vault MITM port with no session token."""
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = urllib.request.build_opener(handler)
    try:
        with opener.open(f"http://{target_host}/headers", timeout=10) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _assert_no_secret_leak(text: str, *, context: str) -> None:
    for name, secret in (("SECRET_A", SECRET_A), ("SECRET_B", SECRET_B)):
        _assert(secret not in text, f"{context} leaked {name}: {text}")


def main() -> int:  # noqa: PLR0915
    """Run the live Docker isolation smoke and return a process exit code."""
    if shutil.which("docker") is None:
        print("docker is required for this live smoke", file=sys.stderr)
        return 1

    suffix = os.getpid()
    network = f"mindroom-agent-vault-isolation-{suffix}"
    container = f"mindroom-agent-vault-isolation-vault-{suffix}"
    echo_container = f"mindroom-agent-vault-isolation-echo-{suffix}"
    results: dict[str, object] = {"agent_vault_image": AGENT_VAULT_IMAGE}
    try:
        # Agent Vault refuses local/private proxy targets, so this uses a TEST-NET bridge.
        _run(["docker", "network", "create", "--subnet", ECHO_SUBNET, network])
        _start_echo_container(echo_container, network)
        _run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--network",
                network,
                "-p",
                "127.0.0.1::14321",
                "-p",
                "127.0.0.1::14322",
                "-e",
                f"AGENT_VAULT_MASTER_PASSWORD={MASTER_PASSWORD}",
                AGENT_VAULT_IMAGE,
                "server",
                "--host",
                "0.0.0.0",  # noqa: S104
                "--port",
                "14321",
                "--mitm-port",
                "14322",
            ],
        )
        api_port = _docker_port(container, 14321)
        proxy_port = _docker_port(container, 14322)
        _wait_for_health(api_port)

        # First registration becomes the instance owner, like hosted bootstrap.
        _register_identity(container, OWNER_HOME, OWNER_EMAIL, OWNER_PASSWORD)

        token_a = _configure_vault(
            container,
            VAULT_A,
            AGENT_A,
            SECRET_A,
            [SHARED_ECHO_HOST],
        )
        token_b = _configure_vault(
            container,
            VAULT_B,
            AGENT_B,
            SECRET_B,
            [SHARED_ECHO_HOST, ONLY_B_ECHO_HOST],
        )
        _assert(bool(token_a) and bool(token_b), "expected non-empty agent tokens")
        _assert(token_a != token_b, "expected distinct agent tokens per vault")

        upstream_proxy_url = f"http://127.0.0.1:{proxy_port}"

        # 1 + 2: each token injects exactly its own vault's credential.
        with start_adapter(
            host="0.0.0.0",  # noqa: S104
            port=0,
            upstream_proxy_url=upstream_proxy_url,
            session_token=token_a,
        ) as adapter_a:
            shared_via_a = _parse_worker_headers(
                _run_worker(adapter_a.port, SHARED_ECHO_HOST).stdout,
            )
            only_b_via_a = _run_worker(adapter_a.port, ONLY_B_ECHO_HOST, check=False)
        with start_adapter(
            host="0.0.0.0",  # noqa: S104
            port=0,
            upstream_proxy_url=upstream_proxy_url,
            session_token=token_b,
        ) as adapter_b:
            shared_via_b = _parse_worker_headers(
                _run_worker(adapter_b.port, SHARED_ECHO_HOST).stdout,
            )

        _assert(
            shared_via_a.get("Authorization") == f"Bearer {SECRET_A}",
            f"token A did not inject vault A credential: {shared_via_a}",
        )
        _assert(
            shared_via_b.get("Authorization") == f"Bearer {SECRET_B}",
            f"token B did not inject vault B credential: {shared_via_b}",
        )
        results["token_a_injects_only_vault_a"] = True
        results["token_b_injects_only_vault_b"] = True

        # 3: token A gets no credential for a service only vault B configured.
        _assert_no_secret_leak(only_b_via_a.stdout, context="token A request to vault-B-only service")
        if only_b_via_a.returncode == 0:
            only_b_headers = _parse_worker_headers(only_b_via_a.stdout)
            _assert(
                "Authorization" not in only_b_headers,
                f"token A request to vault-B-only service got an injected credential: {only_b_headers}",
            )
            results["agent_a_to_vault_b_service"] = "passed through without injection"
        else:
            results["agent_a_to_vault_b_service"] = "request refused"

        # 4: a garbage session token cannot egress through the vault proxy.
        with start_adapter(
            host="0.0.0.0",  # noqa: S104
            port=0,
            upstream_proxy_url=upstream_proxy_url,
            session_token="garbage-token-that-should-never-work",  # noqa: S106
        ) as adapter_garbage:
            garbage_result = _run_worker(adapter_garbage.port, SHARED_ECHO_HOST, check=False)
        _assert_no_secret_leak(garbage_result.stdout, context="garbage token request")
        if garbage_result.returncode == 0:
            garbage_headers = _parse_worker_headers(garbage_result.stdout)
            _assert(
                "Authorization" not in garbage_headers,
                f"garbage token request got an injected credential: {garbage_headers}",
            )
            results["garbage_session"] = "passed through without injection"
        else:
            results["garbage_session"] = "request refused"

        # 5: no session token at all, straight at the MITM port.
        status, body = _request_via_raw_proxy(proxy_port, SHARED_ECHO_HOST)
        _assert_no_secret_leak(body, context="tokenless raw proxy request")
        if status == 200:
            raw_headers = _parse_worker_headers(body)
            _assert(
                "Authorization" not in raw_headers,
                f"tokenless raw proxy request got an injected credential: {raw_headers}",
            )
            results["tokenless_proxy_request"] = "passed through without injection"
        else:
            results["tokenless_proxy_request"] = f"refused (status={status})"

        # 6: agent A's token cannot list or decrypt vault B's credentials.
        cross_read = _agent_cli(
            container,
            token_a,
            ["vault", "credential", "list", "--vault", VAULT_B],
            check=False,
        )
        _assert(
            cross_read.returncode != 0,
            f"agent A unexpectedly listed vault B credentials:\n{cross_read.stdout}",
        )
        _assert_no_secret_leak(cross_read.stdout, context="agent A cross-vault credential list")
        cross_get = _agent_cli(
            container,
            token_a,
            ["vault", "credential", "get", "SERVICE_TOKEN", "--vault", VAULT_B],
            check=False,
        )
        _assert(
            cross_get.returncode != 0,
            f"agent A unexpectedly decrypted a vault B credential:\n{cross_get.stdout}",
        )
        _assert_no_secret_leak(cross_get.stdout, context="agent A cross-vault credential get")
        results["api_cross_vault_read"] = "refused"
        results["api_cross_vault_decrypt"] = "refused"

        # 7: a proxy-role agent cannot decrypt even its own vault's credentials.
        own_get = _agent_cli(
            container,
            token_a,
            ["vault", "credential", "get", "SERVICE_TOKEN", "--vault", VAULT_A],
            check=False,
        )
        _assert(
            own_get.returncode != 0,
            f"proxy-role agent A unexpectedly decrypted its own credential:\n{own_get.stdout}",
        )
        _assert_no_secret_leak(own_get.stdout, context="agent A own-vault credential get")
        results["proxy_role_own_vault_decrypt"] = "refused"

        print(json.dumps(results, sort_keys=True, indent=2))
        return 0
    finally:
        _run(["docker", "rm", "-f", container], check=False)
        _run(["docker", "rm", "-f", echo_container], check=False)
        _run(["docker", "network", "rm", network], check=False)


if __name__ == "__main__":
    raise SystemExit(main())
