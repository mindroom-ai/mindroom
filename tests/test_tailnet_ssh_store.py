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
from mindroom.tailnet_ssh.store import (
    _MAX_KEYS,
    HOSTNAME,
    LinkBook,
    default_hostname,
    finish_removal,
    new_link,
    read_links,
    remove_links,
    set_keys,
    update_links,
)
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

ALICE = "@alice:example.org"
BOB = "@bob:example.org"
ALICE_KEY = "v1:default:user_agent:~@alice:example.org:mind_alice"


def _public_key(comment: str = "") -> str:
    line = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    return f"{line} {comment}".strip()


def test_set_keys_splits_pasted_lines_dedupes_and_caps_at_twenty() -> None:
    """One pasted entry may hold several lines; repeats and blank lines are dropped, and a 21st key is refused."""
    book = LinkBook()
    first, second = _public_key("bas@laptop"), _public_key()
    set_keys(book, ALICE, [f"{first}\n\n  {second}  \r\n", first], now=1.0)
    assert book.keys == {ALICE: [first, second]}

    with pytest.raises(ValueError, match="20"):
        set_keys(book, ALICE, [_public_key() for _ in range(_MAX_KEYS + 1)], now=2.0)
    assert book.keys == {ALICE: [first, second]}

    with pytest.raises(ValueError, match="private key"):
        set_keys(book, ALICE, ["-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"], now=3.0)


def test_removing_a_key_sets_closed_before_adding_does_not() -> None:
    """Only a key that is gone closes that person's sessions; other people's links keep their value."""
    first, second = _public_key(), _public_key()
    alice = new_link(ALICE, "mind_alice", ALICE_KEY, "mind-alice", now=1.0)
    bob = new_link(BOB, "mind_bob", "v1:default:user_agent:~@bob:example.org:mind_bob", "mind-bob", now=1.0)
    book = LinkBook(links=[alice, bob], keys={ALICE: [first]})

    set_keys(book, ALICE, [first, second], now=5.0)
    assert alice.closed_before == 1.0

    set_keys(book, ALICE, [first, f"{second} renamed"], now=6.0)
    assert book.keys == {ALICE: [first, f"{second} renamed"]}
    assert alice.closed_before == 1.0

    set_keys(book, ALICE, [f"{second} renamed"], now=7.0)
    assert book.keys == {ALICE: [f"{second} renamed"]}
    assert alice.closed_before == 7.0
    assert bob.closed_before == 1.0

    set_keys(book, ALICE, [], now=9.0)
    assert ALICE not in book.keys
    assert alice.closed_before == 9.0


def test_new_link_has_16_hex_id_and_loadable_host_key() -> None:
    """A link starts on with its own Ed25519 host key, whose public half is the stored one."""
    link = new_link(ALICE, "mind_alice", ALICE_KEY, "mind-alice", now=10.0)
    assert re.fullmatch("[0-9a-f]{16}", link.id)
    assert link.desired == "on"
    assert link.login_after == 10.0
    assert link.created_at == 10.0
    assert link.enrolled_login is None
    assert link.enrolled_at is None
    assert link.host_public_key.startswith("ssh-ed25519 ")
    private_key = serialization.load_ssh_private_key(link.host_key.encode(), password=None)
    assert isinstance(private_key, Ed25519PrivateKey)
    public_line = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
    )
    assert public_line == link.host_public_key
    assert new_link(ALICE, "mind_alice", ALICE_KEY, "mind-alice", now=10.0).id != link.id


def test_default_hostname() -> None:
    """The default is the agent name in DNS spelling, at most one label long."""
    assert default_hostname("mind_bas") == "mind-bas"
    assert default_hostname("A__b_") == "a-b"
    long_name = default_hostname("a" * 80)
    assert len(long_name) == 63
    assert HOSTNAME.fullmatch(long_name)
    cut_at_dash = default_hostname("a" * 62 + "_" + "b" * 17)
    assert cut_at_dash == "a" * 62
    assert HOSTNAME.fullmatch(cut_at_dash)


