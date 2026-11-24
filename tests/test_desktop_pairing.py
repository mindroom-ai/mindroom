"""Tests for requester-agent Desktop pairing over authenticated Matrix events."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import nio
import pytest
from nio import AuthenticatedDevice, AuthenticatedToDeviceEvent
from nio.durable import RecordKind, SyncBatch, SyncRecord

import mindroom.tools  # noqa: F401
from mindroom.commands.desktop_commands import DesktopCommandScope, desktop_room_agent, handle_desktop_command
from mindroom.commands.handler import handle_command
from mindroom.commands.parsing import Command, CommandType
from mindroom.config.main import Config
from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.desktop.credentials import load_desktop_credentials, save_desktop_credentials
from mindroom.desktop.identity import DesktopIdentityError
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
    DESKTOP_DISCONNECT_CONFIRMED_EVENT_TYPE,
    DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE,
    DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE,
    DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
    DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
    DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE,
    DESKTOP_SETUP_OFFER_EVENT_TYPE,
    DESKTOP_SETUP_REQUEST_EVENT_TYPE,
    DesktopDisconnectConfirmed,
    DesktopDisconnectRequest,
    DesktopPairingAccepted,
    DesktopPairingClaim,
    DesktopPairingConfirm,
    DesktopPairingConfirmed,
    DesktopSetupOffer,
    DesktopSetupRequest,
    desktop_pairing_verification,
)
from mindroom.matrix.device_identity import PinnedMatrixDevice
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.mentions import format_message_with_mentions
from mindroom.matrix.olm_to_device import OlmToDeviceError
from mindroom.matrix.room_membership import ensure_room_membership_synced
from mindroom.message_target import MessageTarget
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import make_conversation_reader_mock, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

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
            device_user_id="@alice:example.org",
            device_id="DEVICE",
            device_ed25519="fingerprint",
            now=101,
        )

    claim_desktop_pairing(
        runtime_paths,
        token=pairing.token,
        agent_name="computer",
        device_user_id="@alice:example.org",
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
    assert confirmed.device_user_id == "@alice:example.org"
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
            device_user_id="@alice:example.org",
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
    device = SimpleNamespace(ed25519="signed-fingerprint", curve25519="curve-key", deleted=False, blacklisted=False)
    client = SimpleNamespace(
        olm=SimpleNamespace(device_store={"@alice:example.org": {"SIGNED": device}}),
    )
    event = AuthenticatedToDeviceEvent(
        source={"content": DesktopPairingClaim(pairing.token).to_content()},
        sender="@alice:example.org",
        type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice("@alice:example.org", "SIGNED", "curve-key", "signed-fingerprint"),
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
    ) == ("@alice:example.org", "SIGNED", "signed-fingerprint")
    assert send_ack.await_args.kwargs["event_type"] == DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE
    assert DesktopPairingAccepted.from_content(send_ack.await_args.kwargs["content"]).verification == (
        desktop_pairing_verification(pairing.token, "signed-fingerprint")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_failure", [False, True])
async def test_pairing_client_retries_until_authenticated_controller_ack(
    monkeypatch: pytest.MonkeyPatch,
    ack_failure: bool,
) -> None:
    """Pairing consumes durable acknowledgements and removes its scoped callback."""
    controller = PinnedMatrixDevice("@computer:example.org", "CLOUD", "cloud-fingerprint")
    verification = desktop_pairing_verification("pair-code", "desktop-fingerprint")
    ready = asyncio.Event()
    acknowledged = asyncio.Event()
    client = nio.AsyncClient("https://matrix.example.org", config=nio.AsyncClientConfig(encryption_enabled=False))
    client.olm = SimpleNamespace(
        account=SimpleNamespace(identity_keys={"ed25519": "desktop-fingerprint"}),
        device_store={
            controller.user_id: {
                controller.device_id: SimpleNamespace(
                    ed25519=controller.ed25519,
                    curve25519="curve-key",
                    deleted=False,
                    blacklisted=False,
                ),
            },
        },
    )
    event = AuthenticatedToDeviceEvent(
        source={"content": DesktopPairingAccepted(verification).to_content()},
        sender=controller.user_id,
        type=DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice(
            controller.user_id,
            controller.device_id,
            "curve-key",
            controller.ed25519,
        ),
    )
    batch = SyncBatch(uuid4(), 1, (SyncRecord(RecordKind.TO_DEVICE, None, event.source),))

    class Source:
        async def run(self) -> None:
            await asyncio.Event().wait()

        async def wait_for_work(self) -> None:
            await ready.wait()

        async def next_batch(self) -> SyncBatch:
            ready.clear()
            return batch

        async def dispatch(self, _record: SyncRecord) -> None:
            await client._on_to_device(event)

        async def ack(self, _batch: SyncBatch) -> None:
            if ack_failure:
                await asyncio.sleep(0.01)
                msg = "durable acknowledgement failed"
                raise OSError(msg)
            acknowledged.set()

    sends = []

    async def send_claim(*_args: object, **_kwargs: object) -> None:
        sends.append("claim")
        if len(sends) == 2:
            ready.set()

    owner = SimpleNamespace(client=client, source=Source())
    monkeypatch.setattr("mindroom.desktop.pairing_client.resolve_pinned_device", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.pairing_client.prepare_desktop_client", AsyncMock())
    monkeypatch.setattr("mindroom.desktop.pairing_client.send_encrypted_to_device", send_claim)
    monkeypatch.setattr("mindroom.desktop.pairing_client._PAIRING_RETRY_SECONDS", 0, raising=False)
    try:
        if ack_failure:
            with pytest.raises(OSError, match="durable acknowledgement failed"):
                await send_desktop_pairing_claim(owner, controller, code="pair-code")
            assert not acknowledged.is_set()
        else:
            assert await send_desktop_pairing_claim(owner, controller, code="pair-code") == verification
            assert sends == ["claim", "claim"]
            assert acknowledged.is_set()
        assert client.to_device_callbacks == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pairing_callback_removed_when_pinning_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Early preparation errors cannot leave a stale pairing callback registered."""
    client = nio.AsyncClient("https://matrix.example.org", config=nio.AsyncClientConfig(encryption_enabled=False))
    client.olm = SimpleNamespace(account=SimpleNamespace(identity_keys={"ed25519": "desktop-fingerprint"}))
    monkeypatch.setattr(
        "mindroom.desktop.pairing_client.resolve_pinned_device",
        AsyncMock(side_effect=OlmToDeviceError("pin changed")),
    )
    try:
        with pytest.raises(OlmToDeviceError, match="pin changed"):
            await send_desktop_pairing_claim(
                SimpleNamespace(client=client, source=object()),
                PinnedMatrixDevice("@c:example.org", "C", "key"),
                code="code",
            )
        assert client.to_device_callbacks == []
    finally:
        await client.close()


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
    device = SimpleNamespace(ed25519="signed-fingerprint", curve25519="curve-key", deleted=False, blacklisted=False)
    client = SimpleNamespace(
        olm=SimpleNamespace(device_store={"@desktop:example.org": {"SIGNED": device}}),
    )
    event = AuthenticatedToDeviceEvent(
        source={"content": DesktopPairingClaim("pairing-token").to_content()},
        sender="@desktop:example.org",
        type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
        authenticated_sender=AuthenticatedDevice("@desktop:example.org", "SIGNED", "curve-key", "signed-fingerprint"),
    )

    await DesktopPairingReceiver(
        client=client,  # type: ignore[arg-type]
        agent_name="computer",
        runtime_paths=test_runtime_paths(tmp_path),
    ).on_event(event)


