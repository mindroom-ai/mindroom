"""Minimal Bash's provider batch is saved before its shell can run."""

# ruff: noqa: D103
from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from mindroom.agent_cli.bash import MinimalBashTools
from mindroom.agent_cli.checkpoint import BatchCheckpoint
from mindroom.agent_loop.model_loop import EMPTY_REQUEST, LoopState
from mindroom.agent_loop.tools import PlannedCall
from mindroom.history.run_visibility import is_model_history_visible_run
from mindroom.llm.media import Audio, Image
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, ToolExecution
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.store import ConversationStore
from mindroom.tool_system.agno_toolkits import ToolValue, adapt_toolkit
from tests.test_agent_tool_calls import _catalog

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.tool_system.agent_tool_calls import PreparedAgentToolCatalog


def _assistant(*call_ids: str, content: str | None = None) -> Message:
    return Message(
        role="assistant",
        content=content,
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": "bash", "arguments": '{"command":"work"}'}}
            for call_id in call_ids
        ],
        provider_data={"signature": "real-provider-data"},
    )


async def _capture(checkpoint: BatchCheckpoint, record: RunRecord, messages: list[Message]) -> None:
    """Capture the batch the last assistant message requests, as the loop does before it runs any call."""
    bash = adapt_toolkit(MinimalBashTools()).functions[0]
    planned = [
        PlannedCall(
            call_id=call["id"],
            function=bash,
            arguments={"command": "work"},
            execution=ToolExecution(tool_call_id=call["id"], tool_name="bash", tool_args={"command": "work"}),
        )
        for call in messages[-1].tool_calls or ()
    ]
    state = LoopState(
        messages=messages,
        tools=(),
        run=checkpoint.catalog.run,
        record=record,
        request_index=1,
        loop_start_ids=frozenset(),
        request_template=EMPTY_REQUEST,
    )
    await checkpoint.capture(state, planned)


async def _stored_catalog(tmp_path: Path) -> PreparedAgentToolCatalog:
    catalog = await _catalog(tmp_path, [])
    catalog.store = create_state_storage("helper", tmp_path, session_table="sessions")
    return catalog


def _record() -> RunRecord:
    return RunRecord(run_id="run", session_id="session", agent_id="helper", status=RunStatus.running)


@pytest.mark.asyncio
async def test_checkpoint_captures_exact_batch_and_propagates_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = await _stored_catalog(tmp_path)
    checkpoint = BatchCheckpoint(catalog)
    record = _record()
    assistant = _assistant("bash-1", "bash-2", content="working")
    messages = [Message(role="user", content="do work"), assistant]
    with pytest.raises(ValueError, match="captured"):
        await checkpoint.persist("bash-1")
    await _capture(checkpoint, record, messages)
    original = deepcopy(messages)
    assistant.content = "mutated after capture"
    await checkpoint.persist("bash-1")
    assert catalog.store is not None
    saved = catalog.store.get_run("run")
    assert isinstance(saved, RunRecord)
    assert [item.to_dict() for item in saved.messages] == [item.to_dict() for item in original]
    assert [tool.tool_call_id for tool in saved.tools or ()] == ["bash-1", "bash-2"]
    # A crash before the run's own terminal save must not replay the unanswered Bash call.
    assert not is_model_history_visible_run(saved)
    assert record.status is RunStatus.running
    assert record.messages == []
    await checkpoint.persist("bash-2")

    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "injected database failure"
        raise OSError(msg)

    monkeypatch.setattr(ConversationStore, "save_run", fail)
    await _capture(checkpoint, record, messages)
    with pytest.raises(OSError, match="injected"):
        await checkpoint.persist("bash-1")


@pytest.mark.asyncio
async def test_approval_checkpoint_refreshes_mutable_session_state(tmp_path: Path) -> None:
    catalog = await _stored_catalog(tmp_path)
    checkpoint = BatchCheckpoint(catalog)
    await _capture(checkpoint, _record(), [_assistant("bash-1")])
    await checkpoint.persist("bash-1")
    catalog.run.session_state["before_approval"] = "kept"
    await checkpoint.persist_approval("bash-1")
    assert catalog.store is not None
    fields = catalog.store.read_session_fields("session")
    assert fields is not None
    assert fields.session_state["before_approval"] == "kept"
    saved = catalog.store.get_run("run")
    assert saved is not None
    assert saved.messages[0].tool_calls[0]["id"] == "bash-1"


@pytest.mark.asyncio
async def test_checkpoint_retains_only_the_current_provider_batch(tmp_path: Path) -> None:
    """Long minimal turns keep one history snapshot, not one per Bash batch."""
    catalog = await _catalog(tmp_path, [])
    checkpoint = BatchCheckpoint(catalog)
    messages = [Message(role="user", content="do work")]
    for index in range(3):
        messages.append(_assistant(f"bash-{index}"))
        await _capture(checkpoint, _record(), messages)
        checkpoint.complete(f"bash-{index}", "done")
        for earlier in range(index):
            with pytest.raises(ValueError, match="captured"):
                checkpoint.complete(f"bash-{earlier}", "late")


@pytest.mark.asyncio
@pytest.mark.parametrize("media", [False, True])
async def test_approval_checkpoint_keeps_completed_sibling_result(tmp_path: Path, media: bool) -> None:
    catalog = await _stored_catalog(tmp_path)
    checkpoint = BatchCheckpoint(catalog)
    await _capture(checkpoint, _record(), [_assistant("done", "waiting")])
    await checkpoint.persist("done")
    result = (
        ToolValue(
            content="real sibling result",
            images=(Image(content=b"sibling-image", mime_type="image/png"),),
            audios=(Audio(url="https://example.test/sibling.wav"),),
        )
        if media
        else "real sibling result"
    )
    checkpoint.complete("done", result)
    await checkpoint.persist_approval("waiting")
    assert catalog.store is not None
    saved = catalog.store.get_run("run")
    assert saved is not None
    assert saved.messages[-1].tool_call_id == "done"
    assert saved.messages[-1].content == "real sibling result"
    assert [(tool.tool_call_id, tool.result) for tool in saved.tools or ()] == [
        ("done", "real sibling result"),
        ("waiting", None),
    ]
    if media:
        assert saved.messages[-1].images[0].content == b"sibling-image"
        assert saved.messages[-1].audio[0].url == "https://example.test/sibling.wav"


@pytest.mark.asyncio
async def test_a_checkpoint_without_session_storage_refuses_the_bash_call(tmp_path: Path) -> None:
    """Bash never runs when its batch cannot be saved first."""
    checkpoint = BatchCheckpoint(await _catalog(tmp_path, []))
    await _capture(checkpoint, _record(), [_assistant("bash-1")])
    with pytest.raises(RuntimeError, match="persisted session storage"):
        await checkpoint.persist("bash-1")
