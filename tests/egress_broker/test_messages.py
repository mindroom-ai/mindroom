"""Tests for the plain sentence every broker refusal carries for the agent to relay."""

from __future__ import annotations

import pytest

from mindroom.egress_broker.messages import git_error_text, refusal_body
from tests.egress_broker.conftest import CONNECT_URL, MANAGE_URL


@pytest.mark.parametrize(
    ("code", "values", "message"),
    [
        (
            "credential_not_configured",
            {"service": "github", "provider": "github", "connectable": True},
            "No github credential is set for this agent. "
            "Ask the user to connect it at connect_url or add a key at manage_url, then retry.",
        ),
        (
            "credential_not_configured",
            {"service": "openai"},
            "No openai key is set for this agent. Ask the user to add one at manage_url, then retry.",
        ),
        (
            "credential_not_configured",
            {"service": "openai", "shared": True, "managers": ["@alice:example.org"]},
            "No openai key is set for this shared agent. "
            "Only its credential managers (@alice:example.org) can add one, at manage_url.",
        ),
        (
            "credential_not_configured",
            {"service": "openai", "shared": True, "managers": ("@alice:example.org", "@bob:example.org")},
            "No openai key is set for this shared agent. "
            "Only its credential managers (@alice:example.org, @bob:example.org) can add one, at manage_url.",
        ),
        (
            "credential_not_configured",
            {"service": "openai", "shared": True, "managers": None},
            "No openai key is set for this shared agent. Only an administrator can add one, at manage_url.",
        ),
        (
            "credential_not_configured",
            {"service": "openai", "shared": True, "managers": ()},
            "No openai key is set for this shared agent. Only an administrator can add one, at manage_url.",
        ),
        (
            "oauth_connection_required",
            {"service": "github", "provider": "github"},
            "The github account for github must be connected again. "
            "Ask the user to reconnect at connect_url or manage_url.",
        ),
        (
            "oauth_refresh_failed",
            {"service": "drive", "provider": "google_drive"},
            "google_drive did not refresh the account token. "
            "Retry in a minute; if it keeps failing, ask the user to reconnect at manage_url.",
        ),
        (
            "path_not_allowed",
            {"host": "api.github.com", "path": "/repos/o/b", "service": "github-limited"},
            "api.github.com/repos/o/b is outside the paths github-limited allows for this agent. "
            "Ask the user to add it at manage_url if the agent needs it.",
        ),
        (
            "bad_request",
            {"path": "/repos/o/a/../b"},
            "The request path is ambiguous (dot segments, //, backslashes, or encoded forms of them) and was refused. "
            "Use a plain path.",
        ),
        (
            "method_not_allowed",
            {},
            "TRACE and TRACK requests are refused because the server would echo the request, "
            "injected credential included, back to the agent.",
        ),
        (
            "tls_required",
            {"host": "api.github.com", "service": "github"},
            "Credentials are only added over HTTPS. Use https:// for api.github.com.",
        ),
        ("host_mismatch", {}, "The Host header does not match the host this connection was opened for."),
        (
            "host_not_allowed",
            {"host": "evil.example"},
            "evil.example is not allowed by the administrator's egress policy.",
        ),
        (
            "destination_blocked",
            {"host": "metadata.internal"},
            "metadata.internal resolves to a private or internal address that the administrator's egress policy does not let agents reach.",
        ),
        ("request_body_too_large", {}, "The request body is larger than the broker accepts."),
        ("upstream_unreachable", {"host": "api.github.com"}, "api.github.com could not be reached."),
        ("upstream_timeout", {"host": "api.github.com"}, "api.github.com did not answer in time."),
        (
            "broker_error",
            {},
            "The credential broker hit an internal error. Retry; if it keeps failing, tell an administrator.",
        ),
        (
            "proxy_authentication_required",
            {},
            "The sandbox's proxy credentials are missing or expired. Start a new tool call.",
        ),
    ],
)
def test_each_refusal_code_has_its_sentence(code: str, values: dict[str, object], message: str) -> None:
    """Every code the broker answers with carries its fixed sentence, with only the given values filled in."""
    body = refusal_body(code, **values)

    assert body["error"] == code
    assert body["message"] == message


