"""The bot forwards journal room activity to the orchestrator-owned observer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code")},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )


def test_bot_forwards_room_activity_observer_to_dispatcher(tmp_path: Path) -> None:
    """Bot forwards room activity observer to dispatcher."""
    seen: list[str] = []
    config = _config(tmp_path)
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="code",
            password=TEST_PASSWORD,
            display_name="Code",
            user_id="@mindroom_code:localhost",
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["!room:localhost"],
        agent_reply_memberships=AgentReplyMembershipIndex(),
        room_activity_observer=seen.append,
    )

    bot._journal_dispatcher._ingress.on_room_activity("!room:localhost")

    assert seen == ["!room:localhost"]
