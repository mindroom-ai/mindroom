"""Opt-in contacts: approved relationships between a personal agent and other agents or people."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.config.judgment import JudgmentConfig
from mindroom.config.schema_hints import dashboard_hint

# Tools that act with the owner's authority beyond the conversation, so a contact turn never offers them.
CONTACT_CONTROL_TOOLS = frozenset(
    {
        "contacts",
        "delegate",
        "agent_manager",
        "self_config",
        "config_manager",
        "dynamic_workflow",
        "claude_agent",
        "skill_manage",
        "dynamic_tools",
    },
)


class ContactsConfig(BaseModel):
    """Contacts settings of one agent: the rules, default tools, and judge of every contact it has."""

    model_config = ConfigDict(extra="forbid")

    rules: list[str] = Field(
        default_factory=list,
        description="Rules that apply to every contact of this agent, ahead of each owner's own rules",
    )
    tools: list[str] = Field(
        default_factory=list,
        description="Default tools offered in contact turns when the owner chose none for that contact",
        json_schema_extra=dashboard_hint(reference="tool"),
    )
    judge: JudgmentConfig | None = Field(
        default=None,
        description="Judgment backend that checks each message against the rules; unset runs no judge",
    )

    @field_validator("tools")
    @classmethod
    def refuse_control_tools(cls, tools: list[str]) -> list[str]:
        """Keep the tools that control MindRoom out of contact turns."""
        for name in tools:
            if name in CONTACT_CONTROL_TOOLS:
                msg = f"contacts.tools may not include {name}"
                raise ValueError(msg)
        return tools