def test_remove_links_then_finish_drops_or_parks() -> None:
    """Removal marks links removing and closes sessions; finishing drops them, or parks them off when asked."""
    dropped = new_link(ALICE, "mind_alice", ALICE_KEY, "mind-alice", now=1.0)
    parked = new_link(ALICE, "mind_other", ALICE_KEY, "mind-other", now=1.0)
    kept = new_link(BOB, "mind_bob", "v1:default:user_agent:~@bob:example.org:mind_bob", "mind-bob", now=1.0)
    parked.enrolled_login = "bas@github"
    parked.enrolled_at = 2.0
    book = LinkBook(links=[dropped, parked, kept])

    assert remove_links(book, lambda link: link.id == dropped.id, now=5.0) == [dropped]
    assert remove_links(book, lambda link: link.id == parked.id, now=6.0, park=True) == [parked]
    assert (dropped.desired, dropped.closed_before, dropped.park_when_removed) == ("removing", 5.0, False)
    assert (parked.desired, parked.closed_before, parked.park_when_removed) == ("removing", 6.0, True)
    assert remove_links(book, lambda link: link.requester_id == ALICE, now=8.0) == []
    assert dropped.closed_before == 5.0
    assert kept.desired == "on"

    finish_removal(book, dropped.id)
    finish_removal(book, parked.id)
    finish_removal(book, kept.id)
    assert book.by_id(dropped.id) is None
    assert parked.desired == "off"
    assert parked.enrolled_login is None
    assert parked.enrolled_at is None
    assert book.links == [parked, kept]
    assert kept.desired == "on"


def test_update_links_round_trips_and_writes_under_tracking(tmp_path: Path) -> None:
    """Changes land in the tracking directory's file, and a change that raises stores nothing."""
    runtime_paths = test_runtime_paths(tmp_path)
    assert read_links(runtime_paths) == LinkBook()
    key = _public_key()

    def add(book: LinkBook) -> str:
        link = new_link(ALICE, "mind_alice", ALICE_KEY, "mind-alice", now=1.0)
        book.links.append(link)
        set_keys(book, ALICE, [key], now=1.0)
        return link.id

    link_id = update_links(runtime_paths, add)
    path = tracking_dir(runtime_paths) / "tailnet_ssh.json"
    assert json.loads(path.read_text(encoding="utf-8"))["keys"] == {ALICE: [key]}
    book = read_links(runtime_paths)
    stored = book.by_id(link_id)
    assert stored is not None
    assert stored.hostname == "mind-alice"
    assert book.keys == {ALICE: [key]}

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

    link = new_link(ALICE, "mind_alice", ALICE_KEY, "mind-alice", now=1.0)
    update_links(runtime_paths, lambda book: book.links.append(link))
    stored = path.stat()

    assert update_links(runtime_paths, lambda book: book.by_id(link.id)) == link
    update_links(runtime_paths, lambda book: remove_links(book, lambda _link: False, now=2.0))
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == (stored.st_ino, stored.st_mtime_ns)

    update_links(runtime_paths, lambda book: remove_links(book, lambda _link: True, now=2.0))
    assert path.stat().st_ino != stored.st_ino
    assert read_links(runtime_paths).links[0].desired == "removing"


def test_invalid_file_error_names_fields_but_not_secrets(tmp_path: Path) -> None:
    """A corrupt file is reported by path and error types, never with its values or input-derived names."""
    runtime_paths = test_runtime_paths(tmp_path)
    record = new_link(ALICE, "mind_alice", ALICE_KEY, "mind-alice", now=1.0).model_dump(mode="json")
    secret = record["host_key"].splitlines()[1]
    del record["hostname"]
    record["desired"] = "sideways"
    record[f"unknown {secret}"] = True
    path = tracking_dir(runtime_paths) / "tailnet_ssh.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    stored = {"links": [record], "keys": {f"key {secret}": "not a list", "hostname": "not a list"}, "host_key": secret}
    path.write_text(json.dumps(stored), encoding="utf-8")

    for call in (lambda: read_links(runtime_paths), lambda: update_links(runtime_paths, lambda _book: None)):
        with pytest.raises(ValueError, match="invalid") as caught:
            call()
        rendered = "".join(traceback.format_exception(caught.value))
        assert str(path) in rendered
        prefix = f"Tailnet SSH file {path} is invalid: 6 validation errors ("
        message = str(caught.value)
        assert message.startswith(prefix)
        assert message.endswith(")")
        assert sorted(message.removeprefix(prefix).removesuffix(")").split(", ")) == [
            "extra_forbidden",
            "extra_forbidden",
            "list_type",
            "list_type",
            "literal_error",
            "missing",
        ]
        assert "hostname" not in rendered
        assert "host_key" not in rendered
        assert secret not in rendered
        assert "sideways" not in rendered
        assert "mind-alice" not in rendered


def test_one_link_per_requester_and_agent() -> None:
    """A link is found by its person and agent, and by its id."""
    alice = new_link(ALICE, "mind_shared", ALICE_KEY, "mind-shared", now=1.0)
    bob = new_link(BOB, "mind_bob", "v1:default:user_agent:~@bob:example.org:mind_bob", "mind-bob", now=1.0)
    book = LinkBook(links=[alice, bob])
    assert book.link(ALICE, "mind_shared") is alice
    assert book.link(BOB, "mind_shared") is None
    assert book.link(ALICE, "mind_bob") is None
    assert book.by_id(bob.id) is bob
    assert book.by_id("0" * 16) is None
