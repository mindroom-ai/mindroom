"""Whether the broker and its personal page are on, which agents use it, the worker env overlay, and runner CA bundles."""

from __future__ import annotations

from collections.abc import Iterable, Mapping  # noqa: TC003
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, urlsplit

from mindroom.constants import EXECUTION_ENV_TOOL_NAMES, RuntimePaths
from mindroom.origins import connections_sign_in_enabled
from mindroom.runtime_env_policy import EGRESS_BROKER_ENV_BY_KEY
from mindroom.tool_system.tool_environment import git_config_env
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.tool_system.agent_tools import AgentTools

__all__ = [
    "BROKER_CA_PEM_ENV",
    "RUNNER_CA_BUNDLE_ENV",
    "EgressAgentScope",
    "apply_runner_ca_bundle",
    "broker_execution_env",
    "broker_git_config",
    "broker_serves_primary_shell",
    "egress_agent_scope",
    "egress_broker_enabled",
    "egress_tool_unavailable",
    "egress_tool_with_only_primary_python",
    "personal_page_served",
    "primary_callback_hosts",
]

BROKER_CA_PEM_ENV = "MINDROOM_EGRESS_BROKER_CA_PEM"
# The env names the runner points at the combined CA bundle; NODE_EXTRA_CA_CERTS gets the broker-only file.
RUNNER_CA_BUNDLE_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")
# Not a secret: the broker ignores it. Clients that drop credentials with an empty password need a value here.
_PROXY_PASSWORD = "mindroom"  # noqa: S105
_BASE_NO_PROXY_HOSTS = ("localhost", "127.0.0.1", "::1", ".svc", ".cluster.local")
# The agent tool that lists the broker's services; an agent that has it opts into Agent Keys.
_EGRESS_TOOL_NAME = "egress_credentials"


def egress_broker_enabled(runtime_paths: RuntimePaths) -> bool:
    """Return whether this deployment runs the egress broker: unless its port setting is `off` or `0`.

    Every Agent Keys surface (its API routes, pages, catalog fields, and tool) follows this, so with the broker off
    nothing of the feature shows or runs. Unset, the broker serves the primary's shell calls on a loopback port. A set
    but invalid port counts as enabled, so startup refuses it loudly.
    """
    port = (runtime_paths.env_value(EGRESS_BROKER_ENV_BY_KEY["port"]) or "").strip().lower()
    return port not in {"off", "0"}


def personal_page_served(runtime_paths: RuntimePaths) -> bool:
    """Return whether the personal egress page is served: the broker is on and its users can sign in.

    Only that page, `/connections/egress`, sets the keys of `user` and `user_agent` agents, which belong to each
    requester. Its API needs a verified Matrix identity, from trusted upstream auth or from Matrix sign-in, which an
    empty MindRoom Chat allowlist turns off.
    """
    if not egress_broker_enabled(runtime_paths):
        return False
    return runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED") or connections_sign_in_enabled(
        runtime_paths,
    )


def egress_tool_unavailable(tool_name: str, runtime_paths: RuntimePaths) -> bool:
    """Return whether `tool_name` is the egress credentials tool while the broker is off.

    That tool only reports the broker's services, so the tool lists leave it out and agents that list it do not get it.
    """
    return tool_name == _EGRESS_TOOL_NAME and not egress_broker_enabled(runtime_paths)


type EgressAgentScope = Literal["user", "user_agent", "shared", "unscoped"]


def egress_agent_scope(config: Config, agent_name: str, runtime_paths: RuntimePaths) -> EgressAgentScope | None:
    """Return whose store an agent's egress services and keys live in, or None when it does not use the broker.

    An agent opts in by listing the `egress_credentials` tool, and its brokered traffic only comes from a command tool
    that gets the broker environment: `shell`, wherever it runs, or any `EXECUTION_ENV_TOOL_NAMES` tool in a worker.
    Both must be among its resolved tools, presets and defaults included. The sandbox proxy adds the broker
    environment to worker-routed calls and the tool-call policy to shell calls in the primary; a primary `python`
    runs in-process, where no call can get an environment of its own, so it alone does not qualify. A name the config
    has no agent for does not use it. The store is one `user_agent` agent's own per requester, every `user` agent's
    of a requester, everyone's on a `shared` agent, or the global one all agents without a worker scope share
    (`unscoped`).
    The sandbox proxy, the tool-call policy, and both egress APIs decide here which agents use the broker.
    """
    if agent_name not in config.agents:
        return None
    # While the broker is off, the egress tool is unavailable, so no agent uses the broker.
    if not _uses_broker(_agent_tools(config, agent_name, runtime_paths), runtime_paths):
        return None
    execution_scope = config.resolve_agent(agent_name).policy.effective_execution_scope
    return "unscoped" if execution_scope is None else execution_scope


