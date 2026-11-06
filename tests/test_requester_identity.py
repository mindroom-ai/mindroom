"""Managed entity recognition in requester identity checks."""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

import nio
import pytest

from mindroom import entity_resolution, requester_identity
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.authorization import classify_responder_candidates_from_cached_room
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.entity_resolution import entity_identity_registry
from mindroom.matrix import state as matrix_state
from mindroom.matrix.state import MatrixState
from mindroom.requester_identity import is_managed_entity_id
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_managed_entity_check_reads_matrix_state_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One check reads Matrix state once for all entities, and still knows persisted accounts and generated IDs.

    API requests run this check hundreds of times, so a state read per entity made the Connections page slow.
    """
    config = bind_runtime_paths(
        Config(agents={name: AgentConfig(display_name=name.title()) for name in ("helper", "coder", "writer")}),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account("agent_coder", "renamed_coder", "pw", domain="localhost")
    state.add_account("agent_helper", "helper", "pw", domain="old.example")
    save_matrix_state(state, runtime_paths)
    reads = 0
    cache_key = matrix_state._matrix_state_cache_key

    def counting_cache_key(state_file: Path) -> tuple[Path, int, int | None, int | None]:
        nonlocal reads
        reads += 1
        return cache_key(state_file)

    monkeypatch.setattr(matrix_state, "_matrix_state_cache_key", counting_cache_key)

    assert is_managed_entity_id("@renamed_coder:localhost", config, runtime_paths)
    assert is_managed_entity_id("@helper:old.example", config, runtime_paths)
    # Coder's saved account was renamed, so only the generated ID can match.
    assert is_managed_entity_id("@mindroom_coder:localhost", config, runtime_paths)
    assert not is_managed_entity_id("@alice:localhost", config, runtime_paths)
    assert reads == 4


@pytest.mark.usefixtures("enforce_turn_authorization")
def test_authorizing_a_message_derives_entity_identities_once_for_every_responder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every agent authorizes each message against each responder, so those checks must not derive identities again.

    Deriving every entity's Matrix IDs per check made a burst of messages to a room of 20 agents cost the fourth
    power of the agent count and held each agent's prompt for seconds behind the others.
    """
    config = bind_runtime_paths(
        Config(agents={f"agent{i:02d}": AgentConfig(display_name=f"Agent {i}", rooms=["lobby"]) for i in range(12)}),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    registry = entity_identity_registry(config, runtime_paths)
    room = nio.MatrixRoom("!lobby:localhost", registry.current_id(ROUTER_AGENT_NAME).full_id)
    for matrix_id in registry.current_ids.values():
        room.add_member(matrix_id.full_id, None, None)
    room.add_member("@alice:localhost", None, None)
    # Both identity sets are derived here, once, as the first message after a start derives them.
    assert not is_managed_entity_id("@alice:localhost", config, runtime_paths)
    derivations: Counter[str] = Counter()

    def counted[**P, T](name: str, function: Callable[P, T]) -> Callable[P, T]:
        def count(*args: P.args, **kwargs: P.kwargs) -> T:
            derivations[name] += 1
            return function(*args, **kwargs)

        return count

    monkeypatch.setattr(
        requester_identity,
        "agent_username_localpart",
        counted("generated ID", requester_identity.agent_username_localpart),
    )
    monkeypatch.setattr(
        entity_resolution,
        "_persisted_entity_id_map",
        counted("registry", entity_resolution._persisted_entity_id_map),
    )

    decisions = [
        classify_responder_candidates_from_cached_room(
            room,
            "@alice:localhost",
            config,
            runtime_paths,
            AgentReplyMembershipIndex(),
        )
        for _ in range(3)
    ]

    assert len(decisions[0].allowed) + len(decisions[0].pending) == 12
    assert decisions[1:] == decisions[:1] * 2
    # Without the memo, each classification derived both sets once per responder, at every check.
    assert derivations == Counter()


def test_managed_entity_ids_follow_renamed_accounts_and_added_agents(tmp_path: Path) -> None:
    """Remembered managed IDs are derived again when the persisted accounts or the configured entities change."""
    config = bind_runtime_paths(
        Config(agents={"helper": AgentConfig(display_name="Helper")}),
        test_runtime_paths(tmp_path),
    )
    runtime_paths = runtime_paths_for(config)
    assert not is_managed_entity_id("@renamed_helper:localhost", config, runtime_paths)
    assert not is_managed_entity_id("@mindroom_coder:localhost", config, runtime_paths)

    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account("agent_helper", "renamed_helper", "pw", domain="localhost")
    state.add_account("agent_coder", "coder_account", "pw", domain="localhost")
    save_matrix_state(state, runtime_paths)
    config.agents["coder"] = AgentConfig(display_name="Coder")

    assert is_managed_entity_id("@renamed_helper:localhost", config, runtime_paths)
    assert is_managed_entity_id("@mindroom_coder:localhost", config, runtime_paths)
    registry = entity_identity_registry(config, runtime_paths)
    assert registry.current_entity_name_for_user_id("@renamed_helper:localhost") == "helper"
    assert registry.current_entity_name_for_user_id("@coder_account:localhost") == "coder"
