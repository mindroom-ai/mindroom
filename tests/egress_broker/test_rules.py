"""Tests for egress broker rule matching and credential injection."""

from __future__ import annotations

import base64

import pytest
import yaml

from mindroom.config.egress_broker import (
    EgressAuth,
    EgressBrokerConfig,
    EgressRule,
    EgressService,
)
from mindroom.egress_broker.rules import (
    Route,
    host_has_rules,
    inject_credentials,
    is_ambiguous_path,
    match_rule,
    path_matches,
    route_request,
    strip_request_headers,
    strip_response_headers,
)


def test_exact_host_beats_wildcard() -> None:
    """Exact host match takes precedence over wildcard."""
    config = EgressBrokerConfig(
        services={
            "wildcard": EgressService(
                rules=[
                    EgressRule(
                        host="*.github.com",
                        auth=EgressAuth(type="bearer"),
                    ),
                ],
            ),
            "exact": EgressService(
                rules=[
                    EgressRule(
                        host="api.github.com",
                        auth=EgressAuth(type="bearer"),
                    ),
                ],
            ),
        },
    )
    result = match_rule(config, "api.github.com", 443, "/user")
    assert result is not None
    assert result.rule.host == "api.github.com"


def test_wildcard_matches_one_label_only() -> None:
    """Wildcard *.example.com matches only one additional label."""
    config = EgressBrokerConfig(
        services={
            "svc": EgressService(
                rules=[
                    EgressRule(
                        host="*.example.com",
                        auth=EgressAuth(type="bearer"),
                    ),
                ],
            ),
        },
    )
    # Matches one label
    assert match_rule(config, "a.example.com", 443, "/") is not None
    # Does not match two labels
    assert match_rule(config, "a.b.example.com", 443, "/") is None
    # Does not match base domain
    assert match_rule(config, "example.com", 443, "/") is None


def test_port_specific_rule_wins_and_other_port_never_matches() -> None:
    """Port-specific rule beats no-port rule; different port never matches."""
    config = EgressBrokerConfig(
        services={
            "svc": EgressService(
                rules=[
                    EgressRule(
                        host="api.example.com",
                        auth=EgressAuth(type="bearer"),
                    ),
                    EgressRule(
                        host="api.example.com",
                        port=443,
                        auth=EgressAuth(type="header", name="X-Key"),
                    ),
                    EgressRule(
                        host="api.example.com",
                        port=8080,
                        auth=EgressAuth(type="header", name="X-Other"),
                    ),
                ],
            ),
        },
    )
    # Port 443 matches the port-specific rule
    result = match_rule(config, "api.example.com", 443, "/")
    assert result is not None
    assert result.rule.auth.name == "X-Key"

    # Port 8080 matches its specific rule
    result = match_rule(config, "api.example.com", 8080, "/")
    assert result is not None
    assert result.rule.auth.name == "X-Other"

    # Port 9000 matches the rule without port
    result = match_rule(config, "api.example.com", 9000, "/")
    assert result is not None
    assert result.rule.auth.name is None


def test_longest_path_prefix_wins() -> None:
    """Longest matching path prefix takes precedence."""
    config = EgressBrokerConfig(
        services={
            "svc": EgressService(
                rules=[
                    EgressRule(
                        host="api.example.com",
                        path_prefix="/",
                        auth=EgressAuth(type="bearer"),
                    ),
                    EgressRule(
                        host="api.example.com",
                        path_prefix="/repos/",
                        auth=EgressAuth(type="header", name="X-Repos"),
                    ),
                ],
            ),
        },
    )
    result = match_rule(config, "api.example.com", 443, "/repos/x")
    assert result is not None
    assert result.rule.path_prefix == "/repos/"


def test_no_rule_returns_none_and_host_has_rules_false() -> None:
    """No matching rule returns None and host_has_rules returns False."""
    config = EgressBrokerConfig(
        services={
            "svc": EgressService(
                rules=[
                    EgressRule(
                        host="api.example.com",
                        auth=EgressAuth(type="bearer"),
                    ),
                ],
            ),
        },
    )
    assert match_rule(config, "other.example.com", 443, "/") is None
    assert not host_has_rules(config, "other.example.com", 443)
    assert host_has_rules(config, "api.example.com", 443)


