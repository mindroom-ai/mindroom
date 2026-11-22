"""Tests for the tailnet SSH link and key store."""

from __future__ import annotations

import json
import re
import traceback
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mindroom.constants import tracking_dir
from mindroom.tailnet_ssh.store import LinkBook, read_links, update_links
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

KEY = "v1:default:shared:mind_alice"


_DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def test_update_links_round_trips_and_writes_under_tracking(tmp_path: Path) -> None:
    """Changes land in the tracking directory's file, and a change that raises stores nothing."""
    runtime_paths = test_runtime_paths(tmp_path)
    assert read_links(runtime_paths) == LinkBook()

    with update_links(runtime_paths) as ledger:
        link = ledger.ensure(KEY)
    link_id = link.id
    path = tracking_dir(runtime_paths) / "tailnet_ssh.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"links": [link.model_dump()]}
    book = read_links(runtime_paths)
    stored = book.by_id(link_id)
    assert stored is not None
    assert stored == link

    def fail() -> None:
        with update_links(runtime_paths) as ledger:
            ledger.links.clear()
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        fail()
    assert read_links(runtime_paths) == book


def test_update_links_writes_only_a_changed_book(tmp_path: Path) -> None:
    """A change that leaves the book as it was writes no file: none before the first link, and no new one after."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = tracking_dir(runtime_paths) / "tailnet_ssh.json"

    with update_links(runtime_paths) as book:
        assert not book.links
    assert not path.exists()

    with update_links(runtime_paths) as book:
        book.ensure(KEY)
    stored = path.stat()

    with update_links(runtime_paths) as book:
        book.ensure(KEY)
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (stored.st_ino, stored.st_mtime_ns)

    with update_links(runtime_paths) as book:
        book.ensure("v1:default:shared:code")
    assert path.stat().st_ino != stored.st_ino
    assert len(read_links(runtime_paths).links) == 2


@pytest.mark.parametrize("update", [False, True])
def test_invalid_file_error_names_fields_but_not_secrets(tmp_path: Path, *, update: bool) -> None:
    """A corrupt file is reported by path and error types, never with its values or input-derived names."""
    runtime_paths = test_runtime_paths(tmp_path)
    record = LinkBook().ensure(KEY).model_dump(mode="json")
    secret = record["host_key"].splitlines()[1]
    del record["hostname"]
    record[f"unknown {secret}"] = True
    path = tracking_dir(runtime_paths) / "tailnet_ssh.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {"links": [record], "host_key": secret}
    path.write_text(json.dumps(stored), encoding="utf-8")

    def read() -> None:
        if update:
            with update_links(runtime_paths):
                pytest.fail("Loaded an invalid link book.")
        else:
            read_links(runtime_paths)

    with pytest.raises(ValueError, match="invalid") as caught:
        read()
    rendered = "".join(traceback.format_exception(caught.value))
    assert str(path) in rendered
    prefix = f"Tailnet SSH file {path} is invalid: 3 validation errors ("
    message = str(caught.value)
    assert message.startswith(prefix)
    assert message.endswith(")")
    assert sorted(message.removeprefix(prefix).removesuffix(")").split(", ")) == [
        "extra_forbidden",
        "extra_forbidden",
        "missing",
    ]
    assert "hostname" not in rendered
    assert "host_key" not in rendered
    assert secret not in rendered
    assert "mind_alice" not in rendered


def test_worker_identity_is_stable_and_has_a_loadable_host_key() -> None:
    """Reusing a worker key keeps the link and key; the persisted record contains no requester/agent pair."""
    book = LinkBook()
    link = book.ensure(KEY)
    assert book.ensure(KEY) is link
    assert re.fullmatch("[0-9a-f]{16}", link.id)
    assert link.id == LinkBook().ensure(KEY).id
    assert link.hostname == "v1-default-shared-mind-alice"
    assert set(link.model_dump()) == {"id", "worker_key", "hostname", "host_key"}
    assert isinstance(serialization.load_ssh_private_key(link.host_key.encode(), password=None), Ed25519PrivateKey)


def test_hostname_collision_and_overlength_use_worker_hash() -> None:
    """DNS normalization can collide without aliasing the two workers or changing the first hostname."""
    book = LinkBook()
    first = book.ensure(KEY)
    second = book.ensure(KEY.replace("_", "-"))
    long = book.ensure(KEY * 10)
    assert len({first.hostname, second.hostname, long.hostname}) == 3
    assert second.hostname.endswith(second.id)
    assert long.hostname.endswith(long.id)
    assert all(_DNS_LABEL.fullmatch(link.hostname) for link in book.links)
    assert book.ensure(KEY).hostname == first.hostname


def test_id_collision_is_refused() -> None:
    """A saved ID can never redirect a link to a different worker key."""
    link = LinkBook().ensure(KEY)
    book = LinkBook(links=[link.model_copy(update={"worker_key": "different"})])
    with pytest.raises(ValueError, match="ID collision"):
        book.ensure(KEY)
