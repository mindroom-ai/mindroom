"""Tests for services users define in their own scope and the effective config the broker matches against."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom.config.egress_broker import EgressBrokerConfig, EgressService
from mindroom.credentials import CredentialsManager
from mindroom.egress_broker import user_services
from mindroom.egress_broker.rules import EgressRules, host_has_rules, route_request
from mindroom.egress_broker.secrets import load_secret, save_secret
from mindroom.egress_broker.user_services import (
    MAX_RULES_PER_SERVICE,
    MAX_USER_SERVICES,
    USER_SERVICES_CREDENTIAL_SERVICE,
    UserServiceConflictError,
    delete_user_service,
    effective_config,
    load_user_services,
    save_user_service,
    user_service_hosts_not_allowed,
)
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget, WorkerScope

_ALICE = "@alice:example.org"
_BOB = "@bob:example.org"
_CONFIG = EgressBrokerConfig.model_validate(
    {"unmatched_hosts": "deny", "services": {"github": {"preset": "github"}}},
)


@pytest.fixture
def manager(tmp_path: Path) -> CredentialsManager:
    """Return a primary credentials manager rooted under `tmp_path`."""
    return CredentialsManager(tmp_path / "mindroom_data" / "credentials")


def _target(
    requester_id: str = _ALICE,
    agent_name: str = "code",
    *,
    scope: WorkerScope | None = "user_agent",
) -> ResolvedWorkerTarget:
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=requester_id,
        room_id="!room:example.org",
        thread_id="$thread",
        resolved_thread_id="$thread",
        session_id="session-1",
        tenant_id=None,
        account_id=None,
    )
    return resolve_worker_target(scope, agent_name, identity, private_agent_names=frozenset())


def _service(host: str = "api.example.com", **fields: object) -> EgressService:
    return EgressService.model_validate({"rules": [{"host": host, "auth": {"type": "bearer"}}], **fields})


def _save(manager: CredentialsManager, target: ResolvedWorkerTarget | None, name: str, service: EgressService) -> None:
    save_user_service(manager, target, name, service, config_services=_CONFIG.services)


def _delete(manager: CredentialsManager, target: ResolvedWorkerTarget | None, name: str) -> bool:
    return delete_user_service(manager, target, name, config_services=_CONFIG.services)


def test_saved_service_is_stored_in_authored_form_in_the_primary_only_scope_store(manager: CredentialsManager) -> None:
    """A preset service keeps its preset form, in the requester's primary-only store under the reserved name."""
    _save(manager, _target(), "work-github", EgressService.model_validate({"preset": "github", "display_name": "Work"}))

    loaded = load_user_services(manager, _target())

    assert list(loaded) == ["work-github"]
    assert loaded["work-github"].oauth_provider == "github"
    assert [rule.host for rule in loaded["work-github"].rules] == ["api.github.com", "uploads.github.com", "github.com"]
    stored = manager.for_primary_runtime_scope(_ALICE, "code").load_credentials(USER_SERVICES_CREDENTIAL_SERVICE)
    assert stored is not None
    assert stored["services"] == {"work-github": {"preset": "github", "display_name": "Work"}}


def test_services_belong_to_their_scope(manager: CredentialsManager) -> None:
    """Personal services stay with their requester and agent; a shared agent's services apply to all its requesters."""
    _save(manager, _target(), "mine", _service())
    _save(manager, _target(scope="shared"), "team", _service("team.example.com"))

    assert list(load_user_services(manager, _target())) == ["mine"]
    assert load_user_services(manager, _target(_BOB)) == {}
    assert load_user_services(manager, _target(agent_name="docs")) == {}
    assert load_user_services(manager, _target(scope="user")) == {}
    assert list(load_user_services(manager, _target(_ALICE, scope="shared"))) == ["team"]
    assert list(load_user_services(manager, _target(_BOB, scope="shared"))) == ["team"]


def test_global_scope_has_its_own_services(manager: CredentialsManager) -> None:
    """Services saved without a target live in the global store that unscoped agents read."""
    _save(manager, None, "everyone", _service())

    assert list(load_user_services(manager, None)) == ["everyone"]
    assert list(load_user_services(manager, _target(scope=None))) == ["everyone"]
    assert load_user_services(manager, _target()) == {}


def test_config_service_name_is_a_conflict(manager: CredentialsManager) -> None:
    """The operator's service wins: saving a user service under its name is refused and stores nothing."""
    with pytest.raises(UserServiceConflictError, match="github"):
        _save(manager, _target(), "github", _service())

    assert load_user_services(manager, _target()) == {}


