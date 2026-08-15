"""Authorization invariants for long-lived tool runtime contexts."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.custom_tools.attachment_helpers import room_access_allowed
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolRuntimeContext
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_relation_lookup, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path, *, allowed: bool) -> Config:
    user_id = "@alice:example.org"
    return bind_runtime_paths(
        Config(
            authorization=AuthorizationConfig(
                default_room_access=False,
                room_permissions={"!other:example.org": [user_id] if allowed else []},
            ),
        ),
        test_runtime_paths(tmp_path),
    )


def test_cross_room_tool_access_uses_current_authorization(tmp_path: Path) -> None:
    """Cross-room tools must not retain room access revoked after context construction."""
    old_config = _config(tmp_path, allowed=True)
    current_config = _config(tmp_path, allowed=False)
    context = ToolRuntimeContext(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!current:example.org",
            thread_id=None,
            reply_to_event_id=None,
        ),
        requester_id="@alice:example.org",
        client=AsyncMock(),
        config=old_config,
        runtime_paths=test_runtime_paths(tmp_path),
        conversation_reader=make_conversation_reader_mock(),
        relations=make_relation_lookup(),
        agent_reply_memberships=AgentReplyMembershipIndex(),
        config_provider=lambda: current_config,
    )

    assert not room_access_allowed(context, "!other:example.org")
