"""Plugins tell people from MindRoom's own accounts, list those accounts, and check administrators through the plugin API."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.config.main import Config
from mindroom.plugin_api import ManagedEntity, entity_directory, is_human_user, is_platform_administrator
from tests.conftest import test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ALICE = "@alice:localhost"
ALICE_BRIDGED = "@telegram_alice:localhost"


def _config(runtime_paths: RuntimePaths, **data: object) -> Config:
    """Return a config with two agents, which take the default tools, and a team, without preparing any account.

    The coder delegates to the writer, which gives it the delegate toolkit without a tool entry.
    """
    agents = {
        "coder": {"display_name": "Coder", "tools": ["calculator"], "delegate_to": ["writer"]},
        "writer": {"display_name": "Writer"},
    }
    return Config.validate_with_runtime(
        {
            "models": {"default": {"provider": "openai", "id": "test-model"}},
            "defaults": {"tools": ["scheduler"]},
            "agents": agents,
            "teams": {"crew": {"display_name": "Crew", "role": "Work together", "agents": ["coder", "writer"]}},
            **data,
        },
        runtime_paths,
    )


def test_people_are_neither_mindroom_accounts_nor_bot_accounts(tmp_path: Path) -> None:
    """The router, agents, teams, the internal user and bot accounts are no person, by saved or generated ID."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(
        runtime_paths,
        mindroom_user={"username": "mindroom_user"},
        bot_accounts=["@bridgebot:localhost"],
        authorization={"aliases": {ALICE: [ALICE_BRIDGED]}},
    )
    persist_entity_accounts(config, runtime_paths, usernames={"coder": "renamed_coder"})

    not_people = [
        "@mindroom_router:localhost",
        "@renamed_coder:localhost",
        # The ID the coder's account would have had, had it not been renamed.
        "@mindroom_coder:localhost",
        "@mindroom_writer:localhost",
        "@mindroom_crew:localhost",
        "@mindroom_user:localhost",
        "@bridgebot:localhost",
    ]
    assert [user_id for user_id in not_people if is_human_user(user_id, config, runtime_paths)] == []
    assert is_human_user(ALICE, config, runtime_paths)
    assert is_human_user(ALICE_BRIDGED, config, runtime_paths)


def test_administrators_include_their_bridge_aliases_but_never_an_agent(tmp_path: Path) -> None:
    """A listed administrator and their bridged account administer; an agent aliased to them does not, nor does Bob."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(
        runtime_paths,
        administrators=[ALICE],
        authorization={"aliases": {ALICE: [ALICE_BRIDGED, "@mindroom_coder:localhost"]}},
    )
    persist_entity_accounts(config, runtime_paths)

    assert is_platform_administrator(ALICE, config, runtime_paths)
    assert is_platform_administrator(ALICE_BRIDGED, config, runtime_paths)
    assert not is_platform_administrator("@mindroom_coder:localhost", config, runtime_paths)
    assert not is_platform_administrator("@bob:localhost", config, runtime_paths)


def test_directory_lists_each_prepared_entity_and_follows_the_config_and_accounts(tmp_path: Path) -> None:
    """Each entity with an account is listed by its user ID; a renamed agent, a new agent and its account show at once."""
    runtime_paths = test_runtime_paths(tmp_path)
    config = _config(runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames={"coder": "renamed_coder"})

    assert entity_directory(config, runtime_paths) == {
        "@mindroom_router:localhost": ManagedEntity("router", "router", "RouterAgent", ()),
        "@renamed_coder:localhost": ManagedEntity("coder", "agent", "Coder", ("calculator", "scheduler", "delegate")),
        "@mindroom_writer:localhost": ManagedEntity("writer", "agent", "Writer", ("scheduler",)),
        "@mindroom_crew:localhost": ManagedEntity("crew", "team", "Crew", ()),
    }

    agents = {"coder": {"display_name": "Code Bot", "tools": ["calculator"]}, "writer": {"display_name": "Writer"}}
    changed = _config(runtime_paths, agents={**agents, "editor": {"display_name": "Editor", "tools": ["file"]}})
    directory = entity_directory(changed, runtime_paths)
    assert directory["@renamed_coder:localhost"].display_name == "Code Bot"
    # The editor's account is not prepared yet, so it is left out rather than failing the whole directory.
    assert "editor" not in {entity.name for entity in directory.values()}

    persist_entity_accounts(changed, runtime_paths)

    assert entity_directory(changed, runtime_paths)["@mindroom_editor:localhost"] == ManagedEntity(
        "editor",
        "agent",
        "Editor",
        ("file", "scheduler"),
    )
