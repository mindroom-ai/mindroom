"""Tests for egress broker rule matching and credential injection."""

from __future__ import annotations

import base64
import re

import pytest

from mindroom.config.egress_broker import (
    EgressAuth,
    EgressBrokerConfig,
    EgressRule,
    EgressService,
)
from mindroom.egress_broker.rules import (
    EgressRules,
    Route,
    config_covers_rule,
    host_has_rules,
    inject_credentials,
    is_ambiguous_path,
    match_rule,
    path_matches,
    route_request,
    strip_hop_by_hop,
)


def _route(config: EgressBrokerConfig, host: str, port: int, path: str) -> Route:
    """Route a request against a config without user services."""
    return route_request(EgressRules.from_config(config), host, port, path)


def _rule(
    host: str,
    path_prefix: str = "/",
    *,
    port: int | None = None,
    auth: EgressAuth | None = None,
) -> EgressRule:
    return EgressRule(host=host, port=port, path_prefix=path_prefix, auth=auth or EgressAuth(type="bearer"))


def _config(*rules: EgressRule) -> EgressBrokerConfig:
    """Return a config with one service, ``svc``, holding `rules`."""
    return EgressBrokerConfig(services={"svc": EgressService(rules=list(rules))})


def test_exact_host_beats_wildcard() -> None:
    """Exact host match takes precedence over wildcard."""
    config = EgressBrokerConfig(
        services={
            "wildcard": EgressService(rules=[_rule("*.github.com")]),
            "exact": EgressService(rules=[_rule("api.github.com")]),
        },
    )
    result = match_rule(config, "api.github.com", 443, "/user")
    assert result is not None
    assert result.rule.host == "api.github.com"


def test_wildcard_matches_one_label_only() -> None:
    """Wildcard *.example.com matches only one additional label."""
    config = _config(_rule("*.example.com"))
    # Matches one label
    assert match_rule(config, "a.example.com", 443, "/") is not None
    # Does not match two labels
    assert match_rule(config, "a.b.example.com", 443, "/") is None
    # Does not match base domain
    assert match_rule(config, "example.com", 443, "/") is None


