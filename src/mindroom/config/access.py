"""Membership-based access configuration models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from mindroom.config.validation import duplicate_items
from mindroom.constants import OWNER_MATRIX_USER_ID_PLACEHOLDER
from mindroom.matrix_identifiers import split_concrete_matrix_user_ids

RoomJoinPolicy = Literal["invite", "knock", "public"]


def _validate_unique_entries(values: list[str], *, field_name: str) -> list[str]:
    duplicates = duplicate_items(values)
    if duplicates:
        msg = f"Duplicate {field_name} are not allowed: {', '.join(duplicates)}"
        raise ValueError(msg)
    return values


def _validate_invite_acceptance_policy(value: bool | list[str]) -> bool | list[str]:
    """Reject duplicate inviter patterns while preserving boolean policies."""
    if isinstance(value, list):
        _validate_unique_entries(value, field_name="accept_invites")
    return value


InviteAcceptancePolicy = Annotated[bool | list[str], AfterValidator(_validate_invite_acceptance_policy)]


def validate_concrete_matrix_user_ids(
    values: list[str],
    *,
    field_name: str,
    allowed_placeholders: frozenset[str] = frozenset(),
) -> list[str]:
    """Validate exact authority identities plus any explicitly inert onboarding placeholders."""
    _validate_unique_entries(values, field_name=field_name)
    _, invalid = split_concrete_matrix_user_ids(value for value in values if value not in allowed_placeholders)
    if invalid:
        msg = f"{field_name} must contain concrete Matrix user IDs: {', '.join(invalid)}"
        raise ValueError(msg)
    return values


class ResponderAccessConfig(BaseModel):
    """Authored conversation-access rules for one responder."""

    model_config = ConfigDict(extra="forbid")

    current_room_members: bool | None = None
    members_of_rooms: list[str] | None = None
    users: list[str] = Field(default_factory=list)

    @field_validator("members_of_rooms", "users")
    @classmethod
    def validate_unique_entries(cls, values: list[str] | None, info: ValidationInfo) -> list[str] | None:
        """Reject duplicate grants so authored intent has one interpretation."""
        if values is None:
            return None
        assert info.field_name is not None
        return _validate_unique_entries(values, field_name=info.field_name)


class RoomDefaultsConfig(BaseModel):
    """Desired Matrix state inherited by every managed room."""

    model_config = ConfigDict(extra="forbid")

    join_policy: RoomJoinPolicy = "invite"
    listed: bool = False
    encrypted: bool = False
    invite_users: list[str] = Field(default_factory=list)
    admins: list[str] = Field(default_factory=list)

    @field_validator("invite_users", "admins")
    @classmethod
    def validate_unique_entries(cls, values: list[str], info: ValidationInfo) -> list[str]:
        """Require unique, concrete room-policy identities."""
        assert info.field_name is not None
        return validate_concrete_matrix_user_ids(
            values,
            field_name=info.field_name,
            allowed_placeholders=frozenset({OWNER_MATRIX_USER_ID_PLACEHOLDER}),
        )
