"""The compaction summary request is the ``compaction_summary`` prompt golden's, byte for byte, from production code.

The golden's first turn calls ``compact_context`` and stores its run; the next reply compacts that run before it
answers, with one non-streaming summary request. Here the run the first turn stores is seeded from the golden's own
requests, and the production compaction path (summarizer loading through the provider registry, the summary input,
and the summary call) sends the summary request to the recorder the prompt goldens were captured with.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.history.compaction import compact_scope_history, load_summarizer, scope_usage_recorder
from mindroom.history.runtime import resolve_entity_preparation_inputs
from mindroom.history.storage import read_scope_state, set_force_compaction_state
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.session_storage.types import ConversationSession
from tests.agno_parity.prompt_scenarios import SCENARIOS, Recorder, call_dict, load_golden, normalize
from tests.conftest import seed_session
from tests.history_helpers import _reported

if TYPE_CHECKING:
    from pathlib import Path

_GOLDEN = "compaction_summary"
# What the first turn stores beside its messages: the Matrix event ids it read and answered, and its requester.
_FIRST_TURN_METADATA = {
    "correlation_id": "correlation-1",
    "matrix_event_id": "$current:localhost",
    "matrix_seen_event_ids": ["$current:localhost", "$earlier:localhost"],
    "reply_to_event_id": "$current:localhost",
    "requester_id": "@user:localhost",
    "room_id": "!room:localhost",
    "thread_id": "$thread:localhost",
}


def _first_turn_run(golden: dict[str, object]) -> RunRecord:
    """Return the run the first turn stores: its request after the tool result, without the prompt, then its answer."""
    requests = golden["requests"]
    assert isinstance(requests, list)
    after_tool = [Message.from_dict(message) for message in requests[1]["messages"] if message["role"] != "system"]
    return RunRecord(
        run_id="run-compact",
        agent_id="assistant",
        session_id="!room:localhost:$thread:localhost",
        status=RunStatus.completed,
        messages=[*after_tool, Message(role="assistant", content="ok")],
        metadata=dict(_FIRST_TURN_METADATA),
    )


@pytest.mark.asyncio
async def test_the_compaction_summary_request_equals_the_golden(tmp_path: Path) -> None:
    """Compacting the first turn's run sends the golden's summary request: prompt, serialized run, model, and shape."""
    golden = load_golden(_GOLDEN)
    scenario = SCENARIOS[_GOLDEN]
    assert scenario.turn is not None
    turn = scenario.turn(tmp_path)
    scope = HistoryScope(kind="agent", scope_id=turn.agent_name)
    session = ConversationSession(
        session_id="!room:localhost:$thread:localhost",
        kind="agent",
        owner_id=turn.agent_name,
        runs=[_first_turn_run(golden)],
    )
    set_force_compaction_state(session, scope, HistoryScopeState(), force=True)
    storage = create_session_storage(turn.agent_name, turn.config, turn.runtime_paths, execution_identity=None)
    seed_session(storage, session)
    inputs = resolve_entity_preparation_inputs(
        config=turn.config,
        entity_name=turn.agent_name,
        static_prompt_tokens=0,
        active_model_name=None,
        active_context_window=None,
    )
    recorder = Recorder(responder=scenario.responder)

    with recorder.installed():
        outcome = await compact_scope_history(
            storage=storage,
            session=session,
            scope=scope,
            state=read_scope_state(session, scope),
            summarizer=load_summarizer(
                inputs.execution_plan,
                inputs.history_settings,
                config=turn.config,
                runtime_paths=turn.runtime_paths,
                session_id=session.session_id,
                scope=scope,
                on_usage=scope_usage_recorder(storage, session.session_id),
            ),
            available_history_budget=None,
            before_tokens=0,
            replay_window_tokens=None,
            threshold_tokens=None,
            progress=None,
            complete=_reported,
            replay_model=None,
        )

    summary_request = golden["requests"][-1]
    assert summary_request["stream"] is False
    assert normalize([call_dict(call) for call in recorder.ordered_calls()], tmp_path) == [summary_request]
    assert outcome is not None
    assert outcome.compacted_run_count == 1
    stored = storage.load_session(session.session_id, "agent")
    assert stored is not None
    assert stored.summary is not None
    assert stored.runs == []
