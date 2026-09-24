"""Tests for runtime authorship proofs over relayed requester identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom import relay_proof
from mindroom.constants import ORIGINAL_SENDER_KEY, RELAY_PROOF_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import TRUSTED_INTERNAL_RELAY_SOURCE_KIND
from mindroom.relay_proof import relay_metadata_is_runtime_authored, sign_relay_metadata
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def test_signed_metadata_verifies_and_unsigned_metadata_does_not(tmp_path: Path) -> None:
    """Only content this runtime stamped may claim to relay another identity."""
    runtime_paths = test_runtime_paths(tmp_path)
    content = {
        ORIGINAL_SENDER_KEY: "@owner:localhost",
        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    }

    assert not relay_metadata_is_runtime_authored(content, runtime_paths)

    sign_relay_metadata(content, runtime_paths)

    assert relay_metadata_is_runtime_authored(content, runtime_paths)
    assert isinstance(content[RELAY_PROOF_KEY], str)


def test_content_without_an_identity_claim_needs_no_proof(tmp_path: Path) -> None:
    """Ordinary managed-account content claims no identity, so it stays trusted."""
    runtime_paths = test_runtime_paths(tmp_path)

    assert relay_metadata_is_runtime_authored({"body": "hello"}, runtime_paths)


def test_signing_drops_a_stale_proof_when_the_claim_is_removed(tmp_path: Path) -> None:
    """A relay that stops naming a requester must not keep its old proof."""
    runtime_paths = test_runtime_paths(tmp_path)
    content = {ORIGINAL_SENDER_KEY: "@owner:localhost"}
    sign_relay_metadata(content, runtime_paths)

    content.pop(ORIGINAL_SENDER_KEY)
    sign_relay_metadata(content, runtime_paths)

    assert RELAY_PROOF_KEY not in content


def test_a_proof_does_not_carry_to_another_claim(tmp_path: Path) -> None:
    """A proof lifted from one relay cannot authorize a different identity or source kind."""
    runtime_paths = test_runtime_paths(tmp_path)
    content = {
        ORIGINAL_SENDER_KEY: "@owner:localhost",
        SOURCE_KIND_KEY: TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    }
    sign_relay_metadata(content, runtime_paths)
    proof = content[RELAY_PROOF_KEY]

    assert isinstance(proof, str)
    flipped = proof[:-1] + ("1" if proof.endswith("0") else "0")

    assert not relay_metadata_is_runtime_authored({**content, ORIGINAL_SENDER_KEY: "@admin:localhost"}, runtime_paths)
    assert not relay_metadata_is_runtime_authored({**content, SOURCE_KIND_KEY: "scheduled"}, runtime_paths)
    assert not relay_metadata_is_runtime_authored({**content, RELAY_PROOF_KEY: flipped}, runtime_paths)


def test_a_non_ascii_proof_is_refused_without_raising(tmp_path: Path) -> None:
    """Proof text arrives from an event body, so verification must never raise on it."""
    runtime_paths = test_runtime_paths(tmp_path)
    content = {ORIGINAL_SENDER_KEY: "@owner:localhost", RELAY_PROOF_KEY: "é" * 64}

    assert not relay_metadata_is_runtime_authored(content, runtime_paths)


def test_proofs_do_not_transfer_between_installs(tmp_path: Path) -> None:
    """Each storage root keys its own proofs, so a stolen proof stays local."""
    first = test_runtime_paths(tmp_path / "first")
    second = test_runtime_paths(tmp_path / "second")
    content = {ORIGINAL_SENDER_KEY: "@owner:localhost"}
    sign_relay_metadata(content, first)

    assert relay_metadata_is_runtime_authored(content, first)
    assert not relay_metadata_is_runtime_authored(content, second)


def test_the_signing_key_is_persisted_once_and_kept_private(tmp_path: Path) -> None:
    """A restart must keep honouring proofs stamped before it."""
    runtime_paths = test_runtime_paths(tmp_path)
    content = {ORIGINAL_SENDER_KEY: "@owner:localhost"}
    sign_relay_metadata(content, runtime_paths)

    key_path = runtime_paths.storage_root / "relay_signing_key"
    assert key_path.stat().st_mode & 0o077 == 0

    relay_proof._signing_keys.clear()

    assert relay_metadata_is_runtime_authored(content, test_runtime_paths(tmp_path))


def test_a_symlinked_key_path_is_refused(tmp_path: Path) -> None:
    """A link left in the storage root must not choose the key the runtime signs with."""
    runtime_paths = test_runtime_paths(tmp_path)
    attacker_key = tmp_path / "attacker_key"
    attacker_key.write_bytes(b"A" * 32)
    runtime_paths.storage_root.mkdir(parents=True, exist_ok=True)
    (runtime_paths.storage_root / "relay_signing_key").symlink_to(attacker_key)
    relay_proof._signing_keys.clear()

    with pytest.raises(OSError, match=r"Too many levels of symbolic links|symbolic link"):
        sign_relay_metadata({ORIGINAL_SENDER_KEY: "@owner:localhost"}, runtime_paths)