def test_pairing_receiver_registration_owns_desktop_enablement_check(tmp_path: Path) -> None:
    """The transport collaborator registers only concrete agents, skipping router/unknown entities."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
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
        agent_name="router",
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
) -> None:
    """Hosted runtimes can print one reachable Desktop setup command."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
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
    controller_identity = lambda _entity_name: SimpleNamespace(  # noqa: E731
        user_id="@computer:example.org",
        device_id="CLOUD",
        ed25519="cloud-fingerprint",
    )
    scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="computer",
        requester_id="@alice:example.org",
        controller_identity=controller_identity,
    )

    setup_response = handle_desktop_command("setup", scope=scope)
    setup_command = next(line for line in setup_response.splitlines() if line.startswith("mindroom desktop setup"))
    assert setup_command.startswith(
        "mindroom desktop setup --user-id @alice:example.org --homeserver http://localhost:8008 --code ",
    )
    assert "--allow-requester @alice:example.org" in setup_command
    assert "--allow-agent computer" in setup_command
    assert "--controller-user-id @computer:example.org" in setup_command
    assert "--cloudflare-access" not in setup_command

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
    public_setup_command = next(
        line for line in public_setup_response.splitlines() if line.startswith("mindroom desktop setup")
    )
    assert "--homeserver https://matrix.example.org" in public_setup_command
    assert public_setup_command.endswith("--cloudflare-access")
    message_content = format_message_with_mentions(
        config,
        public_runtime_paths,
        public_setup_response,
    )
    assert message_content["body"] == public_setup_response
    assert "https://matrix.to/#/" not in message_content["formatted_body"]
    assert "m.mentions" not in message_content


def test_confirmation_keeps_claim_retryable_when_controller_lookup_fails(
    tmp_path: Path,
) -> None:
    """Startup-guidance failure must not consume a valid pairing claim."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "defaults": {"tools": []},
            "agents": {"computer": {"display_name": "Computer", "role": "Operate apps", "tools": ["desktop"]}},
        },
        runtime_paths,
    )
    controller = SimpleNamespace(
        user_id="@computer:example.org",
        device_id="CLOUD",
        ed25519="cloud-fingerprint",
    )
    identity_lookup = Mock(return_value=controller)
    scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="computer",
        requester_id="@alice:example.org",
        controller_identity=identity_lookup,
    )
    setup_response = handle_desktop_command("setup", scope=scope)
    token_match = re.search(r"--code ([A-Za-z0-9_-]+)", setup_response)
    assert token_match is not None
    token = token_match.group(1)
    claim_desktop_pairing(
        runtime_paths,
        token=token,
        agent_name="computer",
        device_user_id="@alice:example.org",
        device_id="ALICE",
        device_ed25519="alice-fingerprint",
    )
    verification = desktop_pairing_verification(token, "alice-fingerprint")

    identity_lookup.side_effect = DesktopIdentityError("controller unavailable")
    assert "controller unavailable" in handle_desktop_command(f"confirm {token} {verification}", scope=scope)
    identity_lookup.side_effect = None
    assert "Desktop paired" in handle_desktop_command(f"confirm {token} {verification}", scope=scope)


def test_chat_confirmation_saves_only_the_initiating_requester_agent_scope(
    tmp_path: Path,
) -> None:
    """A normal agent stores each requester's Desktop separately from every other scope."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
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
    controller_identity = lambda _entity_name: SimpleNamespace(  # noqa: E731
        user_id="@computer:example.org",
        device_id="CLOUD",
        ed25519="cloud-fingerprint",
    )
    alice_scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="computer",
        requester_id="@alice:example.org",
        controller_identity=controller_identity,
    )
    bob_scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="computer",
        requester_id="@bob:example.org",
        controller_identity=controller_identity,
    )
    alice_other_agent_scope = DesktopCommandScope(
        config=config,
        runtime_paths=runtime_paths,
        agent_name="other",
        requester_id="@alice:example.org",
        controller_identity=controller_identity,
    )

    setup_response = handle_desktop_command("setup", scope=alice_scope)
    assert "mindroom desktop setup --user-id @alice:example.org --homeserver http://localhost:8008" in setup_response
    token_match = re.search(r"--code ([A-Za-z0-9_-]+)", setup_response)
    assert token_match is not None
    token = token_match.group(1)
    claim_desktop_pairing(
        runtime_paths,
        token=token,
        agent_name="computer",
        device_user_id="@alice:example.org",
        device_id="ALICE",
        device_ed25519="alice-fingerprint",
    )
    verification = desktop_pairing_verification(token, "alice-fingerprint")

    assert "does not belong" in handle_desktop_command(f"confirm {token} {verification}", scope=bob_scope)
    assert "verification does not match" in handle_desktop_command(f"confirm {token} vérification", scope=alice_scope)
    confirmation = handle_desktop_command(f"confirm {token} {verification}", scope=alice_scope)
    assert "Desktop paired" in confirmation
    assert "mindroom desktop run \\" in confirmation
    assert "--controller-user-id @computer:example.org" in confirmation
    assert "--controller-device-id CLOUD" in confirmation
    assert "--controller-ed25519 cloud-fingerprint" in confirmation
    assert "--allow-requester @alice:example.org" in confirmation
    assert "--allow-agent computer" in confirmation
    assert "--allow-app APPLICATION_ID" in confirmation
    assert "--allow-control" in confirmation
    assert "add the same option" in confirmation
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
            device_user_id=scope.requester_id,
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

    assert "mindroom desktop setup" in handle_desktop_command("rotate", scope=alice_scope)
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