_REUSE_OFF = {"service": "drive", "provider": "google_drive", "reuse_off": True, "manage_url": MANAGE_URL}


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {},
            "No drive key is set for this agent, and Agent Keys may not use a connected google_drive account. "
            "Ask the user to add a key or allow that account at manage_url, then retry.",
        ),
        (
            {"requester_owned": True},
            "No drive key is set for this agent, and Agent Keys may not use a connected google_drive account. "
            "Ask the user to add a key or allow that account at manage_url, then retry.",
        ),
        (
            {"shared": True, "managers": ("@bob:example.org",)},
            "No drive key is set for this shared agent, and Agent Keys may not use its connected google_drive "
            "account. Only its credential managers (@bob:example.org) can add a key or allow that account, "
            "at manage_url.",
        ),
        (
            {"shared": True},
            "No drive key is set for this shared agent, and Agent Keys may not use its connected google_drive "
            "account. Only an administrator can add a key or allow that account, at manage_url.",
        ),
        (
            {"shared": True, "managers": ("@bob:example.org",), "requester_owned": True},
            "No drive key is set for this shared agent, and Agent Keys may not use the user's own google_drive "
            "account. Ask the user to allow it at manage_url, then retry.",
        ),
    ],
    ids=["personal", "personal-own-account", "shared", "shared-without-managers", "shared-own-account"],
)
def test_an_account_agent_keys_may_not_use_says_who_can_allow_it(values: dict[str, object], message: str) -> None:
    """A refusal for an account nobody allowed names the provider, says so in `oauth_reuse`, and offers no link.

    On a shared agent the agent's own account is its managers' to allow, while a requester's own account is theirs.
    """
    body = refusal_body("credential_not_configured", **{**_REUSE_OFF, **values})

    assert body == {
        "error": "credential_not_configured",
        "service": "drive",
        "provider": "google_drive",
        "oauth_reuse": "off",
        "manage_url": MANAGE_URL,
        "message": message,
    }


def test_shared_agent_with_a_connect_link_gets_the_connectable_text() -> None:
    """A refusal that carries a connect link says so, even on a shared agent whose managers could be named."""
    body = refusal_body(
        "credential_not_configured",
        service="github",
        provider="github",
        shared=True,
        connectable=True,
        managers=("@alice:example.org",),
    )

    assert body["message"] == (
        "No github credential is set for this agent. "
        "Ask the user to connect it at connect_url or add a key at manage_url, then retry."
    )
    assert "@alice:example.org" not in str(body)


def test_path_not_allowed_names_the_display_name_and_on_shared_agents_who_can_change_it() -> None:
    """The limit's display name names it; a shared agent's refusal names its managers, or an administrator."""
    values = {"host": "api.github.com", "path": "/repos/o/b", "service": "github-limited"}
    outside = "api.github.com/repos/o/b is outside the paths GitHub (o/a) allows for this agent. "

    personal = refusal_body("path_not_allowed", service_label="GitHub (o/a)", **values)
    shared = refusal_body(
        "path_not_allowed",
        service_label="GitHub (o/a)",
        shared=True,
        managers=("@alice:example.org", "@bob:example.org"),
        **values,
    )
    unmanaged = refusal_body("path_not_allowed", service_label="GitHub (o/a)", shared=True, **values)

    assert personal["message"] == outside + "Ask the user to add it at manage_url if the agent needs it."
    assert shared["message"] == (
        outside + "Only its credential managers (@alice:example.org, @bob:example.org) can change this, at manage_url."
    )
    assert unmanaged["message"] == outside + "Only an administrator can change this, at manage_url."
    # The machine-readable field keeps the service name.
    assert {personal["service"], shared["service"], unmanaged["service"]} == {"github-limited"}


@pytest.mark.parametrize(
    ("shared", "managers"),
    [(False, ()), (True, ("@alice:example.org",)), (True, ())],
    ids=["personal", "shared-with-managers", "shared-without-managers"],
)
def test_path_not_allowed_by_the_operator_names_an_administrator_whatever_the_scope(
    shared: bool,
    managers: tuple[str, ...],
) -> None:
    """Only an administrator edits the operator's services, so neither the user nor the managers are sent to do it."""
    body = refusal_body(
        "path_not_allowed",
        host="api.github.com",
        path="/user",
        service="github",
        service_label="GitHub",
        shared=shared,
        managers=managers,
        by_operator=True,
    )

    assert body["message"] == (
        "api.github.com/user is outside the paths GitHub allows for this agent. "
        "Only an administrator can change this, at manage_url."
    )
    assert set(body) == {"error", "service", "message"}


# Where the personal page is not served, a personal scope's keys and services cannot be set anywhere.
_PAGE_NOT_SERVED = (
    "on the Connections page, which nobody can sign in to here. "
    "An administrator can enable Connections sign-in or make the agent shared."
)
_PATH = {"host": "api.github.com", "path": "/repos/o/b?page=2", "service": "github-limited"}


@pytest.mark.parametrize(
    ("code", "values", "message"),
    [
        (
            "credential_not_configured",
            {"service": "openai"},
            "No openai key: each user sets this agent's keys " + _PAGE_NOT_SERVED,
        ),
        (
            "credential_not_configured",
            {"service": "github", "provider": "github", "connectable": True},
            "No github credential is set for this agent. Ask the user to connect it at connect_url, then retry.",
        ),
        (
            "oauth_connection_required",
            {"service": "github", "provider": "github", "connectable": True},
            "The github account for github must be connected again. Ask the user to reconnect at connect_url.",
        ),
        (
            "oauth_connection_required",
            {"service": "github", "provider": "github"},
            "Reconnect the github account for github: each user does it " + _PAGE_NOT_SERVED,
        ),
        (
            "oauth_refresh_failed",
            {"service": "drive", "provider": "google_drive"},
            "google_drive did not refresh the account token. "
            "Retry in a minute; if it keeps failing, tell an administrator.",
        ),
        (
            "path_not_allowed",
            _PATH,
            "api.github.com/repos/o/b is outside the paths github-limited allows for this agent. "
            "Each user sets this agent's services " + _PAGE_NOT_SERVED,
        ),
    ],
)
def test_personal_scope_without_the_personal_page_gets_no_manage_link(
    code: str,
    values: dict[str, object],
    message: str,
) -> None:
    """Only the personal page sets a personal scope's keys, so where it is not served no refusal links a page.

    The body drops `manage_url`, which would be the dashboard, and git's ERR line says the same sentence. A reconnect
    without a connect link (`connectable` false) names none either.
    """
    body = refusal_body(code, personal_page=False, manage_url="https://m.test/", **values)

    assert body["message"] == message
    assert "manage_url" not in body
    assert git_error_text(body) == message