def _github_repo_config(*, restrict: bool, graphql: bool = False) -> EgressBrokerConfig:
    """Return a GitHub service limited to basnijholt/agent-cli, as the repository helper would write it."""
    prefixes = ["/repos/basnijholt/agent-cli"] + (["/graphql"] if graphql else [])
    return EgressBrokerConfig(
        services={
            "github": EgressService(
                restrict_to_rules=restrict,
                rules=[
                    EgressRule(host="api.github.com", path_prefix=prefix, auth=EgressAuth(type="bearer"))
                    for prefix in prefixes
                ],
            ),
        },
    )


@pytest.mark.parametrize(
    ("prefix", "path", "expected"),
    [
        ("/repos/basnijholt/agent-cli", "/repos/basnijholt/agent-cli", True),
        ("/repos/basnijholt/agent-cli", "/repos/basnijholt/agent-cli/", True),
        ("/repos/basnijholt/agent-cli", "/repos/basnijholt/agent-cli/pulls", True),
        ("/repos/basnijholt/agent-cli", "/repos/basnijholt/agent-cli-old", False),
        ("/repos/basnijholt/agent-cli", "/repos/basnijholt/agent-clix/pulls", False),
        ("/repos/basnijholt/agent-cli", "/repos/basnijholt/agent", False),
        ("/repos/basnijholt/agent-cli", "/repos/basnijholt/agent-cli.git", False),
        ("/drive/", "/drive/v3/files", True),
        ("/drive/", "/drive/", True),
        ("/drive/", "/drive", False),
        ("/drive/", "/drivex/v3", False),
        ("/", "/", True),
        ("/", "/anything/at/all", True),
    ],
)
def test_path_matches_by_segment(prefix: str, path: str, expected: bool) -> None:
    """A prefix without a trailing slash matches whole segments; one with a trailing slash matches by plain prefix."""
    assert path_matches(prefix, path) is expected


def test_match_rule_skips_sibling_repository_sharing_a_prefix() -> None:
    """`/repos/o/agent-cli` never selects the rule for `/repos/o/agent-cli-old`, so it gets no credentials."""
    config = _github_repo_config(restrict=False)

    assert match_rule(config, "api.github.com", 443, "/repos/basnijholt/agent-cli/pulls") is not None
    assert match_rule(config, "api.github.com", 443, "/repos/basnijholt/agent-cli-old") is None
    assert match_rule(config, "api.github.com", 443, "/repos/basnijholt/agent-cli-old/pulls") is None


@pytest.mark.parametrize(
    "path",
    [
        "/repos/basnijholt/agent-cli/../other",
        "/repos/basnijholt/agent-cli/..",
        "/repos/basnijholt/agent-cli/./pulls",
        "/repos/basnijholt/agent-cli/.",
        "/repos/basnijholt/agent-cli/%2e%2e/other",
        "/repos/basnijholt/agent-cli/%2E%2e/other",
        "/repos/basnijholt/agent-cli/.%2E/other",
        "/repos/basnijholt/agent-cli/%2e/pulls",
        "/repos/basnijholt/agent-cli/..;/other",
        "/repos/basnijholt/agent-cli/%2e%2e%3b/other",
        "/repos/basnijholt/agent-cli//pulls",
        "//repos/basnijholt/agent-cli",
        "/repos/basnijholt/agent-cli/\\..\\other",
        "/repos/basnijholt/agent-cli\\pulls",
        "/repos/basnijholt/agent-cli%5c..%5Cother",
        "/repos/basnijholt/agent-cli%2f..%2Fother",
        "/repos/basnijholt/agent-cli/%252e%252e/other",
        "/repos/basnijholt/agent-cli/x%252f..%252f..%252fother",
        "/repos/basnijholt/agent-cli/%25252e%25252e/other",
        "/repos/basnijholt/agent-cli/..%00/other",
        "/repos/basnijholt/agent-cli/%00",
        "/repos/basnijholt/agent-cli/%c0%ae%c0%ae/other",
        "/repos/basnijholt/agent-cli/..%20/other",
        "/repos/basnijholt/agent-cli/%2525252541",
    ],
)
def test_ambiguous_paths_are_detected(path: str) -> None:
    """Dot segments at any decoding layer, empty segments, backslashes, NUL, and invalid UTF-8 are ambiguous.

    A path still changing after four rounds of percent-decoding is ambiguous too.
    """
    assert is_ambiguous_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/repos/basnijholt/agent-cli",
        "/repos/basnijholt/agent-cli/",
        "/repos/basnijholt/agent-cli/contents/.github/workflows",
        "/repos/basnijholt/agent-cli/compare/main...feature",
        "/files/a..b",
        "/files/.../x",
        "/search/hello%20world",
        "/files/%2e%2e%2e",
        "/repos/basnijholt%2Fagent-cli",
        "/api/v4/projects/group%2Fproject",
        "/repos/basnijholt/agent-cli/labels/area%2Fbackend",
        "/files/100%25.txt",
        "/files/%25252541",
    ],
)
def test_ordinary_paths_are_not_ambiguous(path: str) -> None:
    """Dots inside names, trailing slashes, encoded slashes, and up to four layers of encoding are left alone.

    GitLab's `group%2Fproject` and clients that encode `/` in label or branch names must keep working.
    """
    assert not is_ambiguous_path(path)


