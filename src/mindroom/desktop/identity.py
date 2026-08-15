"""Cloud controller identity lookup for desktop-device pinning."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class DesktopIdentityError(RuntimeError):
    """A configured MindRoom entity has no pinnable Matrix device identity."""


@dataclass(frozen=True, slots=True)
class DesktopControllerIdentity:
    """Public Matrix identity fields copied to the local desktop bridge."""

    entity_name: str
    user_id: str
    device_id: str
    ed25519: str


class _AgentUserIdentity(Protocol):
    @property
    def user_id(self) -> str: ...

    @property
    def device_id(self) -> str | None: ...


class _OlmAccountIdentity(Protocol):
    @property
    def identity_keys(self) -> dict[str, str]: ...


class _OlmIdentity(Protocol):
    @property
    def account(self) -> _OlmAccountIdentity: ...


class _MatrixClientIdentity(Protocol):
    @property
    def user_id(self) -> str: ...

    @property
    def device_id(self) -> str | None: ...

    @property
    def olm(self) -> _OlmIdentity | None: ...


class _LiveBotIdentity(Protocol):
    @property
    def running(self) -> bool: ...

    @property
    def agent_name(self) -> str: ...

    @property
    def agent_user(self) -> _AgentUserIdentity: ...

    @property
    def client(self) -> _MatrixClientIdentity | None: ...


def controller_identity_for_live_bot(
    entity_name: str,
    bot: _LiveBotIdentity | None,
) -> DesktopControllerIdentity:
    """Resolve one pin from the exact running bot that already owns its store."""
    if type(entity_name) is not str or not entity_name:
        msg = "Desktop controller entity name is invalid."
        raise DesktopIdentityError(msg)
    if bot is None or bot.running is not True:
        msg = f"MindRoom entity {entity_name!r} is not running."
        raise DesktopIdentityError(msg)
    if bot.agent_name != entity_name:
        msg = f"MindRoom entity {entity_name!r} does not match the live bot registry."
        raise DesktopIdentityError(msg)

    agent_user = bot.agent_user
    client = bot.client
    if client is None:
        msg = f"MindRoom entity {entity_name!r} has a mismatched live Matrix device."
        raise DesktopIdentityError(msg)
    expected_user_id = agent_user.user_id
    expected_device_id = agent_user.device_id
    user_id = client.user_id
    device_id = client.device_id
    if (
        type(expected_user_id) is not str
        or not expected_user_id
        or type(expected_device_id) is not str
        or not expected_device_id
        or type(user_id) is not str
        or not user_id
        or type(device_id) is not str
        or not device_id
        or user_id != expected_user_id
        or device_id != expected_device_id
    ):
        msg = f"MindRoom entity {entity_name!r} has a mismatched live Matrix device."
        raise DesktopIdentityError(msg)

    olm = client.olm
    account = olm.account if olm is not None else None
    identity_keys = account.identity_keys if account is not None else None
    fingerprint = identity_keys.get("ed25519") if type(identity_keys) is dict else None
    if type(fingerprint) is not str or not fingerprint:
        msg = f"MindRoom entity {entity_name!r} has no live Ed25519 device identity."
        raise DesktopIdentityError(msg)
    return DesktopControllerIdentity(
        entity_name=entity_name,
        user_id=user_id,
        device_id=device_id,
        ed25519=fingerprint,
    )


__all__ = [
    "DesktopControllerIdentity",
    "DesktopIdentityError",
    "controller_identity_for_live_bot",
]