def test_claim_enforces_requester_account_for_olm_while_preserving_legacy_bridge_account(tmp_path: Path) -> None:
    """Olm pairings strictly require the requester's Matrix account; legacy manual setup still supports dedicated bridge accounts."""
    runtime_paths = test_runtime_paths(tmp_path)

    # Legacy manual /desktop setup (initiator_device_id is None): dedicated bridge Matrix account succeeds.
    legacy = create_desktop_pairing(
        runtime_paths,
        requester_id="@alice:example.org",
        agent_name="computer",
        now=100,
    )
    legacy_claimed = claim_desktop_pairing(
        runtime_paths,
        token=legacy.token,
        agent_name="computer",
        device_user_id="@desktop-bridge:example.org",
        device_id="BRIDGE",
        device_ed25519="bridge-fingerprint",
        now=101,
    )
    assert legacy_claimed.device_user_id == "@desktop-bridge:example.org"
    legacy_confirmed = confirm_desktop_pairing(
        runtime_paths,
        token=legacy.token,
        requester_id="@alice:example.org",
        agent_name="computer",
        verification=desktop_pairing_verification(legacy.token, "bridge-fingerprint"),
        now=102,
    )
    assert legacy_confirmed.device_user_id == "@desktop-bridge:example.org"

    # Olm-initiated pairing (initiator_device_id is set): another Matrix account is rejected.
    olm = create_desktop_pairing(
        runtime_paths,
        requester_id="@alice:example.org",
        agent_name="computer",
        initiator_device_id="CHAT1",
        initiator_ed25519="chat1-key",
        now=100,
    )
    with pytest.raises(DesktopPairingError, match="does not belong to this Matrix account"):
        claim_desktop_pairing(
            runtime_paths,
            token=olm.token,
            agent_name="computer",
            device_user_id="@mallory:example.org",
            device_id="MALLORY",
            device_ed25519="mallory-fingerprint",
            now=101,
        )

    claimed = claim_desktop_pairing(
        runtime_paths,
        token=olm.token,
        agent_name="computer",
        device_user_id="@alice:example.org",
        device_id="HELPER",
        device_ed25519="helper-fingerprint",
        now=102,
    )
    assert claimed.device_user_id == "@alice:example.org"
    # Legacy /desktop confirm (without initiator_device_id) cannot confirm an Olm-initiated pairing.
    with pytest.raises(DesktopPairingError, match="initiating device"):
        confirm_desktop_pairing(
            runtime_paths,
            token=olm.token,
            requester_id="@alice:example.org",
            agent_name="computer",
            verification=desktop_pairing_verification(olm.token, "helper-fingerprint"),
            now=103,
        )