def test_port_specific_rule_wins_and_other_port_never_matches() -> None:
    """Port-specific rule beats no-port rule; different port never matches."""
    config = _config(
        _rule("api.example.com"),
        _rule("api.example.com", port=443, auth=EgressAuth(type="header", name="X-Key")),
        _rule("api.example.com", port=8080, auth=EgressAuth(type="header", name="X-Other")),
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
    config = _config(
        _rule("api.example.com", "/"),
        _rule("api.example.com", "/repos/", auth=EgressAuth(type="header", name="X-Repos")),
    )
    result = match_rule(config, "api.example.com", 443, "/repos/x")
    assert result is not None
    assert result.rule.path_prefix == "/repos/"


def test_equally_specific_rules_go_to_the_service_whose_name_sorts_first() -> None:
    """A tie goes to the service whose name sorts first, whatever order the services are written in."""
    tied = EgressService(rules=[_rule("api.example.com")])
    for services in ({"zeta": tied, "alpha": tied}, {"alpha": tied, "zeta": tied}):
        result = match_rule(EgressBrokerConfig(services=services), "api.example.com", 443, "/")
        assert result is not None
        assert result.service == "alpha"


def test_equally_specific_rules_of_one_service_keep_their_order() -> None:
    """Within one service, the first of two equally specific rules wins."""
    first = _rule("api.example.com", auth=EgressAuth(type="header", name="X-First"))
    second = _rule("api.example.com", auth=EgressAuth(type="header", name="X-Second"))
    result = match_rule(_config(first, second), "api.example.com", 443, "/")
    assert result is not None
    assert result.rule is first


def test_config_service_wins_a_tie_with_a_user_service_whatever_their_names() -> None:
    """On equally specific rules, the operator's service beats the requester's own, even one named earlier."""
    tied = EgressService(rules=[_rule("api.example.com")])
    route = route_request(_merged({"zeta": tied}, {"alpha": tied}), "api.example.com", 443, "/")
    assert route.match is not None
    assert route.match.service == "zeta"


def test_no_rule_returns_none_and_host_has_rules_false() -> None:
    """No matching rule returns None and host_has_rules returns False."""
    config = _config(_rule("api.example.com"))
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
        "/repos/basnijholt/agent-cli/..%3f/other",
        "/repos/basnijholt/agent-cli/..%23/other",
        "/repos/basnijholt/agent-cli/..%253f/other",
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

    matched = _route(config, "api.github.com", 443, "/repos/basnijholt/agent-cli/pulls")
    unmatched = _route(config, "API.GITHUB.COM", 443, "/repos/basnijholt/agent-cli-old")

    assert matched.host_has_rules
    assert matched.match is not None
    assert matched.match.service == "github"
    assert matched.refusal is None
    assert unmatched == Route(host_has_rules=True)


@pytest.mark.parametrize(
    "path",
    ["/repos/basnijholt/agent-cli/../other/repo", "/repos/basnijholt/agent-cli/%2E%2e/x", "//user", "/user\\x"],
)
def test_route_refuses_ambiguous_paths_only_on_rule_hosts(path: str) -> None:
    """Ambiguous paths get 400 `bad_request` on hosts with rules; hosts without rules are left alone."""
    config = _github_repo_config(restrict=False)

    refused = _route(config, "api.github.com", 443, path)

    assert refused == Route(host_has_rules=True, refusal="bad_request")
    assert _route(config, "example.com", 443, path) == Route(host_has_rules=False)


def test_restrict_to_rules_refuses_unmatched_paths() -> None:
    """With `restrict_to_rules`, a path no rule matches gets 403 `path_not_allowed` instead of being forwarded."""
    config = _github_repo_config(restrict=True)

    for path in ["/repos/basnijholt/agent-cli-old", "/repos/basnijholt/other", "/user", "/"]:
        refused = _route(config, "api.github.com", 443, path)
        assert refused == Route(
            host_has_rules=True,
            refusal="path_not_allowed",
            restricted_by="github",
            restricted_by_label="github",
            restricted_by_operator=True,
        )
    allowed = _route(config, "api.github.com", 443, "/repos/basnijholt/agent-cli/issues")
    assert allowed.match is not None
    assert _route(config, "example.com", 443, "/user") == Route(host_has_rules=False)


def test_restrict_to_rules_refuses_graphql_unless_listed() -> None:
    """GraphQL can reach any repository the key can, so a restricted service refuses it unless a rule lists it."""
    assert _route(_github_repo_config(restrict=True), "api.github.com", 443, "/graphql").refusal == ("path_not_allowed")
    listed = _route(_github_repo_config(restrict=True, graphql=True), "api.github.com", 443, "/graphql")
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

    assert _route(config, "api.example.com", 443, "/c").refusal == "path_not_allowed"
    open_route = _route(config, "api.example.com", 443, "/b/x")
    assert open_route.match is not None
    assert open_route.match.service == "open"
    assert _route(config, "other.example.com", 443, "/c") == Route(
        host_has_rules=True,
        match=match_rule(config, "other.example.com", 443, "/c"),
    )

    port_bound = EgressService(
        restrict_to_rules=True,
        rules=[EgressRule(host="api.example.com", port=8443, path_prefix="/a", auth=EgressAuth(type="bearer"))],
    )
    config = EgressBrokerConfig(services={"open": open_service, "port_bound": port_bound})
    assert _route(config, "api.example.com", 443, "/c") == Route(host_has_rules=True)
    assert _route(config, "api.example.com", 8443, "/c").refusal == "path_not_allowed"


def _merged(
    operator: dict[str, EgressService],
    user: dict[str, EgressService],
    *,
    deny: bool = False,
) -> EgressRules:
    """Return an operator config merged with one scope's user services, as the broker builds them."""
    unmatched = "deny" if deny else "passthrough"
    config = EgressBrokerConfig(unmatched_hosts=unmatched, services=operator)
    # Unvalidated, like `effective_config`: a config refuses restrict_only services, which only users add.
    return EgressRules(operator=config, effective=config.model_copy(update={"services": {**operator, **user}}))


def test_deny_keeps_user_rules_to_hosts_and_ports_the_operator_names() -> None:
    """Under deny a user rule counts only where an operator rule names the host and port, wildcards included."""
    operator = EgressService(
        rules=[_rule("api.example.com"), _rule("*.cdn.example.com"), _rule("ports.example.com", port=8443)],
    )
    mine = EgressService(
        rules=[
            _rule("api.example.com", "/mine"),
            _rule("evil.example.com"),
            _rule("a.cdn.example.com"),
            _rule("ports.example.com"),
        ],
    )
    rules = _merged({"op": operator}, {"mine": mine}, deny=True)

    assert not rules.intercepts("evil.example.com", 443)
    assert route_request(rules, "evil.example.com", 443, "/x") == Route(host_has_rules=False)
    assert not rules.intercepts("ports.example.com", 443)
    assert rules.intercepts("ports.example.com", 8443)
    on_operator_host = route_request(rules, "api.example.com", 443, "/mine/x")
    assert on_operator_host.match is not None
    assert on_operator_host.match.service == "mine"
    under_wildcard = route_request(rules, "a.cdn.example.com", 443, "/x")
    assert under_wildcard.match is not None
    assert under_wildcard.match.service == "mine"
    # Under passthrough the same user hosts are the scope's own to intercept.
    assert _merged({"op": operator}, {"mine": mine}).intercepts("evil.example.com", 443)


def test_operator_restriction_is_decided_by_operator_rules_alone() -> None:
    """User rules cannot open a path an operator's restricted host leaves out, but a narrower one picks its own key."""
    rules = _merged(
        {"op": EgressService(restrict_to_rules=True, rules=[_rule("api.github.com", "/repos/o/")])},
        {
            "open-all": EgressService(rules=[_rule("api.github.com")]),
            "narrow": EgressService(rules=[_rule("api.github.com", "/repos/o/a")]),
        },
    )

    for path in ["/repos/other/x", "/graphql", "/"]:
        assert route_request(rules, "api.github.com", 443, path).refusal == "path_not_allowed"
    narrow = route_request(rules, "api.github.com", 443, "/repos/o/a/pulls")
    assert narrow.match is not None
    assert narrow.match.service == "narrow"
    operator = route_request(rules, "api.github.com", 443, "/repos/o/b")
    assert operator.match is not None
    assert operator.match.service == "op"
    assert route_request(rules, "api.github.com", 443, "/repos/o/a/../../x").refusal == "bad_request"
    assert route_request(rules, "api.github.com", 443, "/graphql/../repos/o/a").refusal == "bad_request"


def test_user_restriction_still_narrows_its_scope() -> None:
    """A user service's own `restrict_to_rules` refuses unlisted paths on its host, operator rules' paths included."""
    rules = _merged(
        {"op": EgressService(rules=[_rule("api.example.com", "/op")])},
        {"mine": EgressService(restrict_to_rules=True, rules=[_rule("api.example.com", "/mine")])},
    )

    for path in ["/other", "/op/x"]:
        assert route_request(rules, "api.example.com", 443, path) == Route(
            host_has_rules=True,
            refusal="path_not_allowed",
            restricted_by="mine",
            restricted_by_label="mine",
        )
    mine = route_request(rules, "api.example.com", 443, "/mine/x")
    assert mine.match is not None
    assert mine.match.service == "mine"
    operator_only = EgressRules.from_config(rules.operator)
    assert route_request(operator_only, "api.example.com", 443, "/other") == Route(host_has_rules=True)
    operator = route_request(operator_only, "api.example.com", 443, "/op/x")
    assert operator.match is not None
    assert operator.match.service == "op"


def test_user_restriction_narrows_operator_services_on_the_host() -> None:
    """A user's repo limit also limits the operator's broad service on that host, for that requester only."""
    operator = {"github": EgressService(rules=[_rule("api.github.com"), _rule("uploads.github.com")])}
    limited = EgressService(restrict_to_rules=True, rules=[_rule("api.github.com", "/repos/o/a")])
    rules = _merged(operator, {"github-limited": limited})

    allowed = route_request(rules, "api.github.com", 443, "/repos/o/a/pulls")
    assert allowed.match is not None
    assert allowed.match.service == "github-limited"
    for path in ["/repos/o/b", "/user", "/graphql"]:
        assert route_request(rules, "api.github.com", 443, path) == Route(
            host_has_rules=True,
            refusal="path_not_allowed",
            restricted_by="github-limited",
            restricted_by_label="github-limited",
        )
    # Another requester on the same host has no user services: the operator's service still covers everything.
    other = route_request(_merged(operator, {}), "api.github.com", 443, "/repos/o/b")
    assert other.match is not None
    assert other.match.service == "github"
    assert other.refusal is None
    # The limit holds on the hosts the user's rules name: the operator's other host is not limited.
    elsewhere = route_request(rules, "uploads.github.com", 443, "/x")
    assert elsewhere.match is not None
    assert elsewhere.match.service == "github"


def _limit(*rules: EgressRule, display_name: str | None = None) -> EgressService:
    """Return a restrict-only service, as "Limit to repositories" saves it."""
    return EgressService(restrict_only=True, display_name=display_name, rules=list(rules))


def test_limit_in_place_keeps_the_admin_services_credential_for_allowed_paths_and_refuses_others() -> None:
    """A user's restrict-only limit narrows the operator's GitHub service; allowed paths still use its credential."""
    operator = {"github": EgressService(rules=[_rule("api.github.com"), _rule("github.com")])}
    limited = _limit(
        _rule("api.github.com", "/repos/o/a"),
        _rule("github.com", "/o/a"),
        display_name="GitHub (o/a)",
    )
    rules = _merged(operator, {"github-limited": limited})

    # The restrict-only rules are the most specific ones here, yet the operator's `/` rule picks the credential.
    for host, path in [
        ("api.github.com", "/repos/o/a/pulls"),
        ("api.github.com", "/repos/o/a"),
        ("github.com", "/o/a"),
    ]:
        allowed = route_request(rules, host, 443, path)
        assert allowed.refusal is None
        assert allowed.match is not None
        assert allowed.match.service == "github"
        assert allowed.match.rule.path_prefix == "/"
    for host, path in [("api.github.com", "/repos/o/b"), ("api.github.com", "/user"), ("github.com", "/o/b")]:
        assert route_request(rules, host, 443, path) == Route(
            host_has_rules=True,
            refusal="path_not_allowed",
            restricted_by="github-limited",
            restricted_by_label="GitHub (o/a)",
        )
    # Another requester without the limit keeps every path.
    assert route_request(_merged(operator, {}), "api.github.com", 443, "/repos/o/b").match is not None


def test_limit_in_place_also_narrows_uploads_github_com_with_its_rules_for_the_repositories() -> None:
    """The limit editor writes uploads.github.com rules too, so the operator's `/` rule there stops reaching every repo."""
    operator = {
        "github": EgressService(rules=[_rule("api.github.com"), _rule("uploads.github.com"), _rule("github.com")]),
    }
    limited = _limit(
        _rule("api.github.com", "/repos/o/r"),
        _rule("github.com", "/o/r"),
        _rule("github.com", "/o/r.git"),
        _rule("uploads.github.com", "/repos/o/r"),
        display_name="GitHub (o/r)",
    )
    rules = _merged(operator, {"github-limited": limited})

    assert route_request(rules, "uploads.github.com", 443, "/repos/x/y/releases/1/assets") == Route(
        host_has_rules=True,
        refusal="path_not_allowed",
        restricted_by="github-limited",
        restricted_by_label="GitHub (o/r)",
    )
    allowed = route_request(rules, "uploads.github.com", 443, "/repos/o/r/releases/1/assets")
    assert allowed.refusal is None
    assert allowed.match is not None
    assert (allowed.match.service, allowed.match.rule.host, allowed.match.rule.path_prefix) == (
        "github",
        "uploads.github.com",
        "/",
    )


def test_a_restrict_only_service_never_injects() -> None:
    """Paths a restrict-only service allows go out without credentials when no other service on the host matches."""
    rules = _merged(
        {"docs": EgressService(rules=[_rule("api.example.com", "/docs")])},
        {"limit": _limit(_rule("api.example.com", "/docs"), _rule("api.example.com", "/public"))},
    )

    public = route_request(rules, "api.example.com", 443, "/public/x")
    assert public == Route(host_has_rules=True)
    docs = route_request(rules, "api.example.com", 443, "/docs/x")
    assert docs.match is not None
    assert docs.match.service == "docs"
    assert route_request(rules, "api.example.com", 443, "/other").refusal == "path_not_allowed"
    # Alone on a host it limits paths and never matches.
    alone = _merged({}, {"limit": _limit(_rule("api.example.com", "/ok"))})
    assert route_request(alone, "api.example.com", 443, "/ok/x") == Route(host_has_rules=True)
    assert route_request(alone, "api.example.com", 443, "/no").refusal == "path_not_allowed"
    assert match_rule(alone.effective, "api.example.com", 443, "/ok/x") is None
    assert alone.intercepts("api.example.com", 443)


def test_route_names_the_restricting_service_and_whether_the_operator_defined_it() -> None:
    """The route names the restricting service, whether the operator defined it, and its label, for either party."""
    rules = _merged(
        {"op": EgressService(restrict_to_rules=True, display_name="Ops API", rules=[_rule("api.example.com", "/op")])},
        {"mine": _limit(_rule("api.example.com", "/op/mine"), display_name="My limit")},
    )

    by_operator = route_request(rules, "api.example.com", 443, "/other")
    by_user = route_request(rules, "api.example.com", 443, "/op/else")

    assert (by_operator.restricted_by, by_operator.restricted_by_operator) == ("op", True)
    assert (by_user.restricted_by, by_user.restricted_by_operator) == ("mine", False)
    assert (by_operator.restricted_by_label, by_user.restricted_by_label) == ("Ops API", "My limit")


def test_restricted_by_label_falls_back_to_the_service_name() -> None:
    """The refusal's message names the restricting service's display name, or its name when it has none."""
    named = _merged({}, {"mine": _limit(_rule("api.example.com", "/a"), display_name="My API")})
    unnamed = _merged({}, {"mine": _limit(_rule("api.example.com", "/a"))})

    assert route_request(named, "api.example.com", 443, "/b").restricted_by_label == "My API"
    assert route_request(unnamed, "api.example.com", 443, "/b").restricted_by_label == "mine"


def test_user_restriction_cannot_open_an_operator_restricted_path() -> None:
    """Both restrictions apply: a user limit to `/` never opens what the operator's restriction leaves out."""
    rules = _merged(
        {"op": EgressService(restrict_to_rules=True, rules=[_rule("api.github.com", "/repos/o/")])},
        {"mine": EgressService(restrict_to_rules=True, rules=[_rule("api.github.com")])},
    )

    assert route_request(rules, "api.github.com", 443, "/user") == Route(
        host_has_rules=True,
        refusal="path_not_allowed",
        restricted_by="op",
        restricted_by_label="op",
        restricted_by_operator=True,
    )
    allowed = route_request(rules, "api.github.com", 443, "/repos/o/a")
    assert allowed.match is not None
    assert allowed.match.service == "op"


def test_restricted_by_names_the_first_restricting_service_of_the_failing_party() -> None:
    """The refusal names the restricting service whose name sorts first in the party whose check the path fails."""
    rules = _merged(
        {
            "op-open": EgressService(rules=[_rule("api.example.com", "/op")]),
            # Written out of name order: the name decides, not the order in the file.
            "op-second": EgressService(restrict_to_rules=True, rules=[_rule("api.example.com", "/b")]),
            "op-first": EgressService(restrict_to_rules=True, rules=[_rule("api.example.com", "/a")]),
        },
        {
            "mine-open": EgressService(rules=[_rule("api.example.com", "/a/open")]),
            "mine-first": EgressService(restrict_to_rules=True, rules=[_rule("api.example.com", "/a/mine")]),
        },
    )

    assert route_request(rules, "api.example.com", 443, "/c").restricted_by == "op-first"
    assert route_request(rules, "api.example.com", 443, "/b").restricted_by == "mine-first"
    assert route_request(rules, "api.example.com", 443, "/a/x").restricted_by == "mine-first"
    for path, service in [("/a/open/x", "mine-open"), ("/a/mine/x", "mine-first")]:
        route = route_request(rules, "api.example.com", 443, path)
        assert route.match is not None
        assert (route.match.service, route.restricted_by) == (service, None)


_HOST = "api.example.com"
_OTHER = "other.example.com"


def _restricted(*rules: EgressRule) -> EgressService:
    return EgressService(restrict_to_rules=True, rules=list(rules))


# Each case: (row of the `route_request` docstring table, operator services, user services, deny, host, path,
# expected (host_has_rules, refusal, restricted_by, restricted_by_operator, matched service)).
_ROUTE_DECISION_CASES = [
    pytest.param(
        1,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {},
        False,
        _OTHER,
        "/x",
        (False, None, None, False, None),
        id="1-no-rule-names-the-host",
    ),
    pytest.param(
        1,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {},
        False,
        _OTHER,
        "/a/../b",
        (False, None, None, False, None),
        id="1-comes-before-the-ambiguous-path-check",
    ),
    pytest.param(
        1,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {"mine": EgressService(rules=[_rule(_OTHER)])},
        True,
        _OTHER,
        "/x",
        (False, None, None, False, None),
        id="1-deny-keeps-a-user-rule-off-a-host-the-operator-does-not-name",
    ),
    pytest.param(
        1,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {"limit": _limit(_rule(_OTHER, "/ok"))},
        True,
        _OTHER,
        "/no",
        (False, None, None, False, None),
        id="1-deny-keeps-a-user-limit-off-a-host-the-operator-does-not-name",
    ),
    pytest.param(
        2,
        {"op": _restricted(_rule(_HOST, "/a"))},
        {},
        False,
        _HOST,
        "/b/../a",
        (True, "bad_request", None, False, None),
        id="2-ambiguous-path-is-refused-before-any-restriction",
    ),
    pytest.param(
        2,
        {"op": _restricted(_rule(_HOST, "/a"))},
        {},
        False,
        _HOST,
        "/a/../b",
        (True, "bad_request", None, False, None),
        id="2-ambiguous-path-is-refused-even-where-a-rule-matches",
    ),
    pytest.param(
        3,
        {"op": _restricted(_rule(_HOST, "/a"))},
        {"mine": EgressService(rules=[_rule(_HOST)])},
        False,
        _HOST,
        "/b",
        (True, "path_not_allowed", "op", True, None),
        id="3-user-rules-cannot-open-an-operator-restricted-path",
    ),
    pytest.param(
        3,
        {"op": _restricted(_rule(_HOST, "/a"))},
        {"limit": _limit(_rule(_HOST, "/b"))},
        False,
        _HOST,
        "/c",
        (True, "path_not_allowed", "op", True, None),
        id="3-operator-restriction-is-checked-before-the-user-limit",
    ),
    pytest.param(
        3,
        {"op": _restricted(_rule(_HOST, "/a"))},
        {},
        True,
        _HOST,
        "/b",
        (True, "path_not_allowed", "op", True, None),
        id="3-operator-restriction-under-deny",
    ),
    pytest.param(
        4,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {"mine": _restricted(_rule(_HOST, "/mine"))},
        False,
        _HOST,
        "/other",
        (True, "path_not_allowed", "mine", False, None),
        id="4-user-restrict-to-rules-narrows-the-operator-service",
    ),
    pytest.param(
        4,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {"limit": _limit(_rule(_HOST, "/ok"))},
        False,
        _HOST,
        "/no",
        (True, "path_not_allowed", "limit", False, None),
        id="4-user-restrict-only-limit-narrows-the-operator-service",
    ),
    pytest.param(
        4,
        {"op": _restricted(_rule(_HOST, "/a"))},
        {"limit": _limit(_rule(_HOST, "/a/ok"))},
        False,
        _HOST,
        "/a/no",
        (True, "path_not_allowed", "limit", False, None),
        id="4-user-limit-refuses-a-path-the-operator-restriction-allows",
    ),
    pytest.param(
        4,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {"limit": _limit(_rule(_HOST, "/ok"))},
        True,
        _HOST,
        "/no",
        (True, "path_not_allowed", "limit", False, None),
        id="4-user-limit-on-an-operator-host-under-deny",
    ),
    pytest.param(
        5,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {"limit": _limit(_rule(_HOST, "/ok"))},
        False,
        _HOST,
        "/ok/x",
        (True, None, None, False, "op"),
        id="5-restrict-only-rules-never-choose-the-credential",
    ),
    pytest.param(
        5,
        {},
        {"limit": _limit(_rule(_HOST, "/ok"))},
        False,
        _HOST,
        "/ok/x",
        (True, None, None, False, None),
        id="5-a-limit-alone-forwards-allowed-paths-without-credentials",
    ),
    pytest.param(
        5,
        {"op": EgressService(rules=[_rule(_HOST)])},
        {"mine": EgressService(rules=[_rule(_HOST, "/mine")])},
        False,
        _HOST,
        "/mine/x",
        (True, None, None, False, "mine"),
        id="5-the-more-specific-user-rule-chooses-the-credential",
    ),
    pytest.param(
        5,
        {"op": _restricted(_rule(_HOST, "/a"))},
        {"mine": _restricted(_rule(_HOST, "/a/mine"))},
        False,
        _HOST,
        "/a/mine/x",
        (True, None, None, False, "mine"),
        id="5-a-path-both-restrictions-allow-matches",
    ),
    pytest.param(
        5,
        {"op": EgressService(rules=[_rule(_HOST, "/a")])},
        {},
        True,
        _HOST,
        "/b",
        (True, None, None, False, None),
        id="5-no-matching-rule-goes-out-without-credentials-even-under-deny",
    ),
]


@pytest.mark.parametrize(
    ("row", "operator", "user", "deny", "host", "path", "expected"),
    _ROUTE_DECISION_CASES,
)
def test_route_request_follows_its_decision_table(
    row: int,
    operator: dict[str, EgressService],
    user: dict[str, EgressService],
    deny: bool,
    host: str,
    path: str,
    expected: tuple[bool, str | None, str | None, bool, str | None],
) -> None:
    """Each case exercises the row of the `route_request` docstring table it names, so the two cannot drift apart."""
    route = route_request(_merged(operator, user, deny=deny), host, 443, path)

    decision = (
        route.host_has_rules,
        route.refusal,
        route.restricted_by,
        route.restricted_by_operator,
        route.match.service if route.match is not None else None,
    )
    assert decision == expected, f"row {row}"


def test_route_decision_cases_cover_every_row_of_the_table() -> None:
    """The cases above name exactly the rows the `route_request` docstring table has."""
    table_rows = {int(row) for row in re.findall(r"^\s*\| (\d+) ", route_request.__doc__ or "", flags=re.MULTILINE)}

    assert table_rows == {1, 2, 3, 4, 5}
    assert {case.values[0] for case in _ROUTE_DECISION_CASES} == table_rows


@pytest.mark.parametrize(
    ("covering", "rule", "covered"),
    [
        (_rule("api.example.com"), _rule("api.example.com", "/x"), True),
        (_rule("*.example.com"), _rule("api.example.com"), True),
        (_rule("*.example.com"), _rule("*.example.com"), True),
        (_rule("api.example.com"), _rule("*.example.com"), False),
        (_rule("*.example.com"), _rule("*.api.example.com"), False),
        (_rule("*.example.com"), _rule("example.com"), False),
        (_rule("api.example.com", port=8443), _rule("api.example.com"), False),
        (_rule("api.example.com", port=8443), _rule("api.example.com", port=8443), True),
        (_rule("api.example.com"), _rule("api.example.com", port=8443), True),
    ],
)
def test_config_covers_rule(covering: EgressRule, rule: EgressRule, covered: bool) -> None:
    """A config covers a rule when it has a rule for every host and port that rule can match."""
    config = EgressBrokerConfig(services={"op": EgressService(rules=[covering])})

    assert config_covers_rule(config, rule) is covered


@pytest.mark.parametrize(
    ("auth", "header"),
    [
        pytest.param(EgressAuth(type="bearer"), (b"authorization", b"Bearer s3cret"), id="bearer"),
        pytest.param(
            EgressAuth(type="basic", username="x-access-token"),
            (b"authorization", b"Basic " + base64.b64encode(b"x-access-token:s3cret")),
            id="basic",
        ),
        pytest.param(
            EgressAuth(type="header", name="X-Api-Key", template="Key {secret}"),
            (b"x-api-key", b"Key s3cret"),
            id="header-template",
        ),
    ],
)
def test_inject_adds_the_credential_header(auth: EgressAuth, header: tuple[bytes, bytes]) -> None:
    """Bearer adds Authorization, basic encodes username:secret in base64, and a header rule applies its template."""
    headers: list[tuple[bytes, bytes]] = []
    target = b"/api/endpoint"

    new_headers, new_target = inject_credentials(headers, target, auth, "s3cret")
    assert new_target == target
    assert header in new_headers


@pytest.mark.parametrize(
    ("auth", "target", "secret", "expected_target"),
    [
        pytest.param(
            EgressAuth(type="query", name="key"),
            b"/v1?key=client&a=1",
            "s3cret",
            b"/v1?key=s3cret&a=1",
            id="replaces-client-value",
        ),
        pytest.param(
            EgressAuth(type="query", name="token", template="Bearer {secret}"),
            b"/api?token=old",
            "s3cret",
            b"/api?token=Bearer%20s3cret",
            id="custom-template-applied-before-encoding",
        ),
        # Space becomes %20, & becomes %26 (single encoding).
        pytest.param(
            EgressAuth(type="query", name="key"),
            b"/v1?key=old",
            "a b&c",
            b"/v1?key=a%20b%26c",
            id="special-characters-encoded-once",
        ),
        # "a=hello%20world" is preserved byte-for-byte, not changed to "a=hello+world".
        pytest.param(
            EgressAuth(type="query", name="key"),
            b"/v1?a=hello%20world&key=x",
            "secret",
            b"/v1?a=hello%20world&key=secret",
            id="other-params-keep-their-encoding",
        ),
        # The existing param name is already encoded in the URL; "my key" is "my%20key" in the output.
        pytest.param(
            EgressAuth(type="query", name="my key"),
            b"/v1?my%20key=old&a=1",
            "s3cret",
            b"/v1?my%20key=s3cret&a=1",
            id="param-name-encoded",
        ),
        # The path goes upstream byte for byte, `;params` on its last segment included; only the query changes.
        pytest.param(
            EgressAuth(type="query", name="key"),
            b"/repos/o/r;v=1?x=1",
            "s3cret",
            b"/repos/o/r;v=1?x=1&key=s3cret",
            id="path-params-kept",
        ),
        pytest.param(
            EgressAuth(type="query", name="key"),
            b"/repos/o/r;v=1",
            "s3cret",
            b"/repos/o/r;v=1?key=s3cret",
            id="path-params-kept-without-query",
        ),
    ],
)
def test_inject_query_replaces_the_param_and_leaves_headers_alone(
    auth: EgressAuth,
    target: bytes,
    secret: str,
    expected_target: bytes,
) -> None:
    """Query injection puts the secret in the named param, encoded once, and adds no header."""
    headers: list[tuple[bytes, bytes]] = []

    new_headers, new_target = inject_credentials(headers, target, auth, secret)
    assert new_headers == headers
    assert new_target == expected_target


def test_inject_query_removes_thousands_of_duplicates_and_keeps_the_rest_in_order() -> None:
    """Every pair named like the param goes, bare or percent-encoded, and the new pair takes the first one's place."""
    auth = EgressAuth(type="query", name="key")
    pairs = ["a=1", "key=old", "", "key", "%6Bey=x", "key=", "keys=1"] * 1000 + ["b=2"]
    target = ("/v1/x;v=1?" + "&".join(pairs)).encode()
    expected = ["a=1", "key=s3cret", "", "keys=1"] + ["a=1", "", "keys=1"] * 999 + ["b=2"]

    new_headers, new_target = inject_credentials([], target, auth, "s3cret")

    assert new_headers == []
    assert new_target == ("/v1/x;v=1?" + "&".join(expected)).encode()


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


def test_strip_hop_by_hop_removes_proxy_auth_and_connection_listed() -> None:
    """Hop-by-hop stripping removes hop-by-hop headers and those in Connection."""
    headers: list[tuple[bytes, bytes]] = [
        (b"connection", b"close, X-Foo"),
        (b"x-foo", b"bar"),
        (b"proxy-authorization", b"secret"),
        (b"x-keep", b"this"),
        (b"keep-alive", b"timeout=5"),
    ]
    result = strip_hop_by_hop(headers, keep_upgrade=False)

    # Connection-listed header removed
    assert not any(h[0] == b"x-foo" for h in result)
    # Hop-by-hop headers removed
    assert not any(h[0] == b"connection" for h in result)
    assert not any(h[0] == b"proxy-authorization" for h in result)
    assert not any(h[0] == b"keep-alive" for h in result)
    # Normal headers kept
    assert (b"x-keep", b"this") in result


def test_keep_upgrade_preserves_websocket_handshake() -> None:
    """keep_upgrade=True preserves Connection and Upgrade headers."""
    headers: list[tuple[bytes, bytes]] = [
        (b"connection", b"Upgrade"),
        (b"upgrade", b"websocket"),
        (b"x-keep", b"this"),
    ]
    result = strip_hop_by_hop(headers, keep_upgrade=True)

    assert (b"connection", b"Upgrade") in result
    assert (b"upgrade", b"websocket") in result
    assert (b"x-keep", b"this") in result