def test_route_matches_injects_and_forwards_unmatched_paths_without_restriction() -> None:
    """Without `restrict_to_rules`, an unmatched path on a rule host is forwarded without credentials."""
    config = _github_repo_config(restrict=False)

    matched = route_request(config, "api.github.com", 443, "/repos/basnijholt/agent-cli/pulls")
    unmatched = route_request(config, "API.GITHUB.COM", 443, "/repos/basnijholt/agent-cli-old")

    assert matched.host_has_rules
    assert matched.match is not None
    assert matched.match.service == "github"
    assert matched.refusal is None
    assert unmatched == Route(host_has_rules=True)
    with pytest.raises(ValueError, match="not refused"):
        _ = unmatched.refusal_status


@pytest.mark.parametrize(
    "path",
    ["/repos/basnijholt/agent-cli/../other/repo", "/repos/basnijholt/agent-cli/%2E%2e/x", "//user", "/user\\x"],
)
def test_route_refuses_ambiguous_paths_only_on_rule_hosts(path: str) -> None:
    """Ambiguous paths get 400 `bad_request` on hosts with rules; hosts without rules are left alone."""
    config = _github_repo_config(restrict=False)

    refused = route_request(config, "api.github.com", 443, path)

    assert refused == Route(host_has_rules=True, refusal="bad_request")
    assert refused.refusal_status == 400
    assert route_request(config, "example.com", 443, path) == Route(host_has_rules=False)


def test_restrict_to_rules_refuses_unmatched_paths() -> None:
    """With `restrict_to_rules`, a path no rule matches gets 403 `path_not_allowed` instead of being forwarded."""
    config = _github_repo_config(restrict=True)

    for path in ["/repos/basnijholt/agent-cli-old", "/repos/basnijholt/other", "/user", "/"]:
        refused = route_request(config, "api.github.com", 443, path)
        assert refused == Route(host_has_rules=True, refusal="path_not_allowed")
        assert refused.refusal_status == 403
    allowed = route_request(config, "api.github.com", 443, "/repos/basnijholt/agent-cli/issues")
    assert allowed.match is not None
    assert route_request(config, "example.com", 443, "/user") == Route(host_has_rules=False)


def test_restrict_to_rules_refuses_graphql_unless_listed() -> None:
    """GraphQL can reach any repository the key can, so a restricted service refuses it unless a rule lists it."""
    assert route_request(_github_repo_config(restrict=True), "api.github.com", 443, "/graphql").refusal == (
        "path_not_allowed"
    )
    listed = route_request(_github_repo_config(restrict=True, graphql=True), "api.github.com", 443, "/graphql")
    assert listed.match is not None
    assert listed.match.rule.path_prefix == "/graphql"


