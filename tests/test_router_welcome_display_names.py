"""Router welcome messages use display names, not entity config keys or Matrix IDs."""

from pathlib import Path
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.commands.handler import generate_welcome_message_for_room
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from tests.authorization_helpers import isolated_membership_index
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts


def _synced_client() -> AsyncMock:
    """A Matrix client whose member lookup reports an empty, already-synced room."""
    client = AsyncMock(spec=nio.AsyncClient)
    client.joined_members = AsyncMock(return_value=nio.JoinedMembersResponse(members=[], room_id="!room:localhost"))
    return client


@pytest.mark.asyncio
async def test_router_welcome_lists_agents_by_display_name_not_mxid(tmp_path: Path) -> None:
    """Router welcome must show display names (e.g., 'Mind'), not raw Matrix IDs.

    The router welcome is sent with skip_mentions=True, so the `@{entity_name}` template
    previously resolved to the full Matrix ID in the plain body (e.g., @mindroom_mind_<ns>:server).
    Now it shows the clean display name without @ prefix since mentions are skipped anyway.
    """
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id="@mindroom_router:localhost")
    room.add_member("@actual_mind:localhost", "Mind", None)
    room.members_synced = True

    config = Config(
        agents={
            "mind": AgentConfig(
                display_name="Mind",
                role="Personal assistant",
            ),
        },
    )
    runtime_paths = test_runtime_paths(tmp_path)
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "mindroom_router", "mind": "actual_mind"},
    )

    welcome_message = await generate_welcome_message_for_room(
        _synced_client(),
        room,
        "@alice:localhost",
        config,
        runtime_paths,
        isolated_membership_index(),
    )

    # Should show display name "Mind", not "@mind" or "@actual_mind:localhost"
    assert "• **Mind**: Personal assistant" in welcome_message
    # Should NOT show the config key with @ prefix
    assert "• **@mind**" not in welcome_message
    # Should NOT show the Matrix ID
    assert "@actual_mind:localhost" not in welcome_message
    assert "@mindroom_mind" not in welcome_message
