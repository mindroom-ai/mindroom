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