def test_restrict_from_any_service_on_the_host_applies() -> None:
    """One restricting service on a host refuses paths no service matches; other hosts and ports are unaffected."""
    restricting = EgressService(
        restrict_to_rules=True,
        rules=[EgressRule(host="api.example.com", path_prefix="/a", auth=EgressAuth(type="bearer"))],
    )
    open_service = EgressService(
        rules=[
            EgressRule(host="api.example.com", path_prefix="/b", auth=EgressAuth(type="bearer")),
            EgressRule(host="other.example.com", auth=EgressAuth(type="bearer")),
        ],
    )
    config = EgressBrokerConfig(services={"open": open_service, "restricting": restricting})

    assert route_request(config, "api.example.com", 443, "/c").refusal == "path_not_allowed"
    open_route = route_request(config, "api.example.com", 443, "/b/x")
    assert open_route.match is not None
    assert open_route.match.service == "open"
    assert route_request(config, "other.example.com", 443, "/c") == Route(
        host_has_rules=True,
        match=match_rule(config, "other.example.com", 443, "/c"),
    )

    port_bound = EgressService(
        restrict_to_rules=True,
        rules=[EgressRule(host="api.example.com", port=8443, path_prefix="/a", auth=EgressAuth(type="bearer"))],
    )
    config = EgressBrokerConfig(services={"open": open_service, "port_bound": port_bound})
    assert route_request(config, "api.example.com", 443, "/c") == Route(host_has_rules=True)
    assert route_request(config, "api.example.com", 8443, "/c").refusal == "path_not_allowed"


def test_inject_bearer() -> None:
    """Bearer auth adds Authorization header."""
    auth = EgressAuth(type="bearer")
    headers: list[tuple[bytes, bytes]] = []
    target = b"/api/endpoint"

    new_headers, new_target = inject_credentials(headers, target, auth, "s3cret")
    assert new_target == target
    assert (b"authorization", b"Bearer s3cret") in new_headers


def test_inject_basic() -> None:
    """Basic auth encodes username:secret in base64."""
    auth = EgressAuth(type="basic", username="x-access-token")
    headers: list[tuple[bytes, bytes]] = []
    target = b"/api/endpoint"

    new_headers, new_target = inject_credentials(headers, target, auth, "s3cret")
    assert new_target == target

    expected_value = b"Basic " + base64.b64encode(b"x-access-token:s3cret")
    assert (b"authorization", expected_value) in new_headers


def test_inject_header_template() -> None:
    """Header injection uses template substitution."""
    auth = EgressAuth(type="header", name="X-Api-Key", template="Key {secret}")
    headers: list[tuple[bytes, bytes]] = []
    target = b"/api/endpoint"

    new_headers, new_target = inject_credentials(headers, target, auth, "s3cret")
    assert new_target == target
    assert (b"x-api-key", b"Key s3cret") in new_headers


def test_inject_query_replaces_client_value() -> None:
    """Query injection replaces client-provided param value with secret."""
    auth = EgressAuth(type="query", name="key")
    headers: list[tuple[bytes, bytes]] = []
    target = b"/v1?key=client&a=1"

    new_headers, new_target = inject_credentials(headers, target, auth, "s3cret")
    assert new_headers == headers
    assert new_target == b"/v1?key=s3cret&a=1"


def test_inject_query_custom_template() -> None:
    """Query injection applies template before encoding."""
    auth = EgressAuth(type="query", name="token", template="Bearer {secret}")
    headers: list[tuple[bytes, bytes]] = []
    target = b"/api?token=old"

    new_headers, new_target = inject_credentials(headers, target, auth, "s3cret")
    assert new_headers == headers
    assert new_target == b"/api?token=Bearer%20s3cret"


def test_inject_query_special_characters() -> None:
    """Query injection URL-encodes special characters once."""
    auth = EgressAuth(type="query", name="key")
    headers: list[tuple[bytes, bytes]] = []
    target = b"/v1?key=old"

    new_headers, new_target = inject_credentials(headers, target, auth, "a b&c")
    assert new_headers == headers
    # Space becomes %20, & becomes %26 (single encoding)
    assert new_target == b"/v1?key=a%20b%26c"


def test_inject_query_preserves_other_params_encoding() -> None:
    """Query injection preserves exact encoding of other params."""
    auth = EgressAuth(type="query", name="key")
    headers: list[tuple[bytes, bytes]] = []
    # Other param "a" uses %20 encoding for space
    target = b"/v1?a=hello%20world&key=x"

    new_headers, new_target = inject_credentials(headers, target, auth, "secret")
    assert new_headers == headers
    # "a=hello%20world" preserved byte-for-byte, not changed to "a=hello+world"
    assert new_target == b"/v1?a=hello%20world&key=secret"


