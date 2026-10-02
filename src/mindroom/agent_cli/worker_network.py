"""Fail-closed checks for the Docker minimal-mode profile.

Worker shells keep network access, including to the primary API. These checks
prove the primary API rejects worker credentials and that the worker reaches this
primary's CLI routes with its own grant; they do not claim TCP isolation.
"""

from __future__ import annotations

import asyncio
import ipaddress
import secrets
from typing import TYPE_CHECKING

import httpx

from mindroom.agent_cli.worker_protocol import CLI_DOCKER_HOST_ALIAS, safe_origin
from mindroom.runtime_state import get_api_server_address

if TYPE_CHECKING:
    from mindroom.agent_cli.worker_protocol import CliWorkerLaunch
    from mindroom.constants import RuntimePaths

_PRIMARY_URL_ENV = "MINDROOM_AGENT_CLI_PRIMARY_URL"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def cli_primary_url(runtime_paths: RuntimePaths) -> str | None:
    """Return the MindRoom API origin as reached from inside a Docker worker, when it is known.

    An explicit setting wins; otherwise the running API's address is used, through the
    Docker host alias when it listens on every interface. Raises ValueError for a
    malformed explicit setting.
    """
    if configured := runtime_paths.env_value(_PRIMARY_URL_ENV):
        return safe_origin(configured)
    api_address = get_api_server_address()
    if api_address is None or _loopback(api_address.host):
        return None
    host = CLI_DOCKER_HOST_ALIAS if api_address.host in {"0.0.0.0", "::"} else api_address.host  # noqa: S104
    return safe_origin(f"http://{f'[{host}]' if ':' in host else host}:{api_address.port}")


def cli_deployment_problems(runtime_paths: RuntimePaths) -> list[str]:
    """Return every primary auth or endpoint setting that keeps Docker minimal mode from running safely.

    Each problem is phrased as its fix, so callers can show the whole list at once.
    """
    problems = []
    # Worker shells keep network access to the primary, so its API must not be open.
    if not runtime_paths.env_value("MINDROOM_API_KEY"):
        problems.append(
            "Set `MINDROOM_API_KEY` to a long random secret, because minimal-mode worker shells can reach "
            "the MindRoom API; the dashboard then asks for this key.",
        )
    if runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED") and not runtime_paths.env_flag(
        "MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT",
    ):
        problems.append(
            "Set `MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT=true`, because worker shells could forge "
            "trusted-upstream headers.",
        )
    if runtime_paths.env_flag("OPENAI_COMPAT_ALLOW_UNAUTHENTICATED"):
        problems.append(
            "Unset `OPENAI_COMPAT_ALLOW_UNAUTHENTICATED`, because worker shells could run agents through "
            "the unauthenticated OpenAI-compatible API.",
        )
    try:
        primary_url = cli_primary_url(runtime_paths)
    except ValueError:
        problems.append(f"Set `{_PRIMARY_URL_ENV}` to a plain `http(s)://host:port` origin.")
    else:
        if primary_url is None:
            problems.append(
                "Serve the MindRoom API on a non-loopback address, or set "
                f"`{_PRIMARY_URL_ENV}` to the MindRoom API origin as reached from inside worker containers.",
            )
    return problems


def validate_cli_deployment(runtime_paths: RuntimePaths) -> str:
    """Return the worker-facing primary URL, rejecting settings which expose authority to the worker."""
    if problems := cli_deployment_problems(runtime_paths):
        raise ValueError(" ".join(problems))
    primary_url = cli_primary_url(runtime_paths)
    assert primary_url is not None
    return primary_url


def _headers_to_reject(*, grant: str, control_token: str) -> tuple[dict[str, str], ...]:
    return (
        {},
        {"Authorization": f"Bearer {grant}"},
        {"x-mindroom-sandbox-token": control_token},
        {"x-mindroom-sandbox-token": grant},
        {"x-forwarded-user": "admin", "x-auth-request-user": "admin", "x-auth-request-email": "admin@example.org"},
    )


async def probe_cli_network(
    launch: CliWorkerLaunch,
    *,
    control_token: str,
    client: httpx.AsyncClient,
) -> None:
    """Probe real existing routes from inside the worker, before installing its grant."""
    grant = launch.token.get_secret_value()
    primary_headers = _headers_to_reject(grant=grant, control_token=control_token)
    # Peers validate neither CLI grants nor this worker's derived token, so live
    # values would prove nothing there and only expose them to other workers.
    peer_headers = _headers_to_reject(grant=secrets.token_urlsafe(32), control_token=secrets.token_urlsafe(32))
    # Bound independent route checks without caching a changing peer inventory.
    limit = asyncio.Semaphore(8)

    async def check_route(url: str, *, peer: bool = False) -> None:
        async with limit:
            for headers in peer_headers if peer else primary_headers:
                try:
                    response = await client.get(url, headers=headers)
                except httpx.ConnectError:
                    if peer:
                        # Peers may start/retire after Docker inspection. Keep
                        # checking other credentials in case the listener opens.
                        continue
                    raise
                if response.status_code not in {401, 403}:
                    msg = "CLI worker can reach an endpoint without verified protected authority"
                    raise ValueError(msg)

    results = await asyncio.gather(
        check_route(f"{launch.primary_url}/api/config/raw"),
        check_route(f"{launch.primary_url}/v1/models"),
        *(check_route(f"{origin}/api/sandbox-runner/workers", peer=True) for origin in launch.control_urls),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
    # Only this primary accepts the worker's own grant, so success proves the URL
    # names this MindRoom and the negative checks above probed the real API.
    response = await client.post(
        f"{launch.primary_url}/api/agent-cli/operations",
        headers={"Authorization": f"Bearer {grant}"},
        json={"operation": "context.list"},
    )
    if response.status_code != 200:
        msg = "CLI worker cannot reach this MindRoom's CLI routes at its primary URL"
        raise ValueError(msg)