def test_pairing_db_migrates_legacy_schema_without_initiator_columns(tmp_path: Path) -> None:
    """Opening an existing 7-column pairing database adds initiator columns without losing rows."""
    runtime_paths = test_runtime_paths(tmp_path)
    db_path = runtime_paths.storage_root / "tracking" / "desktop_pairing.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE desktop_pairings (
                token_hash TEXT PRIMARY KEY,
                requester_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                device_user_id TEXT,
                device_id TEXT,
                device_ed25519 TEXT
            )
            """,
        )

    pairing = create_desktop_pairing(
        runtime_paths,
        requester_id="@alice:example.org",
        agent_name="computer",
        initiator_device_id="CHAT1",
        initiator_ed25519="chat1-key",
        now=100,
    )
    claimed = claim_desktop_pairing(
        runtime_paths,
        token=pairing.token,
        agent_name="computer",
        device_user_id="@alice:example.org",
        device_id="HELPER",
        device_ed25519="helper-key",
        now=101,
    )
    assert (claimed.initiator_device_id, claimed.initiator_ed25519) == ("CHAT1", "chat1-key")


def _olm_device(ed25519: str, curve25519: str = "curve-key", *, deleted: bool = False) -> SimpleNamespace:
    return SimpleNamespace(ed25519=ed25519, curve25519=curve25519, deleted=deleted, blacklisted=False)


def _olm_event(
    *,
    sender: str,
    device_id: str,
    ed25519: str,
    event_type: str,
    content: dict[str, object],
    curve25519: str = "curve-key",
) -> AuthenticatedToDeviceEvent:
    return AuthenticatedToDeviceEvent(
        source={"content": content},
        sender=sender,
        type=event_type,
        authenticated_sender=AuthenticatedDevice(sender, device_id, curve25519, ed25519),
    )


def _desktop_config(runtime_paths: object) -> Config:
    return Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "defaults": {"tools": []},
            "agents": {
                "computer": {"display_name": "Computer", "role": "Operate apps", "tools": ["desktop"]},
                "notes": {"display_name": "Notes", "role": "Take notes", "tools": []},
            },
        },
        runtime_paths,  # type: ignore[arg-type]
    )


def _olm_receiver(
    runtime_paths: object,
    monkeypatch: pytest.MonkeyPatch,
    *,
    devices: dict[str, dict[str, SimpleNamespace]],
    agent_name: str = "computer",
    config: Config | None = None,
) -> tuple[DesktopPairingReceiver, list[tuple[PinnedMatrixDevice, str, dict[str, object]]]]:
    sent: list[tuple[PinnedMatrixDevice, str, dict[str, object]]] = []

    async def capture_send(
        _client: object,
        target: PinnedMatrixDevice,
        *,
        event_type: str,
        content: dict[str, object],
    ) -> None:
        sent.append((target, event_type, content))

    monkeypatch.setattr("mindroom.desktop.pairing_receiver.send_encrypted_to_device", capture_send)
    client = SimpleNamespace(olm=SimpleNamespace(device_store=devices))
    receiver = DesktopPairingReceiver(
        client=client,  # type: ignore[arg-type]
        agent_name=agent_name,
        runtime_paths=runtime_paths,  # type: ignore[arg-type]
        config=config if config is not None else _desktop_config(runtime_paths),
    )
    return receiver, sent


async def _start_and_claim_olm_pairing(
    receiver: DesktopPairingReceiver,
    sent: list[tuple[PinnedMatrixDevice, str, dict[str, object]]],
) -> tuple[DesktopSetupOffer, str]:
    await receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="CHAT1",
            ed25519="chat1-key",
            event_type=DESKTOP_SETUP_REQUEST_EVENT_TYPE,
            content=DesktopSetupRequest(nonce="setup-nonce-01234567", agent_name="computer").to_content(),
        ),
    )
    target, event_type, content = sent.pop()
    assert target == PinnedMatrixDevice("@alice:example.org", "CHAT1", "chat1-key")
    assert event_type == DESKTOP_SETUP_OFFER_EVENT_TYPE
    offer = DesktopSetupOffer.from_content(content)
    assert offer.eligible is True
    assert offer.code is not None

    await receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="HELPER1",
            ed25519="helper1-key",
            event_type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
            content=DesktopPairingClaim(offer.code).to_content(),
        ),
    )
    target, event_type, content = sent.pop()
    assert target == PinnedMatrixDevice("@alice:example.org", "HELPER1", "helper1-key")
    assert event_type == DESKTOP_PAIRING_ACCEPTED_EVENT_TYPE
    return offer, DesktopPairingAccepted.from_content(content).verification


@pytest.mark.asyncio
async def test_olm_pairing_happy_path_setup_claim_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Olm setup_request -> pairing_claim -> pairing_confirm completes and saves scoped credentials."""
    runtime_paths = replace(
        test_runtime_paths(tmp_path),
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_DESKTOP_MATRIX_HOMESERVER": "https://matrix.example.org",
            "MINDROOM_DESKTOP_CLOUDFLARE_ACCESS": "true",
        },
    )
    devices = {"@alice:example.org": {"CHAT1": _olm_device("chat1-key"), "HELPER1": _olm_device("helper1-key")}}
    config = _desktop_config(runtime_paths)
    receiver, sent = _olm_receiver(runtime_paths, monkeypatch, devices=devices, config=config)

    offer, verification = await _start_and_claim_olm_pairing(receiver, sent)
    assert offer.homeserver == "https://matrix.example.org"
    assert offer.user_id == "@alice:example.org"
    assert offer.cloudflare_access is True
    assert offer.code is not None

    confirm_event = _olm_event(
        sender="@alice:example.org",
        device_id="CHAT1",
        ed25519="chat1-key",
        event_type=DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
        content=DesktopPairingConfirm(
            nonce="confirm-nonce-012345",
            token=offer.code,
            verification=verification,
            device_id="HELPER1",
            device_ed25519="helper1-key",
        ).to_content(),
    )
    await receiver.on_event(confirm_event)
    target, event_type, content = sent.pop()
    assert target == PinnedMatrixDevice("@alice:example.org", "CHAT1", "chat1-key")
    assert event_type == DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE
    assert DesktopPairingConfirmed.from_content(content) == DesktopPairingConfirmed(
        nonce="confirm-nonce-012345",
        agent_name="computer",
        ok=True,
    )
    assert load_desktop_credentials(
        get_runtime_credentials_manager(runtime_paths),
        requester_id="@alice:example.org",
        agent_name="computer",
    ) == {"device_user_id": "@alice:example.org", "device_id": "HELPER1", "device_ed25519": "helper1-key"}

    # Replaying confirmation fails because the token was consumed.
    await receiver.on_event(confirm_event)
    assert DesktopPairingConfirmed.from_content(sent.pop()[2]).ok is False

    # Ineligible agent under explicit_only replies with eligible=False and no code.
    explicit_config = config.model_copy(
        update={"defaults": config.defaults.model_copy(update={"desktop_pairing": "explicit_only"})},
    )
    ineligible_receiver, sent2 = _olm_receiver(
        runtime_paths,
        monkeypatch,
        devices=devices,
        agent_name="notes",
        config=explicit_config,
    )
    await ineligible_receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="CHAT1",
            ed25519="chat1-key",
            event_type=DESKTOP_SETUP_REQUEST_EVENT_TYPE,
            content=DesktopSetupRequest(nonce="inelig-nonce-0123456", agent_name="notes").to_content(),
        ),
    )
    denied = DesktopSetupOffer.from_content(sent2.pop()[2])
    assert denied.eligible is False
    assert denied.code is None
    assert denied.error is not None


@pytest.mark.asyncio
async def test_olm_pairing_confirm_rejects_different_chat_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second Chat device of the same user cannot confirm an Olm pairing initiated elsewhere."""
    runtime_paths = test_runtime_paths(tmp_path)
    devices = {
        "@alice:example.org": {
            "CHAT1": _olm_device("chat1-key"),
            "CHAT2": _olm_device("chat2-key"),
            "HELPER1": _olm_device("helper1-key"),
            "DELETED": _olm_device("deleted-key", deleted=True),
        },
    }
    receiver, sent = _olm_receiver(runtime_paths, monkeypatch, devices=devices)
    offer, verification = await _start_and_claim_olm_pairing(receiver, sent)
    assert offer.code is not None

    # Untrusted/deleted device is silently ignored.
    await receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="DELETED",
            ed25519="deleted-key",
            event_type=DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
            content=DesktopPairingConfirm(
                nonce="confirm-deleted-0123",
                token=offer.code,
                verification=verification,
                device_id="HELPER1",
                device_ed25519="helper1-key",
            ).to_content(),
        ),
    )
    assert sent == []

    # Second trusted device gets ok=False and credentials are not saved.
    await receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="CHAT2",
            ed25519="chat2-key",
            event_type=DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
            content=DesktopPairingConfirm(
                nonce="confirm-chat2-012345",
                token=offer.code,
                verification=verification,
                device_id="HELPER1",
                device_ed25519="helper1-key",
            ).to_content(),
        ),
    )
    target, event_type, content = sent.pop()
    assert target == PinnedMatrixDevice("@alice:example.org", "CHAT2", "chat2-key")
    assert event_type == DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE
    reply = DesktopPairingConfirmed.from_content(content)
    assert reply.ok is False
    assert reply.error is not None
    assert (
        load_desktop_credentials(
            get_runtime_credentials_manager(runtime_paths),
            requester_id="@alice:example.org",
            agent_name="computer",
        )
        is None
    )


@pytest.mark.asyncio
async def test_olm_pairing_confirm_rejects_mismatched_device_key_or_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Confirmation rejects mismatched device_id, device_ed25519, or verification without saving."""
    runtime_paths = test_runtime_paths(tmp_path)
    devices = {"@alice:example.org": {"CHAT1": _olm_device("chat1-key"), "HELPER1": _olm_device("helper1-key")}}
    receiver, sent = _olm_receiver(runtime_paths, monkeypatch, devices=devices)
    offer, verification = await _start_and_claim_olm_pairing(receiver, sent)
    assert offer.code is not None

    for bad_confirm in (
        DesktopPairingConfirm("nonce-bad-dev-012345", offer.code, verification, "OTHER_DEV", "helper1-key"),
        DesktopPairingConfirm("nonce-bad-key-012345", offer.code, verification, "HELPER1", "wrong-key"),
        DesktopPairingConfirm("nonce-bad-ver-012345", offer.code, "BADVERIFY", "HELPER1", "helper1-key"),
    ):
        await receiver.on_event(
            _olm_event(
                sender="@alice:example.org",
                device_id="CHAT1",
                ed25519="chat1-key",
                event_type=DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
                content=bad_confirm.to_content(),
            ),
        )
        _, event_type, content = sent.pop()
        assert event_type == DESKTOP_PAIRING_CONFIRMED_EVENT_TYPE
        reply = DesktopPairingConfirmed.from_content(content)
        assert reply.ok is False
        assert reply.error is not None

    assert (
        load_desktop_credentials(
            get_runtime_credentials_manager(runtime_paths),
            requester_id="@alice:example.org",
            agent_name="computer",
        )
        is None
    )


