"""Internal model calls retain returned usage as admin-only system overhead."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from mindroom import helper_usage, routing, thread_summary, topic_generator
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
from mindroom.matrix.identity import MatrixID
from mindroom.scheduling import tasks as scheduling
from mindroom.usage.stats import collect_admin_usage
from tests.conftest import bind_runtime_paths, install_model_providers, test_runtime_paths
from tests.fakes.scripted_model import ScriptedModel, answer, usage
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
async def test_internal_calls_retain_each_paid_run_even_when_output_is_rejected(  # noqa: C901, PLR0912 - four caller paths
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
    if storage_failure:

        async def fail_write(*_args: object, **_kwargs: object) -> None:
            raise OSError

        monkeypatch.setattr(helper_usage, "run_session_storage_operation", fail_write)

    if caller == "routing":
        good_answer = json.dumps({"entity_name": "helper", "reasoning": "Matched"})
    elif caller == "schedule":
        good_answer = scheduling.ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=scheduling.CronSchedule(minute="0", hour="9"),
            message="Check",
            description="Check",
        ).model_dump_json(exclude_unset=True)
    elif caller == "topic":
        good_answer = json.dumps({"topic": "A room topic"})
    else:
        good_answer = json.dumps({"summary": "A summary"})
    model = ScriptedModel(
        [answer(good_answer, usage(20, 2)), answer("PRIVATE MALFORMED MODEL CONTENT", usage(30, 3))],
    )
    install_model_providers(monkeypatch, {"default": model})

    if caller == "routing":

        async def invoke() -> object:
            return await routing.suggest_responder("Help", ["helper"], config, paths)

    elif caller == "schedule":
        responders = [MatrixID(username="helper", domain="localhost")]

        async def invoke() -> object:
            return await scheduling._parse_workflow_schedule("Daily at 9", config, paths, responders)

    elif caller == "topic":

        async def invoke() -> object:
            return await topic_generator.generate_room_topic_ai("lobby", "Lobby", config, paths)

    else:

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
