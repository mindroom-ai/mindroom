"""The ``io.mindroom.ai_run`` metadata a team reply publishes: the leader's identity and the whole team's usage.

Providers are scripted at the team runtime seam (``tests/team_turn_helpers.py``), so the usage comes from the
records the leader's and the member's runs stored.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.llm.errors import ProviderError
from tests.fakes.scripted_model import Await, ScriptStep, answer, calls, tool_call, usage
from tests.team_turn_helpers import (
    PROBE_TOOL,
    ProbeToolkit,
    TeamTurn,
    authored_team_config,
    build_team_turn,
    registered_probe,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path


@pytest.fixture(autouse=True)
def probe() -> Iterator[type[ProbeToolkit]]:
    """Register the probe toolkit the member's config names."""
    with registered_probe() as toolkit:
        yield toolkit


def _team(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    leader: Sequence[Sequence[ScriptStep]],
    general: Sequence[Sequence[ScriptStep]],
) -> TeamTurn:
    """Build the ad hoc team of ``general`` on ``general-model``, led on ``default``."""
    authored = authored_team_config(
        {
            "general": {
                "display_name": "GeneralAgent",
                "role": "Answers.",
                "model": "general-model",
                "tools": [PROBE_TOOL],
            },
        },
        models=("default", "general-model"),
    )
    return build_team_turn(
        tmp_path,
        monkeypatch,
        authored,
        members=("general",),
        scripts={"default": leader, "general-model": general},
    )


def _delegate(metrics: Any) -> list[ScriptStep]:  # noqa: ANN401
    return calls(
        tool_call("delegate_task_to_member", "call-team", member_id="general", task="Analyze"),
        metrics=metrics,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_team_reply_publishes_the_leader_s_identity_and_the_whole_team_s_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
) -> None:
    """Usage sums the leader's requests and the member's run; the duration stays the leader's, which spans both."""
    team = _team(
        tmp_path,
        monkeypatch,
        leader=[
            _delegate(usage(500, 70, cache_read_tokens=640, cache_write_tokens=32, reasoning_tokens=24)),
            answer("Team answer", usage(300, 50)),
        ],
        general=[calls(tool_call("touch", "call-touch", label="a"), metrics=usage(200, 30)), answer("Member answer")],
    )
    collector: dict[str, Any] = {}

    text = await team.reply(stream=stream, run_metadata_collector=collector)

    assert "Member answer" in text
    payload = collector[AI_RUN_METADATA_KEY]
    assert (payload["version"], payload["run_id"], payload["status"]) == (1, "run-1", "completed")
    assert payload["model"] == {"config": "default", "id": "scripted-model", "provider": "Synthetic"}
    usage_payload = payload["usage"]
    assert (usage_payload["input_tokens"], usage_payload["output_tokens"]) == (1000, 150)
    assert (usage_payload["cache_read_tokens"], usage_payload["cache_write_tokens"]) == (640, 32)
    assert usage_payload["reasoning_tokens"] == 24
    record, [member] = team.stored_runs()
    assert record.metrics is not None
    assert member.metrics is not None
    assert member.metrics.duration
    assert float(usage_payload["duration"]) == pytest.approx(record.metrics.duration)
    # The leader's delegation and the member's call.
    assert payload["tools"] == {"count": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_a_failed_team_reply_still_publishes_the_usage_it_was_billed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    stream: bool,
) -> None:
    """Billed tokens of a failed turn reach the metadata, with the error status."""

    async def fail() -> None:
        msg = "provider exploded"
        raise ProviderError(msg, provider="Synthetic")

    team = _team(
        tmp_path,
        monkeypatch,
        leader=[_delegate(usage(500, 80)), [Await(fail)]],
        general=[answer("Member answer", usage(300, 50))],
    )
    collector: dict[str, Any] = {}

    text = await team.reply(stream=stream, run_metadata_collector=collector)

    assert "provider exploded" in text
    payload = collector[AI_RUN_METADATA_KEY]
    assert (payload["run_id"], payload["status"]) == ("run-1", "error")
    assert (payload["usage"]["input_tokens"], payload["usage"]["output_tokens"]) == (800, 130)