@pytest.mark.asyncio
async def test_olm_disconnect_deletes_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """disconnect_request.v1 deletes only the sender's scoped credentials and replies ok=True."""
    runtime_paths = test_runtime_paths(tmp_path)
    creds_manager = get_runtime_credentials_manager(runtime_paths)
    save_desktop_credentials(
        creds_manager,
        {"device_user_id": "@alice:example.org", "device_id": "ALICE", "device_ed25519": "alice-key"},
        requester_id="@alice:example.org",
        agent_name="computer",
    )
    save_desktop_credentials(
        creds_manager,
        {"device_user_id": "@bob:example.org", "device_id": "BOB", "device_ed25519": "bob-key"},
        requester_id="@bob:example.org",
        agent_name="computer",
    )
    devices = {"@alice:example.org": {"CHAT1": _olm_device("chat1-key")}}
    receiver, sent = _olm_receiver(runtime_paths, monkeypatch, devices=devices)

    await receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="CHAT1",
            ed25519="chat1-key",
            event_type=DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE,
            content=DesktopDisconnectRequest(nonce="disc-wrong-agent-123", agent_name="other").to_content(),
        ),
    )
    assert DesktopDisconnectConfirmed.from_content(sent.pop()[2]).ok is False
    assert load_desktop_credentials(creds_manager, requester_id="@alice:example.org", agent_name="computer") is not None

    await receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="CHAT1",
            ed25519="chat1-key",
            event_type=DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE,
            content=DesktopDisconnectRequest(nonce="disc-nonce-012345678", agent_name="computer").to_content(),
        ),
    )
    target, event_type, content = sent.pop()
    assert target == PinnedMatrixDevice("@alice:example.org", "CHAT1", "chat1-key")
    assert event_type == DESKTOP_DISCONNECT_CONFIRMED_EVENT_TYPE
    assert DesktopDisconnectConfirmed.from_content(content) == DesktopDisconnectConfirmed(
        nonce="disc-nonce-012345678",
        agent_name="computer",
        ok=True,
    )
    assert load_desktop_credentials(creds_manager, requester_id="@alice:example.org", agent_name="computer") is None
    assert load_desktop_credentials(creds_manager, requester_id="@bob:example.org", agent_name="computer") is not None


