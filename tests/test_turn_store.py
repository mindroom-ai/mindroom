"""Tests for TurnStore ownership and migration guards."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.handled_turns import HandledTurnState
from mindroom.matrix.users import AgentMatrixUser
from mindroom.turn_store import TurnStore, TurnStoreDeps
from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for, test_runtime_paths


def test_turn_store_constructs_private_ledger_from_tracking_base_path(tmp_path: Path) -> None:
    """TurnStore should own its private ledger and persist through the tracking base path."""
    tracking_path = tmp_path / "tracking"
    store = TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            tracking_base_path=tracking_path,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )

    store.record_turn(HandledTurnState.from_source_event_id("$event", response_event_id="$response"))

    reloaded_store = TurnStore(
        TurnStoreDeps(
            agent_name="agent",
            tracking_base_path=tracking_path,
            state_writer=MagicMock(),
            resolver=MagicMock(),
            tool_runtime=MagicMock(),
        ),
    )

    assert reloaded_store.is_handled("$event")
    turn_record = reloaded_store.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.response_event_id == "$response"


def test_turn_store_reuses_reserved_response_transaction_id_across_reload(tmp_path: Path) -> None:
    """Pending response transaction IDs should survive reload without marking the turn handled."""
    tracking_path = tmp_path / "tracking"
    deps = TurnStoreDeps(
        agent_name="agent",
        tracking_base_path=tracking_path,
        state_writer=MagicMock(),
        resolver=MagicMock(),
        tool_runtime=MagicMock(),
    )
    store = TurnStore(deps)

    first = store.reserve_pending_response(HandledTurnState.from_source_event_id("$event"))

    reloaded_store = TurnStore(deps)
    second = reloaded_store.reserve_pending_response(HandledTurnState.from_source_event_id("$event"))

    assert first.transaction_id == second.transaction_id
    assert reloaded_store.is_handled("$event") is False
    turn_record = reloaded_store.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.response_transaction_id == first.transaction_id
    assert turn_record.completed is False


def test_turn_store_reuses_reserved_visible_echo_transaction_id_across_reload(tmp_path: Path) -> None:
    """Pending visible-echo transaction IDs should survive reload without marking the turn handled."""
    tracking_path = tmp_path / "tracking"
    deps = TurnStoreDeps(
        agent_name="agent",
        tracking_base_path=tracking_path,
        state_writer=MagicMock(),
        resolver=MagicMock(),
        tool_runtime=MagicMock(),
    )
    store = TurnStore(deps)

    first = store.reserve_visible_echo(HandledTurnState.from_source_event_id("$event"))

    reloaded_store = TurnStore(deps)
    second = reloaded_store.reserve_visible_echo(HandledTurnState.from_source_event_id("$event"))

    assert first.transaction_id == second.transaction_id
    assert reloaded_store.is_handled("$event") is False
    turn_record = reloaded_store.get_turn_record("$event")
    assert turn_record is not None
    assert turn_record.visible_echo_transaction_id == first.transaction_id
    assert turn_record.visible_echo_event_id is None
    assert turn_record.completed is False


def test_turn_store_replays_pending_inbound_claims_until_turn_completes(tmp_path: Path) -> None:
    """Pending inbound claims should replay until a terminal handled-turn outcome exists."""
    tracking_path = tmp_path / "tracking"
    deps = TurnStoreDeps(
        agent_name="agent",
        tracking_base_path=tracking_path,
        state_writer=MagicMock(),
        resolver=MagicMock(),
        tool_runtime=MagicMock(),
    )
    store = TurnStore(deps)
    event_source = {
        "type": "m.room.message",
        "event_id": "$event",
        "sender": "@user:example.com",
        "content": {
            "msgtype": "m.text",
            "body": "hello",
        },
    }

    assert store.claim_pending_inbound(room_id="!room:example.com", event_source=event_source) is True

    reloaded_store = TurnStore(deps)
    pending_replays = reloaded_store.pending_inbound_replays()

    assert len(pending_replays) == 1
    assert pending_replays[0].room_id == "!room:example.com"
    assert pending_replays[0].event_id == "$event"
    assert pending_replays[0].event_source == event_source

    assert reloaded_store.claim_pending_inbound(room_id="!room:example.com", event_source=event_source) is False
    assert [replay.event_id for replay in reloaded_store.pending_inbound_replays()] == ["$event"]

    reloaded_store.record_turn(HandledTurnState.from_source_event_id("$event", response_event_id="$response"))

    assert reloaded_store.pending_inbound_replays() == []


def test_turn_store_preserves_each_pending_event_after_coalesced_response_state_is_written(tmp_path: Path) -> None:
    """Coalesced response bookkeeping must not collapse multiple replayable source events into one."""
    tracking_path = tmp_path / "tracking"
    deps = TurnStoreDeps(
        agent_name="agent",
        tracking_base_path=tracking_path,
        state_writer=MagicMock(),
        resolver=MagicMock(),
        tool_runtime=MagicMock(),
    )
    store = TurnStore(deps)
    first_event_source = {
        "type": "m.room.message",
        "event_id": "$first",
        "sender": "@user:example.com",
        "content": {
            "msgtype": "m.text",
            "body": "first",
        },
    }
    second_event_source = {
        "type": "m.room.message",
        "event_id": "$second",
        "sender": "@user:example.com",
        "content": {
            "msgtype": "m.text",
            "body": "second",
        },
    }

    assert store.claim_pending_inbound(room_id="!room:example.com", event_source=first_event_source) is True
    assert store.claim_pending_inbound(room_id="!room:example.com", event_source=second_event_source) is True
    store.reserve_pending_response(HandledTurnState.create(["$first", "$second"]))

    reloaded_store = TurnStore(deps)
    pending_replays = reloaded_store.pending_inbound_replays()

    assert [replay.event_id for replay in pending_replays] == ["$first", "$second"]


def test_only_turn_store_imports_handled_turn_ledger_in_production() -> None:
    """HandledTurnLedger imports should stay isolated to TurnStore in production code."""
    src_root = Path(__file__).resolve().parents[1] / "src" / "mindroom"
    offenders: list[str] = []

    for path in src_root.rglob("*.py"):
        if path.name in {"turn_store.py", "handled_turns.py"}:
            continue
        module = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(module):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "mindroom.handled_turns":
                continue
            if any(alias.name == "HandledTurnLedger" for alias in node.names):
                offenders.append(path.relative_to(src_root).as_posix())
                break

    assert offenders == []


def test_agent_bot_does_not_expose_removed_handled_turn_ledger_shim(tmp_path: Path) -> None:
    """AgentBot instances should route handled-turn state only through TurnStore."""
    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    bot = AgentBot(
        agent_user=AgentMatrixUser(
            agent_name="agent",
            user_id="@mindroom_agent:localhost",
            display_name="Agent",
            password=TEST_PASSWORD,
        ),
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )

    # Split the string so this guard test does not match its own source text.
    removed_attr = "_handled" + "_turn_ledger"
    assert removed_attr not in AgentBot.__dict__
    assert not hasattr(bot, removed_attr)
    assert removed_attr not in vars(bot)


def test_no_test_references_removed_bot_handled_turn_ledger_shim() -> None:
    """Tests should route all handled-turn access through TurnStore."""
    tests_root = Path(__file__).resolve().parent
    # Split the string so this guard test does not match its own source text.
    needle = "._handled" + "_turn_ledger"
    offenders = [
        path.relative_to(tests_root).as_posix() for path in tests_root.rglob("*.py") if needle in path.read_text()
    ]

    assert offenders == []
