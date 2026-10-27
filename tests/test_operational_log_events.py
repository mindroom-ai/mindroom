"""Operators alert on these log events, so their names and fields change only together with their docs page."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from structlog.testing import capture_logs

from mindroom import event_loop_stall
from mindroom.agent_loop.model_loop import EMPTY_REQUEST
from mindroom.agent_loop.run import run_agent
from mindroom.error_handling import get_user_friendly_error_message
from mindroom.event_loop_stall import EventLoopStallDetector
from mindroom.hooks import HookRegistry
from mindroom.llm.errors import RateLimited
from mindroom.llm.provider import ModelRequest
from mindroom.matrix.decrypt_failure import handle_decrypt_failure
from mindroom.matrix.sync_diagnostics import SyncStallDiagnostics
from mindroom.oauth.credential_lifecycle import refresh_oauth_credentials_with_result
from mindroom.oauth.providers import OAuthRefreshRejectedError
from mindroom.providers.pipeline import retry
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.session_storage.agent_storage import prepare_conversation_database
from mindroom.session_storage.schema import TableNames
from tests.agent_loop.helpers import collect, context, spec, toolkit
from tests.agent_tool_helpers import tool_policy
from tests.fakes.scripted_model import ScriptedModel, calls, tool_call
from tests.providers.test_provider_pipeline import _drain, _ScriptedProvider, _transient, _turn
from tests.test_decrypt_failure import _megolm_event, _mock_client, _mock_room
from tests.test_decrypt_failure import _runtime_paths as _decrypt_runtime_paths
from tests.test_oauth_service import _context, _credentials, _FakeOAuthProvider, _save

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

_DOCS_PAGE = Path(__file__).resolve().parents[1] / "docs" / "deployment" / "operational-log-events.md"

# Log-based metrics and alerts match these event names and extract these fields.
# A rename silently disables them, so update docs/deployment/operational-log-events.md in the same change.
_STABLE_EVENTS = {
    "event_loop_scheduler_lag_summary": {
        "sample_count",
        "p50_ms",
        "p95_ms",
        "p99_ms",
        "max_ms",
        "max_lag_scheduled_at",
        "max_lag_observed_at",
        "gc_collections_total",
    },
    "matrix_sync_stall_diagnostics": {"agent", "no_progress_seconds", "sync_age", "generation", "snapshots"},
    "tool_call_limit_reached": {"entity", "budget", "model_requests"},
    "tool_call_failed": {
        "tool_name",
        "agent_name",
        "error_type",
        "error_message",
        "duration_ms",
        "correlation_id",
        "channel",
    },
    "provider_request_retried": {
        "model_id",
        "reason",
        "error_type",
        "status_code",
        "error",
        "attempt",
        "max_retries",
        "delay_seconds",
    },
    "agent_error": {"agent", "error_type", "error"},
    "oauth_credentials_refresh_failed": {
        "provider_id",
        "credential_service",
        "reason",
        "error_type",
        "oauth_error",
        "has_refresh_token",
        "expires_at",
    },
    "event_loop_stall_detected": {
        "stalled_for_seconds",
        "threshold_seconds",
        "stack_capture_suppressed",
        "stack",
        "process_cpu_seconds_since_heartbeat",
        "other_thread_stacks",
        "omitted_thread_stack_count",
    },
    "event_loop_stall_ended": {"stall_duration_seconds"},
    "matrix_event_decryption_failed": {
        "room_id",
        "event_id",
        "sender",
        "sender_device_id",
        "session_id",
        "agent",
        "key_request_already_sent",
        "hint",
    },
    "conversation_converted": {"path", "runs", "messages", "compacted", "duration_seconds"},
}
_STABLE_EVENT_LEVELS = {
    "event_loop_scheduler_lag_summary": "info",
    "matrix_sync_stall_diagnostics": "warning",
    "tool_call_limit_reached": "warning",
    "tool_call_failed": "warning",
    "provider_request_retried": "warning",
    "agent_error": "error",
    "oauth_credentials_refresh_failed": "warning",
    "event_loop_stall_detected": "error",
    "event_loop_stall_ended": "warning",
    "matrix_event_decryption_failed": "warning",
    "conversation_converted": "info",
}


def _assert_logged(logs: list[dict[str, object]], event: str) -> dict[str, object]:
    """Assert ``event`` was logged once, at its pinned level, with every documented field; return the record.

    A record may carry further diagnostic fields, which the docs page does not promise.
    """
    [entry] = [entry for entry in logs if entry["event"] == event]
    assert entry["log_level"] == _STABLE_EVENT_LEVELS[event]
    assert _STABLE_EVENTS[event] <= set(entry)
    return entry


def test_docs_page_documents_exactly_the_stable_events_and_fields() -> None:
    """Each stable event has a docs section whose field table lists exactly its pinned fields."""
    sections = re.split(r"^### `([a-z_]+)`$", _DOCS_PAGE.read_text(encoding="utf-8"), flags=re.MULTILINE)
    documented = {
        name: set(re.findall(r"^\| `([a-z0-9_]+)` \|", body, flags=re.MULTILINE))
        for name, body in zip(sections[1::2], sections[2::2], strict=True)
    }
    assert documented == _STABLE_EVENTS
    assert _STABLE_EVENT_LEVELS.keys() == _STABLE_EVENTS.keys()


def test_conversation_converted_keeps_its_name_and_fields(tmp_path: Path) -> None:
    """Converting a database that holds a table logs the pinned record."""
    names = TableNames("code")
    database = names.database_path(tmp_path)
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("CREATE TABLE other (x)")

    with capture_logs() as logs:
        prepare_conversation_database(database, names)

    _assert_logged(logs, "conversation_converted")


def test_scheduler_lag_summary_keeps_its_name_and_fields() -> None:
    """A completed 60-second window logs the pinned summary."""
    detector = EventLoopStallDetector()
    detector._scheduler_lag_samples.append((0.002, 0.0, 0.0))

    with capture_logs() as logs:
        detector._report_scheduler_lag(60.0)

    _assert_logged(logs, "event_loop_scheduler_lag_summary")


@pytest.mark.asyncio
async def test_matrix_sync_stall_diagnostics_keeps_its_name_and_fields() -> None:
    """Ninety seconds without receive-loop progress logs the pinned diagnostics."""
    diagnostics = SyncStallDiagnostics("router", last_progress_monotonic=0.0, generation=None)

    with capture_logs() as logs:
        diagnostics.observe(now=90.0, sync_age=None, generation=None)

    _assert_logged(logs, "matrix_sync_stall_diagnostics")


@pytest.mark.asyncio
async def test_tool_call_limit_reached_keeps_its_name_and_fields() -> None:
    """The model request refused after budget plus two requests logs the pinned warning."""

    def probe() -> str:
        """Record one call."""
        return "probed"

    model = ScriptedModel([calls(tool_call("probe", f"call_{index}")) for index in range(3)])

    with capture_logs() as logs:
        await collect(
            run_agent(
                spec(model, toolkit(probe), max_tool_calls_per_turn=0),
                [],
                ctx=context(),
                persist=None,
                request_template=EMPTY_REQUEST,
            ),
        )

    assert len(model.requests) == 2
    _assert_logged(logs, "tool_call_limit_reached")


@pytest.mark.asyncio
async def test_tool_call_failed_keeps_its_name_and_fields() -> None:
    """A tool that raises logs the pinned warning."""

    async def explode() -> None:
        msg = "boom"
        raise ValueError(msg)

    with capture_logs() as logs, pytest.raises(ValueError, match="boom"):
        await tool_policy(HookRegistry.empty(), agent_name="code")("explode", explode, {})

    _assert_logged(logs, "tool_call_failed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "reason", "status_code"),
    [(_transient(), "transient_error", 503), (1.0, "stream_stalled", 504)],
    ids=["transient", "stall"],
)
async def test_provider_request_retried_keeps_its_name_and_fields_for_either_reason(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception | float,
    reason: str,
    status_code: int,
) -> None:
    """A transient failure and a stream silent past its idle limit log the same pinned warning, told apart by reason."""
    monkeypatch.setattr(retry, "_retry_delay_seconds", lambda _attempt: 0.0)
    inner = _ScriptedProvider([[failure, _turn()], [_turn()]])
    provider = WrappedProvider(inner, partial(retry.retrying_stream, idle_timeout_seconds=0.05))

    with capture_logs() as logs:
        await _drain(provider, ModelRequest(messages=[]))

    entry = _assert_logged(logs, "provider_request_retried")
    assert (entry["reason"], entry["status_code"]) == (reason, status_code)


def test_agent_error_keeps_its_name_and_fields() -> None:
    """A failed reply logs the pinned error with the provider error's class name."""
    with capture_logs() as logs:
        get_user_friendly_error_message(RateLimited("slow down", provider="Scripted"), "general")

    assert _assert_logged(logs, "agent_error")["error_type"] == "RateLimited"