@pytest.mark.parametrize("code", ["credential_not_configured", "oauth_connection_required", "oauth_refresh_failed"])
def test_personal_page_not_served_messages_fit_the_reason_phrase_for_a_provider_like_github(code: str) -> None:
    """The HTTP reason phrase is cut at 200 characters, so these refusals stay short enough to carry their message whole."""
    body = refusal_body(code, personal_page=False, service="GitHub", provider="GitHub")

    assert len(str(body["message"])) <= 200


@pytest.mark.parametrize(
    ("code", "values"),
    [
        ("credential_not_configured", {"service": "openai", "managers": ("@alice:example.org",)}),
        ("credential_not_configured", {"service": "github", "provider": "github", "connectable": True}),
        ("oauth_connection_required", {"service": "github", "provider": "github"}),
        ("oauth_connection_required", {"service": "github", "provider": "github", "connectable": True}),
        ("oauth_refresh_failed", {"service": "drive", "provider": "google_drive"}),
        ("path_not_allowed", {**_PATH, "managers": ("@alice:example.org",)}),
    ],
)
def test_shared_scopes_keep_their_refusals_without_the_personal_page(code: str, values: dict[str, object]) -> None:
    """Shared and unscoped keys are set on the dashboard, so their refusals stay the same whether the page is served."""
    served = refusal_body(code, shared=True, manage_url=MANAGE_URL, **values)

    assert refusal_body(code, shared=True, personal_page=False, manage_url=MANAGE_URL, **values) == served
    assert served["manage_url"] == MANAGE_URL


def test_operator_limit_on_a_personal_scope_keeps_the_manage_link_without_the_personal_page() -> None:
    """Only an administrator changes the operator's services, on the dashboard, whether or not the page is served."""
    body = refusal_body("path_not_allowed", by_operator=True, personal_page=False, manage_url=MANAGE_URL, **_PATH)

    assert body["manage_url"] == MANAGE_URL
    assert body["message"] == (
        "api.github.com/repos/o/b is outside the paths github-limited allows for this agent. "
        "Only an administrator can change this, at manage_url."
    )


def test_path_in_a_message_never_includes_the_query() -> None:
    """A query can hold a secret the client sent, so the message names the path without it."""
    body = refusal_body(
        "path_not_allowed",
        host="api.github.com",
        path="/repos/o/b?access_token=leaked&page=2",
        service="github",
    )

    assert body["message"] == (
        "api.github.com/repos/o/b is outside the paths github allows for this agent. "
        "Ask the user to add it at manage_url if the agent needs it."
    )
    assert "leaked" not in str(body)


def test_body_keeps_its_fields_and_adds_only_service_and_provider() -> None:
    """Service and provider join the body when given; other fields pass through, and the text inputs stay out."""
    body = refusal_body(
        "credential_not_configured",
        service="github",
        provider="github",
        connectable=True,
        manage_url=MANAGE_URL,
        connect_url=CONNECT_URL,
    )

    assert body == {
        "error": "credential_not_configured",
        "service": "github",
        "provider": "github",
        "manage_url": MANAGE_URL,
        "connect_url": CONNECT_URL,
        "message": "No github credential is set for this agent. "
        "Ask the user to connect it at connect_url or add a key at manage_url, then retry.",
    }
    assert "connect_token" not in str(body["message"])
    assert refusal_body("host_not_allowed", host="evil.example", services=["github"]) == {
        "error": "host_not_allowed",
        "services": ["github"],
        "message": "evil.example is not allowed by the administrator's egress policy.",
    }


def test_malformed_request_without_a_path_gets_its_own_text() -> None:
    """A `bad_request` without a path is a request the broker could not read, not an ambiguous path."""
    assert refusal_body("bad_request") == {
        "error": "bad_request",
        "message": "The request is malformed and was refused.",
    }


def test_unknown_code_or_missing_value_is_a_programming_error() -> None:
    """A code without a text, or a text missing a value it names, raises instead of writing a broken sentence."""
    with pytest.raises(ValueError, match="no_such_code"):
        refusal_body("no_such_code")
    with pytest.raises(KeyError, match="host"):
        refusal_body("upstream_unreachable")
