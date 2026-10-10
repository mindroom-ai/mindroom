"""Pure functions for egress broker rule matching and credential injection."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, unquote, urlparse

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule  # noqa: TC001

__all__ = [
    "Route",
    "RuleMatch",
    "host_has_rules",
    "inject_credentials",
    "is_ambiguous_path",
    "match_rule",
    "path_matches",
    "route_request",
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


# Percent-decoding rounds checked for dot segments; a path still changing after these is refused outright.
_MAX_DECODE_LAYERS = 4
_SEGMENT_SEPARATORS = re.compile(r"[/\\]")


@dataclass(frozen=True)
class RuleMatch:
    """A matched rule with its service name."""

    service: str
    rule: EgressRule


@dataclass(frozen=True)
class Route:
    """How the broker handles one request, decided from the target host, port, and path alone.

    `match` names the rule whose service secret is injected. `refusal` is the error code when the request
    is refused instead: `bad_request` (400) for an ambiguous path, `path_not_allowed` (403) for a path no rule
    lists on a host restricted to its rules. With neither, the request is forwarded without credentials;
    when `host_has_rules` is false no rule names the host at all, so `unmatched_hosts` decides.
    """

    host_has_rules: bool
    match: RuleMatch | None = None
    refusal: Literal["bad_request", "path_not_allowed"] | None = None

    @property
    def refusal_status(self) -> int:
        """Return the HTTP status that goes with `refusal`; a route that is not refused has none."""
        if self.refusal is None:
            msg = "Route is not refused."
            raise ValueError(msg)
        return {"bad_request": 400, "path_not_allowed": 403}[self.refusal]


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


def _rule_applies(rule: EgressRule, host: str, port: int) -> bool:
    """Return whether `rule` names this host and port; a rule bound to another port never applies."""
    if rule.port is not None and rule.port != port:
        return False
    return _host_matches(rule.host, host)


def path_matches(prefix: str, path: str) -> bool:
    """Return whether `path` falls under `prefix`, comparing whole path segments.

    A prefix ending in ``/`` matches by plain prefix, so ``/`` matches every path. Any other prefix matches
    the exact path or the path followed by ``/``: ``/repos/o/r`` matches ``/repos/o/r/pulls`` but never
    ``/repos/o/r-old``.
    """
    if prefix.endswith("/"):
        return path.startswith(prefix)
    return path == prefix or path.startswith(f"{prefix}/")


def _has_dot_segment(path: str) -> bool:
    """Return whether any segment between slashes or backslashes is ``.`` or ``..``.

    ``;`` parameters and surrounding whitespace are removed first, as lenient servers read ``..;`` or ``..%20``.
    """
    return any(segment.partition(";")[0].strip() in {".", ".."} for segment in _SEGMENT_SEPARATORS.split(path))


def is_ambiguous_path(path: str) -> bool:
    """Return whether an upstream could resolve `path` above or beside the segments it appears to name.

    Raw backslashes and empty segments (``//``) are refused outright. Then the raw path and every
    percent-decoded layer must hold no dot segment and no NUL, which catches ``%2e%2e``, ``%252e%252e``,
    and ``x%2f..%2fy``. A layer that is not valid UTF-8 (such as the overlong ``%c0%ae``) is refused, and so is
    a path still changing after `_MAX_DECODE_LAYERS` rounds. A bare encoded slash such as GitLab's
    ``group%2Fproject`` stays allowed: rules match the raw path, so only a dot segment can climb out of a prefix.
    The broker refuses rather than normalizes, because servers disagree on how to normalize.
    """
    if "\\" in path or "//" in path:
        return True
    layer = path
    for _ in range(_MAX_DECODE_LAYERS + 1):
        if "\x00" in layer or _has_dot_segment(layer):
            return True
        try:
            decoded = unquote(layer, errors="strict")
        except UnicodeDecodeError:
            return True
        if decoded == layer:
            return False
        layer = decoded
    return True


def host_has_rules(config: EgressBrokerConfig, host: str, port: int) -> bool:
    """Check if any rule matches the given host and port."""
    host = host.lower()
    return any(_rule_applies(rule, host, port) for service in config.services.values() for rule in service.rules)


def match_rule(config: EgressBrokerConfig, host: str, port: int, path: str) -> RuleMatch | None:
    """Find the best matching rule for the given request.

    Paths match by segment (see `path_matches`). This does not refuse ambiguous paths;
    request handling goes through `route_request`, which does.

    Matching priority:
    1. Exact host beats wildcard
    2. Port-specific rule beats no-port rule (different port never matches)
    3. Longest path_prefix wins
    4. Declaration order (across services and rules)
    """
    host = host.lower()
    candidates: list[tuple[str, EgressRule, tuple[int, int, int]]] = [
        (service_name, rule, _rule_priority(rule, port))
        for service_name, service in config.services.items()
        for rule in service.rules
        if _rule_applies(rule, host, port) and path_matches(rule.path_prefix, path)
    ]

    if not candidates:
        return None

    # Sort by priority (descending), keeping declaration order for ties
    # Python's sort is stable, so declaration order is preserved
    candidates.sort(key=lambda x: x[2], reverse=True)

    service_name, rule, _ = candidates[0]
    return RuleMatch(service=service_name, rule=rule)


def route_request(config: EgressBrokerConfig, host: str, port: int, path: str) -> Route:
    """Decide how to handle a request for `path` (origin-form, query removed) on `host`:`port`.

    Hosts without rules are left to `unmatched_hosts` whatever their path. On a host with rules,
    an ambiguous path is refused before matching, so path tricks cannot select a different rule.
    A path no rule matches is forwarded without credentials, unless a service with rules on this host
    sets `restrict_to_rules`, in which case it is refused.
    """
    host = host.lower()
    services_on_host = [
        service
        for service in config.services.values()
        if any(_rule_applies(rule, host, port) for rule in service.rules)
    ]
    if not services_on_host:
        return Route(host_has_rules=False)
    if is_ambiguous_path(path):
        return Route(host_has_rules=True, refusal="bad_request")
    match = match_rule(config, host, port, path)
    if match is not None:
        return Route(host_has_rules=True, match=match)
    if any(service.restrict_to_rules for service in services_on_host):
        return Route(host_has_rules=True, refusal="path_not_allowed")
    return Route(host_has_rules=True)


def _inject_query_param(target: bytes, param_name: str, template: str, secret: str) -> bytes:
    """Inject a query parameter with template substitution.

    Preserves exact encoding of other parameters byte-for-byte.
    Replaces all occurrences of the param with one at the first occurrence position.
    """
    target_str = target.decode("latin-1")
    parsed = urlparse(target_str)
    path = parsed.path
    raw_query = parsed.query

    if not raw_query:
        # No existing query string, just append the new param
        value = template.replace("{secret}", secret)
        encoded_value = quote(value, safe="")
        encoded_name = quote(param_name, safe="")
        new_target_str = f"{path}?{encoded_name}={encoded_value}"
        return new_target_str.encode("latin-1")

    # Split query on "&" to preserve exact encoding of other params
    param_pairs = raw_query.split("&")
    first_index = None
    filtered_pairs = []

    # Find first occurrence and filter out all occurrences
    for _i, pair in enumerate(param_pairs):
        # Split on first "=" to get name and value
        if "=" in pair:
            pair_name, _ = pair.split("=", 1)
            decoded_name = unquote(pair_name)
        else:
            # Handle param without value (e.g., "?flag")
            decoded_name = unquote(pair)

        if decoded_name == param_name:
            if first_index is None:
                first_index = len(filtered_pairs)
            # Skip this param (remove all occurrences)
        else:
            filtered_pairs.append(pair)

    # Build the new param with template applied and encoded once
    value = template.replace("{secret}", secret)
    encoded_value = quote(value, safe="")
    encoded_name = quote(param_name, safe="")
    new_param = f"{encoded_name}={encoded_value}"

    # Insert at first occurrence position or append
    if first_index is not None:
        filtered_pairs.insert(first_index, new_param)
    else:
        filtered_pairs.append(new_param)

    # Rebuild query string
    new_query = "&".join(filtered_pairs)
    new_target_str = f"{path}?{new_query}"

    return new_target_str.encode("latin-1")


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
        new_target = _inject_query_param(target, auth.name, auth.template, secret)
        return new_headers, new_target

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
