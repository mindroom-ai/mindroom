"""Characterization tables of where scoped credentials are read from and written to.

Each row pins current behavior, so a refactor of the placement decisions can show that it moves no credential.
Rows marked `Changed:` differ from the placement before the single placement resolver, as that change intended.
Store labels:
- `base`: the manager the calls receive, which is also its shared layer.
- `agent`: the primary-only store of agent `alpha`.
- `requester`: the primary-only store of the requester across agents.
- `requester_agent`: the primary-only store of the requester with agent `alpha`.
- `worker`: the target worker's own store, which worker code can write.
- `mirror`: a dedicated worker runtime's separate shared layer, the directory `MINDROOM_SHARED_CREDENTIALS_PATH` names.
A read cell lists the stores whose planted document reached the merged result, and a write cell names the one store
that holds the saved document.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import pytest

import mindroom.constants as constants_mod
from mindroom.api import credentials_target
from mindroom.api.credentials import _DashboardCredentialAccess
from mindroom.api.credentials_oauth_policy import OAuthCredentialServices
from mindroom.api.credentials_target import (
    RequestCredentialsTarget,
    delete_credentials_for_target,
    load_credentials_for_target,
    save_credentials_for_target,
)
from mindroom.credentials.manager import (
    CredentialsManager,
    delete_scoped_credentials,
    load_scoped_credentials,
    save_scoped_credentials,
)
from mindroom.desktop.credentials import delete_desktop_credentials, load_desktop_credentials, save_desktop_credentials
from mindroom.egress_broker.secrets import delete_egress_document, load_egress_document, save_egress_document
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, resolve_worker_target

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

AGENT = "alpha"
REQUESTER = "@alice:example.test"
IDENTITY = ToolExecutionIdentity("matrix", AGENT, REQUESTER, None, None, None, None, tenant_id="test-tenant")
RAISES = "raises ValueError"
PRIMARY_BUILT_TOOL = "postgres"
SERVICES = {
    "regular": "openweather",
    "local_only": "homeassistant",
    "oauth_token": "google_drive_oauth",
    "oauth_client": "google_drive_oauth_client",
    "egress": "egress_github",
    "ungrantable": "google_vertex_adc",
    "primary_built": PRIMARY_BUILT_TOOL,
    "desktop": "desktop",
}

type Read = tuple[str, ...] | str
# (read without a shared grant, read with the service in `allowed_shared_services`, write store)
type Placement = tuple[Read, Read, str]

# Key: (worker scope, requester identity present, primary_built_tool).
SCOPED_PLACEMENT: dict[str, dict[tuple[str | None, bool, bool], Placement]] = {
    # Worker store over the granted base layer; a primary-built tool moves the scoped layer to the agent or requester.
    # Without a requester a user scope reads only a granted base, and a save is refused.
    "regular": {
        (None, False, False): (("base",), ("base",), "base"),
        (None, False, True): (("base",), ("base",), "base"),
        ("shared", False, False): (("worker",), ("base", "worker"), "worker"),
        ("shared", False, True): (("agent",), ("agent", "base"), "agent"),
        ("shared", True, False): (("worker",), ("base", "worker"), "worker"),
        ("shared", True, True): (("agent",), ("agent", "base"), "agent"),
        # Changed: a user scope without a requester has no worker store, so a save is refused instead of landing in
        # base.
        ("user", False, False): ((), ("base",), RAISES),
        ("user", False, True): ((), ("base",), RAISES),
        ("user", True, False): (("worker",), ("base", "worker"), "worker"),
        ("user", True, True): (("requester",), ("base", "requester"), "requester"),
        # Changed: as for `user`.
        ("user_agent", False, False): ((), ("base",), RAISES),
        ("user_agent", False, True): ((), ("base",), RAISES),
        ("user_agent", True, False): (("worker",), ("base", "worker"), "worker"),
        ("user_agent", True, True): (("requester_agent",), ("base", "requester_agent"), "requester_agent"),
    },
    # Shared scope uses base, user scopes the requester stores; no requester raises, and grants never matter.
    "local_only": {
        (None, False, False): (("base",), ("base",), "base"),
        (None, False, True): (("base",), ("base",), "base"),
        ("shared", False, False): (("base",), ("base",), "base"),
        ("shared", False, True): (("base",), ("base",), "base"),
        ("shared", True, False): (("base",), ("base",), "base"),
        ("shared", True, True): (("base",), ("base",), "base"),
        ("user", False, False): (RAISES, RAISES, RAISES),
        ("user", False, True): (RAISES, RAISES, RAISES),
        ("user", True, False): (("requester",), ("requester",), "requester"),
        ("user", True, True): (("requester",), ("requester",), "requester"),
        ("user_agent", False, False): (RAISES, RAISES, RAISES),
        ("user_agent", False, True): (RAISES, RAISES, RAISES),
        ("user_agent", True, False): (("requester_agent",), ("requester_agent",), "requester_agent"),
        ("user_agent", True, True): (("requester_agent",), ("requester_agent",), "requester_agent"),
    },
    # Like local_only, except that shared scope binds the token to the agent store.
    "oauth_token": {
        (None, False, False): (("base",), ("base",), "base"),
        (None, False, True): (("base",), ("base",), "base"),
        ("shared", False, False): (("agent",), ("agent",), "agent"),
        ("shared", False, True): (("agent",), ("agent",), "agent"),
        ("shared", True, False): (("agent",), ("agent",), "agent"),
        ("shared", True, True): (("agent",), ("agent",), "agent"),
        ("user", False, False): (RAISES, RAISES, RAISES),
        ("user", False, True): (RAISES, RAISES, RAISES),
        ("user", True, False): (("requester",), ("requester",), "requester"),
        ("user", True, True): (("requester",), ("requester",), "requester"),
        ("user_agent", False, False): (RAISES, RAISES, RAISES),
        ("user_agent", False, True): (RAISES, RAISES, RAISES),
        ("user_agent", True, False): (("requester_agent",), ("requester_agent",), "requester_agent"),
        ("user_agent", True, True): (("requester_agent",), ("requester_agent",), "requester_agent"),
    },
    # Changed: an OAuth app client config is global, so every scope reads and writes base, never the worker store.
    "oauth_client": {
        (None, False, False): (("base",), ("base",), "base"),
        (None, False, True): (("base",), ("base",), "base"),
        ("shared", False, False): (("base",), ("base",), "base"),
        ("shared", False, True): (("base",), ("base",), "base"),
        ("shared", True, False): (("base",), ("base",), "base"),
        ("shared", True, True): (("base",), ("base",), "base"),
        ("user", False, False): (("base",), ("base",), "base"),
        ("user", False, True): (("base",), ("base",), "base"),
        ("user", True, False): (("base",), ("base",), "base"),
        ("user", True, True): (("base",), ("base",), "base"),
        ("user_agent", False, False): (("base",), ("base",), "base"),
        ("user_agent", False, True): (("base",), ("base",), "base"),
        ("user_agent", True, False): (("base",), ("base",), "base"),
        ("user_agent", True, True): (("base",), ("base",), "base"),
    },
    # Always placed as a primary-built tool that can never be granted.
    "egress": {
        (None, False, False): (("base",), ("base",), "base"),
        (None, False, True): (("base",), ("base",), "base"),
        ("shared", False, False): (("agent",), ("agent",), "agent"),
        ("shared", False, True): (("agent",), ("agent",), "agent"),
        ("shared", True, False): (("agent",), ("agent",), "agent"),
        ("shared", True, True): (("agent",), ("agent",), "agent"),
        ("user", False, False): ((), (), RAISES),
        ("user", False, True): ((), (), RAISES),
        ("user", True, False): (("requester",), ("requester",), "requester"),
        ("user", True, True): (("requester",), ("requester",), "requester"),
        ("user_agent", False, False): ((), (), RAISES),
        ("user_agent", False, True): ((), (), RAISES),
        ("user_agent", True, False): (("requester_agent",), ("requester_agent",), "requester_agent"),
        ("user_agent", True, True): (("requester_agent",), ("requester_agent",), "requester_agent"),
    },
    # Like regular, except that a grant never adds the base layer.
    "ungrantable": {
        (None, False, False): (("base",), ("base",), "base"),
        (None, False, True): (("base",), ("base",), "base"),
        ("shared", False, False): (("worker",), ("worker",), "worker"),
        ("shared", False, True): (("agent",), ("agent",), "agent"),
        ("shared", True, False): (("worker",), ("worker",), "worker"),
        ("shared", True, True): (("agent",), ("agent",), "agent"),
        # Changed: a user scope without a requester has no worker store, so a save is refused instead of landing in
        # base.
        ("user", False, False): ((), (), RAISES),
        ("user", False, True): ((), (), RAISES),
        ("user", True, False): (("worker",), ("worker",), "worker"),
        ("user", True, True): (("requester",), ("requester",), "requester"),
        # Changed: as for `user`.
        ("user_agent", False, False): ((), (), RAISES),
        ("user_agent", False, True): ((), (), RAISES),
        ("user_agent", True, False): (("worker",), ("worker",), "worker"),
        ("user_agent", True, True): (("requester_agent",), ("requester_agent",), "requester_agent"),
    },
    # Changed: a Desktop pairing lives in the requester's store with the agent in every scope, as the Desktop
    # helpers place it; a target without a requester reads nothing and refuses a save instead of using base or the
    # requester-wide store.
    "desktop": {
        (None, False, False): ((), (), RAISES),
        (None, False, True): ((), (), RAISES),
        ("shared", False, False): ((), (), RAISES),
        ("shared", False, True): ((), (), RAISES),
        ("shared", True, False): (("requester_agent",), ("requester_agent",), "requester_agent"),
        ("shared", True, True): (("requester_agent",), ("requester_agent",), "requester_agent"),
        ("user", False, False): ((), (), RAISES),
        ("user", False, True): ((), (), RAISES),
        ("user", True, False): (("requester_agent",), ("requester_agent",), "requester_agent"),
        ("user", True, True): (("requester_agent",), ("requester_agent",), "requester_agent"),
        ("user_agent", False, False): ((), (), RAISES),
        ("user_agent", False, True): ((), (), RAISES),
        ("user_agent", True, False): (("requester_agent",), ("requester_agent",), "requester_agent"),
        ("user_agent", True, True): (("requester_agent",), ("requester_agent",), "requester_agent"),
    },
}

# Dashboard targets always carry agent `alpha`, and a scoped target carries IDENTITY and its worker store.
# The primary owns the settings of PRIMARY_BUILT_TOOL only.
DASHBOARD_PLACEMENT: dict[str, dict[str | None, Placement]] = {
    "regular": {
        None: (("base",), ("base",), "base"),
        "shared": (("worker",), ("base", "worker"), "worker"),
        "user": (("worker",), ("base", "worker"), "worker"),
        "user_agent": (("worker",), ("base", "worker"), "worker"),
    },
    "local_only": {
        None: (("base",), ("base",), "base"),
        "shared": (("base",), ("base",), "base"),
        "user": (("requester",), ("requester",), "requester"),
        "user_agent": (("requester_agent",), ("requester_agent",), "requester_agent"),
    },
    # OAuth app client configs are global: every scope uses base.
    "oauth_client": {
        None: (("base",), ("base",), "base"),
        "shared": (("base",), ("base",), "base"),
        "user": (("base",), ("base",), "base"),
        "user_agent": (("base",), ("base",), "base"),
    },
    "primary_built": {
        None: (("base",), ("base",), "base"),
        "shared": (("agent",), ("agent", "base"), "agent"),
        "user": (("requester",), ("base", "requester"), "requester"),
        "user_agent": (("requester_agent",), ("base", "requester_agent"), "requester_agent"),
    },
}

# Egress documents for a target with IDENTITY: (read, write store).
EGRESS_PLACEMENT: dict[str | None, tuple[Read, str]] = {
    None: (("base",), "base"),
    "shared": (("agent",), "agent"),
    "user": (("requester",), "requester"),
    "user_agent": (("requester_agent",), "requester_agent"),
}

# The store whose `api_key` wins when every store holds one, for each granted read above that merges two stores.
# Key: (API, worker scope, requester identity present, primary_built_tool); dashboard targets carry IDENTITY.
MERGED_READ_WINNER: dict[tuple[str, str, bool, bool], str] = {
    # SCOPED_PLACEMENT regular rows: the scoped layer overrides the granted base.
    ("scoped", "shared", False, False): "worker",
    ("scoped", "shared", False, True): "agent",
    ("scoped", "shared", True, False): "worker",
    ("scoped", "shared", True, True): "agent",
    ("scoped", "user", True, False): "worker",
    ("scoped", "user", True, True): "requester",
    ("scoped", "user_agent", True, False): "worker",
    ("scoped", "user_agent", True, True): "requester_agent",
    # DASHBOARD_PLACEMENT regular and primary_built rows: the same order.
    ("dashboard", "shared", True, False): "worker",
    ("dashboard", "shared", True, True): "agent",
    ("dashboard", "user", True, False): "worker",
    ("dashboard", "user", True, True): "requester",
    ("dashboard", "user_agent", True, False): "worker",
    ("dashboard", "user_agent", True, True): "requester_agent",
}

# A granted base document of the regular service reaches a `shared` read only when a worker may inherit its
# `_source`; None plants no `_source`. Value: (read without primary_built_tool, read with it).
GRANTED_BASE_BY_SOURCE: dict[str | None, tuple[Read, Read]] = {
    # Changed: copies earlier releases imported carry `_source: "env"`; the primary deletes them when it opens its store,
    # and the environment's own declarations carry no source, so that tag is never passed on.
    "env": (("worker",), ("agent",)),
    "ui": (("base", "worker"), ("agent", "base")),
    None: (("base", "worker"), ("agent", "base")),
    "oauth": (("worker",), ("agent",)),
    "oauth_dynamic_client_registration": (("worker",), ("agent",)),
}

# Reads of the regular service through a dedicated worker's runtime manager: `worker` is the manager the calls
# receive, over its separate `mirror`, and `agent`, `requester`, and `requester_agent` resolve under the worker root.
# A scoped target has the manager's own worker key.
# Key: (worker scope, primary_built_tool); value: (read without a shared grant, read with it, store whose `api_key` wins).
WORKER_RUNTIME_READS: dict[tuple[str | None, bool], tuple[Read, Read, str]] = {
    (None, False): (("mirror", "worker"), ("mirror", "worker"), "worker"),
    # The whole mirror is read with no grant and no `_source` check.
    ("shared", False): (("mirror", "worker"), ("mirror", "worker"), "worker"),
    ("user", False): (("mirror", "worker"), ("mirror", "worker"), "worker"),
    ("user_agent", False): (("mirror", "worker"), ("mirror", "worker"), "worker"),
    # Surprising: the primary-only stores of primary-built settings resolve inside the worker root, which worker code
    # can write, and they also merge over the whole mirror.
    ("shared", True): (("agent", "mirror"), ("agent", "mirror"), "agent"),
    ("user", True): (("mirror", "requester"), ("mirror", "requester"), "requester"),
    ("user_agent", True): (("mirror", "requester_agent"), ("mirror", "requester_agent"), "requester_agent"),
}

GRANTED_SERVICE = "tavily"
# One regular, granted, local-only, OAuth client config, egress, and primary-built service each; `homeassistant` is
# also a shared-only integration, and `gmail` is a local-only service that is not.
LISTED_SERVICES = (
    SERVICES["regular"],
    GRANTED_SERVICE,
    SERVICES["local_only"],
    "gmail",
    SERVICES["oauth_client"],
    SERVICES["egress"],
    PRIMARY_BUILT_TOOL,
    SERVICES["desktop"],
)
# Services the dashboard lists for a target when every LISTED_SERVICES document sits in one store; a store not named
# lists nothing. The target grants GRANTED_SERVICE only.
DASHBOARD_LISTING: dict[str | None, dict[str, tuple[str, ...]]] = {
    # Everything in the primary store except egress services.
    None: {"base": ("gmail", "google_drive_oauth_client", "homeassistant", "openweather", "postgres", "tavily")},
    # Changed: a worker-store copy of a local-only service or of the OAuth client config, which reads ignore, is no
    # longer listed.
    "shared": {
        "agent": ("postgres",),
        "base": ("gmail", "google_drive_oauth_client", "homeassistant", "tavily"),
        "requester_agent": ("desktop",),
        "worker": ("openweather", "tavily"),
    },
    # The requester scopes drop `homeassistant` from every store.
    # Changed: as for `shared`.
    "user": {
        "base": ("google_drive_oauth_client", "tavily"),
        "requester": ("gmail", "postgres"),
        "requester_agent": ("desktop",),
        "worker": ("openweather", "tavily"),
    },
    # Changed: as for `shared`.
    "user_agent": {
        "base": ("google_drive_oauth_client", "tavily"),
        "requester_agent": ("desktop", "gmail", "postgres"),
        "worker": ("openweather", "tavily"),
    },
}


def _target(scope: str | None, identity: ToolExecutionIdentity | None) -> ResolvedWorkerTarget | None:
    return None if scope is None else resolve_worker_target(scope, AGENT, identity, tenant_id="test-tenant")


def _primary_scoped_stores(manager: CredentialsManager) -> dict[str, CredentialsManager]:
    return {
        "agent": manager.for_primary_runtime_agent_scope(AGENT),
        "requester": manager.for_primary_runtime_scope(REQUESTER, None),
        "requester_agent": manager.for_primary_runtime_scope(REQUESTER, AGENT),
    }


def _stores(root: Path, target: ResolvedWorkerTarget | None) -> dict[str, CredentialsManager]:
    """Return a fresh labelled store tree; `base` is the manager the calls receive."""
    manager = CredentialsManager(root / "credentials")
    stores = {"base": manager, **_primary_scoped_stores(manager)}
    if target is not None and target.worker_key is not None:
        stores["worker"] = manager.for_worker(target.worker_key)
    return stores


def _dashboard_target(
    stores: dict[str, CredentialsManager],
    scope: str | None,
    identity: ToolExecutionIdentity | None,
    allowed: frozenset[str] = frozenset(),
) -> RequestCredentialsTarget:
    """Mirror `resolve_request_credentials_target`: an unscoped target uses the primary store directly."""
    manager = stores["base"]
    return RequestCredentialsTarget(
        runtime_paths=constants_mod.resolve_primary_runtime_paths(
            config_path=manager.storage_root / "config.yaml",
            storage_path=manager.storage_root,
        ),
        base_manager=manager,
        worker_scope=scope,
        agent_name=AGENT,
        execution_identity=identity,
        allowed_shared_services=None if scope is None else allowed,
    )


def _primary_owns_only_primary_built_tool_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    # Like the real check, an unscoped target never has primary-owned scoped settings.
    monkeypatch.setattr(
        credentials_target,
        "_target_primary_owns_tool_settings",
        lambda tool, target: target.worker_scope is not None and tool == PRIMARY_BUILT_TOOL,
    )


def _plant(stores: dict[str, CredentialsManager], service: str, source: str | None = "ui") -> None:
    """Save one document per store, naming its store in its own field and in the `api_key` field all stores share.

    A `source` of None plants no `_source`.
    """
    for label, store in stores.items():
        document: dict[str, Any] = {f"from_{label}": True, "api_key": label}
        if source is not None:
            document["_source"] = source
        store.save_credentials(service, document)


def _read(
    stores: dict[str, CredentialsManager],
    service: str,
    load: Callable[[], dict[str, Any] | None],
    source: str | None = "ui",
) -> Read:
    """Plant one document per store and return the labels whose document `load` returns."""
    _plant(stores, service, source)
    try:
        result = load() or {}
    except ValueError:
        return RAISES
    return tuple(sorted(label for label in stores if f"from_{label}" in result))


def _write_then_delete(
    stores: dict[str, CredentialsManager],
    service: str,
    save: Callable[[], None],
    delete: Callable[[], None],
) -> str:
    """Return the label of the one store `save` wrote, after checking that `delete` removes it again."""
    root = stores["base"].storage_root
    file_name = stores["base"].get_credentials_path(service).name
    try:
        save()
    except ValueError:
        assert not list(root.rglob(file_name))
        with pytest.raises(ValueError, match="requester"):
            delete()
        return RAISES
    label_by_dir = {store.base_path.resolve(): label for label, store in stores.items()}
    written = [label_by_dir.get(path.parent.resolve(), str(path)) for path in root.rglob(file_name)]
    assert len(written) == 1, written
    delete()
    assert not list(root.rglob(file_name))
    return written[0]


def _scoped_cases() -> Iterator[Any]:
    for service_class, rows in SCOPED_PLACEMENT.items():
        for (scope, identity, primary_built), expected in rows.items():
            case_id = f"{service_class}-{scope}-{'requester' if identity else 'anonymous'}"
            if primary_built:
                case_id += "-primary_built"
            yield pytest.param(SERVICES[service_class], scope, identity, primary_built, expected, id=case_id)


@pytest.mark.parametrize(("service", "scope", "identity", "primary_built", "expected"), list(_scoped_cases()))
def test_scoped_credential_placement(
    tmp_path: Path,
    service: str,
    scope: str | None,
    identity: bool,
    primary_built: bool,
    expected: Placement,
) -> None:
    """`load/save/delete_scoped_credentials` read and write the stores the table lists."""
    target = _target(scope, IDENTITY if identity else None)
    reads = []
    for allowed in (frozenset(), frozenset({service})):
        stores = _stores(tmp_path / f"read-{len(allowed)}", target)
        load = partial(
            load_scoped_credentials,
            service,
            credentials_manager=stores["base"],
            worker_target=target,
            allowed_shared_services=allowed,
            primary_built_tool=primary_built,
        )
        reads.append(_read(stores, service, load))
    stores = _stores(tmp_path / "write", target)
    scope_args = {"credentials_manager": stores["base"], "worker_target": target, "primary_built_tool": primary_built}
    save = partial(save_scoped_credentials, service, {"api_key": "k"}, **scope_args)
    write = _write_then_delete(stores, service, save, partial(delete_scoped_credentials, service, **scope_args))
    assert (*reads, write) == expected


def _dashboard_cases() -> Iterator[Any]:
    for service_class, rows in DASHBOARD_PLACEMENT.items():
        for scope, expected in rows.items():
            yield pytest.param(SERVICES[service_class], scope, expected, id=f"{service_class}-{scope}")


@pytest.mark.parametrize(("service", "scope", "expected"), list(_dashboard_cases()))
def test_dashboard_credential_placement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service: str,
    scope: str | None,
    expected: Placement,
) -> None:
    """Dashboard target functions read and write the stores the table lists."""
    _primary_owns_only_primary_built_tool_settings(monkeypatch)
    worker_target = _target(scope, IDENTITY)
    identity = None if scope is None else IDENTITY
    reads = []
    for allowed in (frozenset(), frozenset({service})):
        stores = _stores(tmp_path / f"read-{len(allowed)}", worker_target)
        target = _dashboard_target(stores, scope, identity, allowed)
        reads.append(_read(stores, service, partial(load_credentials_for_target, service, target)))
    stores = _stores(tmp_path / "write", worker_target)
    write_target = _dashboard_target(stores, scope, identity)
    save = partial(save_credentials_for_target, service, {"api_key": "k"}, write_target)
    write = _write_then_delete(stores, service, save, partial(delete_credentials_for_target, service, write_target))
    assert (*reads, write) == expected


@pytest.mark.parametrize("scope", list(EGRESS_PLACEMENT))
def test_egress_document_placement(tmp_path: Path, scope: str | None) -> None:
    """Egress document helpers read and write the stores the table lists."""
    service = SERVICES["egress"]
    target = _target(scope, IDENTITY)
    stores = _stores(tmp_path / "read", target)
    read = _read(stores, service, partial(load_egress_document, stores["base"], target, service))
    stores = _stores(tmp_path / "write", target)
    save = partial(save_egress_document, stores["base"], target, service, {"api_key": "k"})
    write = _write_then_delete(stores, service, save, partial(delete_egress_document, stores["base"], target, service))
    assert (read, write) == EGRESS_PLACEMENT[scope]


@pytest.mark.parametrize(
    ("api", "scope", "identity", "primary_built", "winner"),
    [(*key, winner) for key, winner in MERGED_READ_WINNER.items()],
)
def test_merged_read_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    api: str,
    scope: str,
    identity: bool,
    primary_built: bool,
    winner: str,
) -> None:
    """A field that both merged stores hold comes from the store the table names."""
    service = PRIMARY_BUILT_TOOL if primary_built else SERVICES["regular"]
    target = _target(scope, IDENTITY if identity else None)
    stores = _stores(tmp_path, target)
    granted = frozenset({service})
    if api == "dashboard":
        _primary_owns_only_primary_built_tool_settings(monkeypatch)
        load = partial(load_credentials_for_target, service, _dashboard_target(stores, scope, IDENTITY, granted))
    else:
        load = partial(
            load_scoped_credentials,
            service,
            credentials_manager=stores["base"],
            worker_target=target,
            allowed_shared_services=granted,
            primary_built_tool=primary_built,
        )
    _plant(stores, service)
    assert (load() or {}).get("api_key") == winner


@pytest.mark.parametrize("source", list(GRANTED_BASE_BY_SOURCE))
def test_granted_base_layer_by_source(tmp_path: Path, source: str | None) -> None:
    """A granted base document joins a scoped read only for the sources the table lists."""
    service = SERVICES["regular"]
    target = _target("shared", IDENTITY)
    reads = []
    for primary_built in (False, True):
        stores = _stores(tmp_path / str(primary_built), target)
        load = partial(
            load_scoped_credentials,
            service,
            credentials_manager=stores["base"],
            worker_target=target,
            allowed_shared_services=frozenset({service}),
            primary_built_tool=primary_built,
        )
        reads.append(_read(stores, service, load, source))
    assert tuple(reads) == GRANTED_BASE_BY_SOURCE[source]


@pytest.mark.parametrize(("scope", "primary_built"), list(WORKER_RUNTIME_READS))
def test_dedicated_worker_runtime_reads(tmp_path: Path, scope: str | None, primary_built: bool) -> None:
    """A dedicated worker's runtime manager reads the stores the table lists."""
    service = SERVICES["regular"]
    target = _target(scope, IDENTITY)
    worker_key = _target(scope or "shared", IDENTITY).worker_key
    reads = []
    for allowed in (frozenset(), frozenset({service})):
        # Mirrors `get_runtime_credentials_manager` under a dedicated worker's runner environment.
        root = tmp_path / f"worker-{len(allowed)}"
        manager = CredentialsManager(
            root / "credentials",
            shared_base_path=root / ".shared_credentials",
            current_worker_key=worker_key,
            current_worker_root=root,
        )
        stores = {"worker": manager, "mirror": manager.shared_manager(), **_primary_scoped_stores(manager)}
        load = partial(
            load_scoped_credentials,
            service,
            credentials_manager=manager,
            worker_target=target,
            allowed_shared_services=allowed,
            primary_built_tool=primary_built,
        )
        reads.append(_read(stores, service, load))
    assert (*reads, (load() or {}).get("api_key")) == WORKER_RUNTIME_READS[scope, primary_built]


