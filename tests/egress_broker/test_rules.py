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
    host_has_rules,
    inject_credentials,
    match_rule,
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
