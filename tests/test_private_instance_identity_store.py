"""Strict historical key reconstruction and protected namespace aliases."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest

from mindroom import private_instance_identity_store as store
from mindroom.tool_system.worker_routing import private_instance_scope_root_path

if TYPE_CHECKING:
    from pathlib import Path

_OLD = "v1:default:user:@alice:example.org"
_NEW = "v1:default:user:~@alice:example.org"
_REQUESTER = "@alice:example.org"


def _owner(base: Path, key: str = _NEW, requester: str = _REQUESTER) -> Path:
    store.ensure_private_instance_identity(base, worker_key=key, requester_id=requester)
    return private_instance_scope_root_path(base, key)


@pytest.mark.parametrize(
    ("key", "requester", "expected"),
    [
        (_NEW, _REQUESTER, _OLD),
        (_OLD, _REQUESTER, _OLD),
        (
            "v1:default:user_agent:~@alice:example.org:writer",
            _REQUESTER,
            "v1:default:user_agent:@alice:example.org:writer",
        ),
        ("v1:default:user:alice_bob", "alice/bob", "v1:default:user:alice_bob"),
    ],
)
def test_reconstruct_historical_key(key: str, requester: str, expected: str) -> None:
    """Exact historical and current private shapes reconstruct the same retained name."""
    assert store.historical_private_instance_worker_key(key, requester) == expected


@pytest.mark.parametrize(
    "key",
    [
        "v2:default:user:alice",
        "v1:default:shared:alice",
        "v1:default:user:wrong",
        "v1:default:user_agent:alice",
        "v1:default:user:alice:extra",
    ],
)
def test_reconstruct_historical_key_rejects_mismatched_shape(key: str) -> None:
    """A scope prefix alone cannot hide a different requester or extra key segments."""
    with pytest.raises(store.PrivateInstanceIdentityError):
        store.historical_private_instance_worker_key(key, "alice")


def test_load_alias_requires_existing_namespace_provenance(tmp_path: Path) -> None:
    """Current owner records do not imply that an old path was ever owned."""
    assert store.load_private_instance_legacy_alias(tmp_path, _NEW) is None
    new = _owner(tmp_path)
    assert store.load_private_instance_legacy_alias(tmp_path, _NEW) is None
    old = private_instance_scope_root_path(tmp_path, _OLD)
    old.symlink_to(new.name, target_is_directory=True)
    assert store.load_private_instance_legacy_alias(tmp_path, _NEW) == old
    with pytest.raises(store.PrivateInstanceIdentityError):
        store.load_private_instance_identity(tmp_path, old)


@pytest.mark.parametrize(
    "damage",
    ["absolute", "relative_dot", "chain", "missing", "real", "owner", "canonical_link", "wrong_hash"],
)
def test_load_alias_rejects_invalid_owner_or_link(tmp_path: Path, damage: str) -> None:
    """Aliases must bind one literal sibling name to its real current owner directory."""
    new = _owner(tmp_path)
    old = private_instance_scope_root_path(tmp_path, _OLD)
    if damage == "absolute":
        old.symlink_to(new)
    elif damage == "relative_dot":
        old.symlink_to(f"./{new.name}")
    elif damage == "chain":
        (old.parent / "chain").symlink_to(new.name)
        old.symlink_to("chain")
    elif damage == "real":
        old.mkdir()
    else:
        old.symlink_to(new.name)
        if damage == "missing":
            (new / ".mindroom-private-instance.json").unlink()
        elif damage == "owner":
            record = new / ".mindroom-private-instance.json"
            payload = json.loads(record.read_text())
            payload["requester_id"] = "@other:example.org"
            record.write_text(json.dumps(payload))
        else:
            saved = new.with_name("saved")
            if damage == "canonical_link":
                new.rename(saved)
                new.symlink_to(saved.name)
            else:
                shutil.copytree(new, saved)
                old.unlink()
                old.symlink_to(saved.name)
    with pytest.raises(store.PrivateInstanceIdentityError):
        store.load_private_instance_legacy_alias(tmp_path, _NEW)


def test_historical_collision_never_selects_another_owner(tmp_path: Path) -> None:
    """Lossy historical normalization grants no path or credentials to another current requester."""
    first_requester, second_requester = "alice/bob", "alice_bob"
    old_key = "v1:default:user:alice_bob"
    first_key = store.reconstruct_private_instance_worker_key(old_key, first_requester)
    second_key = "v1:default:user:~alice_bob"
    first = _owner(tmp_path, first_key, first_requester)
    second = _owner(tmp_path, second_key, second_requester)
    old = private_instance_scope_root_path(tmp_path, old_key)
    old.symlink_to(first.name)
    (first / "credentials.bin").write_bytes(b"first owner")
    (second / "credentials.bin").write_bytes(b"second owner")
    assert store.load_private_instance_legacy_alias(tmp_path, first_key) == old
    assert store.load_private_instance_legacy_alias(tmp_path, second_key) is None
    assert store.load_private_instance_identity(tmp_path, second).requester_id == second_requester
    assert (second / "credentials.bin").read_bytes() == b"second owner"