def test_desktop_credentials_use_the_requester_agent_store(tmp_path: Path) -> None:
    """The Desktop helpers read and write the requester's store with the agent, never the worker store."""
    service = "desktop"
    target = _target("user_agent", IDENTITY)
    identity = {"requester_id": REQUESTER, "agent_name": AGENT}
    stores = _stores(tmp_path / "read", target)
    read = _read(stores, service, partial(load_desktop_credentials, stores["base"], **identity))
    stores = _stores(tmp_path / "write", target)
    save = partial(save_desktop_credentials, stores["base"], {"device_id": "D"}, **identity)
    write = _write_then_delete(stores, service, save, partial(delete_desktop_credentials, stores["base"], **identity))
    assert (read, write) == (("requester_agent",), "requester_agent")


@pytest.mark.parametrize("scope", list(DASHBOARD_LISTING))
def test_dashboard_listing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str | None) -> None:
    """The dashboard lists the services the table names for each store, and their union for every store."""
    _primary_owns_only_primary_built_tool_settings(monkeypatch)
    stores = _stores(tmp_path, _target(scope, IDENTITY))
    target = _dashboard_target(stores, scope, None if scope is None else IDENTITY, frozenset({GRANTED_SERVICE}))
    access = _DashboardCredentialAccess(target=target, oauth_services=OAuthCredentialServices(providers={}))
    listed = {}
    for label, store in stores.items():
        for service in LISTED_SERVICES:
            store.save_credentials(service, {"api_key": label, "_source": "ui"})
        listed[label] = tuple(access.list_services())
        for service in LISTED_SERVICES:
            store.delete_credentials(service)
    assert {label: services for label, services in listed.items() if services} == DASHBOARD_LISTING[scope]
    for service in LISTED_SERVICES:
        _plant(stores, service)
    assert access.list_services() == sorted(set().union(*DASHBOARD_LISTING[scope].values()))


