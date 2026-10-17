"""Pure functions for egress broker rule matching and credential injection."""

from __future__ import annotations

import base64
import ipaddress
import re
import unicodedata
from collections.abc import Collection, Mapping  # noqa: TC003
from dataclasses import dataclass
from typing import Literal
from urllib.parse import quote, unquote, urlsplit

from mindroom.config.egress_broker import EgressAuth, EgressBrokerConfig, EgressRule, EgressService  # noqa: TC001

__all__ = [
    "EgressRules",
    "Route",
    "RuleMatch",
    "config_covers_rule",
    "host_has_rules",
    "inject_credentials",
    "is_ambiguous_path",
    "match_rule",
    "path_matches",
    "route_request",
    "strip_hop_by_hop",
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
_SEGMENT_TAIL = re.compile(r"[;?#]")


@dataclass(frozen=True)
class RuleMatch:
    """A matched rule with its service's name and definition: the rule whose service's credential is injected.

    The secret resolver gets `definition` too, so it resolves a credential only while `service` still has it.
    """

    service: str
    rule: EgressRule
    definition: EgressService

    @property
    def auth(self) -> EgressAuth:
        """Return how the credential is sent; a match never names a `restrict_only` rule, so its rule has auth."""
        if self.rule.auth is None:
            msg = f"Rule of service {self.service} has no auth to inject with."
            raise ValueError(msg)
        return self.rule.auth


@dataclass(frozen=True)
class Route:
    """How the broker handles one request, decided from the target host, port, and path alone.

    `match` names the rule whose service secret is injected. `refusal` is the error code when the request
    is refused instead: `bad_request` (400) for an ambiguous path, `path_not_allowed` (403) for a path that a
    restricted host's rules do not list (see `route_request`), and then `restricted_by` names the service whose
    restriction refused it, `restricted_by_label` its display name (its name without one), for the message, and
    `restricted_by_operator` whether it is the operator's service, which only an administrator can change.
    With neither, the request is forwarded without credentials; when `host_has_rules` is false no applicable rule
    names the host, so `unmatched_hosts` decides.
    """

    host_has_rules: bool
    match: RuleMatch | None = None
    refusal: Literal["bad_request", "path_not_allowed"] | None = None
    restricted_by: str | None = None
    restricted_by_label: str | None = None
    restricted_by_operator: bool = False


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

    Anything from ``;``, ``?``, or ``#`` on and surrounding whitespace are removed first, as lenient servers read
    ``..;`` or ``..%20``, and a server that decodes and re-parses reads ``..%3f`` as ``..`` plus a query.
    """
    return any(
        _SEGMENT_TAIL.split(segment, maxsplit=1)[0].strip() in {".", ".."}
        for segment in _SEGMENT_SEPARATORS.split(path)
    )


def is_ambiguous_path(path: str) -> bool:
    """Return whether an upstream could resolve `path` above or beside the segments it appears to name.

    Raw backslashes and empty segments (``//``) are refused outright. Then the raw path and every
    percent-decoded layer must hold no dot segment and no NUL, which catches ``%2e%2e``, ``%252e%252e``,
    and ``x%2f..%2fy``. Each layer is also checked in NFKC form, because a server that applies it reads the two
    dot leader (U+2025) or two fullwidth full stops (U+FF0E) as ``..``. Decoding continues from both forms, so
    folding only adds refusals: NFKC turns the fullwidth form of ``%2e`` into ``%2e``, but it also merges a hex
    letter with a following combining mark (``%5C`` + U+0301), hiding an escape the unfolded form still decodes.
    A layer that is not valid UTF-8 (such as the overlong ``%c0%ae``) is refused, and so is a path still changing
    after `_MAX_DECODE_LAYERS` rounds. That includes a fullwidth percent sign (U+FF05) before hex that is not
    UTF-8 once folded, as in ``100`` + U+FF05 + ``CBD``: refusing it fails safe. A bare encoded slash such as
    GitLab's ``group%2Fproject`` stays allowed: rules match the raw path, so only a dot segment can climb out of a
    prefix. The broker refuses rather than normalizes, because servers disagree on how to normalize.
    """
    if "\\" in path or "//" in path:
        return True
    layers = {path}
    for _ in range(_MAX_DECODE_LAYERS + 1):
        decoded_layers: set[str] = set()
        for layer in layers:
            folded = unicodedata.normalize("NFKC", layer)
            if "\x00" in layer or _has_dot_segment(layer) or (folded != layer and _has_dot_segment(folded)):
                return True
            for form in dict.fromkeys((layer, folded)):
                try:
                    decoded = unquote(form, errors="strict")
                except UnicodeDecodeError:
                    return True
                if decoded != layer:
                    decoded_layers.add(decoded)
        if not decoded_layers:
            return False
        layers = decoded_layers
    return True


def host_has_rules(config: EgressBrokerConfig, host: str, port: int) -> bool:
    """Check if any rule matches the given host and port."""
    host = host.lower()
    return any(_rule_applies(rule, host, port) for service in config.services.values() for rule in service.rules)


def config_covers_rule(config: EgressBrokerConfig, rule: EgressRule) -> bool:
    """Return whether `config` has a rule for every host and port that `rule` can match.

    A wildcard rule is covered only by the same wildcard, and a rule for any port only by a rule for any port.
    """
    return any(
        (covering.port is None or covering.port == rule.port) and _host_matches(covering.host, rule.host)
        for service in config.services.values()
        for covering in service.rules
    )


@dataclass(frozen=True)
class EgressRules:
    """The rules one requester's traffic matches: the operator's config and its merge with the scope's own services.

    The scope's services can narrow operator policy, never widen it. Under `unmatched_hosts: deny` a user rule
    applies only on hosts the operator's services name (`intercepts`). On a host where an operator service sets
    `restrict_to_rules` a path must match an operator rule, and where one of the scope's services sets it the path
    must also match one of the scope's rules (`route_request`). Top-level settings come from the operator's config.
    """

    operator: EgressBrokerConfig
    effective: EgressBrokerConfig

    @classmethod
    def from_config(cls, config: EgressBrokerConfig) -> EgressRules:
        """Return the rules of a config without user services."""
        return cls(operator=config, effective=config)

    @property
    def unmatched_hosts(self) -> Literal["passthrough", "deny"]:
        """Return what happens to hosts without applicable rules, as the operator configured it."""
        return self.operator.unmatched_hosts

    @property
    def private_networks(self) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
        """Return the private networks the operator lets requests reach."""
        return tuple(ipaddress.ip_network(network) for network in self.operator.private_networks)

    def intercepts(self, host: str, port: int) -> bool:
        """Return whether a rule applies on `host`:`port`; under `deny` only where an operator rule names it."""
        if self.operator.unmatched_hosts == "deny" and not host_has_rules(self.operator, host, port):
            return False
        return host_has_rules(self.effective, host, port)


def match_rule(
    config: EgressBrokerConfig,
    host: str,
    port: int,
    path: str,
    *,
    preferred: Collection[str] = (),
) -> RuleMatch | None:
    """Find the best matching rule for the given request.

    Paths match by segment (see `path_matches`). This does not refuse ambiguous paths;
    request handling goes through `route_request`, which does.

    Matching priority:
    1. Exact host beats wildcard
    2. Port-specific rule beats no-port rule (different port never matches)
    3. Longest path_prefix wins
    4. A `preferred` service beats the others (`route_request` prefers the operator's services)
    5. The service whose name sorts first, then the service's first rule

    So the order services are written in never decides a match. Rules of `restrict_only` services never match here:
    they only limit paths and have no credential.
    """
    credential_services = {name: service for name, service in config.services.items() if not service.restrict_only}
    return _best_match(credential_services, host.lower(), port, path, preferred=preferred)


def _best_match(
    services: Mapping[str, EgressService],
    host: str,
    port: int,
    path: str,
    *,
    preferred: Collection[str] = (),
) -> RuleMatch | None:
    """Return the most specific rule of `services` for a lowercase host, in `match_rule`'s priority order."""
    candidates = [
        (service_name, rule)
        for service_name, service in services.items()
        for rule in service.rules
        if _rule_applies(rule, host, port) and path_matches(rule.path_prefix, path)
    ]
    if not candidates:
        return None

    def rank(candidate: tuple[str, EgressRule]) -> tuple[int, int, int, bool, str]:
        service_name, rule = candidate
        host_priority, port_priority, path_priority = _rule_priority(rule, port)
        return (-host_priority, -port_priority, -path_priority, service_name not in preferred, service_name)

    # `min` keeps the first of equal candidates, so one service's rules keep their order.
    service_name, rule = min(candidates, key=rank)
    return RuleMatch(service=service_name, rule=rule, definition=services[service_name])


def _restricting_service(services: Mapping[str, EgressService], host: str, port: int) -> str | None:
    """Return the service whose name sorts first among those that restrict paths and have a rule for the host.

    A service restricts paths with `restrict_to_rules` or `restrict_only`.
    """
    return min(
        (
            name
            for name, service in services.items()
            if (service.restrict_to_rules or service.restrict_only)
            and any(_rule_applies(rule, host, port) for rule in service.rules)
        ),
        default=None,
    )


def _restriction_refusal(
    party: Mapping[str, EgressService],
    host: str,
    port: int,
    path: str,
    *,
    by_operator: bool,
) -> Route | None:
    """Return the `path_not_allowed` route when a service of `party` restricts the host and no rule of `party` matches.

    Every rule of the party counts, `restrict_only` ones included. The refusal names the party's restricting service
    whose name sorts first, and `by_operator` whether the party is the operator's.
    """
    restricted_by = _restricting_service(party, host, port)
    if restricted_by is None or _best_match(party, host, port, path) is not None:
        return None
    return Route(
        host_has_rules=True,
        refusal="path_not_allowed",
        restricted_by=restricted_by,
        restricted_by_label=party[restricted_by].display_name or restricted_by,
        restricted_by_operator=by_operator,
    )


def route_request(rules: EgressRules, host: str, port: int, path: str) -> Route:
    """Decide how to handle a request for `path` (origin-form, query removed) on `host`:`port`.

    The first row whose condition holds decides:

    | Row | Condition                                                         | Route                              |
    |-----|-------------------------------------------------------------------|------------------------------------|
    | 1   | No applicable rule names the host and port.                       | `host_has_rules=False`             |
    | 2   | The path is ambiguous (`is_ambiguous_path`).                      | `refusal="bad_request"` (400)      |
    | 3   | An operator service restricts the host; no operator rule matches. | `refusal="path_not_allowed"` (403) |
    | 4   | A user service restricts the host; no user rule matches.          | `refusal="path_not_allowed"` (403) |
    | 5   | Otherwise.                                                        | `match=match_rule(...)`, or None   |

    Row 1 leaves the request to the caller's `unmatched_hosts`, whatever the path. `EgressRules.intercepts` decides
    whether rules apply: under `deny` a user rule applies only on a host and port that an operator rule names.
    Row 2 refuses before matching, so path tricks cannot select a different rule.
    Rows 3 and 4 are each party's restriction (`_restriction_refusal`). A service with `restrict_to_rules` or
    `restrict_only` and a rule for the host restricts it; the path must then match one of that party's rules,
    `restrict_only` ones included. So a user's limit narrows the operator's services on that host for that requester
    alone, and never opens a path the operator's limit refuses. The refusal names the party's restricting service
    whose name sorts first and sets `restricted_by_operator` in row 3.
    Row 5 chooses the credential: the most specific applicable rule, operator's or user's, outside `restrict_only`
    services; an operator rule wins a tie with a user rule. So a user's limit in place keeps the operator's service,
    with the user's connection or key, for the paths it allows. With no matching rule the request goes out without
    credentials; `unmatched_hosts` does not apply, since the host has rules.
    """
    host = host.lower()
    if not rules.intercepts(host, port):
        return Route(host_has_rules=False)
    if is_ambiguous_path(path):
        return Route(host_has_rules=True, refusal="bad_request")
    operator = rules.operator.services
    # A user service never takes an operator service's name, so the names tell the parties apart.
    user = {name: service for name, service in rules.effective.services.items() if name not in operator}
    operator_refusal = _restriction_refusal(operator, host, port, path, by_operator=True)
    if operator_refusal is not None:
        return operator_refusal
    user_refusal = _restriction_refusal(user, host, port, path, by_operator=False)
    if user_refusal is not None:
        return user_refusal
    return Route(host_has_rules=True, match=match_rule(rules.effective, host, port, path, preferred=operator))


def _inject_query_param(target: bytes, param_name: str, template: str, secret: str) -> bytes:
    """Put the templated secret in the target's query, keeping the path and every other pair byte for byte.

    Every pair whose decoded name is `param_name` is removed, and the new pair takes the first one's place, or goes
    last when there is none. The name and value are percent-encoded once.
    """
    parsed = urlsplit(target.decode("latin-1"))
    new_param = f"{quote(param_name, safe='')}={quote(template.replace('{secret}', secret), safe='')}"
    pairs = parsed.query.split("&") if parsed.query else []
    matches = {i for i, pair in enumerate(pairs) if unquote(pair.split("=", 1)[0]) == param_name}
    kept = [pair for i, pair in enumerate(pairs) if i not in matches]
    # Every pair before the first match is kept, so its index in `pairs` is its index in `kept`.
    kept.insert(min(matches, default=len(kept)), new_param)
    return f"{parsed.path}?{'&'.join(kept)}".encode("latin-1")


def inject_credentials(
    headers: list[tuple[bytes, bytes]],
    target: bytes,
    auth: EgressAuth,
    secret: str,
) -> tuple[list[tuple[bytes, bytes]], bytes]:
    """Return the headers and target with the credential injected, replacing any header of the same name."""
    if auth.type == "query":
        assert auth.name is not None  # The config requires a name for query auth.
        return list(headers), _inject_query_param(target, auth.name, auth.template, secret)
    if auth.type == "header":
        assert auth.name is not None  # The config requires a name for header auth.
        name, value = auth.name.lower().encode(), auth.template.replace("{secret}", secret).encode()
    elif auth.type == "basic":
        name, value = b"authorization", b"Basic " + base64.b64encode(f"{auth.username}:{secret}".encode())
    else:  # bearer, the last of the four auth types the config allows
        name, value = b"authorization", f"Bearer {secret}".encode()
    return [(k, v) for k, v in headers if k.lower() != name] + [(name, value)], target


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


def strip_hop_by_hop(
    headers: list[tuple[bytes, bytes]],
    *,
    keep_upgrade: bool,
) -> list[tuple[bytes, bytes]]:
    """Remove hop-by-hop headers and those the Connection header lists, in either direction.

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