@pytest.mark.parametrize(
    "name",
    ["_services", "", "GitHub", "a" * 64, "-leading-dash", "with space", "mine_oauth", "mine_oauth_client"],
)
def test_invalid_names_are_rejected(manager: CredentialsManager, name: str) -> None:
    """Names follow the config rules, so none can produce the reserved `egress__services` store name."""
    with pytest.raises(ValueError, match="service name"):
        _save(manager, _target(), name, _service())

    assert load_user_services(manager, _target()) == {}


def test_oauth_on_shared_workers_stays_operator_only(manager: CredentialsManager) -> None:
    """Only an operator may let personal accounts serve shared sandboxes."""
    with pytest.raises(ValueError, match="oauth_on_shared_workers"):
        _save(manager, _target(), "mine", _service(oauth_provider="github", oauth_on_shared_workers=True))

    assert load_user_services(manager, _target()) == {}


def test_service_count_is_limited_per_scope(manager: CredentialsManager) -> None:
    """A scope holds at most 50 services; replacing one of them still works at the limit."""
    for index in range(MAX_USER_SERVICES):
        _save(manager, _target(), f"svc-{index}", _service())

    with pytest.raises(ValueError, match=f"at most {MAX_USER_SERVICES} services"):
        _save(manager, _target(), "one-too-many", _service())
    _save(manager, _target(), "svc-0", _service("replaced.example.com"))

    loaded = load_user_services(manager, _target())
    assert len(loaded) == MAX_USER_SERVICES
    assert loaded["svc-0"].rules[0].host == "replaced.example.com"
    # Another scope has its own allowance.
    _save(manager, _target(_BOB), "svc-0", _service())


def test_rule_count_is_limited_per_service(manager: CredentialsManager) -> None:
    """A service has at most 50 rules."""
    rules = [
        {"host": f"h{index}.example.com", "auth": {"type": "bearer"}} for index in range(MAX_RULES_PER_SERVICE + 1)
    ]

    _save(manager, _target(), "fits", EgressService.model_validate({"rules": rules[:MAX_RULES_PER_SERVICE]}))
    with pytest.raises(ValueError, match=f"at most {MAX_RULES_PER_SERVICE} rules"):
        _save(manager, _target(), "too-big", EgressService.model_validate({"rules": rules}))

    assert list(load_user_services(manager, _target())) == ["fits"]


def test_invalid_stored_entry_is_skipped_by_name_only(manager: CredentialsManager) -> None:
    """One entry that no longer validates is skipped with a warning naming it; the rest of the scope still loads."""
    store = manager.for_primary_runtime_scope(_ALICE, "code")
    good = _service().authored_model_dump()
    store.save_credentials(
        USER_SERVICES_CREDENTIAL_SERVICE,
        {
            "services": {
                "good": good,
                "bad-auth": {"rules": [{"host": "x.example.com", "auth": {"type": "carrier-pigeon"}}]},
                "opted-in": {**good, "oauth_on_shared_workers": True},
                "Bad Name": good,
                "not-a-dict": "rules",
            },
        },
    )

    with capture_logs() as logs:
        loaded = load_user_services(manager, _target())

    assert list(loaded) == ["good"]
    skipped = [entry["service"] for entry in logs if entry["event"] == "egress_broker_user_service_invalid"]
    assert skipped == ["bad-auth", "opted-in", "Bad Name", "not-a-dict"]
    assert "carrier-pigeon" not in str(logs)
    assert "x.example.com" not in str(logs)


def test_malformed_document_reads_as_no_services(manager: CredentialsManager) -> None:
    """A document without a services mapping is ignored with a warning instead of failing the scope."""
    store = manager.for_primary_runtime_scope(_ALICE, "code")
    store.save_credentials(USER_SERVICES_CREDENTIAL_SERVICE, {"services": ["not", "a", "mapping"]})

    with capture_logs() as logs:
        assert load_user_services(manager, _target()) == {}

    assert [entry["event"] for entry in logs] == ["egress_broker_user_services_malformed"]


def test_delete_removes_one_service_and_the_document_with_the_last(manager: CredentialsManager) -> None:
    """Deleting reports whether the service existed; the last deletion removes the document itself."""
    _save(manager, _target(), "one", _service())
    _save(manager, _target(), "two", _service())

    assert _delete(manager, _target(), "one") is True
    assert _delete(manager, _target(), "one") is False
    assert list(load_user_services(manager, _target())) == ["two"]
    assert _delete(manager, _target(), "two") is True
    store = manager.for_primary_runtime_scope(_ALICE, "code")
    assert store.load_credentials(USER_SERVICES_CREDENTIAL_SERVICE) is None


