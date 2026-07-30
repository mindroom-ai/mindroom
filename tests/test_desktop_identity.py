"""Tests for cloud Matrix controller identity lookup."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio
import pytest
from nio.crypto import Olm
from nio.store import SqliteStore

from mindroom.desktop.identity import DesktopIdentityError, controller_identity_for_entity
from mindroom.matrix.client_session import olm_store_dir
from mindroom.matrix.identity import managed_account_key
from mindroom.matrix.state import MatrixState
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def test_controller_identity_reads_the_persisted_olm_account(tmp_path: Path) -> None:
    """The printed pin comes from the controller's local crypto store."""
    runtime_paths = test_runtime_paths(tmp_path)
    user_id = "@computer:example.org"
    device_id = "CLOUDDEVICE"
    state = MatrixState()
    state.add_account(
        managed_account_key("computer"),
        "computer",
        "unused-password",
        domain="example.org",
        device_id=device_id,
        access_token="unused-token",  # noqa: S106 - Test-only Matrix state fixture.
    )
    state.save(runtime_paths)
    store_path = olm_store_dir(user_id, runtime_paths)
    store_path.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(
        user_id,
        device_id,
        str(store_path),
        pickle_key=nio.AsyncClientConfig().pickle_key,
    )
    olm = Olm(user_id, device_id, store)
    expected_fingerprint = olm.account.identity_keys["ed25519"]
    store.database.close()

    identity = controller_identity_for_entity("computer", runtime_paths=runtime_paths)

    assert identity.user_id == user_id
    assert identity.device_id == device_id
    assert identity.ed25519 == expected_fingerprint


def test_controller_identity_requires_a_started_entity(tmp_path: Path) -> None:
    """Missing account state produces a setup instruction instead of a partial pin."""
    runtime_paths = test_runtime_paths(tmp_path)

    with pytest.raises(DesktopIdentityError, match="start MindRoom once"):
        controller_identity_for_entity("computer", runtime_paths=runtime_paths)