@pytest.mark.asyncio
async def test_oauth_credentials_refresh_failed_keeps_its_name_and_fields(tmp_path: Path) -> None:
    """A rejected refresh grant logs the pinned warning."""

    async def reject(_credentials: Mapping[str, Any]) -> None:
        msg = "dead refresh grant"
        raise OAuthRefreshRejectedError(msg, oauth_error="invalid_grant")

    oauth_context = _context(tmp_path, _FakeOAuthProvider(reject))
    _save(oauth_context, _credentials("access", "refresh", expires_at=1.0))

    with capture_logs() as logs, pytest.raises(OAuthRefreshRejectedError):
        await refresh_oauth_credentials_with_result(oauth_context)

    _assert_logged(logs, "oauth_credentials_refresh_failed")


def test_event_loop_stall_events_keep_their_names_and_fields() -> None:
    """A stall past the threshold logs the pinned detection, and its end the pinned duration."""
    detector = EventLoopStallDetector(threshold_seconds=0.1)
    heartbeat = event_loop_stall._Heartbeat(monotonic_seconds=1.0, process_cpu_seconds=0.0)

    with capture_logs() as logs:
        detector._note_stalled(1.2, heartbeat)
        detector._note_stall_ended(1.3)

    _assert_logged(logs, "event_loop_stall_detected")
    _assert_logged(logs, "event_loop_stall_ended")


@pytest.mark.asyncio
async def test_matrix_event_decryption_failed_keeps_its_name_and_fields(tmp_path: Path) -> None:
    """An undecryptable message logs the pinned warning."""

    async def cannot_notify() -> bool:
        return False

    with capture_logs() as logs:
        await handle_decrypt_failure(
            _mock_client(),
            _mock_room(),
            _megolm_event(),
            agent_name="assistant",
            runtime_paths=_decrypt_runtime_paths(tmp_path),
            can_notify=cannot_notify,
        )

    _assert_logged(logs, "matrix_event_decryption_failed")
