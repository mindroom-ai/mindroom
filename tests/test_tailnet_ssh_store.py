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
from mindroom.tailnet_ssh.store import LinkBook, default_hostname, new_link, read_links, update_links
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ALICE = "@alice:example.org"
BOB = "@bob:example.org"


_DNS_LABEL = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")


def test_new_link_has_16_hex_id_and_loadable_host_key() -> None:
    """Each claim has exactly the five identity fields and a fresh ID and host key."""
    link = new_link(ALICE, "mind_alice")
    assert set(link.model_dump()) == {"id", "requester_id", "agent_name", "hostname", "host_key"}
    assert re.fullmatch("[0-9a-f]{16}", link.id)
    key = serialization.load_ssh_private_key(link.host_key.encode(), password=None)
    assert isinstance(key, Ed25519PrivateKey)
    restored = new_link(ALICE, "mind_alice")
    assert (restored.hostname, restored.id != link.id, restored.host_key != link.host_key) == (link.hostname, True, True)


def test_default_hostname() -> None:
    """The default is the agent name in DNS spelling, at most one label long."""
    assert default_hostname("mind_bas") == "mind-bas"
    assert default_hostname("A__b_") == "a-b"
    long_name = default_hostname("a" * 80)
    assert len(long_name) == 63
    assert _DNS_LABEL.fullmatch(long_name)
    cut_at_dash = default_hostname("a" * 62 + "_" + "b" * 17)
    assert cut_at_dash == "a" * 62
    assert _DNS_LABEL.fullmatch(cut_at_dash)


@pytest.mark.parametrize("agent", ["research", "Res_Earch", "a" * 62 + "_b" * 10, "a" * 90])
def test_hostname_collision_uses_localpart_then_stable_hash(agent: str) -> None:
    """A normalized localpart shared across domains falls back to a stable hash that can extend."""
    base = new_link(BOB, agent).hostname
    short = new_link(ALICE, agent, {base}).hostname
    assert short.endswith("-alice")
    reserved = {base, short}
    other = "@alice:other.org"
    hashed = new_link(other, agent, reserved).hostname
    assert new_link(other, agent, reserved).hostname == hashed
    extended = new_link(other, agent, reserved | {hashed}).hostname
    distinct = reserved | {hashed, extended}
    assert len(distinct) == 4
    assert all(_DNS_LABEL.fullmatch(hostname) for hostname in distinct)


def test_normalized_and_truncated_agent_names_reserve_the_same_base() -> None:
    """Normalization and truncation collisions cannot reassign an existing name."""
    for first, second in [("Foo_Bar", "foo-bar"), ("a" * 63 + "b", "a" * 63 + "c")]:
        reserved = new_link(BOB, first).hostname
        assert default_hostname(second) == reserved
        assert new_link(ALICE, second, {reserved}).hostname.endswith("-alice")
    assert default_hostname("___") == "worker"


def test_update_links_round_trips_and_writes_under_tracking(tmp_path: Path) -> None:
    """Changes land in the tracking directory's file, and a change that raises stores nothing."""
    runtime_paths = test_runtime_paths(tmp_path)
    assert read_links(runtime_paths) == LinkBook()

    def add(book: LinkBook) -> str:
        link = new_link(ALICE, "mind_alice")
        book.links.append(link)
        book.retiring_ids.add(link.id)
        return link.id

    link_id = update_links(runtime_paths, add)
    path = tracking_dir(runtime_paths) / "tailnet_ssh.json"
    assert json.loads(path.read_text(encoding="utf-8"))["retiring_ids"] == [link_id]
    book = read_links(runtime_paths)
    stored = book.by_id(link_id)
    assert stored is not None
    assert stored.hostname == "mind-alice"
    assert book.retiring_ids == {link_id}

    def fail(book: LinkBook) -> None:
        book.links.clear()
        msg = "boom"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        update_links(runtime_paths, fail)
    assert read_links(runtime_paths) == book


def test_update_links_writes_only_a_changed_book(tmp_path: Path) -> None:
    """A change that leaves the book as it was writes no file: none before the first link, and no new one after."""
    runtime_paths = test_runtime_paths(tmp_path)
    path = tracking_dir(runtime_paths) / "tailnet_ssh.json"

    assert update_links(runtime_paths, lambda book: len(book.links)) == 0
    assert not path.exists()

    link = new_link(ALICE, "mind_alice")
    update_links(runtime_paths, lambda book: book.links.append(link))
    stored = path.stat()

    assert update_links(runtime_paths, lambda book: book.by_id(link.id)) == link
    update_links(runtime_paths, lambda book: book.retiring_ids.discard(link.id))
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (stored.st_ino, stored.st_mtime_ns)

    update_links(runtime_paths, lambda book: book.retiring_ids.add(link.id))
    assert path.stat().st_ino != stored.st_ino
    assert read_links(runtime_paths).retiring_ids == {link.id}


def test_invalid_file_error_names_fields_but_not_secrets(tmp_path: Path) -> None:
    """A corrupt file is reported by path and error types, never with its values or input-derived names."""
    runtime_paths = test_runtime_paths(tmp_path)
    record = new_link(ALICE, "mind_alice").model_dump(mode="json")
    secret = record["host_key"].splitlines()[1]
    del record["hostname"]
    record[f"unknown {secret}"] = True
    path = tracking_dir(runtime_paths) / "tailnet_ssh.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {"links": [record], "retiring_ids": [True], "host_key": secret}
    path.write_text(json.dumps(stored), encoding="utf-8")

    for call in (lambda: read_links(runtime_paths), lambda: update_links(runtime_paths, lambda _book: None)):
        with pytest.raises(ValueError, match="invalid") as caught:
            call()
        rendered = "".join(traceback.format_exception(caught.value))
        assert str(path) in rendered
        prefix = f"Tailnet SSH file {path} is invalid: 4 validation errors ("
        message = str(caught.value)
        assert message.startswith(prefix)
        assert message.endswith(")")
        assert sorted(message.removeprefix(prefix).removesuffix(")").split(", ")) == [
            "extra_forbidden",
            "extra_forbidden",
            "missing",
            "string_type",
        ]
        assert "hostname" not in rendered
        assert "host_key" not in rendered
        assert secret not in rendered
        assert "mind-alice" not in rendered


def test_one_link_per_requester_and_agent() -> None:
    """A link is found by its person and agent, and by its id."""
    alice = new_link(ALICE, "mind_shared")
    bob = new_link(BOB, "mind_bob")
    book = LinkBook(links=[alice, bob])
    assert book.link(ALICE, "mind_shared") is alice
    assert book.link(BOB, "mind_shared") is None
    assert book.link(ALICE, "mind_bob") is None
    assert book.by_id(bob.id) is bob
    assert book.by_id("0" * 16) is None