@pytest.mark.asyncio
async def test_slash_desktop_setup_named_agent_in_multi_agent_room_and_rejects_other_human(
    tmp_path: Path,
) -> None:
    """`/desktop setup <agent>` works in a multi-agent room with one human and refuses any second human."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "matrix_sync": {"mode": "classic"},
            "defaults": {"tools": []},
            "agents": {
                "dev": {"display_name": "Dev", "role": "Develop", "tools": ["desktop"]},
                "notes": {"display_name": "Notes", "role": "Notes", "tools": []},
            },
        },
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "mindroom_router", "dev": "mindroom_dev", "notes": "mindroom_notes"},
    )
    members = ["@mindroom_router:localhost", "@mindroom_dev:localhost", "@mindroom_notes:localhost", "@alice:localhost"]
    room = nio.MatrixRoom(room_id="!multi:localhost", own_user_id="@mindroom_dev:localhost")
    for user_id in members:
        room.add_member(user_id, None, None)
    client = AsyncMock()
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id, None, None) for user_id in members],
        room_id=room.room_id,
    )
    assert await ensure_room_membership_synced(client, room, sender_id="@alice:localhost")

    # Unnamed /desktop is ambiguous in a multi-agent room; under self_service both named concrete agents work.
    assert desktop_room_agent(config, runtime_paths, room, "@alice:localhost", args_text="setup") is None
    assert desktop_room_agent(config, runtime_paths, room, "@alice:localhost", args_text="setup notes") == "notes"

    explicit_config = config.model_copy(
        update={"defaults": config.defaults.model_copy(update={"desktop_pairing": "explicit_only"})},
    )
    disabled_config = config.model_copy(
        update={"defaults": config.defaults.model_copy(update={"desktop_pairing": "disabled"})},
    )
    assert desktop_room_agent(explicit_config, runtime_paths, room, "@alice:localhost", args_text="setup notes") is None
    assert desktop_room_agent(explicit_config, runtime_paths, room, "@alice:localhost", args_text="setup dev") == "dev"
    assert desktop_room_agent(disabled_config, runtime_paths, room, "@alice:localhost", args_text="setup dev") is None
    for args_text in (
        "setup dev",
        "status dev",
        "rotate dev",
        "disconnect dev",
        "disconnect dev confirm",
        "confirm dev tok ver",
    ):
        assert desktop_room_agent(config, runtime_paths, room, "@alice:localhost", args_text=args_text) == "dev"

    controller = SimpleNamespace(user_id="@mindroom_dev:localhost", device_id="CLOUD", ed25519="cloud-key")
    send_response = AsyncMock(return_value="$reply")
    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        logger=Mock(),
        conversation_reader=make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(room.room_id, None, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=send_response,
        responder_candidates_for_room=AsyncMock(return_value=[MatrixID.parse("@mindroom_dev:localhost")]),
        controller_identity=lambda _name: controller,
    )

    async def run_cmd(args_text: str) -> str:
        send_response.reset_mock()
        await handle_command(
            context=context,
            room=room,
            event=SimpleNamespace(
                sender="@alice:localhost",
                event_id="$event",
                body=f"!desktop {args_text}",
                source={"content": {"body": f"!desktop {args_text}"}},
            ),
            command=Command(type=CommandType.DESKTOP, args={"args_text": args_text}, raw_text=f"!desktop {args_text}"),
            requester_user_id="@alice:localhost",
        )
        return send_response.await_args.args[0]

    setup_reply = await run_cmd("setup dev")
    assert "Desktop pairing started" in setup_reply
    assert "/desktop confirm dev <code> <verification>" in setup_reply
    token_match = re.search(r"--code ([A-Za-z0-9_-]+)", setup_reply)
    assert token_match is not None
    token = token_match.group(1)
    claim_desktop_pairing(
        runtime_paths,
        token=token,
        agent_name="dev",
        device_user_id="@alice:localhost",
        device_id="ALICE",
        device_ed25519="alice-key",
    )
    verification = desktop_pairing_verification(token, "alice-key")
    assert "Desktop paired" in await run_cmd(f"confirm dev {token} {verification}")
    assert "Desktop is configured" in await run_cmd("status dev")
    assert "/desktop disconnect dev confirm" in await run_cmd("disconnect dev")
    assert "Desktop disconnected" in await run_cmd("disconnect dev confirm")

    # Adding a second human immediately causes /desktop setup dev to be refused.
    members_with_bob = [*members, "@bob:localhost"]
    room_with_bob = nio.MatrixRoom(room_id="!shared:localhost", own_user_id="@mindroom_dev:localhost")
    for user_id in members_with_bob:
        room_with_bob.add_member(user_id, None, None)
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[nio.RoomMember(user_id, None, None) for user_id in members_with_bob],
        room_id=room_with_bob.room_id,
    )
    assert await ensure_room_membership_synced(client, room_with_bob, sender_id="@alice:localhost")
    assert desktop_room_agent(config, runtime_paths, room_with_bob, "@alice:localhost", args_text="setup dev") is None


@pytest.mark.asyncio
async def test_self_service_registers_pairing_receiver_without_static_desktop_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """self_service registers pairing on concrete agents without desktop tool and honors live config reloads."""
    runtime_paths = test_runtime_paths(tmp_path)
    sent: list[tuple[PinnedMatrixDevice, str, dict[str, object]]] = []

    async def capture_send(
        _client: object,
        target: PinnedMatrixDevice,
        *,
        event_type: str,
        content: dict[str, object],
    ) -> None:
        sent.append((target, event_type, content))

    monkeypatch.setattr("mindroom.desktop.pairing_receiver.send_encrypted_to_device", capture_send)
    base_config = _desktop_config(runtime_paths)
    current_config = base_config.model_copy(
        update={"defaults": base_config.defaults.model_copy(update={"desktop_pairing": "disabled"})},
    )
    devices = {"@alice:example.org": {"CHAT1": _olm_device("chat1-key")}}
    client = SimpleNamespace(add_to_device_callback=Mock(), olm=SimpleNamespace(device_store=devices))

    register_desktop_pairing_receiver(
        current_config,
        client=client,  # type: ignore[arg-type]
        agent_name="notes",
        runtime_paths=runtime_paths,
        callback_wrapper=lambda callback: callback,
        config_getter=lambda: current_config,
    )
    callback, event_type = client.add_to_device_callback.call_args.args
    assert event_type is AuthenticatedToDeviceEvent

    request_event = _olm_event(
        sender="@alice:example.org",
        device_id="CHAT1",
        ed25519="chat1-key",
        event_type=DESKTOP_SETUP_REQUEST_EVENT_TYPE,
        content=DesktopSetupRequest(nonce="reload-nonce-0123456", agent_name="notes").to_content(),
    )
    await callback(request_event)
    offer = DesktopSetupOffer.from_content(sent.pop()[2])
    assert offer.eligible is False
    assert offer.error == "Desktop pairing is disabled by configuration."

    current_config = base_config
    await callback(request_event)
    offer = DesktopSetupOffer.from_content(sent.pop()[2])
    assert offer.eligible is True
    assert offer.code is not None


@pytest.mark.asyncio
async def test_setup_request_reports_ineligible_when_policy_disabled_or_explicit_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """setup_request.v1 returns eligible=False with clear errors for disabled or explicit_only without desktop."""
    runtime_paths = test_runtime_paths(tmp_path)
    base_config = _desktop_config(runtime_paths)
    devices = {"@alice:example.org": {"CHAT1": _olm_device("chat1-key"), "HELPER1": _olm_device("helper1-key")}}

    def with_policy(policy: str) -> Config:
        return base_config.model_copy(
            update={"defaults": base_config.defaults.model_copy(update={"desktop_pairing": policy})},
        )

    def setup_event(agent_name: str, nonce: str) -> AuthenticatedToDeviceEvent:
        return _olm_event(
            sender="@alice:example.org",
            device_id="CHAT1",
            ed25519="chat1-key",
            event_type=DESKTOP_SETUP_REQUEST_EVENT_TYPE,
            content=DesktopSetupRequest(nonce=nonce, agent_name=agent_name).to_content(),
        )

    disabled_receiver, sent = _olm_receiver(
        runtime_paths,
        monkeypatch,
        devices=devices,
        agent_name="computer",
        config=with_policy("disabled"),
    )
    await disabled_receiver.on_event(setup_event("computer", "disabled-nonce-01234"))
    offer = DesktopSetupOffer.from_content(sent.pop()[2])
    assert (offer.eligible, offer.error) == (False, "Desktop pairing is disabled by configuration.")

    pending = create_desktop_pairing(runtime_paths, requester_id="@alice:example.org", agent_name="computer")
    await disabled_receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="HELPER1",
            ed25519="helper1-key",
            event_type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
            content=DesktopPairingClaim(pending.token).to_content(),
        ),
    )
    assert sent == []

    explicit_config = with_policy("explicit_only")
    notes_receiver, sent = _olm_receiver(
        runtime_paths,
        monkeypatch,
        devices=devices,
        agent_name="notes",
        config=explicit_config,
    )
    await notes_receiver.on_event(setup_event("notes", "explicit-notes-01234"))
    notes_offer = DesktopSetupOffer.from_content(sent.pop()[2])
    assert (notes_offer.eligible, notes_offer.error) == (False, "Agent 'notes' does not declare the Desktop tool.")

    computer_receiver, sent = _olm_receiver(
        runtime_paths,
        monkeypatch,
        devices=devices,
        agent_name="computer",
        config=explicit_config,
    )
    await computer_receiver.on_event(setup_event("computer", "explicit-comp-012345"))
    comp_offer = DesktopSetupOffer.from_content(sent.pop()[2])
    assert comp_offer.eligible is True
    assert comp_offer.code is not None


@pytest.mark.asyncio
async def test_confirm_accepts_named_and_unnamed_forms_in_single_agent_room(tmp_path: Path) -> None:
    """In a 1-agent room, both `/desktop confirm <code> <ver>` and `/desktop confirm <agent> <code> <ver>` succeed."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _desktop_config(runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames={"computer": "mindroom_computer"})
    room = nio.MatrixRoom(room_id="!dm:localhost", own_user_id="@mindroom_computer:localhost")
    for user_id in ("@alice:localhost", "@mindroom_computer:localhost"):
        room.add_member(user_id, None, None)
    client = SimpleNamespace(
        user_id="@mindroom_computer:localhost",
        joined_members=AsyncMock(
            return_value=nio.JoinedMembersResponse(
                members=[
                    nio.RoomMember("@alice:localhost", None, None),
                    nio.RoomMember("@mindroom_computer:localhost", None, None),
                ],
                room_id=room.room_id,
            ),
        ),
    )
    assert await ensure_room_membership_synced(client, room, sender_id="@alice:localhost")

    for use_named in (False, True):
        pairing = create_desktop_pairing(runtime_paths, requester_id="@alice:localhost", agent_name="computer")
        claim_desktop_pairing(
            runtime_paths,
            token=pairing.token,
            agent_name="computer",
            device_user_id="@alice:localhost",
            device_id="DEV1",
            device_ed25519="dev1-key",
        )
        ver = desktop_pairing_verification(pairing.token, "dev1-key")
        args_text = f"confirm computer {pairing.token} {ver}" if use_named else f"confirm {pairing.token} {ver}"
        assert desktop_room_agent(config, runtime_paths, room, "@alice:localhost", args_text=args_text) == "computer"
        reply = handle_desktop_command(
            args_text,
            scope=DesktopCommandScope(
                config=config,
                runtime_paths=runtime_paths,
                agent_name="computer",
                requester_id="@alice:localhost",
                controller_identity=lambda _name: SimpleNamespace(
                    user_id="@mindroom_computer:localhost",
                    device_id="CLOUD",
                    ed25519="cloud-key",
                ),
            ),
        )
        assert "Desktop paired" in reply


