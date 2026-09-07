"""Test that responder authorization observes live config updates."""

from pathlib import Path

from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import is_sender_allowed_for_responder
from mindroom.config.access import ResponderAccessConfig
from tests.access_schema_support import membership_config
from tests.conftest import runtime_paths_for


def test_authorization_check_uses_updated_config(tmp_path: Path) -> None:
    """Replacing an authored responder policy must take effect immediately."""
    config = membership_config(tmp_path, access={"users": ["@alice:example.com"]})
    runtime_paths = runtime_paths_for(config)
    memberships = AgentReplyMembershipIndex()

    def allowed(sender_id: str) -> bool:
        return is_sender_allowed_for_responder(
            sender_id,
            "talent",
            "!test:example.com",
            config,
            runtime_paths,
            memberships,
        )

    assert allowed("@alice:example.com")
    assert not allowed("@bob:example.com")

    config.agents["talent"].access = ResponderAccessConfig(
        users=["@alice:example.com", "@bob:example.com"],
    )

    assert allowed("@alice:example.com")
    assert allowed("@bob:example.com")
