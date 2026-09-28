"""Tests for canonical human participation in thread history."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom import thread_utils
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.entity_resolution import entity_identity_registry
from mindroom.thread_utils import check_agent_mentioned, has_multiple_non_agent_users_in_thread
from tests.conftest import bind_runtime_paths, make_visible_message, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Create persisted identities and two humans with bridge aliases."""
    result = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper")},
            bot_accounts=["@bridgebot:localhost"],
            mindroom_user=MindRoomUserConfig(username="internal_account"),
        ),
        test_runtime_paths(tmp_path),
    )
    result.authorization.aliases = {
        "@alice:localhost": ["@bridge_alice:localhost", "@other_alice:localhost"],
        "@bob:localhost": ["@bridge_bob:localhost"],
    }
    return result


@pytest.mark.parametrize(
    ("history_senders", "current_sender_id", "expected"),
    [
        (["@bridge_alice:localhost"], "@alice:localhost", False),
        (["@alice:localhost"], "@bridge_alice:localhost", False),
        (["@bridge_alice:localhost"], "@other_alice:localhost", False),
        (["@alice:localhost", "@bridge_alice:localhost", "@other_alice:localhost"], None, False),
        (["@bridge_alice:localhost"], "@bob:localhost", True),
        (["@bridge_alice:localhost", "@bridge_bob:localhost"], None, True),
        ([], "@bridge_alice:localhost", False),
    ],
)
def test_thread_counts_unique_human_identities(
    config: Config,
    history_senders: list[str],
    current_sender_id: str | None,
    *,
    expected: bool,
) -> None:
    """A human's bridge aliases count once without rewriting message provenance."""
    history = [make_visible_message(sender=sender, body="hello") for sender in history_senders]

    assert (
        has_multiple_non_agent_users_in_thread(
            history,
            config,
            runtime_paths_for(config),
            current_sender_id=current_sender_id,
        )
        is expected
    )
    assert [message.sender for message in history] == history_senders


@pytest.mark.parametrize(
    "nonhuman_sender",
    ["@bridgebot:localhost", "@mindroom_helper:localhost", "@mindroom_router:localhost", "@internal_account:localhost"],
)
@pytest.mark.parametrize("as_current_sender", [False, True])
def test_nonhuman_aliases_do_not_count_as_human_participants(
    config: Config,
    nonhuman_sender: str,
    *,
    as_current_sender: bool,
) -> None:
    """Bot and managed agent aliases cannot create a second human identity."""
    config.authorization.aliases = {"@alice:localhost": [nonhuman_sender]}
    history_sender = "@bob:localhost" if as_current_sender else nonhuman_sender
    current_sender = nonhuman_sender if as_current_sender else "@bob:localhost"

    assert not has_multiple_non_agent_users_in_thread(
        [make_visible_message(sender=history_sender)],
        config,
        runtime_paths_for(config),
        current_sender_id=current_sender,
    )


@pytest.mark.parametrize("canonical_id", ["@bridgebot:localhost", "@mindroom_helper:localhost"])
def test_human_aliases_cannot_merge_into_nonhuman_identity(config: Config, canonical_id: str) -> None:
    """An invalid alias target cannot hide two distinct human participants."""
    config.authorization.aliases = {canonical_id: ["@alice:localhost", "@bob:localhost"]}

    assert has_multiple_non_agent_users_in_thread(
        [make_visible_message(sender="@alice:localhost"), make_visible_message(sender="@bob:localhost")],
        config,
        runtime_paths_for(config),
    )


@pytest.mark.parametrize("sender", ["@internal_account:localhost", "@renamed_internal:localhost"])
def test_internal_account_excludes_persisted_and_configured_identity(config: Config, sender: str) -> None:
    """Internal identity remains nonhuman when configuration differs from persisted account."""
    config.mindroom_user = MindRoomUserConfig(username="renamed_internal")
    assert not has_multiple_non_agent_users_in_thread(
        [make_visible_message(sender=sender)],
        config,
        runtime_paths_for(config),
        current_sender_id="@alice:localhost",
    )


def test_check_agent_mentioned_resolves_many_distinct_mentions_with_one_registry(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thousands of distinct explicit mentions build the entity registry once and keep non-agent detection."""
    runtime_paths = runtime_paths_for(config)
    helper_id = entity_identity_registry(config, runtime_paths).current_id("helper")
    registry_builds = 0

    def counting_registry(*args: object, **kwargs: object) -> object:
        nonlocal registry_builds
        registry_builds += 1
        return entity_identity_registry(*args, **kwargs)

    monkeypatch.setattr(thread_utils, "entity_identity_registry", counting_registry)
    user_ids = [f"@user{index}:localhost" for index in range(5_000)]
    event_source = {
        "content": {
            "msgtype": "m.text",
            "body": "hello",
            "m.mentions": {"user_ids": [*user_ids, helper_id.full_id, "@bridgebot:localhost", *user_ids]},
        },
    }
    room = nio.MatrixRoom("!room:localhost", "@mindroom_helper:localhost")
    room.members_synced = True
    room.add_member("@user4999:localhost", None, None)

    mentioned_agents, am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
        event_source,
        helper_id,
        config,
        runtime_paths,
        room=room,
    )

    assert mentioned_agents == [helper_id]
    assert am_i_mentioned is True
    assert has_non_agent_mentions is True
    assert registry_builds == 1


def test_thread_mention_planning_scans_each_visible_revision_once(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Planning a later turn over the same history reuses each revision's mentions instead of rescanning its text."""
    runtime_paths = runtime_paths_for(config)
    helper_id = entity_identity_registry(config, runtime_paths).current_id("helper")
    scanned: list[str] = []
    original_scan = thread_utils.resolve_mentioned_user_ids_from_text

    def counting_scan(text: str, *args: object) -> list[str]:
        scanned.append(text)
        return original_scan(text, *args)

    monkeypatch.setattr(thread_utils, "resolve_mentioned_user_ids_from_text", counting_scan)
    history = [make_visible_message(event_id=f"$m{index}", body=f"@helper step {index}") for index in range(50)]

    first = thread_utils.get_all_mentioned_agents_in_thread(history, config, runtime_paths)
    second = thread_utils.get_all_mentioned_agents_in_thread(history, config, runtime_paths)
    changed_text = [*history[:-1], replace(history[-1], content={"body": "@helper resolved text"})]
    thread_utils.get_all_mentioned_agents_in_thread(changed_text, config, runtime_paths)

    assert first == second == [helper_id]
    assert len(scanned) == 51
    assert scanned[-1] == "@helper resolved text"