@pytest.mark.parametrize(
    ("service_class", "expected"),
    [
        *((service_class, (("base",), "base")) for service_class in SERVICES if service_class != "desktop"),
        # Changed: a Desktop pairing needs a requester, and an unscoped dashboard target passes on none, so it reads
        # nothing and refuses a save instead of using base.
        ("desktop", ((), RAISES)),
    ],
)
def test_unscoped_dashboard_target_with_identity_uses_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    service_class: str,
    expected: tuple[Read, str],
) -> None:
    """An unscoped dashboard target reads and writes base even when it carries a requester identity."""
    _primary_owns_only_primary_built_tool_settings(monkeypatch)
    service = SERVICES[service_class]
    stores = _stores(tmp_path / "read", None)
    load = partial(load_credentials_for_target, service, _dashboard_target(stores, None, IDENTITY))
    read = _read(stores, service, load)
    stores = _stores(tmp_path / "write", None)
    target = _dashboard_target(stores, None, IDENTITY)
    save = partial(save_credentials_for_target, service, {"api_key": "k"}, target)
    write = _write_then_delete(stores, service, save, partial(delete_credentials_for_target, service, target))
    assert (read, write) == expected


@pytest.mark.parametrize("scope", ["shared", "user", "user_agent"])
def test_oauth_client_config_never_comes_from_the_worker_store(tmp_path: Path, scope: str) -> None:
    """An OAuth app's client config lives in the primary store for every scope; worker code can write its own store."""
    service = SERVICES["oauth_client"]
    target = _target(scope, IDENTITY)
    stores = _stores(tmp_path, target)
    stores["base"].save_credentials(service, {"client_id": "primary"})
    stores["worker"].save_credentials(service, {"client_id": "planted"})
    scope_args = {"credentials_manager": stores["base"], "worker_target": target}

    assert load_scoped_credentials(service, **scope_args) == {"client_id": "primary"}
    save_scoped_credentials(service, {"client_id": "saved"}, **scope_args)
    assert stores["base"].load_credentials(service) == {"client_id": "saved"}
    assert stores["worker"].load_credentials(service) == {"client_id": "planted"}


