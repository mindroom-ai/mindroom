"""A team turn's usage is its leader's and every reporting member's metrics, counted once.

The test is a plain function, so it lives outside ``test_run_team``, whose module mark runs every test under asyncio.
"""

from __future__ import annotations

from mindroom.llm.runs import RunRecord, TeamRunRecord
from mindroom.llm.usage import RunMetrics, TokenCounts
from mindroom.teams.runtime import team_usage, team_usage_runs


def test_a_team_turns_usage_is_the_leader_and_every_reporting_member_counted_once() -> None:
    """One selection of runs feeds the turn's metadata sum and the API's per-provider sum."""
    leader = TeamRunRecord(
        run_id="team",
        team_id="squad",
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=10, total_tokens=110), duration=5.0),
    )
    writer = RunRecord(
        run_id="writer",
        agent_id="writer",
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=50, output_tokens=5, total_tokens=55), duration=3.0),
    )
    silent = RunRecord(run_id="silent", agent_id="silent")
    checker = RunRecord(
        run_id="checker",
        agent_id="checker",
        metrics=RunMetrics(tokens=TokenCounts(input_tokens=20, output_tokens=2, total_tokens=22), duration=1.0),
    )
    leader.ended_member_runs = [writer, silent, checker]

    assert [(run.run_id, metrics.tokens.input_tokens) for run, metrics in team_usage_runs(leader)] == [
        ("team", 100),
        ("writer", 50),
        ("checker", 20),
    ]
    total = team_usage(leader)
    assert total is not None
    assert (total.tokens.input_tokens, total.tokens.total_tokens, total.duration) == (170, 187, 5.0)
    # The sum is a new object; the stored records keep their own counters.
    assert (leader.metrics.tokens.input_tokens, writer.metrics.duration) == (100, 3.0)  # type: ignore[union-attr]

    leader.ended_member_runs = [silent]
    assert team_usage(leader) is leader.metrics
    leaderless = TeamRunRecord(run_id="team", team_id="squad", ended_member_runs=[writer])
    assert team_usage(leaderless) is writer.metrics
    leaderless.ended_member_runs = [silent]
    assert team_usage(leaderless) is None
