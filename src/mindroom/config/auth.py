"""Authorization configuration models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.config.validation import duplicate_items


class AgentReplyPermission(BaseModel):
    """Static users and managed-room memberships that may use one entity."""

    model_config = ConfigDict(extra="forbid")

    users: list[str] = Field(
        default_factory=list,
        description="Canonical Matrix user IDs or glob patterns allowed to use the entity.",
    )
    joined_rooms: list[str] = Field(
        default_factory=list,
        description="Managed room keys whose joined members may use the entity.",
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_list_shorthand(cls, value: object) -> object:
        """Normalize the legacy user-list syntax into a structured policy."""
        if isinstance(value, list):
            return {"users": value}
        return value

    @field_validator("joined_rooms")
    @classmethod
    def validate_unique_joined_rooms(cls, room_keys: list[str]) -> list[str]:
        """Reject duplicate managed-room grants within one policy."""
        duplicates = duplicate_items(room_keys)
        if duplicates:
            msg = f"Duplicate joined_rooms are not allowed: {', '.join(duplicates)}"
            raise ValueError(msg)
        return room_keys


class AuthorizationConfig(BaseModel):
    """Authorization configuration with fine-grained permissions."""

    global_users: list[str] = Field(
        default_factory=list,
        description="Users with access to all rooms (e.g., '@user:example.com')",
    )
    room_permissions: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Room-specific user permissions. Keys may be room IDs ('!room:example.com'), "
            "full aliases ('#room:example.com'), or managed room keys ('room')"
        ),
    )
    default_room_access: bool = Field(
        default=False,
        description="Default permission for rooms not explicitly configured",
    )
    config_command_enabled: bool = Field(
        default=False,
        description="Enable the chat !config command for global admin users.",
    )
    aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Map canonical Matrix user IDs to bridge aliases. "
            "A message from any alias is treated as if sent by the canonical user. "
            "E.g., {'@alice:example.com': ['@telegram_123:example.com']}"
        ),
    )
    agent_reply_permissions: dict[str, AgentReplyPermission] = Field(
        default_factory=dict,
        description=(
            "Per-agent reply policies keyed by agent/team name. "
            "A '*' key applies to all entities without an explicit override. "
            "Values may use the legacy user-list shorthand or structured users and joined_rooms. "
            "A '*' user entry allows all senders for that entity."
        ),
    )

    @field_validator("aliases")
    @classmethod
    def validate_unique_aliases(cls, aliases: dict[str, list[str]]) -> dict[str, list[str]]:
        """Ensure each alias is assigned to at most one canonical user."""
        duplicates = duplicate_items([alias for alias_list in aliases.values() for alias in alias_list])
        if duplicates:
            msg = f"Duplicate bridge aliases are not allowed: {', '.join(duplicates)}"
            raise ValueError(msg)
        return aliases

    def resolve_alias(self, sender_id: str) -> str:
        """Return the canonical user ID for a bridge alias, or the sender_id itself."""
        for canonical, alias_list in self.aliases.items():
            if sender_id in alias_list:
                return canonical
        return sender_id

    def agent_reply_policy(self, entity_name: str) -> AgentReplyPermission | None:
        """Return the explicit entity policy or wildcard fallback."""
        policy = self.agent_reply_permissions.get(entity_name)
        if policy is not None:
            return policy
        return self.agent_reply_permissions.get("*")