@pytest.mark.asyncio
async def test_disconnect_succeeds_under_disabled_desktop_pairing_via_chat_and_olm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing pairings can still be disconnected via chat and Olm even when desktop_pairing == 'disabled'."""
    runtime_paths = test_runtime_paths(tmp_path)
    base_config = _desktop_config(runtime_paths)
    disabled_config = base_config.model_copy(
        update={"defaults": base_config.defaults.model_copy(update={"desktop_pairing": "disabled"})},
    )
    persist_entity_accounts(
        disabled_config,
        runtime_paths,
        usernames={"computer": "mindroom_computer", "notes": "mindroom_notes"},
    )
    creds_manager = get_runtime_credentials_manager(runtime_paths)
    saved_creds = {"device_user_id": "@alice:example.org", "device_id": "HELPER1", "device_ed25519": "helper1-key"}

    # 1. Chat /desktop disconnect in both 1-agent and multi-agent rooms succeeds under disabled policy.
    room = nio.MatrixRoom(room_id="!shared:localhost", own_user_id="@mindroom_computer:localhost")
    for user_id in ("@alice:example.org", "@mindroom_computer:localhost", "@mindroom_notes:localhost"):
        room.add_member(user_id, None, None)
    client = SimpleNamespace(
        user_id="@mindroom_computer:localhost",
        joined_members=AsyncMock(
            return_value=nio.JoinedMembersResponse(
                members=[
                    nio.RoomMember(u, None, None)
                    for u in ("@alice:example.org", "@mindroom_computer:localhost", "@mindroom_notes:localhost")
                ],
                room_id=room.room_id,
            ),
        ),
    )
    assert await ensure_room_membership_synced(client, room, sender_id="@alice:example.org")

    save_desktop_credentials(creds_manager, saved_creds, requester_id="@alice:example.org", agent_name="computer")
    assert (
        desktop_room_agent(disabled_config, runtime_paths, room, "@alice:example.org", args_text="setup computer")
        is None
    )
    assert (
        desktop_room_agent(
            disabled_config,
            runtime_paths,
            room,
            "@alice:example.org",
            args_text="disconnect computer confirm",
        )
        == "computer"
    )
    chat_reply = handle_desktop_command(
        "disconnect computer confirm",
        scope=DesktopCommandScope(
            config=disabled_config,
            runtime_paths=runtime_paths,
            agent_name="computer",
            requester_id="@alice:example.org",
            controller_identity=lambda _name: SimpleNamespace(
                user_id="@mindroom_computer:localhost",
                device_id="CLOUD",
                ed25519="cloud-key",
            ),
        ),
    )
    assert "Desktop disconnected" in chat_reply
    assert load_desktop_credentials(creds_manager, requester_id="@alice:example.org", agent_name="computer") is None

    # Room ownership check still rejects rooms with another human even for disconnect.
    room_with_bob = nio.MatrixRoom(room_id="!twohumans:localhost", own_user_id="@mindroom_computer:localhost")
    for user_id in ("@alice:example.org", "@bob:example.org", "@mindroom_computer:localhost"):
        room_with_bob.add_member(user_id, None, None)
    client.joined_members.return_value = nio.JoinedMembersResponse(
        members=[
            nio.RoomMember(u, None, None)
            for u in ("@alice:example.org", "@bob:example.org", "@mindroom_computer:localhost")
        ],
        room_id=room_with_bob.room_id,
    )
    assert await ensure_room_membership_synced(client, room_with_bob, sender_id="@alice:example.org")
    assert (
        desktop_room_agent(
            disabled_config,
            runtime_paths,
            room_with_bob,
            "@alice:example.org",
            args_text="disconnect confirm",
        )
        is None
    )

    # 2. Olm disconnect_request.v1 succeeds under disabled policy while setup_request.v1 stays rejected.
    save_desktop_credentials(creds_manager, saved_creds, requester_id="@alice:example.org", agent_name="computer")
    devices = {"@alice:example.org": {"CHAT1": _olm_device("chat1-key")}}
    receiver, sent = _olm_receiver(
        runtime_paths,
        monkeypatch,
        devices=devices,
        agent_name="computer",
        config=disabled_config,
    )
    await receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="CHAT1",
            ed25519="chat1-key",
            event_type=DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE,
            content=DesktopDisconnectRequest(nonce="disc-disabled-01234", agent_name="computer").to_content(),
        ),
    )
    _, event_type, content = sent.pop()
    assert event_type == DESKTOP_DISCONNECT_CONFIRMED_EVENT_TYPE
    assert DesktopDisconnectConfirmed.from_content(content) == DesktopDisconnectConfirmed(
        nonce="disc-disabled-01234",
        agent_name="computer",
        ok=True,
    )
    assert load_desktop_credentials(creds_manager, requester_id="@alice:example.org", agent_name="computer") is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("enforce_turn_authorization")
async def test_olm_pairing_receiver_rejects_unauthorized_senders_on_all_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unauthorized senders are dropped silently on setup, claim, confirm, and disconnect without leaking metadata."""
    runtime_paths = replace(
        test_runtime_paths(tmp_path),
        process_env={
            "MATRIX_HOMESERVER": "http://localhost:8008",
            "MINDROOM_DESKTOP_MATRIX_HOMESERVER": "https://secret.example.org",
            "MINDROOM_DESKTOP_CLOUDFLARE_ACCESS": "true",
        },
    )
    config = Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "defaults": {"tools": []},
            "agents": {
                "computer": {
                    "display_name": "Computer",
                    "role": "Operate apps",
                    "tools": ["desktop"],
                    "access": {"users": ["@alice:example.org"]},
                },
            },
        },
        runtime_paths,
    )
    devices = {
        "@alice:example.org": {"CHAT_A": _olm_device("chat-a-key"), "HELP_A": _olm_device("help-a-key")},
        "@mallory:example.org": {"CHAT_M": _olm_device("chat-m-key"), "HELP_M": _olm_device("help-m-key")},
    }
    receiver, sent = _olm_receiver(runtime_paths, monkeypatch, devices=devices, agent_name="computer", config=config)

    # Pre-create a claimed pairing and saved credentials for mallory to prove none can be mutated or confirmed.
    pending = create_desktop_pairing(
        runtime_paths,
        requester_id="@mallory:example.org",
        agent_name="computer",
        initiator_device_id="CHAT_M",
        initiator_ed25519="chat-m-key",
    )
    creds_manager = get_runtime_credentials_manager(runtime_paths)
    existing_creds = {"device_user_id": "@mallory:example.org", "device_id": "OLD", "device_ed25519": "old-key"}
    save_desktop_credentials(creds_manager, existing_creds, requester_id="@mallory:example.org", agent_name="computer")

    # 1. setup_request.v1 from unauthorized sender -> silently dropped, no homeserver/cloudflare_access leaked.
    await receiver.on_event(
        _olm_event(
            sender="@mallory:example.org",
            device_id="CHAT_M",
            ed25519="chat-m-key",
            event_type=DESKTOP_SETUP_REQUEST_EVENT_TYPE,
            content=DesktopSetupRequest(nonce="unauth-setup-01234", agent_name="computer").to_content(),
        ),
    )
    assert sent == []

    # 2. pairing_claim.v1 from unauthorized sender -> silently dropped, pairing remains unclaimed.
    await receiver.on_event(
        _olm_event(
            sender="@mallory:example.org",
            device_id="HELP_M",
            ed25519="help-m-key",
            event_type=DESKTOP_PAIRING_CLAIM_EVENT_TYPE,
            content=DesktopPairingClaim(pending.token).to_content(),
        ),
    )
    assert sent == []

    # Claim in DB directly so confirm would otherwise succeed if authorization weren't checked.
    claim_desktop_pairing(
        runtime_paths,
        token=pending.token,
        agent_name="computer",
        device_user_id="@mallory:example.org",
        device_id="HELP_M",
        device_ed25519="help-m-key",
    )
    ver = desktop_pairing_verification(pending.token, "help-m-key")

    # 3. pairing_confirm.v1 from unauthorized sender -> silently dropped, credentials untouched.
    await receiver.on_event(
        _olm_event(
            sender="@mallory:example.org",
            device_id="CHAT_M",
            ed25519="chat-m-key",
            event_type=DESKTOP_PAIRING_CONFIRM_EVENT_TYPE,
            content=DesktopPairingConfirm(
                nonce="unauth-confirm-012",
                token=pending.token,
                verification=ver,
                device_id="HELP_M",
                device_ed25519="help-m-key",
            ).to_content(),
        ),
    )
    assert sent == []
    assert (
        load_desktop_credentials(creds_manager, requester_id="@mallory:example.org", agent_name="computer")
        == existing_creds
    )

    # 4. disconnect_request.v1 from unauthorized sender -> silently dropped, credentials untouched.
    await receiver.on_event(
        _olm_event(
            sender="@mallory:example.org",
            device_id="CHAT_M",
            ed25519="chat-m-key",
            event_type=DESKTOP_DISCONNECT_REQUEST_EVENT_TYPE,
            content=DesktopDisconnectRequest(nonce="unauth-disc-012345", agent_name="computer").to_content(),
        ),
    )
    assert sent == []
    assert (
        load_desktop_credentials(creds_manager, requester_id="@mallory:example.org", agent_name="computer")
        == existing_creds
    )

    # Authorized sender (@alice:example.org) still succeeds end-to-end!
    await receiver.on_event(
        _olm_event(
            sender="@alice:example.org",
            device_id="CHAT_A",
            ed25519="chat-a-key",
            event_type=DESKTOP_SETUP_REQUEST_EVENT_TYPE,
            content=DesktopSetupRequest(nonce="auth-setup-0123456", agent_name="computer").to_content(),
        ),
    )
    assert len(sent) == 1
    offer = DesktopSetupOffer.from_content(sent.pop()[2])
    assert offer.eligible is True
    assert offer.homeserver == "https://secret.example.org"
