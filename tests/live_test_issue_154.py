"""Standalone live check for ISSUE-154 cross-sink correlation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import structlog
from agno.models.message import Message
from agno.run.base import RunStatus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mindroom.ai import ai_response
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import DebugConfig, ModelConfig
from mindroom.constants import tracking_dir
from mindroom.handled_turns import HandledTurnState
from mindroom.history import PreparedHistoryState
from mindroom.hooks import HookRegistry
from mindroom.llm_request_logging import install_llm_request_logging
from mindroom.logging_config import get_logger, setup_logging
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.tool_system.runtime_context import ToolDispatchContext
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge
from mindroom.turn_policy import PreparedDispatch, ResponseAction
from tests.conftest import bind_runtime_paths, replace_turn_controller_deps, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@dataclass
class _LoggingModel:
    id: str = "test-model"
    system_prompt: str | None = None
    temperature: float | None = 0.7
    client: object | None = None
    async_client: object | None = None

    async def ainvoke(self, *_args: object, **_kwargs: object) -> dict[str, str]:
        return {"status": "ok"}

    async def ainvoke_stream(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> AsyncIterator[dict[str, str]]:
        yield {"status": "ok"}


class _InvokeAgent:
    def __init__(
        self,
        *,
        model: _LoggingModel,
        bridge: object,
        captured_metadata: list[dict[str, object]],
    ) -> None:
        self.model = model
        self.name = "GeneralAgent"
        self.add_history_to_context = False
        self.db = None
        self.learning = None
        self._bridge = bridge
        self._captured_metadata = captured_metadata

    async def arun(self, prompt: str, **kwargs: object) -> SimpleNamespace:
        self._captured_metadata.append(dict(kwargs["metadata"]))
        await self._bridge(
            "demo_tool",
            _demo_tool,
            {"text": prompt, "authorization": "Bearer hidden-token"},
        )
        await self.model.ainvoke(
            messages=[Message(role="user", content=prompt)],
            assistant_message=Message(role="assistant"),
            tools=[{"name": "demo_tool", "description": "Echo"}],
        )
        return SimpleNamespace(
            content="Done",
            tools=[],
            messages=[],
            run_id="run-live-test",
            session_id=kwargs["session_id"],
            status=RunStatus.completed,
            model=self.model.id,
            model_provider="openai",
        )


async def _demo_tool(*, text: str, authorization: str) -> dict[str, str]:
    return {"echo": text, "authorization": authorization}


def _config(runtime_paths: object) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _read_llm_entries(log_dir: Path) -> list[dict[str, object]]:
    log_files = list(log_dir.glob("llm-requests-*.jsonl"))
    if len(log_files) != 1:
        msg = f"expected one llm request log file, found {len(log_files)}"
        raise RuntimeError(msg)
    return _read_jsonl(log_files[0])


def _tool_calls_path(runtime_paths: object) -> Path:
    return tracking_dir(runtime_paths) / "tool_calls.jsonl"


def _read_structured_log_entry(log_dir: Path, event_name: str) -> dict[str, object]:
    log_files = sorted(log_dir.glob("mindroom_*.log"))
    if len(log_files) != 1:
        msg = f"expected one structured log file, found {len(log_files)}"
        raise RuntimeError(msg)
    for line in log_files[0].read_text(encoding="utf-8").splitlines():
        payload = json.loads(line)
        if payload.get("event") == event_name:
            return payload
    msg = f"missing structured log event {event_name!r}"
    raise RuntimeError(msg)


async def _run_live_check() -> dict[str, object]:  # noqa: PLR0915
    with tempfile.TemporaryDirectory(prefix="issue-154-live-") as tmp:
        tmp_path = Path(tmp)
        runtime_paths = test_runtime_paths(tmp_path)
        config = _config(runtime_paths)
        llm_log_dir = tmp_path / "llm"

        os.environ["MINDROOM_LOG_FORMAT"] = "json"
        setup_logging(level="INFO", runtime_paths=runtime_paths)

        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="GeneralAgent",
            password="test_password",  # noqa: S106
        )
        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=["!test:localhost"],
        )
        bot.client = AsyncMock()

        target = MessageTarget.resolve(
            room_id="!test:localhost",
            thread_id="$thread-root:localhost",
            reply_to_event_id="$event:localhost",
        )
        dispatch_context = ToolDispatchContext.from_target(
            agent_name="general",
            runtime_paths=runtime_paths,
            requester_user_id="@user:localhost",
            target=target,
        )
        model = _LoggingModel()
        install_llm_request_logging(
            model,
            debug_config=DebugConfig(log_llm_requests=True, llm_request_log_dir=str(llm_log_dir)),
            default_log_dir=tmp_path / "unused",
        )
        captured_metadata: list[dict[str, object]] = []
        bridge = build_tool_hook_bridge(
            HookRegistry.empty(),
            agent_name="general",
            dispatch_context=dispatch_context,
            runtime_paths=runtime_paths,
        )
        agent = _InvokeAgent(model=model, bridge=bridge, captured_metadata=captured_metadata)

        async def generate_response(request: PreparedDispatch) -> str | None:
            with (
                patch(
                    "mindroom.ai._prepare_agent_and_prompt",
                    new=AsyncMock(return_value=(agent, "expanded prompt", [], PreparedHistoryState())),
                ),
                patch("mindroom.ai._agent_tools_schema", return_value=[{"name": "demo_tool", "description": "Echo"}]),
            ):
                response = await ai_response(
                    agent_name="general",
                    prompt=request.prompt,
                    model_prompt="model prompt",
                    session_id=target.session_id or "session-1",
                    runtime_paths=runtime_paths,
                    config=config,
                    room_id=request.room_id,
                    thread_id=request.thread_id,
                    reply_to_event_id=request.reply_to_event_id,
                    user_id=request.user_id,
                    execution_identity=dispatch_context.execution_identity,
                    matrix_run_metadata=request.matrix_run_metadata,
                )
            if response != "Done":
                msg = f"unexpected response {response!r}"
                raise RuntimeError(msg)
            return "$response:localhost"

        response_runner = SimpleNamespace(
            generate_response=AsyncMock(side_effect=generate_response),
            generate_team_response_helper=AsyncMock(),
        )
        controller = replace_turn_controller_deps(
            bot,
            logger=get_logger("tests.issue_154.live"),
            response_runner=response_runner,
        )

        room = MagicMock()
        room.room_id = "!test:localhost"
        event = SimpleNamespace(
            event_id="$event:localhost",
            body="hello",
            source={},
        )
        dispatch = PreparedDispatch(
            requester_user_id="@user:localhost",
            context=SimpleNamespace(
                am_i_mentioned=False,
                thread_id=target.resolved_thread_id,
                thread_history=(),
                requires_full_thread_history=False,
            ),
            target=target,
            correlation_id="$event:localhost",
            envelope=MagicMock(),
        )

        await controller._execute_response_action(
            room,
            event,
            dispatch,
            ResponseAction(kind="individual"),
            AsyncMock(),
            processing_log="Preparing agent and prompt",
            dispatch_started_at=time.monotonic(),
            handled_turn=HandledTurnState.from_source_event_id(
                event.event_id,
                requester_id="@user:localhost",
                correlation_id="$event:localhost",
            ),
        )

        llm_entry = _read_llm_entries(llm_log_dir)[0]
        tool_entry = _read_jsonl(_tool_calls_path(runtime_paths))[0]
        log_dir = runtime_paths.storage_root / "logs"
        log_entry = _read_structured_log_entry(log_dir, "Preparing agent and prompt")
        turn_record = bot._turn_store.get_turn_record("$event:localhost")
        if turn_record is None:
            msg = "missing handled turn record"
            raise RuntimeError(msg)
        metadata = captured_metadata[0]

        correlation_values = {
            "llm_requests": llm_entry["correlation_id"],
            "tool_calls": tool_entry["correlation_id"],
            "structured_log": log_entry["correlation_id"],
            "agno_metadata": metadata["correlation_id"],
            "handled_turn": turn_record.correlation_id,
        }
        expected_correlation_id = "$event:localhost"
        if set(correlation_values.values()) != {expected_correlation_id}:
            msg = f"cross-sink correlation mismatch: {correlation_values!r}"
            raise RuntimeError(msg)

        if llm_entry["agent_id"] != "general" or llm_entry["model_id"] != "test-model":
            msg = "llm request log lost agent/model split"
            raise RuntimeError(msg)
        if tool_entry["success"] is not True or tool_entry["reply_to_event_id"] != "$event:localhost":
            msg = "tool call log lost success or reply linkage"
            raise RuntimeError(msg)
        if log_entry["requester_id"] != "@user:localhost":
            msg = "structured log lost requester_id"
            raise RuntimeError(msg)
        if turn_record.requester_id != "@user:localhost":
            msg = "handled turn lost requester_id"
            raise RuntimeError(msg)

        return {
            "status": "ok",
            "storage_root": str(runtime_paths.storage_root),
            "correlation_id": expected_correlation_id,
            "llm_request_path": str(next(llm_log_dir.glob("llm-requests-*.jsonl"))),
            "tool_calls_path": str(_tool_calls_path(runtime_paths)),
            "structured_log_path": str(next(log_dir.glob("mindroom_*.log"))),
            "correlation_values": correlation_values,
            "metadata_keys": sorted(metadata.keys()),
        }


async def main() -> int:
    """Run the live ISSUE-154 correlation check and print a JSON report."""
    try:
        result = await _run_live_check()
    finally:
        logging.shutdown()
        logging.getLogger().handlers.clear()
        structlog.reset_defaults()
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