def broker_serves_primary_shell(config: Config, agent_name: str, runtime_paths: RuntimePaths) -> bool:
    """Return whether an agent that uses the broker runs its `shell` in the primary.

    The tool-call policy then gives each of its shell calls the broker environment; a worker-routed shell call gets it
    from the sandbox proxy instead.
    """
    if agent_name not in config.agents:
        return False
    return _primary_shell_uses_broker(_agent_tools(config, agent_name, runtime_paths), runtime_paths)


def _primary_shell_uses_broker(agent_tools: AgentTools, runtime_paths: RuntimePaths) -> bool:
    tool_names = agent_tools.tool_names
    return (
        _EGRESS_TOOL_NAME in tool_names
        and "shell" in tool_names
        and not _runs_in_a_worker(agent_tools, ("shell",), runtime_paths)
    )


def egress_tool_with_only_primary_python(config: Config, agent_name: str, runtime_paths: RuntimePaths) -> bool:
    """Return whether an agent lists `egress_credentials`, and its only command tool is a `python` in the primary.

    The broker then never reaches the agent's commands, so `egress_agent_scope` leaves it out. An agent with no command
    tool is not named: it has nothing to route, as with `egress_credentials` in `defaults.tools`.
    """
    if agent_name not in config.agents:
        return False
    agent_tools = _agent_tools(config, agent_name, runtime_paths)
    command_tools = EXECUTION_ENV_TOOL_NAMES.intersection(agent_tools.tool_names)
    return (
        _EGRESS_TOOL_NAME in agent_tools.tool_names
        and bool(command_tools)
        and not _uses_broker(agent_tools, runtime_paths)
        # A worker-routed command misses only the default broker's worker-facing URL, not a python in the primary.
        and not _runs_in_a_worker(agent_tools, command_tools, runtime_paths)
    )


def _agent_tools(config: Config, agent_name: str, runtime_paths: RuntimePaths) -> AgentTools:
    # Deferred: the plugin loader and the agent tools import this module.
    from mindroom.tool_system.agent_tools import resolve_agent_tools  # noqa: PLC0415
    from mindroom.tool_system.plugins import config_plugins  # noqa: PLC0415

    return resolve_agent_tools(config, agent_name, config_plugins(config, runtime_paths).tools)


def _uses_broker(agent_tools: AgentTools, runtime_paths: RuntimePaths) -> bool:
    """Return whether the agent lists `egress_credentials` and a `shell`, or another command tool in a worker.

    The default broker, without a port, has no worker-facing URL, so only a `shell` in the primary uses it.
    """
    tool_names = agent_tools.tool_names
    if not runtime_paths.env_value(EGRESS_BROKER_ENV_BY_KEY["port"], default="").strip():
        return _primary_shell_uses_broker(agent_tools, runtime_paths)
    return _EGRESS_TOOL_NAME in tool_names and (
        "shell" in tool_names
        or _runs_in_a_worker(agent_tools, EXECUTION_ENV_TOOL_NAMES.intersection(tool_names), runtime_paths)
    )


def _runs_in_a_worker(agent_tools: AgentTools, tool_names: Iterable[str], runtime_paths: RuntimePaths) -> bool:
    """Return whether any of the agent's own `tool_names` runs in a worker."""
    # Deferred: the sandbox proxy imports this module.
    from mindroom.tool_system.sandbox_proxy import tool_placement  # noqa: PLC0415

    try:
        placement = tool_placement(runtime_paths, agent_tools.tools)
        return any(agent_tools.runs_in_worker(tool_name, placement) for tool_name in tool_names)
    except WorkerBackendError:
        # An unreadable worker backend never runs the tool in the primary, so the agent stays listed and each page
        # degrades per service instead of failing.
        return True


