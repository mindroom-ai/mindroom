"""Characterization tables of where scoped credentials are read from and written to.

Each row pins current behavior, so a refactor of the placement decisions can show that it moves no credential.
Store labels:
- `base`: the manager the calls receive, which is also its shared layer.
- `agent`: the primary-only store of agent `alpha`.
- `requester`: the primary-only store of the requester across agents.
- `requester_agent`: the primary-only store of the requester with agent `alpha`.
- `worker`: the target worker's own store, which worker code can write.
A read cell lists the stores whose planted document reached the merged result, and a write cell names the one store
that holds the saved document.
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING, Any

import pytest

import mindroom.constants as constants_mod
from mindroom.api import credentials_target
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
}

type Read = tuple[str, ...] | str
# (read without a shared grant, read with the service in `allowed_shared_services`, write store)
type Placement = tuple[Read, Read, str]

# Key: (worker scope, requester identity present, primary_built_tool).
SCOPED_PLACEMENT: dict[str, dict[tuple[str | None, bool, bool], Placement]] = {
    # Worker store over the granted base layer; a primary-built tool moves the scoped layer to the agent or requester.
    # Without a requester a user scope reads only a granted base, yet a save writes base.
    "regular": {
        (None, False, False): (("base",), ("base",), "base"),
        (None, False, True): (("base",), ("base",), "base"),
        ("shared", False, False): (("worker",), ("base", "worker"), "worker"),
        ("shared", False, True): (("agent",), ("agent", "base"), "agent"),
        ("shared", True, False): (("worker",), ("base", "worker"), "worker"),
        ("shared", True, True): (("agent",), ("agent", "base"), "agent"),
        ("user", False, False): ((), ("base",), "base"),
        ("user", False, True): ((), ("base",), RAISES),
        ("user", True, False): (("worker",), ("base", "worker"), "worker"),
        ("user", True, True): (("requester",), ("base", "requester"), "requester"),
        ("user_agent", False, False): ((), ("base",), "base"),
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
    # These functions route a global OAuth app client config to the worker store; only the dashboard sends it to base.
    "oauth_client": {
        (None, False, False): (("base",), ("base",), "base"),
        (None, False, True): (("base",), ("base",), "base"),
        ("shared", False, False): (("worker",), ("worker",), "worker"),
        ("shared", False, True): (("worker",), ("worker",), "worker"),
        ("shared", True, False): (("worker",), ("worker",), "worker"),
        ("shared", True, True): (("worker",), ("worker",), "worker"),
        ("user", False, False): ((), (), "base"),
        ("user", False, True): ((), (), "base"),
        ("user", True, False): (("worker",), ("worker",), "worker"),
        ("user", True, True): (("worker",), ("worker",), "worker"),
        ("user_agent", False, False): ((), (), "base"),
        ("user_agent", False, True): ((), (), "base"),
        ("user_agent", True, False): (("worker",), ("worker",), "worker"),
        ("user_agent", True, True): (("worker",), ("worker",), "worker"),
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
        ("user", False, False): ((), (), "base"),
        ("user", False, True): ((), (), RAISES),
        ("user", True, False): (("worker",), ("worker",), "worker"),
        ("user", True, True): (("requester",), ("requester",), "requester"),
        ("user_agent", False, False): ((), (), "base"),
        ("user_agent", False, True): ((), (), RAISES),
        ("user_agent", True, False): (("worker",), ("worker",), "worker"),
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


def _target(scope: str | None, identity: ToolExecutionIdentity | None) -> ResolvedWorkerTarget | None:
    return None if scope is None else resolve_worker_target(scope, AGENT, identity, tenant_id="test-tenant")


def _stores(root: Path, target: ResolvedWorkerTarget | None) -> dict[str, CredentialsManager]:
    """Return a fresh labelled store tree; `base` is the manager the calls receive."""
    manager = CredentialsManager(root / "credentials")
    stores = {
        "base": manager,
        "agent": manager.for_primary_runtime_agent_scope(AGENT),
        "requester": manager.for_primary_runtime_scope(REQUESTER, None),
        "requester_agent": manager.for_primary_runtime_scope(REQUESTER, AGENT),
    }
    if target is not None and target.worker_key is not None:
        stores["worker"] = manager.for_worker(target.worker_key)
    return stores


def _read(stores: dict[str, CredentialsManager], service: str, load: Callable[[], dict[str, Any] | None]) -> Read:
    """Plant one document per store and return the labels whose document `load` returns."""
    for label, store in stores.items():
        store.save_credentials(service, {f"from_{label}": True, "_source": "ui"})
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
        with pytest.raises(ValueError, match="requester identity"):
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
    monkeypatch.setattr(
        credentials_target,
        "target_primary_owns_tool_settings",
        lambda tool, _target: tool == PRIMARY_BUILT_TOOL,
    )
    worker_target = _target(scope, IDENTITY)

    def target(stores: dict[str, CredentialsManager], allowed: frozenset[str]) -> RequestCredentialsTarget:
        # Mirrors `resolve_request_credentials_target`: an unscoped target uses the primary store directly.
        manager = stores["base"]
        return RequestCredentialsTarget(
            runtime_paths=constants_mod.resolve_primary_runtime_paths(
                config_path=manager.storage_root / "config.yaml",
                storage_path=manager.storage_root,
            ),
            base_manager=manager,
            target_manager=stores.get("worker", manager),
            worker_scope=scope,
            agent_name=AGENT,
            execution_identity=None if scope is None else IDENTITY,
            allowed_shared_services=None if scope is None else allowed,
        )

    reads = []
    for allowed in (frozenset(), frozenset({service})):
        stores = _stores(tmp_path / f"read-{len(allowed)}", worker_target)
        reads.append(_read(stores, service, partial(load_credentials_for_target, service, target(stores, allowed))))
    stores = _stores(tmp_path / "write", worker_target)
    write_target = target(stores, frozenset())
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
