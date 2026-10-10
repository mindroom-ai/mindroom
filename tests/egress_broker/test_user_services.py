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
# Provider ids a user service may name, as the API reads them from the registry.
_PROVIDERS = frozenset({"github"})
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
    save_user_service(manager, target, name, service, config_services=_CONFIG.services, oauth_providers=_PROVIDERS)


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
    save_user_service(
        manager,
        _target(),
        "github",
        _service("evil.example.com"),
        config_services={},
        oauth_providers=_PROVIDERS,
    )
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
    save_user_service(manager, _target(), "openai", _service(), config_services={}, oauth_providers=_PROVIDERS)
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


@pytest.mark.parametrize(
    "variable",
    ["LD_PRELOAD", "NODE_OPTIONS", "BASH_ENV", "PYTHONPATH", "TOKENIZER_MODE", "_GH_TOKEN", "GIT_ASKPASS"],
)
def test_placeholder_names_must_be_credential_words(manager: CredentialsManager, variable: str) -> None:
    """A user placeholder needs a credential word between underscores, so loader and shell variables are refused."""
    with pytest.raises(ValueError, match="placeholder_env name"):
        _save(manager, _target(), "mine", _service(placeholder_env={variable: "mindroom-brokered"}))

    assert load_user_services(manager, _target()) == {}


def test_credential_placeholder_names_are_accepted(manager: CredentialsManager) -> None:
    """Common credential variables qualify."""
    names = ["GH_TOKEN", "OPENAI_API_KEY", "GITLAB_PAT", "AWS_ACCESS_KEY_ID", "DB_PASSWORD", "X_AUTH", "APP_SECRET"]
    _save(manager, _target(), "mine", _service(placeholder_env=dict.fromkeys(names, "mindroom-brokered")))

    assert list(load_user_services(manager, _target())["mine"].placeholder_env) == names


@pytest.mark.parametrize("value", ["", "two words", "a/b", "x" * 257, "$(id)", "semi;colon", "pa\tss"])
def test_placeholder_values_must_be_plain(manager: CredentialsManager, value: str) -> None:
    """A user placeholder value is 1 to 256 plain characters; the error never quotes it."""
    with pytest.raises(ValueError, match="placeholder_env value of 'GH_TOKEN'") as raised:
        _save(manager, _target(), "mine", _service(placeholder_env={"GH_TOKEN": value}))

    if value:
        assert value not in str(raised.value)
    assert load_user_services(manager, _target()) == {}


def test_stored_placeholders_users_may_not_set_are_dropped(manager: CredentialsManager) -> None:
    """A stored placeholder that fails the user rules is dropped by name; the service and its other placeholders stay."""
    store = manager.for_primary_runtime_scope(_ALICE, "code")
    authored = {
        **_service().authored_model_dump(),
        "placeholder_env": {"GH_TOKEN": "mindroom-brokered", "LD_PRELOAD": "evil.so", "NPM_TOKEN": "has space"},
    }
    store.save_credentials(USER_SERVICES_CREDENTIAL_SERVICE, {"services": {"mine": authored}})

    with capture_logs() as logs:
        loaded = load_user_services(manager, _target())

    assert loaded["mine"].placeholder_env == {"GH_TOKEN": "mindroom-brokered"}
    dropped = [
        (entry["service"], entry["variable"])
        for entry in logs
        if entry["event"] == "egress_broker_user_service_placeholder_ignored"
    ]
    assert dropped == [("mine", "LD_PRELOAD"), ("mine", "NPM_TOKEN")]
    assert "evil.so" not in str(logs)
    assert "has space" not in str(logs)


def test_a_service_is_limited_to_16_kib(manager: CredentialsManager) -> None:
    """One service's authored JSON may take at most 16 KiB."""
    _save(manager, _target(), "fits", _service(description="x" * 16_000))
    with pytest.raises(ValueError, match="at most 16 KiB"):
        _save(manager, _target(), "too-big", _service(description="x" * 16_500))

    assert list(load_user_services(manager, _target())) == ["fits"]


def test_a_scope_document_is_limited_to_256_kib(manager: CredentialsManager) -> None:
    """All of a scope's services together may take at most 256 KiB, well before the 50-service limit."""
    for index in range(16):
        _save(manager, _target(), f"svc-{index}", _service(description="x" * 15_500))

    with pytest.raises(ValueError, match="at most 256 KiB"):
        _save(manager, _target(), "svc-16", _service(description="x" * 15_500))

    assert len(load_user_services(manager, _target())) == 16
    # Replacing a service with a smaller one still fits.
    _save(manager, _target(), "svc-0", _service())


