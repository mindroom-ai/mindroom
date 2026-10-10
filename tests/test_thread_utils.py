"""Tests for canonical human participation in thread history."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.constants import ORIGINAL_SENDER_KEY
from mindroom.thread_utils import has_multiple_non_agent_users_in_thread
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


def test_a_relay_counts_the_person_it_speaks_for(config: Config) -> None:
    """A thread whose people were re-posted by the router, such as a moved thread, still knows who took part."""
    relay = make_visible_message(
        sender="@mindroom_router:localhost",
        body="Alice: is this the outage?",
        content={ORIGINAL_SENDER_KEY: "@alice:localhost"},
    )

    assert has_multiple_non_agent_users_in_thread(
        [relay],
        config,
        runtime_paths_for(config),
        current_sender_id="@bob:localhost",
    )


def test_a_relay_of_the_same_person_adds_no_one(config: Config) -> None:
    """A handoff or voice transcript naming someone who also posted counts that person once."""
    history = [
        make_visible_message(sender="@alice:localhost", body="help"),
        make_visible_message(
            sender="@mindroom_router:localhost",
            body="@helper could you help with this?",
            content={ORIGINAL_SENDER_KEY: "@bridge_alice:localhost"},
        ),
    ]

    assert not has_multiple_non_agent_users_in_thread(
        history,
        config,
        runtime_paths_for(config),
        current_sender_id="@alice:localhost",
    )


def test_a_person_cannot_claim_to_speak_for_someone_else(config: Config) -> None:
    """Only MindRoom's own accounts can name the person a message speaks for."""
    claimed = make_visible_message(
        sender="@alice:localhost",
        body="hi",
        content={ORIGINAL_SENDER_KEY: "@bob:localhost"},
    )

    assert not has_multiple_non_agent_users_in_thread(
        [claimed],
        config,
        runtime_paths_for(config),
        current_sender_id="@alice:localhost",
    )