def test_effective_config_puts_config_services_first_and_ignores_shadowed_user_services(
    manager: CredentialsManager,
) -> None:
    """A user service named like a config service never changes it; top-level settings come from config only."""
    # Saved while the config had no `github`, then the operator added one.
    save_user_service(manager, _target(), "github", _service("evil.example.com"), config_services={})
    _save(manager, _target(), "mine", _service())

    effective = effective_config(_CONFIG, manager, _target())

    assert list(effective.services) == ["github", "mine"]
    assert effective.services["github"] is _CONFIG.services["github"]
    assert effective.unmatched_hosts == "deny"
    assert not host_has_rules(effective, "evil.example.com", 443)
    assert _CONFIG.services.keys() == {"github"}


def test_effective_config_without_user_services_is_the_config(manager: CredentialsManager) -> None:
    """A scope without services matches against the config object itself."""
    assert effective_config(_CONFIG, manager, _target()) is _CONFIG
    assert effective_config(_CONFIG, manager, None) is _CONFIG


def test_effective_config_is_cached_and_invalidated_by_save_and_delete(manager: CredentialsManager) -> None:
    """Repeated reads reuse one merged config; every save and delete makes the next read see the change."""
    assert effective_config(_CONFIG, manager, _target()) is _CONFIG

    _save(manager, _target(), "mine", _service())
    saved = effective_config(_CONFIG, manager, _target())
    assert list(saved.services) == ["github", "mine"]
    assert effective_config(_CONFIG, manager, _target()) is saved

    _save(manager, _target(), "mine", _service("other.example.com"))
    replaced = effective_config(_CONFIG, manager, _target())
    assert replaced.services["mine"].rules[0].host == "other.example.com"

    _delete(manager, _target(), "mine")
    assert effective_config(_CONFIG, manager, _target()) is _CONFIG


def test_effective_config_follows_a_config_change(manager: CredentialsManager) -> None:
    """A new config object is merged afresh: its services appear and a now-shadowed user service drops out."""
    save_user_service(manager, _target(), "openai", _service(), config_services={})
    before = effective_config(_CONFIG, manager, _target())
    changed = EgressBrokerConfig.model_validate({"services": {"openai": {"preset": "openai"}}})

    after = effective_config(changed, manager, _target())

    assert list(before.services) == ["github", "openai"]
    assert before.services["openai"].rules[0].host == "api.example.com"
    assert list(after.services) == ["openai"]
    assert after.services["openai"] is changed.services["openai"]
    assert after.unmatched_hosts == "passthrough"


