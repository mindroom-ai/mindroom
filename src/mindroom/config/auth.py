"""Authorization configuration models."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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
    aliases: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Map canonical Matrix user IDs to bridge aliases. "
            "A message from any alias is treated as if sent by the canonical user. "
            "E.g., {'@alice:example.com': ['@telegram_123:example.com']}"
        ),
    )
    agent_reply_permissions: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Per-agent reply allowlists keyed by agent/team name. "
            "A '*' key applies to all entities without an explicit override. "
            "A '*' user entry allows all senders for that entity. "
            "When set for an entity, it only replies to these user IDs "
            "(after alias resolution)."
        ),
    )

    @field_validator("aliases")
    @classmethod
    def validate_unique_aliases(cls, aliases: dict[str, list[str]]) -> dict[str, list[str]]:
        """Ensure each alias is assigned to at most one canonical user."""
        seen_aliases: set[str] = set()
        duplicates: list[str] = []
        for alias_list in aliases.values():
            for alias in alias_list:
                if alias in seen_aliases and alias not in duplicates:
                    duplicates.append(alias)
                seen_aliases.add(alias)

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
