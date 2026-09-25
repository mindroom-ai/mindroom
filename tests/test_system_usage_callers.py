"""Internal model calls retain returned usage as admin-only system overhead."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from agno.metrics import MessageMetrics, RunMetrics
from agno.models.message import Message
from agno.run.agent import RunOutput

from mindroom import helper_usage, model_loading, routing, scheduling, thread_summary, topic_generator
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.identity import MatrixID
from mindroom.usage_stats import collect_admin_usage
from tests.conftest import bind_runtime_paths, test_runtime_paths
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
@pytest.mark.parametrize("storage_failure", [False, True])
@pytest.mark.parametrize(
    ("caller", "kind"),
    [
        ("routing", "routing"),
        ("schedule", "schedule_parse"),
        ("topic", "room_topic"),
        ("summary", "thread_summary"),
    ],
)
async def test_internal_calls_retain_each_paid_run_even_when_output_is_rejected(  # noqa: C901, PLR0912, PLR0915 - four caller paths
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caller: str,
    kind: str,
    storage_failure: bool,
) -> None:
    """Dropping a returned run or parsing its content first must fail this test."""
    paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"helper": AgentConfig(display_name="Helper", role="Help")},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        paths,
    )
    persist_entity_accounts(config, paths)
    monkeypatch.setattr(model_loading, "get_model_instance", lambda *_args: SimpleNamespace(id="test-model"))
    if storage_failure:

        async def fail_write(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(helper_usage, "run_session_storage_operation", fail_write)

    good_content: object
    if caller == "routing":
        good_content = {"entity_name": "helper", "reasoning": "Matched"}
    elif caller == "schedule":
        good_content = scheduling.ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=scheduling.CronSchedule(minute="0", hour="9"),
            message="Check",
            description="Check",
        )
    elif caller == "topic":
        good_content = topic_generator._RoomTopic(topic="A room topic")
    else:
        good_content = thread_summary._ThreadSummary(summary="A summary")

    good_message = Message(
        role="assistant",
        created_at=1_723_837_600,
        metrics=MessageMetrics(input_tokens=20, output_tokens=2, total_tokens=22),
    )
    bad_message = Message(
        role="assistant",
        created_at=1_723_837_601,
        metrics=MessageMetrics(input_tokens=30, output_tokens=3, total_tokens=33),
    )
    outputs = iter(
        [
            RunOutput(
                run_id=f"{caller}-good",
                model="test-model",
                model_provider="test",
                content=good_content,
                created_at=1_723_837_600,
                metrics=RunMetrics(input_tokens=20, output_tokens=2, total_tokens=22),
                messages=[good_message],
            ),
            RunOutput(
                run_id=f"{caller}-bad",
                model="test-model",
                model_provider="test",
                content="PRIVATE MALFORMED MODEL CONTENT",
                created_at=1_723_837_601,
                metrics=RunMetrics(input_tokens=30, output_tokens=3, total_tokens=33),
                messages=[bad_message],
            ),
        ],
    )

    async def model_run(*_args: object, **_kwargs: object) -> RunOutput:
        return next(outputs)

    class ModelAgent:
        def __init__(self, **_kwargs: object) -> None:
            pass

        arun = model_run

    if caller == "routing":
        monkeypatch.setattr(routing, "Agent", ModelAgent)

        async def invoke() -> object:
            return await routing.suggest_responder("Help", ["helper"], config, paths)

    elif caller == "schedule":
        monkeypatch.setattr(scheduling, "Agent", ModelAgent)
        responders = [MatrixID(username="helper", domain="localhost")]

        async def invoke() -> object:
            return await scheduling._parse_workflow_schedule("Daily at 9", config, paths, responders)

    elif caller == "topic":
        monkeypatch.setattr(topic_generator, "Agent", ModelAgent)

        async def invoke() -> object:
            return await topic_generator.generate_room_topic_ai("lobby", "Lobby", config, paths)

    else:
        monkeypatch.setattr(thread_summary, "Agent", ModelAgent)

        async def invoke() -> object:
            return await thread_summary._generate_summary(
                [ResolvedVisibleMessage.synthetic(sender="@person:localhost", body="Help", event_id="$message")],
                config,
                paths,
                trusted_sender_ids=frozenset(),
            )

    valid = await invoke()
    if caller == "routing":
        assert isinstance(valid, routing.ResponderSelection)
        assert valid.entity_name == "helper"
    elif caller == "schedule":
        assert isinstance(valid, scheduling.ScheduledWorkflow)
        assert valid.schedule_type == "cron"
    elif caller == "topic":
        assert valid == "A room topic"
    else:
        assert valid == "A summary"
    if storage_failure:
        return
    rejected = await invoke()
    if caller in {"routing", "summary"}:
        assert rejected is None

    report = collect_admin_usage(config=config, runtime_paths=paths, include_requests=True)
    assert report.totals.total_tokens == 55
    assert report.totals.input_tokens == 50
    assert report.totals.output_tokens == 5
    assert report.request_breakdown is not None
    assert sorted((row.kind, row.totals.total_tokens, row.user_id) for row in report.request_breakdown) == [
        (kind, 22, None),
        (kind, 33, None),
    ]
    assert b"PRIVATE MALFORMED MODEL CONTENT" not in (paths.storage_root / "system/sessions/system.db").read_bytes()
