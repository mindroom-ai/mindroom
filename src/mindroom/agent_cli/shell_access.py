"""Where minimal Bash runs and how its commands reach this MindRoom's CLI routes.

Minimal Bash is the agent's own shell. When that shell runs in MindRoom, the agent
is fully trusted (see docs/architecture/security-posture.md) and the CLI calls the
local API address. Otherwise the agent's ordinary worker calls MindRoom back over
the network, so the API must refuse requests without credentials.
"""

from __future__ import annotations

import ipaddress
import shlex
import sys
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from mindroom.agent_cli.shell_contract import AgentCliShellEnv
from mindroom.runtime_state import get_api_server_address
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_enabled_for_tool
from mindroom.workers.backends.docker_config import DOCKER_HOST_ALIAS
from mindroom.workers.runtime import primary_worker_backend_name

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = ["agent_cli_shell_env", "minimal_shell_problems"]

_PRIMARY_URL_ENV = "MINDROOM_AGENT_CLI_PRIMARY_URL"
# Run the CLI with this interpreter wherever the installer put console scripts; `-P` keeps a
# `mindroom` directory in the shell's working directory from shadowing the installed package.
_LAUNCHER = (
    f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -P -c "
    f'{shlex.quote("import sys; from mindroom.agent_cli.main import main; sys.exit(main())")} "$@"\n'
)


def _shell_runs_in_primary(config: Config, runtime_paths: RuntimePaths, agent_name: str) -> bool:
    return not sandbox_proxy_enabled_for_tool(
        "shell",
        runtime_paths=runtime_paths,
        worker_tools_override=config.get_agent_worker_tools(agent_name),
    )


def _local_bin_dir(runtime_paths: RuntimePaths) -> str:
    """Return a private directory exposing only `mindroom-agent`, not MindRoom's interpreter and dependencies.

    It lives in MindRoom's own storage rather than shared temp, where another local user
    could recreate the name with their own programs after a temp cleaner removed it.
    """
    bin_dir = runtime_paths.storage_root / "agent_cli_bin"
    launcher = bin_dir / "mindroom-agent"
    # A launcher left by an earlier installation runs that installation's interpreter.
    if not launcher.is_file() or launcher.read_text() != _LAUNCHER:
        bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        launcher.write_text(_LAUNCHER)
        launcher.chmod(0o700)
    return str(bin_dir)


def _safe_origin(value: str) -> str:
    """Require a concrete HTTP origin, without credential, query or path injection."""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(value)
    _ = parsed.port
    return value.rstrip("/")


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _worker_primary_url(runtime_paths: RuntimePaths) -> str | None:
    """Return the MindRoom API origin as reached from inside a worker, when it is known.

    An explicit setting wins; otherwise the running API's address is used, through the
    Docker host alias when it listens on every interface. Raises ValueError for a
    malformed explicit setting.
    """
    if configured := runtime_paths.env_value(_PRIMARY_URL_ENV):
        return _safe_origin(configured)
    api_address = get_api_server_address()
    if api_address is None or _loopback(api_address.host):
        return None
    host = api_address.host
    if host in {"0.0.0.0", "::"}:  # noqa: S104
        if primary_worker_backend_name(runtime_paths) != "docker":
            return None
        host = DOCKER_HOST_ALIAS
    return _safe_origin(f"http://{f'[{host}]' if ':' in host else host}:{api_address.port}")


def _worker_problems(runtime_paths: RuntimePaths) -> list[str]:
    problems = []
    # Worker shells reach the MindRoom API, so it must not be open.
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
        primary_url = _worker_primary_url(runtime_paths)
    except ValueError:
        problems.append(f"Set `{_PRIMARY_URL_ENV}` to a plain `http(s)://host:port` origin.")
    else:
        if primary_url is None:
            problems.append(
                f"Set `{_PRIMARY_URL_ENV}` to the MindRoom API origin as reached from inside worker shells.",
            )
    return problems


def minimal_shell_problems(config: Config, runtime_paths: RuntimePaths, agent_name: str) -> list[str]:
    """Return what keeps minimal Bash from reaching MindRoom where this agent's shell runs, each phrased as its fix."""
    # Only this process's API serves the response grants, wherever the shell runs.
    if get_api_server_address() is None:
        return ["Run MindRoom with its API server (without `--no-api`), because the CLI calls back through it."]
    if _shell_runs_in_primary(config, runtime_paths, agent_name):
        return []
    return _worker_problems(runtime_paths)


def agent_cli_shell_env(config: Config, runtime_paths: RuntimePaths, agent_name: str, token: str) -> AgentCliShellEnv:
    """Return the CLI environment for one response's commands, once `minimal_shell_problems` found none."""
    if _shell_runs_in_primary(config, runtime_paths, agent_name):
        api_address = get_api_server_address()
        assert api_address is not None
        return AgentCliShellEnv(api_address.base_url, token, _local_bin_dir(runtime_paths))
    primary_url = _worker_primary_url(runtime_paths)
    assert primary_url is not None
    return AgentCliShellEnv(primary_url, token)
