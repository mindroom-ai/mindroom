"""Whose Matrix account a personal agent may use: its owner's, and only when the owner asks it directly.

The person is read from the running call's turn alone, never from an argument, so nothing the model writes can name
someone else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.contacts.turns import contact_room
from mindroom.dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    EDIT_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    INTERACTIVE_SELECTION_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    SILENT_SCHEDULE_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.entity_resolution import current_entity_id
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias
from mindroom.tool_system.runtime_context import current_execution_identity, get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = ["MatrixUserActor", "MatrixUserRefused", "matrix_user_actor"]

# The turns the requester started: their own message in any form, their answer to a prompt, and a schedule they set.
# Every other kind came from a hook, a trigger, or a relay, and a context built without an inbound message has none.
_DIRECT_SOURCE_KINDS = frozenset(
    {
        MESSAGE_SOURCE_KIND,
        VOICE_SOURCE_KIND,
        IMAGE_SOURCE_KIND,
        MEDIA_SOURCE_KIND,
        EDIT_SOURCE_KIND,
        ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
        INTERACTIVE_SELECTION_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        SILENT_SCHEDULE_SOURCE_KIND,
    },
)


@dataclass(frozen=True, slots=True)
class MatrixUserActor:
    """The personal agent making the call, and the owner whose account it acts as."""

    agent_name: str
    # The agent's recorded Matrix ID.
    agent_user_id: str
    # The personal agent's owner.
    person_user_id: str


class MatrixUserRefused(Exception):  # noqa: N818 - it names a refusal, not a fault.
    """Why the running call may not act as its owner; the message is what the model reads."""


def matrix_user_actor(config: Config, runtime_paths: RuntimePaths) -> MatrixUserActor:
    """Return who the running call acts as: the owner of its personal agent, when the owner asked it directly.

    Only the owner's own chat turn with the agent, or the owner's MCP gateway call to it, qualifies.
    """
    identity = current_execution_identity()
    if identity is None or identity.channel not in {"matrix", "mcp"}:
        msg = "matrix_user works only in a chat turn or through the MCP gateway."
        raise MatrixUserRefused(msg)
    if identity.recipient != identity.agent_name:
        msg = "matrix_user is not available to a team."
        raise MatrixUserRefused(msg)
    instance = config.personal_agent_instance(identity.agent_name)
    if instance is None:
        msg = "matrix_user works only for a personal agent."
        raise MatrixUserRefused(msg)
    requester_id = identity.requester_id
    if not (
        requester_id is not None
        and is_human_requester_id(requester_id, config, runtime_paths)
        and resolve_human_requester_alias(requester_id, config, runtime_paths) == instance.user_id
    ):
        msg = "Only the agent's owner may use matrix_user."
        raise MatrixUserRefused(msg)
    context = get_tool_runtime_context()
    if context is not None and (
        context.source_kind not in _DIRECT_SOURCE_KINDS
        or context.acts_for_requester
        or contact_room(config, runtime_paths, context.room_id, identity.agent_name) is not None
    ):
        msg = (
            "matrix_user works only when the owner asks directly: "
            "not from a hook, a trigger, a contact, or another agent's message."
        )
        raise MatrixUserRefused(msg)
    return MatrixUserActor(
        agent_name=identity.agent_name,
        agent_user_id=current_entity_id(identity.agent_name, runtime_paths).full_id,
        person_user_id=instance.user_id,
    )