def test_inject_query_encodes_param_name() -> None:
    """Query injection URL-encodes the param name when it contains special chars."""
    auth = EgressAuth(type="query", name="my key")
    headers: list[tuple[bytes, bytes]] = []
    # Existing param name is already encoded in the URL
    target = b"/v1?my%20key=old&a=1"

    new_headers, new_target = inject_credentials(headers, target, auth, "s3cret")
    assert new_headers == headers
    # Param name "my key" encoded to "my%20key" in output
    assert new_target == b"/v1?my%20key=s3cret&a=1"


def test_inject_replaces_client_authorization() -> None:
    """Credential injection replaces any existing Authorization header."""
    auth = EgressAuth(type="bearer")
    headers: list[tuple[bytes, bytes]] = [
        (b"authorization", b"token mindroom-brokered"),
        (b"x-other", b"value"),
    ]
    target = b"/api/endpoint"

    new_headers, new_target = inject_credentials(headers, target, auth, "s3cret")
    assert new_target == target

    # Exactly one authorization header
    auth_headers = [h for h in new_headers if h[0] == b"authorization"]
    assert len(auth_headers) == 1
    assert auth_headers[0][1] == b"Bearer s3cret"

    # Other headers preserved
    assert (b"x-other", b"value") in new_headers


def test_strip_request_headers_removes_proxy_auth_and_connection_listed() -> None:
    """Strip request headers removes hop-by-hop headers and those in Connection."""
    headers: list[tuple[bytes, bytes]] = [
        (b"connection", b"close, X-Foo"),
        (b"x-foo", b"bar"),
        (b"proxy-authorization", b"secret"),
        (b"x-keep", b"this"),
        (b"keep-alive", b"timeout=5"),
    ]
    result = strip_request_headers(headers, keep_upgrade=False)

    # Connection-listed header removed
    assert not any(h[0] == b"x-foo" for h in result)
    # Hop-by-hop headers removed
    assert not any(h[0] == b"connection" for h in result)
    assert not any(h[0] == b"proxy-authorization" for h in result)
    assert not any(h[0] == b"keep-alive" for h in result)
    # Normal headers kept
    assert (b"x-keep", b"this") in result


def test_strip_response_headers_removes_set_cookie() -> None:
    """Strip response headers removes set-cookie in addition to hop-by-hop headers."""
    headers: list[tuple[bytes, bytes]] = [
        (b"set-cookie", b"session=abc"),
        (b"connection", b"close"),
        (b"x-keep", b"this"),
    ]
    result = strip_response_headers(headers, keep_upgrade=False)

    assert not any(h[0] == b"set-cookie" for h in result)
    assert not any(h[0] == b"connection" for h in result)
    assert (b"x-keep", b"this") in result


def test_keep_upgrade_preserves_websocket_handshake() -> None:
    """keep_upgrade=True preserves Connection and Upgrade headers."""
    headers: list[tuple[bytes, bytes]] = [
        (b"connection", b"Upgrade"),
        (b"upgrade", b"websocket"),
        (b"x-keep", b"this"),
    ]
    result = strip_request_headers(headers, keep_upgrade=True)

    assert (b"connection", b"Upgrade") in result
    assert (b"upgrade", b"websocket") in result
    assert (b"x-keep", b"this") in result


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "basic"},  # Missing username
        {"type": "header"},  # Missing name
        {"type": "header", "name": "X-Key", "template": "none"},  # Missing {secret}
    ],
)
def test_invalid_auth_rejected(bad: dict) -> None:
    """Invalid auth configurations are rejected."""
    with pytest.raises(ValueError, match=r"(requires|must contain)"):
        EgressAuth(**bad)


@pytest.mark.parametrize(
    "host",
    [
        "*.*.example.com",  # Multiple wildcards
        "https://x.com",  # Has scheme
        "x.com:443",  # Has port
        "x.com/path",  # Has path
    ],
)
def test_invalid_host_rejected(host: str) -> None:
    """Invalid host values are rejected."""
    with pytest.raises(ValueError, match=r"(host|wildcard)"):
        EgressRule(host=host, auth=EgressAuth(type="bearer"))


@pytest.mark.parametrize(
    "name",
    [
        "HTTPS_PROXY",
        "no_proxy",
        "MINDROOM_X",
        "SSL_CERT_FILE",
        "PATH",
        "ALL_PROXY",
        "all_proxy",
        "NODE_USE_ENV_PROXY",
        "lower",  # Must start with uppercase
    ],
)
def test_invalid_placeholder_env_rejected(name: str) -> None:
    """Invalid placeholder env names are rejected."""
    with pytest.raises(ValueError, match=r"(placeholder_env|match|reserved|prefix)"):
        EgressService(
            rules=[EgressRule(host="example.com", auth=EgressAuth(type="bearer"))],
            placeholder_env={name: "value"},
        )


