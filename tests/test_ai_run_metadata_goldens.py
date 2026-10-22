"""The owned builder publishes the ``io.mindroom.ai_run`` metadata MindRoom published under Agno 3.0.9.

``tests/fixtures/agno_parity/events/ai_run.json`` holds the metadata each captured run published, keyed by run id.
Each entry is rebuilt with ``build_ai_run_metadata_content`` from what its call site passes: the run's record as the
run goldens hold it, a team's member runs, the prepared history the record's metadata keeps, and the capture's model
config. The capture normalized timers and ids in the stored runs and in the metadata alike, so the two compare as
they are.
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from mindroom.ai_run_metadata import build_ai_run_metadata_content
from mindroom.config.main import Config
from mindroom.constants import AI_RUN_METADATA_KEY
from mindroom.history.types import CompactionDecision, PreparedHistoryState, ResolvedReplayPlan
from mindroom.llm.runs import AnyRunRecord, RunRecord, TeamRunRecord, run_record_from_dict
from mindroom.teams.attempt import team_tool_executions
from mindroom.teams.runtime import team_usage

_FIXTURES = Path(__file__).parent / "fixtures" / "agno_parity"
_GOLDEN: dict[str, dict[str, Any]] = json.loads((_FIXTURES / "events" / "ai_run.json").read_text(encoding="utf-8"))
_CONFIG = Config.model_validate(
    {
        "models": {
            "code": {
                "provider": "synthetic",
                "id": "code-model",
                "display_name": "Code Model",
                "context_window": 200000,
            },
            "leader": {"provider": "synthetic", "id": "leader-model", "context_window": 100000},
        },
    },
)
# The ``code`` agent's runs live in ``agent_code``; the teams' leaders use ``leader``.
_MODEL_NAMES = {"agent_code": "code", "team_squad": "leader", "team_panel": "leader"}
# The streamed agent reply, whose context counts come from its last request rather than its accumulated usage.
_STREAMED = {"run-tools"}
# Approval resumes prepare no history, so their metadata has no prepared context or compaction.
_RESUMED = {"run-approved", "run-rejected", "run-delegation-approved", "team-approved"}
# An edit cut removed this run's row, so no stored record remains to rebuild its metadata from.
_WITHOUT_RECORD = {"run-c4"}


def _stored_runs() -> dict[str, tuple[str, AnyRunRecord]]:
    """Return each run row the run goldens hold, by run id with its store's label, skipping values nested in a row."""
    runs: dict[str, tuple[str, AnyRunRecord]] = {}
    for name in ("run_output.json", "team_run_output.json"):
        for key, data in json.loads((_FIXTURES / "runs" / name).read_text(encoding="utf-8")).items():
            store, path = key.split(":", 1)
            if path == data["run_id"]:
                assert path not in runs
                runs[path] = (store, run_record_from_dict(data))
    return runs


_RUNS = _stored_runs()


def _prepared_history(record: AnyRunRecord) -> PreparedHistoryState:
    """Return the prepared history whose metadata the run kept when it started."""
    content = (record.metadata or {})[AI_RUN_METADATA_KEY]
    compaction = content["compaction"]
    plan = compaction["replay_plan"]
    return PreparedHistoryState(
        replay_plan=ResolvedReplayPlan(
            mode=plan["mode"],
            estimated_tokens=plan["estimated_tokens"],
            add_history_to_context=True,
        ),
        compaction_decision=CompactionDecision(
            mode=compaction["decision"],
            reason=compaction["reason"],
            current_history_tokens=compaction.get("current_history_tokens"),
            trigger_budget_tokens=compaction.get("trigger_budget_tokens"),
            hard_budget_tokens=compaction.get("hard_budget_tokens"),
            fitted_replay_tokens=compaction.get("fitted_replay_tokens"),
        ),
        compaction_reply_outcome=compaction["outcome"],
        prepared_context_tokens=content["prepared_context"]["tokens"],
    )


def _last_request_counts(record: AnyRunRecord) -> dict[str, Any]:
    """Return the usage counters of the run's last request, which its last assistant message stores."""
    metrics = [message for message in record.messages if message.role == "assistant"][-1].metrics
    assert metrics is not None
    return metrics.to_dict()


def _published(run_id: str) -> dict[str, Any]:
    """Build one run's metadata as its call site does."""
    store, record = _RUNS[run_id]
    prepared = None if run_id in _RESUMED else _prepared_history(record)
    if run_id == "run-error":
        # MindRoom's interrupted-turn record replaced the failed run's row and keeps no usage, model, or provider.
        # The failed run's own record held what Agno stored for the same failing request when no turn record
        # replaced it.
        _, failed = _RUNS["run-native-error"]
        record = replace(failed, run_id=record.run_id, session_id=record.session_id)
    common: dict[str, Any] = {
        "config": _CONFIG,
        "model_name": _MODEL_NAMES[store],
        "run_id": record.run_id,
        "session_id": record.session_id,
        "status": record.status,
        "model": record.model,
        "model_provider": record.model_provider,
        "context_metrics": record.metrics,
        "prepared_history": prepared,
    }
    if isinstance(record, TeamRunRecord):
        members = [
            member for _, member in _RUNS.values() if isinstance(member, RunRecord) and member.parent_run_id == run_id
        ]
        # As ``TeamAttempt.metadata_content`` passes them.
        return build_ai_run_metadata_content(
            **common,
            metrics=team_usage(record, members),
            context_input_tokens=None if prepared is None else prepared.prepared_context_tokens,
            tool_count=len(team_tool_executions(record, members)),
        )
    if prepared is None:
        # As the approval resume passes them.
        return build_ai_run_metadata_content(**common, metrics=record.metrics, tool_count=len(record.tools or ()))
    # As the blocking and streamed agent replies pass them; a streamed reply also passes its last request's counts.
    last_request = _last_request_counts(record) if run_id in _STREAMED else {}
    return build_ai_run_metadata_content(
        **common,
        metrics=record.metrics,
        context_input_tokens=prepared.prepared_context_tokens,
        context_raw_input_tokens=last_request.get("input_tokens"),
        context_cache_read_tokens=last_request.get("cache_read_tokens"),
        context_cache_write_tokens=last_request.get("cache_write_tokens"),
        tool_count=len(record.tools or ()),
    )


def _expected(run_id: str) -> dict[str, Any]:
    expected = copy.deepcopy(_GOLDEN[run_id])
    if run_id in _STREAMED:
        # Agno's streamed reply merged the counters its requests reported into the usage, so the zero cache-write
        # count a request reported appeared as 0; the owned usage leaves zero counters out, as every blocking reply's
        # usage did.
        assert expected[AI_RUN_METADATA_KEY]["usage"].pop("cache_write_tokens") == 0
    return expected


def test_only_the_cut_run_has_no_stored_record() -> None:
    """Every golden run but the one an edit cut removed has a stored record to rebuild its metadata from."""
    assert {run_id for run_id in _GOLDEN if run_id not in _RUNS} == _WITHOUT_RECORD


@pytest.mark.parametrize("run_id", sorted(set(_GOLDEN) - _WITHOUT_RECORD))
def test_the_owned_builder_publishes_the_golden_run_metadata(run_id: str) -> None:
    """Each run's rebuilt metadata, a text, tool, failed, team, or approval-resumed run's, equals the published one."""
    assert _published(run_id) == _expected(run_id)