def test_a_read_racing_a_save_is_never_cached(manager: CredentialsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """A merge built from a read that a save overtook is returned once but not kept, so the next read is fresh."""
    original = user_services.load_user_services

    def read_then_save(
        read_manager: CredentialsManager,
        target: ResolvedWorkerTarget | None,
    ) -> dict[str, EgressService]:
        stale = original(read_manager, target)
        _save(manager, _target(), "late", _service())
        return stale

    monkeypatch.setattr(user_services, "load_user_services", read_then_save)
    assert effective_config(_CONFIG, manager, _target()) is _CONFIG
    monkeypatch.undo()

    assert list(effective_config(_CONFIG, manager, _target()).services) == ["github", "late"]


def test_scopes_have_separate_cache_entries(manager: CredentialsManager) -> None:
    """Alice's cached merge is never served to Bob, whose own services are merged separately."""
    _save(manager, _target(), "alice-svc", _service())
    _save(manager, _target(_BOB), "bob-svc", _service())

    assert list(effective_config(_CONFIG, manager, _target()).services) == ["github", "alice-svc"]
    assert list(effective_config(_CONFIG, manager, _target(_BOB)).services) == ["github", "bob-svc"]
    assert list(effective_config(_CONFIG, manager, _target()).services) == ["github", "alice-svc"]


def test_restrict_to_rules_on_a_user_service_applies_only_to_its_scope(manager: CredentialsManager) -> None:
    """Alice restricting her service's host refuses her unlisted paths; Bob's traffic to that host is untouched."""
    restricted = EgressService.model_validate(
        {
            "rules": [{"host": "api.example.com", "path_prefix": "/repos/alice/x", "auth": {"type": "bearer"}}],
            "restrict_to_rules": True,
        },
    )
    _save(manager, _target(), "repo", restricted)
    # Under passthrough, so the user host is the scope's own to intercept.
    config = EgressBrokerConfig.model_validate({"services": {"github": {"preset": "github"}}})

    alice = EgressRules(operator=config, effective=effective_config(config, manager, _target()))
    bob = EgressRules(operator=config, effective=effective_config(config, manager, _target(_BOB)))

    assert route_request(alice, "api.example.com", 443, "/repos/alice/x/pulls").match is not None
    assert route_request(alice, "api.example.com", 443, "/repos/bob/y").refusal == "path_not_allowed"
    assert route_request(bob, "api.example.com", 443, "/repos/bob/y").host_has_rules is False


def test_hosts_outside_deny_are_reported_for_a_save_check() -> None:
    """Under deny the helper names each user host no config rule covers; under passthrough it names none."""
    service = EgressService.model_validate(
        {
            "rules": [
                {"host": "api.github.com", "path_prefix": "/repos/o/a", "auth": {"type": "bearer"}},
                {"host": "evil.example.com", "auth": {"type": "bearer"}},
                {"host": "evil.example.com", "path_prefix": "/again", "auth": {"type": "bearer"}},
                {"host": "*.github.com", "auth": {"type": "bearer"}},
            ],
        },
    )
    passthrough = EgressBrokerConfig.model_validate({"services": {"github": {"preset": "github"}}})

    assert user_service_hosts_not_allowed(_CONFIG, service) == ["evil.example.com", "*.github.com"]
    assert user_service_hosts_not_allowed(passthrough, service) == []


def test_a_new_service_never_inherits_a_stored_key(manager: CredentialsManager) -> None:
    """Saving a new name clears a key left under it; replacing the service keeps its key; deleting removes it."""
    for target in (_target(), _target(scope="shared")):
        # For example a key a since-removed config service of the same name left behind.
        save_secret(manager, target, "mine", "left-behind")
        _save(manager, target, "mine", _service())
        assert load_secret(manager, target, "mine") is None

        save_secret(manager, target, "mine", "own-key")
        _save(manager, target, "mine", _service("other.example.com"))
        assert load_secret(manager, target, "mine") == "own-key"

        assert _delete(manager, target, "mine")
        assert load_secret(manager, target, "mine") is None


def test_deleting_a_shadowed_entry_keeps_the_key_its_config_service_uses(manager: CredentialsManager) -> None:
    """A config service of the same name shadows the entry and uses the key in this scope, so only the entry goes."""
    shadowing = {**_CONFIG.services, "mine": _service("config.example.com")}
    for target in (_target(), _target(scope="shared")):
        _save(manager, target, "mine", _service())
        save_secret(manager, target, "mine", "key")

        assert delete_user_service(manager, target, "mine", config_services=shadowing) is True
        assert load_user_services(manager, target) == {}
        assert load_secret(manager, target, "mine") == "key"
        assert delete_user_service(manager, target, "mine", config_services=shadowing) is False

        # Without a config service of that name the key goes with the entry.
        _save(manager, target, "mine", _service())
        save_secret(manager, target, "mine", "own-key")
        assert _delete(manager, target, "mine") is True
        assert load_secret(manager, target, "mine") is None


@pytest.mark.parametrize("scope", ["shared", None], ids=["shared", "unscoped"])
def test_oauth_provider_is_refused_outside_personal_scopes(
    manager: CredentialsManager,
    scope: WorkerScope | None,
) -> None:
    """A manager cannot route a shared or unscoped agent's OAuth connection to an arbitrary host, preset or not."""
    for target in (_target(scope=scope), None) if scope is None else (_target(scope=scope),):
        for authored in ({"oauth_provider": "github"}, {"preset": "github"}):
            with pytest.raises(ValueError, match="oauth_provider"):
                _save(
                    manager,
                    target,
                    "mine",
                    EgressService.model_validate({**_service().authored_model_dump(), **authored}),
                )
        _save(manager, target, "keyed", EgressService.model_validate({"preset": "github", "oauth_provider": None}))
        assert load_user_services(manager, target)["keyed"].oauth_provider is None

    _save(manager, _target(), "personal", _service(oauth_provider="github"))
    assert load_user_services(manager, _target())["personal"].oauth_provider == "github"


def test_stored_oauth_provider_is_ignored_outside_personal_scopes(manager: CredentialsManager) -> None:
    """An OAuth provider stored for a shared agent anyway is dropped on load, so nothing resolves or reports a token."""
    store = manager.for_primary_runtime_agent_scope("code")
    store.save_credentials(
        USER_SERVICES_CREDENTIAL_SERVICE,
        {"services": {"team": {"preset": "github"}}},
    )

    with capture_logs() as logs:
        loaded = load_user_services(manager, _target(scope="shared"))

    assert loaded["team"].oauth_provider is None
    assert [rule.host for rule in loaded["team"].rules] == ["api.github.com", "uploads.github.com", "github.com"]
    assert effective_config(_CONFIG, manager, _target(scope="shared")).services["team"].oauth_provider is None
    assert [(entry["event"], entry["service"]) for entry in logs] == [
        ("egress_broker_user_service_oauth_ignored", "team"),
    ]
