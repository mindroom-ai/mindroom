"""Pure functions for egress broker rule matching and credential injection."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urlencode, urlparse

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule  # noqa: TC001

__all__ = [
    "RuleMatch",
    "host_has_rules",
    "inject_credentials",
    "match_rule",
    "strip_request_headers",
    "strip_response_headers",
]

# Hop-by-hop headers that must be stripped (case-insensitive)
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


@dataclass(frozen=True)
class RuleMatch:
    """A matched rule with its service name."""

    service: str
    rule: EgressRule


def _host_matches(rule_host: str, request_host: str) -> bool:
    """Check if a rule's host pattern matches the request host."""
    # Exact match
    if rule_host == request_host:
        return True

    # Wildcard match: *.domain.com matches sub.domain.com but not domain.com or sub.sub.domain.com
    if rule_host.startswith("*."):
        base_domain = rule_host[2:]  # Remove *.
        # Request must end with the base domain
        if not request_host.endswith(f".{base_domain}"):
            return False
        # Count labels: wildcard matches exactly one extra label
        prefix = request_host[: -(len(base_domain) + 1)]  # Everything before .base_domain
        # Prefix must not contain dots (only one label)
        return "." not in prefix

    return False


def _rule_priority(rule: EgressRule, request_port: int) -> tuple[int, int, int]:
    """Return a priority tuple for sorting rules (higher is better).

    Priority order:
    1. Exact host (1) beats wildcard (0)
    2. Port match: specific port (1) beats no port constraint (0)
    3. Path prefix length (longer wins)
    """
    # Exact host beats wildcard
    is_exact = not rule.host.startswith("*.")
    host_priority = 1 if is_exact else 0

    # Port-specific rule beats no-port rule
    port_priority = 1 if rule.port == request_port else 0

    # Longer path prefix wins
    path_priority = len(rule.path_prefix)

    return (host_priority, port_priority, path_priority)


def host_has_rules(config: EgressBrokerConfig, host: str, port: int) -> bool:
    """Check if any rule matches the given host and port."""
    host = host.lower()
    for service in config.services.values():
        for rule in service.rules:
            # Port constraint: rule with different port never matches
            if rule.port is not None and rule.port != port:
                continue
            if _host_matches(rule.host, host):
                return True
    return False


def match_rule(config: EgressBrokerConfig, host: str, port: int, path: str) -> RuleMatch | None:
    """Find the best matching rule for the given request.

    Matching priority:
    1. Exact host beats wildcard
    2. Port-specific rule beats no-port rule (different port never matches)
    3. Longest path_prefix wins
    4. Declaration order (across services and rules)
    """
    host = host.lower()
    candidates: list[tuple[str, EgressRule, tuple[int, int, int]]] = []

    # Collect all matching rules with priorities
    for service_name, service in config.services.items():
        for rule in service.rules:
            # Port constraint: rule with different port never matches
            if rule.port is not None and rule.port != port:
                continue

            # Host match
            if not _host_matches(rule.host, host):
                continue

            # Path prefix match
            if not path.startswith(rule.path_prefix):
                continue

            priority = _rule_priority(rule, port)
            candidates.append((service_name, rule, priority))

    if not candidates:
        return None

    # Sort by priority (descending), keeping declaration order for ties
    # Python's sort is stable, so declaration order is preserved
    candidates.sort(key=lambda x: x[2], reverse=True)

    service_name, rule, _ = candidates[0]
    return RuleMatch(service=service_name, rule=rule)


