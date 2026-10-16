"""Whether the broker and its personal page are on, the worker env overlay, and runner CA bundle materialization."""

from __future__ import annotations

from collections.abc import Iterable, Mapping  # noqa: TC003
from pathlib import Path  # noqa: TC003
from urllib.parse import urlsplit

from mindroom.constants import WORKER_EGRESS_NO_PROXY, RuntimePaths, compose_worker_proxy_env
from mindroom.runtime_env_policy import EGRESS_BROKER_ENV_BY_KEY

__all__ = [
    "BROKER_CA_PEM_ENV",
    "RUNNER_CA_BUNDLE_ENV",
    "apply_runner_ca_bundle",
    "broker_execution_env",
    "egress_broker_enabled",
    "egress_tool_unavailable",
    "personal_page_served",
    "primary_callback_hosts",
]

BROKER_CA_PEM_ENV = "MINDROOM_EGRESS_BROKER_CA_PEM"
# The env names the runner points at the combined CA bundle; NODE_EXTRA_CA_CERTS gets the broker-only file.
RUNNER_CA_BUNDLE_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")
# Not a secret: the broker ignores it. Clients that drop credentials with an empty password need a value here.
_PROXY_PASSWORD = "mindroom"  # noqa: S105


def egress_broker_enabled(runtime_paths: RuntimePaths) -> bool:
    """Return whether this deployment runs the egress broker, which its port setting decides.

    Every Agent Keys surface (its API routes, pages, catalog fields, and tool) follows this, so with the port unset
    nothing of the feature shows or runs. A set but invalid port counts as enabled, so startup refuses it loudly.
    """
    return bool((runtime_paths.env_value(EGRESS_BROKER_ENV_BY_KEY["port"]) or "").strip())


def personal_page_served(runtime_paths: RuntimePaths) -> bool:
    """Return whether the personal egress page is served: the broker is on and trusted upstream auth has JWT on.

    Only that page, `/connections/egress`, sets the keys of `user` and `user_agent` agents, which belong to each
    requester. Its API answers 403 unless trusted upstream auth is enabled and requires a signed JWT.
    """
    return (
        egress_broker_enabled(runtime_paths)
        and runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_AUTH_ENABLED")
        and runtime_paths.env_flag("MINDROOM_TRUSTED_UPSTREAM_REQUIRE_JWT")
    )


def egress_tool_unavailable(tool_name: str, runtime_paths: RuntimePaths) -> bool:
    """Return whether `tool_name` is the egress credentials tool while the broker is off.

    That tool only reports the broker's services, so the tool lists leave it out and agents that list it do not get it.
    """
    return tool_name == "egress_credentials" and not egress_broker_enabled(runtime_paths)


def broker_execution_env(
    *,
    broker_url: str,
    token: str,
    ca_pem: str,
    placeholder_env: Mapping[str, str],
    extra_no_proxy_hosts: Iterable[str],
    call_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the native broker execution env overlay for worker calls.

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
    no_proxy = ",".join(dict.fromkeys([*WORKER_EGRESS_NO_PROXY.split(","), *extra_no_proxy_hosts]))

    # Compose base proxy env
    env = compose_worker_proxy_env(
        # The call's env, never the primary's process env, whose git config must not leak to workers.
        call_env or {},
        proxy_url=broker_url,
        username=token,
        password=_PROXY_PASSWORD,
        ca_file=None,  # CA is handled separately via BROKER_CA_PEM_ENV
        no_proxy=no_proxy,
    )

    # Add broker-specific env
    env[BROKER_CA_PEM_ENV] = ca_pem
    env["NODE_USE_ENV_PROXY"] = "1"
    env.update(placeholder_env)

    return env


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


def apply_runner_ca_bundle(env: dict[str, str], directory: Path) -> bool:
    """Materialize broker CA bundle into directory and set CA env vars.

    Pops BROKER_CA_PEM_ENV from env, writes combined and broker-only bundle
    files, and sets SSL_CERT_FILE (+ friends) to the combined bundle and
    NODE_EXTRA_CA_CERTS to the broker-only file.

    `materialize_ca_bundle` creates the directory with mode 0700 if missing.

    Returns True if the CA was present and materialized, False otherwise.
    """
    ca_pem = env.pop(BROKER_CA_PEM_ENV, None)
    if not ca_pem:
        return False

    # Import lazily to keep the runner import light
    from mindroom.egress_broker.ca import materialize_ca_bundle  # noqa: PLC0415

    # Materialize CA bundle
    combined, broker_only = materialize_ca_bundle(ca_pem, directory)

    # Set CA env vars
    for name in RUNNER_CA_BUNDLE_ENV:
        env[name] = str(combined)
    env["NODE_EXTRA_CA_CERTS"] = str(broker_only)

    return True
