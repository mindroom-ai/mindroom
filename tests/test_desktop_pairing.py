"""Tests for requester-agent Desktop pairing over authenticated Matrix events."""

from __future__ import annotations

import re
import sqlite3
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

import mindroom.tools  # noqa: F401
from mindroom.commands.desktop_commands import DesktopCommandScope, handle_desktop_command
from mindroom.config.main import Config
from mindroom.desktop.pairing import (
    DesktopPairingError,
    claim_desktop_pairing,
    complete_desktop_pairing,
    confirm_desktop_pairing,
    create_desktop_pairing,
    handle_desktop_pairing_claim,
)
from mindroom.desktop.protocol import (
    DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
    DesktopPairingClaim,
    desktop_pairing_verification,
)
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def test_pairing_binds_claim_and_confirmation_to_requester_agent_conversation(tmp_path: Path) -> None:
    """Only the initiating requester-agent conversation can consume a device claim."""
    runtime_paths = test_runtime_paths(tmp_path)
    pairing = create_desktop_pairing(
        runtime_paths,
        requester_id="@alice:example.org",
        agent_name="computer",
        room_id="!private:example.org",
        thread_id="$thread",
        now=100,
    )

    with pytest.raises(DesktopPairingError, match="another agent"):
        claim_desktop_pairing(
            runtime_paths,
            token=pairing.token,
            agent_name="other",
            device_user_id="@desktop:example.org",
            device_id="DEVICE",
            device_ed25519="fingerprint",
            now=101,
        )

    claim_desktop_pairing(
        runtime_paths,
        token=pairing.token,
        agent_name="computer",
        device_user_id="@desktop:example.org",
        device_id="DEVICE",
        device_ed25519="fingerprint",
        now=101,
    )

    with pytest.raises(DesktopPairingError, match="requester, agent, and conversation"):
        confirm_desktop_pairing(
            runtime_paths,
            token=pairing.token,
            requester_id="@bob:example.org",
            agent_name="computer",
            room_id="!private:example.org",
            thread_id="$thread",
            verification="ignored",
            now=102,
        )

    with pytest.raises(DesktopPairingError, match="conversation"):
        confirm_desktop_pairing(
            runtime_paths,
            token=pairing.token,
            requester_id="@alice:example.org",
            agent_name="computer",
            room_id="!private:example.org",
            thread_id="$other-thread",
            verification="ignored",
            now=102,
        )

    verification = desktop_pairing_verification(pairing.token, "fingerprint")
    with pytest.raises(DesktopPairingError, match="verification"):
        confirm_desktop_pairing(
            runtime_paths,
            token=pairing.token,
            requester_id="@alice:example.org",
            agent_name="computer",
            room_id="!private:example.org",
            thread_id="$thread",
            verification="wrong-device",
            now=102,
        )

    confirmed = confirm_desktop_pairing(
        runtime_paths,
        token=pairing.token,
        requester_id="@alice:example.org",
        agent_name="computer",
        room_id="!private:example.org",
        thread_id="$thread",
        verification=verification,
        now=102,
    )
    assert confirmed.device_user_id == "@desktop:example.org"
    assert confirmed.device_id == "DEVICE"
    assert confirmed.device_ed25519 == "fingerprint"

    complete_desktop_pairing(runtime_paths, token=pairing.token)
    with pytest.raises(DesktopPairingError, match="invalid or expired"):
        confirm_desktop_pairing(
            runtime_paths,
            token=pairing.token,
            requester_id="@alice:example.org",
            agent_name="computer",
            room_id="!private:example.org",
            thread_id="$thread",
            verification=verification,
            now=103,
        )


def test_pairing_stores_only_a_hash_and_expires(tmp_path: Path) -> None:
    """The raw bearer code is not persisted and an expired code cannot be claimed."""
    runtime_paths = test_runtime_paths(tmp_path)
    pairing = create_desktop_pairing(
        runtime_paths,
        requester_id="@alice:example.org",
        agent_name="computer",
        room_id="!private:example.org",
        thread_id=None,
        now=100,
    )
    database_path = runtime_paths.storage_root / "tracking" / "desktop_pairing.sqlite"
    with sqlite3.connect(database_path) as connection:
        token_hash = connection.execute("SELECT token_hash FROM desktop_pairings").fetchone()[0]
    assert token_hash != pairing.token
    assert pairing.token.encode() not in database_path.read_bytes()

    with pytest.raises(DesktopPairingError, match="invalid or expired"):
        claim_desktop_pairing(
            runtime_paths,
            token=pairing.token,
            agent_name="computer",
            device_user_id="@desktop:example.org",
            device_id="DEVICE",
            device_ed25519="fingerprint",
            now=pairing.expires_at,
        )