def test_invalid_service_name_rejected() -> None:
    """Invalid service names are rejected."""
    service = EgressService(
        rules=[EgressRule(host="example.com", auth=EgressAuth(type="bearer"))],
    )

    # Uppercase not allowed
    with pytest.raises(ValueError, match=r"service name.*must match"):
        EgressBrokerConfig(services={"GitHub": service})

    # Spaces not allowed
    with pytest.raises(ValueError, match=r"service name.*must match"):
        EgressBrokerConfig(services={"a b": service})


@pytest.mark.parametrize("name", ["x_oauth", "x_oauth_client", "oauth", "oauth_client"])
def test_service_name_with_oauth_suffix_rejected(name: str) -> None:
    """Names whose `egress_<name>` credential service ends like an OAuth service are rejected."""
    service = EgressService(
        rules=[EgressRule(host="example.com", auth=EgressAuth(type="bearer"))],
    )
    with pytest.raises(ValueError, match="OAuth"):
        EgressBrokerConfig(services={name: service})


@pytest.mark.parametrize("name", ["oauth_proxy", "my-oauth", "xoauth"])
def test_service_name_mentioning_oauth_elsewhere_accepted(name: str) -> None:
    """Only the OAuth suffixes are reserved, not the word itself."""
    service = EgressService(
        rules=[EgressRule(host="example.com", auth=EgressAuth(type="bearer"))],
    )
    assert name in EgressBrokerConfig(services={name: service}).services


@pytest.mark.parametrize("name", ["", "X Key", "X-Key:", "X-Key\n", "X-Key\r\nInjected: 1", "X/Key", "Clé"])
def test_header_auth_name_must_be_token(name: str) -> None:
    """A header auth name must be a valid HTTP header field name."""
    with pytest.raises(ValueError, match="header name"):
        EgressAuth(type="header", name=name)


@pytest.mark.parametrize(
    "name",
    [
        "Host",
        "content-length",
        "Transfer-Encoding",
        "connection",
        "Upgrade",
        "TE",
        "trailer",
        "Proxy-Authorization",
        "proxy-connection",
        "Keep-Alive",
    ],
)
def test_header_auth_name_rejects_framing_and_routing_headers(name: str) -> None:
    """The broker owns framing, routing, and proxy headers, so no rule may inject into them."""
    with pytest.raises(ValueError, match="reserved"):
        EgressAuth(type="header", name=name)


@pytest.mark.parametrize("name", ["Authorization", "X-Api-Key", "x-goog-api-key", "Private-Token"])
def test_header_auth_name_accepts_common_headers(name: str) -> None:
    """Ordinary credential headers remain allowed."""
    assert EgressAuth(type="header", name=name).name == name


def test_query_auth_name_must_not_be_empty() -> None:
    """A query auth name must name a parameter."""
    with pytest.raises(ValueError, match="query auth requires a non-empty name"):
        EgressAuth(type="query", name="")


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_rule_port_out_of_range_rejected(port: int) -> None:
    """Rule ports must be valid TCP ports."""
    with pytest.raises(ValueError, match="port"):
        EgressRule(host="example.com", port=port, auth=EgressAuth(type="bearer"))


@pytest.mark.parametrize("port", [1, 443, 65535])
def test_rule_port_in_range_accepted(port: int) -> None:
    """Valid TCP ports are accepted."""
    assert EgressRule(host="example.com", port=port, auth=EgressAuth(type="bearer")).port == port


def test_host_strips_trailing_dot() -> None:
    """Host with trailing dot (FQDN notation) should be normalized."""
    rule = EgressRule(host="api.example.com.", auth=EgressAuth(type="bearer"))
    assert rule.host == "api.example.com"

    # Also test via YAML parsing (as the brief requested)
    config_yaml = """
    services:
      test:
        rules:
          - host: api.github.com.
            auth: { type: bearer }
    """
    config = EgressBrokerConfig(**yaml.safe_load(config_yaml))
    assert config.services["test"].rules[0].host == "api.github.com"
