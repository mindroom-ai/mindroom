"""Structured-log schema tests for compaction chunk sizing events."""
# ruff: noqa: D103

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
import tiktoken
from agno.models.anthropic import Claude
from agno.session.summary import SessionSummary
from structlog.testing import capture_logs

from mindroom.agent_storage import create_session_storage
from mindroom.history.compaction import _rewrite_working_session_for_compaction
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.prompts import COMPACTION_SUMMARY_PROMPT
from tests.conftest import FakeModel
from tests.history_helpers import (  # noqa: F401
    _ALL_HISTORY_SETTINGS,
    _close_test_storages,
    _completed_run,
    _make_config,
    _session,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.session.agent import AgentSession

    from mindroom.history.compaction import _CompactionRewriteResult


async def _rewrite_with_summary_model(
    *,
    storage: BaseDb,
    working_session: AgentSession,
    summary_input_budget: int,
    summary_model: Model,
) -> _CompactionRewriteResult | None:
    return await _rewrite_working_session_for_compaction(
        storage=storage,
        persisted_session=working_session,
        working_session=working_session,
        summary_model=summary_model,
        summary_model_name="summary-model",
        session_id=working_session.session_id,
        scope=HistoryScope(kind="agent", scope_id="test_agent"),
        state=HistoryScopeState(force_compact_before_next_run=True),
        history_settings=_ALL_HISTORY_SETTINGS,
        available_history_budget=None,
        selected_run_ids=("run-1",),
        summary_input_budget=summary_input_budget,
        before_tokens=0,
        runs_before=len(working_session.runs or []),
        threshold_tokens=None,
        summary_prompt=COMPACTION_SUMMARY_PROMPT,
        lifecycle_notice_event_id=None,
        progress_callback=None,
        collect_compaction_hook_messages=False,
    )


def _single_event(logs: list[dict[str, object]], event: str) -> dict[str, object]:
    matches = [entry for entry in logs if entry["event"] == event]
    assert len(matches) == 1
    return matches[0]


def _assert_truthful_sizing_fields(
    entry: dict[str, object],
    *,
    estimate: int,
    budget_tokens: int,
    kind: str,
) -> None:
    assert entry["summary_input_estimate"] == estimate
    assert entry["summary_input_estimate_kind"] == kind
    assert entry["summary_input_budget_tokens"] == budget_tokens
    assert "estimated_input_tokens" not in entry
    assert "summary_input_budget" not in entry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("summary_model", "kind"),
    [
        pytest.param(
            Claude(id="claude-sonnet-5"),
            "utf8_bytes_token_upper_bound",
            id="claude-byte-bound",
        ),
        pytest.param(
            FakeModel(id="gpt-4o", provider="fake"),
            "model_tiktoken_tokens",
            id="known-tiktoken-model",
        ),
        pytest.param(
            FakeModel(id="local-model", provider="fake"),
            "o200k_base_tokens",
            id="unknown-model",
        ),
    ],
)
async def test_chunk_events_report_actual_sizing_strategy(
    tmp_path: Path,
    summary_model: Model,
    kind: str,
) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_inputs: list[str] = []

    async def record_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        return SessionSummary(summary="chunk summary", updated_at=datetime.now(UTC))

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=record_summary),
        ),
        capture_logs() as logs,
    ):
        rewrite_result = await _rewrite_with_summary_model(
            storage=storage,
            working_session=working_session,
            summary_input_budget=8_000,
            summary_model=summary_model,
        )

    assert rewrite_result is not None
    payload = summary_inputs[0]
    if kind == "utf8_bytes_token_upper_bound":
        estimate = len(payload.encode("utf-8"))
    elif kind == "model_tiktoken_tokens":
        estimate = len(tiktoken.encoding_for_model("gpt-4o").encode(payload, disallowed_special=()))
    else:
        estimate = len(tiktoken.get_encoding("o200k_base").encode(payload, disallowed_special=()))
    for event in ("Compaction summary chunk request", "Compaction summary chunk completed"):
        _assert_truthful_sizing_fields(
            _single_event(logs, event),
            estimate=estimate,
            budget_tokens=8_000,
            kind=kind,
        )


@pytest.mark.asyncio
async def test_failed_event_uses_truthful_sizing_fields(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])
    summary_inputs: list[str] = []

    async def fail_summary(*, summary_input: str, **_kwargs: object) -> SessionSummary:
        summary_inputs.append(summary_input)
        msg = "provider exploded"
        raise RuntimeError(msg)

    with (
        patch(
            "mindroom.history.compaction.generate_compaction_summary",
            new=AsyncMock(side_effect=fail_summary),
        ),
        capture_logs() as logs,
        pytest.raises(RuntimeError, match="provider exploded"),
    ):
        await _rewrite_with_summary_model(
            storage=storage,
            working_session=working_session,
            summary_input_budget=8_000,
            summary_model=Claude(id="claude-sonnet-5"),
        )

    _assert_truthful_sizing_fields(
        _single_event(logs, "Compaction summary chunk failed"),
        estimate=len(summary_inputs[0].encode("utf-8")),
        budget_tokens=8_000,
        kind="utf8_bytes_token_upper_bound",
    )


@pytest.mark.asyncio
async def test_no_run_fit_warning_uses_token_budget_field(tmp_path: Path) -> None:
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    working_session = _session("session-1", runs=[_completed_run("run-1")])

    with (
        patch("mindroom.history.compaction.generate_compaction_summary", new=AsyncMock()),
        capture_logs() as logs,
    ):
        rewrite_result = await _rewrite_with_summary_model(
            storage=storage,
            working_session=working_session,
            summary_input_budget=1,
            summary_model=Claude(id="claude-sonnet-5"),
        )

    assert rewrite_result is None
    event = _single_event(logs, "Compaction skipped because no run fit the single-pass summary budget")
    assert event["summary_input_budget_tokens"] == 1
    assert "summary_input_budget" not in event
    assert "estimated_input_tokens" not in event