@pytest.mark.asyncio
async def test_pairing_claim_uses_authenticated_device_store_identity(tmp_path: Path) -> None:
    """Claim content cannot choose the stored Matrix user, device, or fingerprint."""
    runtime_paths = test_runtime_paths(tmp_path)
    pairing = create_desktop_pairing(
        runtime_paths,
        requester_id="@alice:example.org",
        agent_name="computer",
        room_id="!private:example.org",
        thread_id=None,
    )
    device = SimpleNamespace(ed25519="signed-fingerprint", blacklisted=False)
    client = SimpleNamespace(
        olm=SimpleNamespace(device_store={"@desktop:example.org": {"SIGNED": device}}),
    )
    event = AuthenticatedToDeviceEvent(
        source={"content": DesktopPairingClaim(pairing.token).to_content()},
        sender="@desktop:example.org",
        type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
        authenticated_device_id="SIGNED",
    )

    await handle_desktop_pairing_claim(
        event,
        client=client,  # type: ignore[arg-type]
        agent_name="computer",
        runtime_paths=runtime_paths,
    )

    confirmed = confirm_desktop_pairing(
        runtime_paths,
        token=pairing.token,
        requester_id="@alice:example.org",
        agent_name="computer",
        room_id="!private:example.org",
        thread_id=None,
        verification=desktop_pairing_verification(pairing.token, "signed-fingerprint"),
    )
    assert (
        confirmed.device_user_id,
        confirmed.device_id,
        confirmed.device_ed25519,
    ) == ("@desktop:example.org", "SIGNED", "signed-fingerprint")


@pytest.mark.asyncio
async def test_pairing_claim_contains_database_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient pairing storage failures must not fail the Matrix callback."""

    def fail_claim(*_args: object, **_kwargs: object) -> None:
        message = "database is locked"
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr("mindroom.desktop.pairing.claim_desktop_pairing", fail_claim)
    device = SimpleNamespace(ed25519="signed-fingerprint", blacklisted=False)
    client = SimpleNamespace(
        olm=SimpleNamespace(device_store={"@desktop:example.org": {"SIGNED": device}}),
    )
    event = AuthenticatedToDeviceEvent(
        source={"content": DesktopPairingClaim("pairing-token").to_content()},
        sender="@desktop:example.org",
        type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
        authenticated_device_id="SIGNED",
    )

    await handle_desktop_pairing_claim(
        event,
        client=client,  # type: ignore[arg-type]
        agent_name="computer",
        runtime_paths=test_runtime_paths(tmp_path),
    )


def test_chat_confirmation_saves_only_the_initiating_requester_agent_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed chat flow becomes ready only for its exact private user-agent pair."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": []},
            "agents": {
                "computer": {
                    "display_name": "Computer",
                    "role": "Operate local apps",
                    "private": {"per": "user_agent"},
                    "tools": ["desktop"],
                },
                "other": {
                    "display_name": "Other",
                    "role": "Operate other local apps",
                    "private": {"per": "user_agent"},
                    "tools": ["desktop"],
                },
            },
        },
        runtime_paths,
    )
    monkeypatch.setattr(
        "mindroom.commands.desktop_commands.controller_identity_for_entity",
        lambda *_args, **_kwargs: SimpleNamespace(
            user_id="@computer:example.org",
            device_id="CLOUD",
            ed25519="cloud-fingerprint",
        ),
    )
    alice_scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="computer",
        requester_id="@alice:example.org",
        room_id="!private:example.org",
        thread_id=None,
    )
    bob_scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="computer",
        requester_id="@bob:example.org",
        room_id="!private:example.org",
        thread_id=None,
    )
    alice_other_agent_scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="other",
        requester_id="@alice:example.org",
        room_id="!other-private:example.org",
        thread_id=None,
    )

    setup_response = handle_desktop_command("setup", scope=alice_scope)
    token_match = re.search(r"--code ([A-Za-z0-9_-]+)", setup_response)
    assert token_match is not None
    token = token_match.group(1)
    claim_desktop_pairing(
        runtime_paths,
        token=token,
        agent_name="computer",
        device_user_id="@alice-desktop:example.org",
        device_id="ALICE",
        device_ed25519="alice-fingerprint",
    )
    verification = desktop_pairing_verification(token, "alice-fingerprint")

    assert "does not belong" in handle_desktop_command(f"confirm {token} {verification}", scope=bob_scope)
    assert "Desktop paired" in handle_desktop_command(f"confirm {token} {verification}", scope=alice_scope)
    assert "Desktop is configured" in handle_desktop_command("status", scope=alice_scope)
    assert "setup is required" in handle_desktop_command("status", scope=bob_scope)
    assert "setup is required" in handle_desktop_command("status", scope=alice_other_agent_scope)
