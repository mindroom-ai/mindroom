"""Whose Matrix account a personal agent may use: its owner's, when the call runs for the owner outside a contact room.

The person is read from the running call alone, never from an argument, so nothing the model writes can name someone
else. What started the turn does not count: a refused turn could schedule a task or start a background job whose turn
passes any check on its origin, so only who the call runs for and its room decide.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.contacts.turns import contact_room
from mindroom.entity_resolution import MissingManagedEntityAccountError, current_entity_id
from mindroom.requester_identity import is_human_requester_id, resolve_human_requester_alias
from mindroom.tool_system.runtime_context import current_execution_identity, get_tool_runtime_context

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

__all__ = ["MatrixUserActor", "MatrixUserRefused", "matrix_user_actor"]


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
    """Return who the running call acts as: the owner of its personal agent, when the call runs for the owner.

    Only a chat turn or an MCP gateway call qualifies, and a chat turn not in one of the agent's contact rooms.
    """
    identity = current_execution_identity()
    context = get_tool_runtime_context()
    # A matrix identity bound without its turn's context has no room to check.
    if (
        identity is None
        or identity.channel not in {"matrix", "mcp"}
        or (identity.channel == "matrix" and context is None)
    ):
        msg = "matrix_user works only in a chat turn or through the MCP gateway."
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
    if context is not None and contact_room(config, runtime_paths, context.room_id, identity.agent_name) is not None:
        msg = "matrix_user is not available in a contact conversation."
        raise MatrixUserRefused(msg)
    try:
        agent_id = current_entity_id(identity.agent_name, runtime_paths)
    except MissingManagedEntityAccountError:
        msg = "matrix_user is not available: this agent has no Matrix account yet."
        raise MatrixUserRefused(msg) from None
    return MatrixUserActor(
        agent_name=identity.agent_name,
        agent_user_id=agent_id.full_id,
        person_user_id=instance.user_id,
    )