def inject_credentials(
    headers: list[tuple[bytes, bytes]],
    target: bytes,
    auth: EgressAuth,
    secret: str,
) -> tuple[list[tuple[bytes, bytes]], bytes]:
    """Inject credentials into headers or request target.

    Returns new headers list and new target.
    """
    new_headers = list(headers)

    if auth.type == "bearer":
        # Remove any existing Authorization header
        new_headers = [(k, v) for k, v in new_headers if k.lower() != b"authorization"]
        # Add Bearer token
        new_headers.append((b"authorization", f"Bearer {secret}".encode()))
        return new_headers, target

    if auth.type == "basic":
        # Remove any existing Authorization header
        new_headers = [(k, v) for k, v in new_headers if k.lower() != b"authorization"]
        # Encode username:secret in base64
        credentials = f"{auth.username}:{secret}".encode()
        encoded = base64.b64encode(credentials)
        new_headers.append((b"authorization", b"Basic " + encoded))
        return new_headers, target

    if auth.type == "header":
        # auth.name is guaranteed non-None by validators
        assert auth.name is not None
        # Remove any existing header with this name
        header_name = auth.name.lower().encode()
        new_headers = [(k, v) for k, v in new_headers if k.lower() != header_name]
        # Add new header with template substitution
        value = auth.template.replace("{secret}", secret)
        new_headers.append((header_name, value.encode()))
        return new_headers, target

    if auth.type == "query":
        # auth.name is guaranteed non-None by validators
        assert auth.name is not None
        # Parse target to extract path and query
        target_str = target.decode("latin-1")
        parsed = urlparse(target_str)
        path = parsed.path
        query_params = parse_qsl(parsed.query, keep_blank_values=True)

        # Find first occurrence of the param
        param_name = auth.name
        first_index = None
        for i, (key, _) in enumerate(query_params):
            if key == param_name:
                first_index = i
                break

        # Remove all occurrences of the param
        filtered_params = [(k, v) for k, v in query_params if k != param_name]

        # Insert the secret at the first occurrence position (or append if not found)
        secret_encoded = quote(secret, safe="")
        if first_index is not None:
            filtered_params.insert(first_index, (param_name, secret_encoded))
        else:
            filtered_params.append((param_name, secret_encoded))

        # Rebuild query string
        new_query = urlencode(filtered_params)
        new_target_str = f"{path}?{new_query}" if new_query else path

        return new_headers, new_target_str.encode("latin-1")

    # Should never reach here if auth is valid
    return new_headers, target


def _parse_connection_header(headers: list[tuple[bytes, bytes]]) -> set[str]:
    """Extract header names listed in Connection header."""
    listed = set()
    for name, value in headers:
        if name.lower() == b"connection":
            # Parse comma-separated list
            parts = value.decode("latin-1").split(",")
            for part in parts:
                listed.add(part.strip().lower())
    return listed


def strip_request_headers(
    headers: list[tuple[bytes, bytes]],
    *,
    keep_upgrade: bool,
) -> list[tuple[bytes, bytes]]:
    """Remove hop-by-hop headers from request.

    If keep_upgrade is True, preserve Connection and Upgrade headers
    for WebSocket handshakes.
    """
    # Parse Connection header to find additional headers to strip
    connection_listed = _parse_connection_header(headers)

    result = []
    for name, value in headers:
        name_lower = name.lower().decode("latin-1")

        # Skip hop-by-hop headers
        if name_lower in _HOP_BY_HOP:
            # Keep connection and upgrade if requested
            if keep_upgrade and name_lower in ("connection", "upgrade"):
                result.append((name, value))
            continue

        # Skip headers listed in Connection
        if name_lower in connection_listed:
            continue

        result.append((name, value))

    return result


def strip_response_headers(
    headers: list[tuple[bytes, bytes]],
    *,
    keep_upgrade: bool,
) -> list[tuple[bytes, bytes]]:
    """Remove hop-by-hop headers and set-cookie from response.

    If keep_upgrade is True, preserve Connection and Upgrade headers
    for WebSocket handshakes.
    """
    # Parse Connection header to find additional headers to strip
    connection_listed = _parse_connection_header(headers)

    result = []
    for name, value in headers:
        name_lower = name.lower().decode("latin-1")

        # Skip hop-by-hop headers
        if name_lower in _HOP_BY_HOP:
            # Keep connection and upgrade if requested
            if keep_upgrade and name_lower in ("connection", "upgrade"):
                result.append((name, value))
            continue

        # Skip headers listed in Connection
        if name_lower in connection_listed:
            continue

        # Also strip set-cookie from responses
        if name_lower == "set-cookie":
            continue

        result.append((name, value))

    return result
