"""Worker execution env overlay and runner CA bundle materialization."""

from __future__ import annotations

from collections.abc import Iterable, Mapping  # noqa: TC003
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

__all__ = [
    "BROKER_CA_PEM_ENV",
    "apply_runner_ca_bundle",
    "broker_execution_env",
    "primary_callback_hosts",
]

BROKER_CA_PEM_ENV = "MINDROOM_EGRESS_BROKER_CA_PEM"


def broker_execution_env(
    *,
    broker_url: str,
    token: str,
    ca_pem: str,
    placeholder_env: Mapping[str, str],
    extra_no_proxy_hosts: Iterable[str],
) -> dict[str, str]:
    """Return the native broker execution env overlay for worker calls.

    Composes proxy env with the token as userinfo, includes git proxy config,
    NO_PROXY with callback hosts and extras, NODE_USE_ENV_PROXY=1, the broker
    CA PEM for runner-side materialization, and placeholder env vars.

    The password is empty (token:@ userinfo format).
    """
    # Import here to avoid circular dependency with constants
    from mindroom.constants import WORKER_EGRESS_NO_PROXY, compose_worker_proxy_env  # noqa: PLC0415

    # Build NO_PROXY: base + extra hosts (deduplicated)
    base_no_proxy_entries = WORKER_EGRESS_NO_PROXY.split(",")
    all_no_proxy_hosts = [*base_no_proxy_entries, *extra_no_proxy_hosts]
    # Deduplicate while preserving order
    seen = set()
    unique_no_proxy = []
    for host in all_no_proxy_hosts:
        if host not in seen:
            seen.add(host)
            unique_no_proxy.append(host)
    no_proxy = ",".join(unique_no_proxy)

    # Compose base proxy env
    env = compose_worker_proxy_env(
        {},  # broker env composed on primary; primary git config must not leak, hence empty
        proxy_url=broker_url,
        username=token,
        password="",  # empty password
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

    hosts = []
    seen = set()

    for name in env_names:
        value = (runtime_paths.env_value(name) or "").strip()
        if not value:
            continue

        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            if hostname and hostname not in seen:
                seen.add(hostname)
                hosts.append(hostname)
        except Exception:  # noqa: S112
            # Unparsable URL, skip
            continue

    return hosts


def apply_runner_ca_bundle(env: dict[str, str], directory: Path) -> bool:
    """Materialize broker CA bundle into directory and set CA env vars.

    Pops BROKER_CA_PEM_ENV from env, writes combined and broker-only bundle
    files, and sets SSL_CERT_FILE (+ friends) to the combined bundle and
    NODE_EXTRA_CA_CERTS to the broker-only file.

    Creates directory with mode 0700 if missing.

    Returns True if the CA was present and materialized, False otherwise.
    """
    ca_pem = env.pop(BROKER_CA_PEM_ENV, None)
    if not ca_pem:
        return False

    # Create directory if missing
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    # Import lazily to keep the runner import light
    from mindroom.egress_broker.ca import materialize_ca_bundle  # noqa: PLC0415

    # Materialize CA bundle
    combined, broker_only = materialize_ca_bundle(ca_pem, directory)

    # Set CA env vars
    env["SSL_CERT_FILE"] = str(combined)
    env["REQUESTS_CA_BUNDLE"] = str(combined)
    env["CURL_CA_BUNDLE"] = str(combined)
    env["GIT_SSL_CAINFO"] = str(combined)
    env["NODE_EXTRA_CA_CERTS"] = str(broker_only)

    return True
