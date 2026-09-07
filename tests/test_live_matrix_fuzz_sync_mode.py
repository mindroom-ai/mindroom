"""Transport selection reaches the config used by real-server qualification."""

from __future__ import annotations

import json
import sys
from typing import Literal
from unittest.mock import AsyncMock

import pytest
import yaml

from mindroom.config.main import Config
from mindroom.orchestration.config_updates import build_config_update_plan
from scripts.testing import fuzz_live_matrix as fuzz


@pytest.mark.parametrize("profile", ["sustained-stream-capacity", "restart-regression"])
@pytest.mark.parametrize(
    ("mode_args", "expected_mode"),
    [
        ([], "classic"),
        (["--sync-mode", "classic"], "classic"),
        (["--sync-mode", "sliding"], "sliding"),
    ],
)
def test_cli_sync_mode_reaches_generated_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    profile: str,
    mode_args: list[str],
    expected_mode: str,
) -> None:
    """Catch a parsed option that gets lost before capacity or restart startup."""
    monkeypatch.setattr(sys, "argv", ["fuzz_live_matrix.py", "--profile", profile, *mode_args])

    def start_without_services(stack: fuzz.ManagedTuwunelStack) -> None:
        stack._write_config(9292)

    async def inspect_started_config(
        stack: fuzz.ManagedTuwunelStack,
        scenario: fuzz.LiveFuzzScenario,
        *,
        reply_timeout: float,
        settle_seconds: float,
        root_fanout: int,
    ) -> dict[str, str]:
        del scenario, reply_timeout, settle_seconds, root_fanout
        config = yaml.safe_load(stack.config_path.read_text(encoding="utf-8"))
        assert config["matrix_sync"] == {"mode": expected_mode}
        validated = Config.model_validate(config)
        assert validated.matrix_sync.mode == expected_mode
        assert validated.matrix_sync.sliding_timeline_limit == 100
        return {"status": "PASS"}

    monkeypatch.setattr(fuzz.ManagedTuwunelStack, "start", start_without_services)
    monkeypatch.setattr(fuzz, "_run_live", inspect_started_config)

    fuzz.main()

    assert json.loads(capsys.readouterr().out)["sync_mode"] == expected_mode


@pytest.mark.parametrize("mode", ["classic", "sliding"])
def test_restart_profile_config_really_replaces_both_bots(mode: Literal["classic", "sliding"]) -> None:
    """Catch a reload trigger that applies in place instead of exercising restart."""
    stack = fuzz.ManagedTuwunelStack(profile="restart-regression", sync_mode=mode)
    try:
        stack._write_config(9292)
        old = Config.model_validate(yaml.safe_load(stack.config_path.read_text()))
        stack.apply_replacement_config("!restart:example")
        new = Config.model_validate(yaml.safe_load(stack.config_path.read_text()))
        entities = {"general", "router"}
        plan = build_config_update_plan(
            current_config=old,
            new_config=new,
            configured_entities=entities,
            existing_entities=entities,
            agent_bots={entity: AsyncMock() for entity in entities},
        )
        assert plan.entities_to_restart == {"general", "router"}
        assert new.matrix_sync == old.matrix_sync
    finally:
        stack.close()
