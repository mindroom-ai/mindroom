"""Tests for live-bot Desktop controller identity resolution."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.desktop.identity import (
    DesktopControllerIdentity,
    DesktopIdentityError,
    controller_identity_for_live_bot,
)
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


_USER_ID = "@computer:example.org"
_DEVICE_ID = "CLOUDDEVICE"


def _live_bot(
    *,
    fingerprint: str = "live-fingerprint",
    running: bool = True,
    agent_name: str = "computer",
    expected_user_id: str = _USER_ID,
    expected_device_id: str = _DEVICE_ID,
    client_user_id: str = _USER_ID,
    client_device_id: str = _DEVICE_ID,
) -> SimpleNamespace:
    client = SimpleNamespace(
        user_id=client_user_id,
        device_id=client_device_id,
        olm=SimpleNamespace(
            account=SimpleNamespace(identity_keys={"ed25519": fingerprint}),
        ),
    )
    return SimpleNamespace(
        running=running,
        agent_name=agent_name,
        agent_user=SimpleNamespace(
            user_id=expected_user_id,
            device_id=expected_device_id,
        ),
        client=client,
    )


def test_controller_identity_uses_running_live_bot_without_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exclusive store owner itself supplies the Desktop controller pin."""
    monkeypatch.setattr(
        sqlite3,
        "connect",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("SQLite must not open")),
    )

    identity = controller_identity_for_live_bot("computer", _live_bot())

    assert identity == DesktopControllerIdentity(
        entity_name="computer",
        user_id=_USER_ID,
        device_id=_DEVICE_ID,
        ed25519="live-fingerprint",
    )


@pytest.mark.parametrize(
    ("bot", "message"),
    [
        (None, "not running"),
        (_live_bot(running=False), "not running"),
        (_live_bot(agent_name="other"), "live bot registry"),
        (_live_bot(client_user_id="@other:example.org"), "mismatched live Matrix device"),
        (_live_bot(client_device_id="OTHER"), "mismatched live Matrix device"),
        (_live_bot(fingerprint=""), "no live Ed25519"),
    ],
)
def test_controller_identity_fails_closed_for_missing_or_mismatched_live_bot(
    bot: object,
    message: str,
) -> None:
    """Only the current exact running entity/device can supply a pin."""
    with pytest.raises(DesktopIdentityError, match=message):
        controller_identity_for_live_bot("computer", bot)


def test_orchestrator_resolver_tracks_live_bot_replacement(tmp_path: Path) -> None:
    """The injected resolver reads the current registry entry on every call."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))
    first = _live_bot(fingerprint="first")
    replacement = _live_bot(fingerprint="replacement")
    orchestrator.agent_bots["computer"] = first

    assert orchestrator.desktop_controller_identity("computer").ed25519 == "first"

    first.running = False
    orchestrator.agent_bots["computer"] = replacement
    assert orchestrator.desktop_controller_identity("computer").ed25519 == "replacement"


def test_orchestrator_resolver_rejects_missing_target(tmp_path: Path) -> None:
    """No persisted-store fallback is available when the live target is absent."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=test_runtime_paths(tmp_path))

    with pytest.raises(DesktopIdentityError, match="not running"):
        orchestrator.desktop_controller_identity("computer")