def test_oauth_provider_must_be_registered(manager: CredentialsManager) -> None:
    """A user service can name only a provider the registry knows, a preset's provider included."""
    with pytest.raises(ValueError, match="unknown oauth_provider 'nope'"):
        _save(manager, _target(), "mine", _service(oauth_provider="nope"))
    with pytest.raises(ValueError, match="unknown oauth_provider 'atlassian'"):
        _save(manager, _target(), "mine", EgressService.model_validate({"preset": "atlassian"}))

    assert load_user_services(manager, _target()) == {}
    _save(manager, _target(), "mine", _service(oauth_provider="github"))
    assert load_user_services(manager, _target())["mine"].oauth_provider == "github"


def test_equal_config_copies_share_a_cache_entry(manager: CredentialsManager, monkeypatch: pytest.MonkeyPatch) -> None:
    """The broker's and the tools' config objects can be separate equal copies; they share one entry, not evict."""
    _save(manager, _target(), "mine", _service())
    authored = {"unmatched_hosts": "deny", "services": {"github": {"preset": "github"}}}
    api_copy = EgressBrokerConfig.model_validate(authored)
    orchestrator_copy = EgressBrokerConfig.model_validate(authored)
    reads: list[object] = []
    original = user_services.load_user_services

    def counting(read_manager: CredentialsManager, target: ResolvedWorkerTarget | None) -> dict[str, EgressService]:
        reads.append(target)
        return original(read_manager, target)

    monkeypatch.setattr(user_services, "load_user_services", counting)
    first = effective_config(api_copy, manager, _target())
    for _ in range(3):
        assert effective_config(orchestrator_copy, manager, _target()) is first
        assert effective_config(api_copy, manager, _target()) is first
    changed = EgressBrokerConfig.model_validate({**authored, "unmatched_hosts": "passthrough"})
    assert effective_config(changed, manager, _target()).unmatched_hosts == "passthrough"

    assert len(reads) == 2


def test_reordered_config_is_merged_again_so_ties_follow_the_new_order(manager: CredentialsManager) -> None:
    """Equal services in another order break rule ties differently, so the cache never serves the old order."""
    _save(manager, _target(), "mine", _service("mine.example.com"))
    tied = {"rules": [{"host": "api.example.com", "auth": {"type": "bearer"}}]}
    first = EgressBrokerConfig.model_validate({"services": {"alpha": tied, "beta": tied}})
    reordered = EgressBrokerConfig.model_validate({"services": {"beta": tied, "alpha": tied}})
    # Pydantic equality ignores dict order, which is what made the cache reuse the old merge.
    assert first == reordered

    def picked(config: EgressBrokerConfig, target: ResolvedWorkerTarget) -> str:
        rules = EgressRules(operator=config, effective=effective_config(config, manager, target))
        match = route_request(rules, "api.example.com", 443, "/")
        assert match.match is not None
        return match.match.service

    for target in (_target(), _target(_BOB)):  # with and without services of the scope's own
        assert picked(first, target) == "alpha"
        assert picked(reordered, target) == "beta"
        assert picked(first, target) == "alpha"


@pytest.mark.parametrize(
    "variable",
    [
        "ANSIBLE_VAULT_PASSWORD_FILE",
        "SOPS_AGE_KEY_CMD",
        "RESTIC_PASSWORD_COMMAND",
        "PASSWORD_STORE_EXTENSIONS_DIR",
        "PASSWORD_STORE_ENABLE_EXTENSIONS",
        "PASSWORD_STORE_GPG_OPTS",
        "CARGO_REGISTRY_CREDENTIAL_PROVIDER",
        "SSH_AUTH_SOCK",
        "GIT_CREDENTIAL_HELPER",
        "GH_TOKEN_OPTIONS",
    ],
)
def test_placeholder_names_that_tools_run_or_load_are_refused(manager: CredentialsManager, variable: str) -> None:
    """Credential-looking names that tools read as a program, path, or options are refused, and so is SSH_*."""
    with pytest.raises(ValueError, match=f"placeholder_env name '{variable}' may not"):
        _save(manager, _target(), "mine", _service(placeholder_env={variable: "mindroom-brokered"}))

    assert load_user_services(manager, _target()) == {}


def test_stored_placeholders_that_tools_run_or_load_are_dropped(manager: CredentialsManager) -> None:
    """The same names are dropped from stored services on load, by name only."""
    store = manager.for_primary_runtime_scope(_ALICE, "code")
    placeholders = {"GH_TOKEN": "mindroom-brokered", "SSH_AUTH_SOCK": "agent.sock", "SOPS_AGE_KEY_CMD": "age"}
    authored = {**_service().authored_model_dump(), "placeholder_env": placeholders}
    store.save_credentials(USER_SERVICES_CREDENTIAL_SERVICE, {"services": {"mine": authored}})

    with capture_logs() as logs:
        loaded = load_user_services(manager, _target())

    assert loaded["mine"].placeholder_env == {"GH_TOKEN": "mindroom-brokered"}
    dropped = [
        entry["variable"] for entry in logs if entry["event"] == "egress_broker_user_service_placeholder_ignored"
    ]
    assert dropped == ["SSH_AUTH_SOCK", "SOPS_AGE_KEY_CMD"]
    assert "agent.sock" not in str(logs)