@pytest.mark.parametrize("scope", ["user", "user_agent"])
@pytest.mark.parametrize("service_class", ["regular", "ungrantable"])
def test_a_requester_scope_without_a_requester_refuses_writes(tmp_path: Path, scope: str, service_class: str) -> None:
    """With no requester there is no scoped store, so a save is refused instead of landing in the global store."""
    service = SERVICES[service_class]
    stores = _stores(tmp_path, _target(scope, None))

    with pytest.raises(ValueError, match="requester"):
        save_scoped_credentials(
            service,
            {"api_key": "k"},
            credentials_manager=stores["base"],
            worker_target=_target(scope, None),
        )
    assert stores["base"].load_credentials(service) is None


@pytest.mark.parametrize("scope", [None, "shared"])
def test_dashboard_lists_a_saved_credential_it_cannot_decode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str | None,
) -> None:
    """A saved document that does not decode, for example under a changed encryption key, stays listed."""
    _primary_owns_only_primary_built_tool_settings(monkeypatch)
    stores = _stores(tmp_path, _target(scope, IDENTITY))
    store = stores["base" if scope is None else "worker"]
    service = SERVICES["regular"]
    store.get_credentials_path(service).write_bytes(b"not a credential document")
    target = _dashboard_target(stores, scope, None if scope is None else IDENTITY)
    access = _DashboardCredentialAccess(target=target, oauth_services=OAuthCredentialServices(providers={}))

    assert store.load_credentials(service) is None
    assert service in access.list_services()


def test_dashboard_lists_a_granted_shared_credential_only_when_the_grant_passes_it_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A granted service's primary document is listed for a scoped target only when its source may be passed on."""
    _primary_owns_only_primary_built_tool_settings(monkeypatch)
    stores = _stores(tmp_path, _target("shared", IDENTITY))
    target = _dashboard_target(stores, "shared", IDENTITY, frozenset({GRANTED_SERVICE}))
    access = _DashboardCredentialAccess(target=target, oauth_services=OAuthCredentialServices(providers={}))

    stores["base"].save_credentials(GRANTED_SERVICE, {"api_key": "k", "_source": "oauth"})
    assert GRANTED_SERVICE not in access.list_services()
    stores["base"].save_credentials(GRANTED_SERVICE, {"api_key": "k", "_source": "ui"})
    assert GRANTED_SERVICE in access.list_services()
