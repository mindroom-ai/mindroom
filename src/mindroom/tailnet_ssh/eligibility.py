"""Roomless authorization for worker SSH admission."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.authorization import ReplyMembershipPendingError, is_sender_allowed_for_responder

if TYPE_CHECKING:
    from mindroom.agent_reply_membership import AgentReplyMembershipIndex
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths


def shell_authorized(
    requester: str,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    membership_index: AgentReplyMembershipIndex,
) -> bool:
    """Ask the shell's responder policy without a conversation room; unresolved grants fail closed."""
    try:
        return is_sender_allowed_for_responder(
            requester,
            agent_name,
            None,
            config,
            runtime_paths,
            membership_index,
            require_resolved_membership=True,
        )
    except ReplyMembershipPendingError:
        return False