def broker_execution_env(
    *,
    broker_url: str,
    token: str,
    ca_pem: str,
    placeholder_env: Mapping[str, str],
    extra_no_proxy_hosts: Iterable[str],
    call_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the native broker execution env overlay for one call's commands.

    Composes proxy env with the token as userinfo, includes git proxy config,
    NO_PROXY with callback hosts and extras, NODE_USE_ENV_PROXY=1, the broker
    CA PEM for runner-side materialization, and placeholder env vars.

    The token is the user name and the password is the fixed `mindroom`
    (`<token>:mindroom@`): Python urllib and Node fetch send proxy credentials
    only when both parts are non-empty, and the broker reads only the user name.
    `call_env` is the env the call already carries: its Git config entries are
    kept and the broker's are appended, so overlaying the result on it drops
    none of them.
    """
    # NO_PROXY: the base entries, then the extra hosts, each once and in first-seen order
    no_proxy = ",".join(dict.fromkeys([*_BASE_NO_PROXY_HOSTS, *extra_no_proxy_hosts]))
    scheme, _, rest = broker_url.partition("://")
    # Percent-encode the token so URL-significant characters (@ : / # ? % +, whitespace) cannot make HTTP clients
    # mis-parse the userinfo.
    proxy = f"{scheme}://{quote(token, safe='')}:{_PROXY_PASSWORD}@{rest}"
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
        # Git/libcurl does not always send proxy credentials from HTTPS_PROXY userinfo on CONNECT, so
        # environment-backed Git config names the same proxy without writing user or global Git config.
        # It extends the call's env, never the primary's process env, whose Git config must not leak to workers.
        **git_config_env(call_env or {}, broker_git_config(proxy)),
        BROKER_CA_PEM_ENV: ca_pem,
        "NODE_USE_ENV_PROXY": "1",
        **placeholder_env,
    }


def broker_git_config(proxy: str) -> tuple[tuple[str, str], ...]:
    """Return the Git config entries that send Git through the broker proxy `proxy` with its credentials."""
    return (("http.proxy", proxy), ("http.proxyAuthMethod", "basic"))


def primary_callback_hosts(runtime_paths: RuntimePaths) -> list[str]:
    """Extract unique hostnames from primary callback URLs, preserving order.

    Parses MINDROOM_EGRESS_BROKER_URL, MINDROOM_SCRIPT_GATEWAY_URL,
    MINDROOM_AGENT_CLI_PRIMARY_URL, and MINDROOM_AGENT_CLI_URL.
    Skips unset, blank, and unparsable URLs.
    """
    env_names = [
        "MINDROOM_EGRESS_BROKER_URL",
        "MINDROOM_SCRIPT_GATEWAY_URL",
        "MINDROOM_AGENT_CLI_PRIMARY_URL",
        "MINDROOM_AGENT_CLI_URL",
    ]

    hosts: list[str | None] = []
    for name in env_names:
        try:
            # A blank value has no hostname.
            hosts.append(urlsplit((runtime_paths.env_value(name) or "").strip()).hostname)
        except ValueError:
            # Unparsable URL, such as an unclosed IPv6 bracket, skip
            continue
    return list(dict.fromkeys(host for host in hosts if host))


def apply_runner_ca_bundle(
    env: dict[str, str],
    directory: Path,
    *,
    trusted_before: Mapping[str, str] | None = None,
) -> None:
    """Materialize broker CA bundle into directory and set CA env vars.

    Pops BROKER_CA_PEM_ENV from env, writes combined and broker-only bundle
    files, and sets SSL_CERT_FILE (+ friends) to the combined bundle and
    NODE_EXTRA_CA_CERTS to the broker-only file.

    `trusted_before` is the environment whose CA variables these replace; the files keep the certificates of the
    CA files it names, so commands that trusted a private CA through them still do.

    `materialize_ca_bundle` creates the directory with mode 0700 if missing.
    An env without the broker CA is left unchanged.
    """
    ca_pem = env.pop(BROKER_CA_PEM_ENV, None)
    if not ca_pem:
        return

    # Import lazily to keep the runner import light
    from mindroom.egress_broker.ca import materialize_ca_bundle  # noqa: PLC0415

    # Materialize CA bundle
    trusted_before = trusted_before or {}
    node_extra = trusted_before.get("NODE_EXTRA_CA_CERTS")
    combined, broker_only = materialize_ca_bundle(
        ca_pem,
        directory,
        kept_bundles=[Path(trusted_before[name]) for name in RUNNER_CA_BUNDLE_ENV if trusted_before.get(name)],
        kept_node_extra=Path(node_extra) if node_extra else None,
    )

    # Set CA env vars
    for name in RUNNER_CA_BUNDLE_ENV:
        env[name] = str(combined)
    env["NODE_EXTRA_CA_CERTS"] = str(broker_only)
