"""Tests for requester-agent Desktop pairing over authenticated Matrix events."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

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
)
from mindroom.desktop.pairing_client import send_desktop_pairing_claim
from mindroom.desktop.pairing_receiver import DesktopPairingReceiver, register_desktop_pairing_receiver
from mindroom.desktop.protocol import (
    DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE,
    DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
    DesktopPairingAccepted,
    DesktopPairingClaim,
    desktop_pairing_verification,
)
from mindroom.matrix.device_identity import PinnedMatrixDevice
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.to_device import AuthenticatedToDeviceEvent
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def test_pairing_binds_claim_and_confirmation_to_requester_agent(tmp_path: Path) -> None:
    """Only the initiating requester-agent scope can consume a device claim."""
    runtime_paths = test_runtime_paths(tmp_path)
    pairing = create_desktop_pairing(
        runtime_paths,
        requester_id="@alice:example.org",
        agent_name="computer",
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

    with pytest.raises(DesktopPairingError, match="requester and agent"):
        confirm_desktop_pairing(
            runtime_paths,
            token=pairing.token,
            requester_id="@bob:example.org",
            agent_name="computer",
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
            verification="wrong-device",
            now=102,
        )

    confirmed = confirm_desktop_pairing(
        runtime_paths,
        token=pairing.token,
        requester_id="@alice:example.org",
        agent_name="computer",
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
async def test_pairing_claim_uses_authenticated_device_store_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claim content cannot choose the stored Matrix user, device, or fingerprint."""
    runtime_paths = test_runtime_paths(tmp_path)
    pairing = create_desktop_pairing(
        runtime_paths,
        requester_id="@alice:example.org",
        agent_name="computer",
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
    send_ack = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.pairing_receiver.send_encrypted_to_device", send_ack)

    await DesktopPairingReceiver(
        client=client,  # type: ignore[arg-type]
        agent_name="computer",
        runtime_paths=runtime_paths,
    ).on_event(event)

    confirmed = confirm_desktop_pairing(
        runtime_paths,
        token=pairing.token,
        requester_id="@alice:example.org",
        agent_name="computer",
        verification=desktop_pairing_verification(pairing.token, "signed-fingerprint"),
    )
    assert (
        confirmed.device_user_id,
        confirmed.device_id,
        confirmed.device_ed25519,
    ) == ("@desktop:example.org", "SIGNED", "signed-fingerprint")
    assert send_ack.await_args.kwargs["event_type"] == DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE
    assert DesktopPairingAccepted.from_content(send_ack.await_args.kwargs["content"]).verification == (
        desktop_pairing_verification(pairing.token, "signed-fingerprint")
    )


@pytest.mark.asyncio
async def test_pairing_client_retries_until_authenticated_controller_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local CLI reports success only after its pinned controller accepts the claim."""
    controller = PinnedMatrixDevice(
        user_id="@computer:example.org",
        device_id="CLOUD",
        ed25519="cloud-fingerprint",
    )
    verification = desktop_pairing_verification("pair-code", "desktop-fingerprint")
    cloud_device = SimpleNamespace(ed25519="cloud-fingerprint", blacklisted=False)

    class PairingClient:
        def __init__(self) -> None:
            self.olm = SimpleNamespace(
                account=SimpleNamespace(identity_keys={"ed25519": "desktop-fingerprint"}),
                device_store={controller.user_id: {controller.device_id: cloud_device}},
            )
            self.callback: object | None = None
            self.sync_count = 0

        def add_to_device_callback(self, callback: object, _event_type: object) -> None:
            self.callback = callback

        async def sync(self, **_kwargs: object) -> object:
            self.sync_count += 1
            if self.sync_count == 2:
                event = AuthenticatedToDeviceEvent(
                    source={"content": DesktopPairingAccepted(verification).to_content()},
                    sender=controller.user_id,
                    type=DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE,
                    authenticated_device_id=controller.device_id,
                )
                assert self.callback is not None
                await self.callback(event)  # type: ignore[operator]
            return object()

    client = PairingClient()
    send_claim = AsyncMock()
    monkeypatch.setattr("mindroom.desktop.pairing_client.resolve_pinned_device", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.pairing_client.prepare_desktop_client", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.pairing_client.send_encrypted_to_device", send_claim)

    result = await send_desktop_pairing_claim(
        client,  # type: ignore[arg-type]
        controller,
        code="pair-code",
    )

    assert result == verification
    assert send_claim.await_count == 2


@pytest.mark.asyncio
async def test_pairing_claim_contains_database_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient pairing storage failures must not fail the Matrix callback."""

    def fail_claim(*_args: object, **_kwargs: object) -> None:
        message = "database is locked"
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr("mindroom.desktop.pairing_receiver.claim_desktop_pairing", fail_claim)
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

    await DesktopPairingReceiver(
        client=client,  # type: ignore[arg-type]
        agent_name="computer",
        runtime_paths=test_runtime_paths(tmp_path),
    ).on_event(event)


def test_pairing_receiver_registration_owns_desktop_enablement_check(tmp_path: Path) -> None:
    """The transport collaborator, not AgentBot, decides whether to register."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": []},
            "agents": {
                "computer": {
                    "display_name": "Computer",
                    "role": "Operate local apps",
                    "tools": ["desktop"],
                },
                "chat": {
                    "display_name": "Chat",
                    "role": "Talk",
                    "tools": [],
                },
            },
        },
        runtime_paths,
    )
    client = SimpleNamespace(add_to_device_callback=Mock())

    register_desktop_pairing_receiver(
        config,
        client=client,  # type: ignore[arg-type]
        agent_name="chat",
        runtime_paths=runtime_paths,
        callback_wrapper=lambda callback: callback,
    )
    client.add_to_device_callback.assert_not_called()

    register_desktop_pairing_receiver(
        config,
        client=client,  # type: ignore[arg-type]
        agent_name="computer",
        runtime_paths=runtime_paths,
        callback_wrapper=lambda callback: callback,
    )

    callback, event_type = client.add_to_device_callback.call_args.args
    assert callback.__self__.agent_name == "computer"
    assert event_type is AuthenticatedToDeviceEvent


def test_setup_command_uses_public_homeserver_and_access_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hosted runtimes can print a reachable Desktop login command."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": []},
            "agents": {
                "computer": {
                    "display_name": "Computer",
                    "role": "Operate local apps",
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
    scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="computer",
        requester_id="@alice:example.org",
    )

    setup_response = handle_desktop_command("setup", scope=scope)
    login_command = next(line for line in setup_response.splitlines() if line.startswith("mindroom desktop login"))
    pairing_command = next(line for line in setup_response.splitlines() if line.startswith("mindroom desktop pair"))
    assert login_command == ("mindroom desktop login --user-id @alice:example.org --homeserver http://localhost:8008")
    assert "--cloudflare-access" not in pairing_command

    public_runtime_paths = replace(
        runtime_paths,
        process_env={
            **runtime_paths.process_env,
            "MINDROOM_DESKTOP_MATRIX_HOMESERVER": "https://matrix.example.org",
            "MINDROOM_DESKTOP_CLOUDFLARE_ACCESS": "true",
        },
    )
    public_setup_response = handle_desktop_command(
        "setup",
        scope=replace(scope, runtime_paths=public_runtime_paths),
    )
    public_login_command = next(
        line for line in public_setup_response.splitlines() if line.startswith("mindroom desktop login")
    )
    public_pairing_command = next(
        line for line in public_setup_response.splitlines() if line.startswith("mindroom desktop pair")
    )
    assert public_login_command == (
        "mindroom desktop login --user-id @alice:example.org "
        "--homeserver https://matrix.example.org --cloudflare-access"
    )
    assert public_pairing_command.endswith("--cloudflare-access")
    message_content = format_message_with_mentions(
        config,
        public_runtime_paths,
        public_setup_response,
    )
    assert message_content["body"] == public_setup_response
    assert "https://matrix.to/#/" not in message_content["formatted_body"]
    assert "m.mentions" not in message_content


def test_chat_confirmation_saves_only_the_initiating_requester_agent_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A normal agent stores each requester's Desktop separately from every other scope."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "defaults": {"tools": []},
            "agents": {
                "computer": {
                    "display_name": "Computer",
                    "role": "Operate local apps",
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
    )
    bob_scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="computer",
        requester_id="@bob:example.org",
    )
    alice_other_agent_scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="other",
        requester_id="@alice:example.org",
    )

    setup_response = handle_desktop_command("setup", scope=alice_scope)
    assert "mindroom desktop login --user-id @alice:example.org --homeserver http://localhost:8008" in setup_response
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
    assert "verification does not match" in handle_desktop_command(f"confirm {token} vérification", scope=alice_scope)
    assert "Desktop paired" in handle_desktop_command(f"confirm {token} {verification}", scope=alice_scope)
    assert "Desktop is configured" in handle_desktop_command("", scope=alice_scope)
    assert "Desktop is configured" in handle_desktop_command("status", scope=alice_scope)
    assert "setup is required" in handle_desktop_command("status", scope=bob_scope)
    assert "setup is required" in handle_desktop_command("status", scope=alice_other_agent_scope)

    def pair_scope(scope: DesktopCommandScope, *, device_id: str, fingerprint: str) -> None:
        setup = handle_desktop_command("setup", scope=scope)
        scope_token_match = re.search(r"--code ([A-Za-z0-9_-]+)", setup)
        assert scope_token_match is not None
        scope_token = scope_token_match.group(1)
        claim_desktop_pairing(
            runtime_paths,
            token=scope_token,
            agent_name=scope.agent_name,
            device_user_id=f"@{device_id.lower()}-desktop:example.org",
            device_id=device_id,
            device_ed25519=fingerprint,
        )
        scope_verification = desktop_pairing_verification(scope_token, fingerprint)
        assert "Desktop paired" in handle_desktop_command(
            f"confirm {scope_token} {scope_verification}",
            scope=scope,
        )

    pair_scope(bob_scope, device_id="BOB", fingerprint="bob-fingerprint")
    pair_scope(alice_other_agent_scope, device_id="ALICEOTHER", fingerprint="alice-other-fingerprint")

    assert "mindroom desktop pair" in handle_desktop_command("rotate", scope=alice_scope)
    assert "Usage:" in handle_desktop_command("unknown", scope=alice_scope)
    assert "disconnect confirm" in handle_desktop_command("disconnect", scope=alice_scope)
    assert "Desktop is configured" in handle_desktop_command("status", scope=alice_scope)

    assert "Desktop disconnected" in handle_desktop_command("disconnect confirm", scope=alice_scope)
    assert "setup is required" in handle_desktop_command("status", scope=alice_scope)
    assert "Desktop is configured" in handle_desktop_command("status", scope=bob_scope)
    assert "Desktop is configured" in handle_desktop_command("status", scope=alice_other_agent_scope)


def test_desktop_command_contains_pairing_database_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient SQLite failure becomes a retryable chat response."""
    monkeypatch.setattr(
        "mindroom.commands.desktop_commands._load_desktop_credentials",
        Mock(side_effect=sqlite3.OperationalError("database is locked")),
    )
    scope = SimpleNamespace(agent_name="computer")

    response = handle_desktop_command("status", scope=scope)  # type: ignore[arg-type]

    assert "temporarily unavailable" in response
