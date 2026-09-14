"""Native checkpoints participate in history planning without replacing stored runs."""

from __future__ import annotations

import base64
from base64 import b64encode
from dataclasses import replace
from io import BytesIO
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from agno.db.sqlite import SqliteDb
from agno.media import Image
from agno.metrics import MessageMetrics
from agno.models.message import Message
from agno.models.response import ModelResponse
from agno.session.summary import SessionSummary
from agno.team import Team
from PIL import Image as PillowImage

from mindroom.agent_storage import get_agent_session
from mindroom.anthropic_claude import MindRoomAnthropicClaude
from mindroom.config.agent import TeamConfig
from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.history.native import configure_native_history, restore_native_history
from mindroom.history.policy import classify_compaction_decision
from mindroom.history.replay import estimate_prompt_visible_history_tokens
from mindroom.history.runtime import (
    finalize_history_preparation,
    prepare_bound_scope_history,
    prepare_scope_history,
    resolve_agent_preparation_inputs,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.storage import write_scope_state
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.native_compaction import record_native_checkpoint
from mindroom.openai_models import MindRoomOpenAIResponses
from mindroom.token_budget import approximate_o200k_tokens, stable_serialize
from tests.conftest import FakeModel, seed_session
from tests.history_helpers import _agent, _completed_run, _completed_team_run, _make_config, _session, _team_session

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("representation", ["bytes", "path", "data_url", "content_block"])
def test_image_history_after_summary_does_not_repeat_compaction_for_encoded_bytes(
    tmp_path: Path,
    representation: str,
) -> None:
    """One uncompressed PNG must fit as visual input after a saved summary."""
    image_file = tmp_path / "image.png"
    PillowImage.new("RGB", (256, 256), "blue").save(image_file, compress_level=0)
    image_bytes = image_file.read_bytes()
    data_url = "data:image/png;base64," + b64encode(image_bytes).decode()
    if representation == "content_block":
        message = Message(
            role="user",
            content=[
                {"type": "input_text", "text": "Describe this image."},
                {"type": "input_image", "image_url": data_url},
            ],
        )
    else:
        image = {
            "bytes": Image(content=image_bytes, format="png"),
            "path": Image(filepath=str(image_file)),
            "data_url": Image(url=data_url),
        }[representation]
        message = Message(role="user", content="Describe this image.", images=[image])
    session = _session("session", runs=[_completed_run("recent", messages=[message])])
    session.summary = SessionSummary(summary="Earlier work is complete.")
    config, _ = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(reserve_tokens=1000, replay_window_tokens=10_000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=20_000)},
    )
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=True)
    resolved = resolve_agent_preparation_inputs(
        agent=_agent(model=model),
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    before = session.to_dict()
    request_before = model._format_messages([message])
    for _ in range(2):
        tokens = estimate_prompt_visible_history_tokens(
            session=session,
            scope=HistoryScope(kind="agent", scope_id="test_agent"),
            history_settings=resolved.history_settings,
            replay_model=model,
        )
        decision = classify_compaction_decision(
            plan=resolved.execution_plan,
            force_compact_before_next_run=False,
            current_history_tokens=tokens,
        )
        assert decision.mode == "none", (tokens, decision.reason)
        assert 77 < tokens < 2000
    assert session.to_dict() == before
    assert model._format_messages([message]) == request_before


def test_responses_image_budget_preserves_visual_cost_and_ignores_png_encoding_size() -> None:
    """The same pixels cost the same budget across lossless encodings."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra")
    estimates = []
    for compression in (0, 9):
        buffer = BytesIO()
        PillowImage.new("RGB", (256, 256), "blue").save(buffer, format="PNG", compress_level=compression)
        estimates.append(
            model.estimate_portable_replay_tokens(
                [Message(role="user", content="Describe.", images=[Image(content=buffer.getvalue(), format="png")])],
            ),
        )
    assert estimates[0] == estimates[1]
    assert 77 <= estimates[0] < 500
    assert estimates[0] > model.estimate_portable_replay_tokens([Message(role="user", content="Describe.")])


def test_responses_image_budget_reads_only_bounded_header_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Estimating a large image must not allocate another full decoded image."""
    buffer = BytesIO()
    PillowImage.new("RGB", (256, 256), "blue").save(buffer, format="PNG", compress_level=0)
    payload = "data:image/png;base64," + b64encode(buffer.getvalue()).decode()
    decoded_sizes = []
    decode = base64.b64decode

    def record_decode(value: str, *, validate: bool = False) -> bytes:
        result = decode(value, validate=validate)
        decoded_sizes.append(len(result))
        return result

    monkeypatch.setattr(base64, "b64decode", record_decode)
    tokens = MindRoomOpenAIResponses(id="gpt-6-astra").estimate_portable_replay_tokens(
        [
            Message(role="user", content=[{"type": "input_image", "image_url": payload}]),
        ],
    )
    assert 77 <= tokens < 200
    assert decoded_sizes
    assert max(decoded_sizes) <= 64 * 1024


def test_responses_jpeg_header_beyond_scan_budget_uses_conservative_allowance() -> None:
    """Valid JPEG metadata can push dimensions beyond the bounded header scan."""
    buffer = BytesIO()
    PillowImage.new("RGB", (1024, 1024), "blue").save(buffer, format="JPEG")
    image = buffer.getvalue()
    metadata = b"metadata" * 8000
    segment = b"\xff\xef" + (len(metadata) + 2).to_bytes(2, "big") + metadata
    image = image[:2] + segment + image[2:]
    with PillowImage.open(BytesIO(image)) as decoded:
        decoded.load()
        assert decoded.size == (1024, 1024)
    tokens = MindRoomOpenAIResponses(id="gpt-6-astra").estimate_portable_replay_tokens(
        [
            Message(role="user", content="Describe.", images=[Image(content=image, format="jpeg")]),
        ],
    )
    assert 36_000 <= tokens < 36_100


def test_responses_unrecognized_webp_header_uses_conservative_allowance() -> None:
    """Agno's silent default dimensions must not become measured dimensions."""
    header = b"RIFF\x10\x00\x00\x00WEBPJUNK\x04\x00\x00\x00test"
    tokens = MindRoomOpenAIResponses(id="gpt-6-astra").estimate_portable_replay_tokens(
        [
            Message(
                role="user",
                content=[
                    {
                        "type": "input_image",
                        "image_url": "data:image/webp;base64," + b64encode(header).decode(),
                    },
                ],
            ),
        ],
    )
    assert 36_000 <= tokens < 36_100


def test_native_checkpoint_tail_images_keep_visual_cost_without_transport_bytes(tmp_path: Path) -> None:
    """A checkpoint keeps its opaque budget while new image input uses visual tokens."""
    config, _ = _make_config(tmp_path)
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=1024)
    assert model.native_compaction is not None
    response = ModelResponse(content="Ready")
    record_native_checkpoint(
        response,
        [{"type": "compaction", "id": "cmp", "encrypted_content": "opaque-checkpoint" * 100}],
        model.native_compaction,
    )
    checkpoint = Message(role="assistant", content=response.content, provider_data=response.provider_data)
    resolved = resolve_agent_preparation_inputs(
        agent=_agent(model=model),
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    estimates = []
    for compression in (0, 9):
        buffer = BytesIO()
        PillowImage.new("RGB", (256, 256), "blue").save(buffer, format="PNG", compress_level=compression)
        tail = Message(
            role="user",
            content=[
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + b64encode(buffer.getvalue()).decode(),
                },
            ],
        )
        session = _session("session", runs=[_completed_run("recent", messages=[checkpoint, tail])])
        estimates.append(
            estimate_prompt_visible_history_tokens(
                session=session,
                scope=HistoryScope(kind="agent", scope_id="test_agent"),
                history_settings=resolved.history_settings,
                native_route=model.native_compaction.route,
                replay_model=model,
            ),
        )
    assert estimates[0] == estimates[1]
    assert 500 < estimates[0] < 2000


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "GIF", "WEBP"])
@pytest.mark.parametrize("detail", ["low", "high", "original", "auto"])
def test_responses_image_header_dimensions_keep_nonzero_visual_budget(image_format: str, detail: str) -> None:
    """Supported image headers all describe the same visual patch area."""
    buffer = BytesIO()
    PillowImage.new("RGB", (1024, 1024), "blue").save(buffer, format=image_format)
    model = MindRoomOpenAIResponses(id="gpt-6-astra")
    tokens = model.estimate_portable_replay_tokens(
        [
            Message(
                role="user",
                content="Describe.",
                images=[Image(content=buffer.getvalue(), format=image_format, detail=detail)],
            ),
        ],
    )
    visual_tokens = 308 if detail == "low" else 1229
    assert visual_tokens <= tokens < visual_tokens + 100


@pytest.mark.parametrize(
    "source",
    ["https://example.test/image.png", "data:image/png;base64,invalid", "data:image/png;base64,aGVsbG8="],
)
@pytest.mark.parametrize(
    ("detail", "visual_tokens"),
    [("low", 308), ("high", 3000), ("original", 36_000), ("auto", 36_000)],
)
def test_responses_unknown_image_dimensions_use_documented_allowance(
    source: str,
    detail: str,
    visual_tokens: int,
) -> None:
    """Remote or unreadable images retain cost without fetching or parsing arbitrary text."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra")
    tokens = model.estimate_portable_replay_tokens(
        [
            Message(role="user", content=[{"type": "input_image", "image_url": source, "detail": detail}]),
        ],
    )
    assert visual_tokens <= tokens < visual_tokens + 100


@pytest.mark.parametrize(
    ("model_id", "size", "detail", "visual_tokens"),
    [
        ("gpt-6-astra", (2048, 2048), "high", 3000),
        ("gpt-6-astra", (2048, 2048), "original", 4916),
        ("gpt-6-astra", (4096, 512), "high", 2458),
        ("gpt-5.6-sol", (2048, 2048), "auto", 4916),
        ("gpt-5.5", (2048, 2048), "auto", 4916),
        ("gpt-5.4", (2048, 2048), "auto", 3000),
    ],
)
def test_responses_image_budget_obeys_documented_patch_bounds(
    model_id: str,
    size: tuple[int, int],
    detail: str,
    visual_tokens: int,
) -> None:
    """Documented patch examples and default detail policies constrain image sizing."""
    buffer = BytesIO()
    PillowImage.new("RGB", size, "blue").save(buffer, format="PNG")
    tokens = MindRoomOpenAIResponses(id=model_id).estimate_portable_replay_tokens(
        [
            Message(
                role="user",
                content="Describe.",
                images=[Image(content=buffer.getvalue(), format="png", detail=detail)],
            ),
        ],
    )
    assert visual_tokens <= tokens < visual_tokens + 100


def test_responses_image_file_id_retains_unknown_dimension_allowance() -> None:
    """An uploaded image has visual cost even without locally available bytes."""
    tokens = MindRoomOpenAIResponses(id="gpt-6-astra").estimate_portable_replay_tokens(
        [
            Message(role="user", content=[{"type": "input_image", "file_id": "file_image", "detail": "high"}]),
        ],
    )
    assert 3000 <= tokens < 3100


def test_responses_budget_preserves_literal_data_urls_files_and_tool_arguments() -> None:
    """Only typed Responses image input is discounted, never similar-looking text."""
    literal = "data:image/png;base64," + "aGVsbG8=" * 1000
    messages = [
        Message(
            role="user",
            content=[
                {"type": "input_text", "text": literal},
                {"type": "input_file", "filename": "example.txt", "file_data": literal},
            ],
        ),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "call_example",
                    "type": "function",
                    "function": {
                        "name": "example",
                        "arguments": stable_serialize({"type": "input_image", "image_url": literal}),
                    },
                },
            ],
        ),
        Message(role="tool", tool_call_id="call_example", content=literal),
    ]
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    assert model.estimate_portable_replay_tokens(messages) == approximate_o200k_tokens(
        stable_serialize(model._format_messages(messages)),
    )


@pytest.mark.asyncio
async def test_bounded_legacy_replay_after_sqlite_restart_preserves_stored_history(tmp_path: Path) -> None:
    """Budget fitting must use safe explicit tool input without rewriting durable legacy records."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(enabled=False, reserve_tokens=1000, replay_window_tokens=1000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=5000)},
    )
    legacy = [
        Message(role="user", content="Check the service."),
        Message(
            role="assistant",
            tool_calls=[
                {
                    "id": "fc_status",
                    "call_id": "call_status",
                    "type": "function",
                    "function": {"name": "get_status", "arguments": "{}"},
                },
            ],
            provider_data={
                "response_id": "resp_legacy",
                "reasoning_output": {"type": "reasoning", "id": "rs_last", "summary": []},
            },
        ),
        Message(role="tool", tool_call_id="fc_status", content="ready"),
    ]
    db_path = str(tmp_path / "history.db")
    db = SqliteDb(db_file=db_path)
    seed_session(
        db,
        _session(
            "session",
            runs=[
                _completed_run("old", messages=[Message(role="user", content="0123456789" * 1800)]),
                _completed_run("recent", messages=legacy),
            ],
        ),
    )
    db.close()
    db = SqliteDb(db_file=db_path)
    session = get_agent_session(db, "session")
    assert session is not None
    original = [run.to_dict() for run in session.runs or []]
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=True)
    agent = _agent(model=model, db=db)
    resolved = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )

    prepared = await prepare_scope_history(
        agent=agent,
        agent_name="test_agent",
        resolved_inputs=resolved,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(HistoryScope(kind="agent", scope_id="test_agent"), db, session),
    )
    final = finalize_history_preparation(prepared_scope_history=prepared, config=config)

    assert final.replay_plan is not None
    assert final.replay_plan.mode == "limited"
    assert final.replay_plan.num_history_runs == 1
    messages = session.get_messages(agent_id="test_agent", last_n_runs=final.replay_plan.num_history_runs)
    assert model.estimate_portable_replay_tokens(messages) <= final.replay_plan.estimated_tokens <= 1000
    messages.append(Message(role="user", content="Continue"))
    assert "previous_response_id" not in model.get_request_params(messages=messages)
    assert model._format_messages(messages) == [
        {"role": "user", "content": "Check the service."},
        {
            "type": "function_call",
            "call_id": "call_status",
            "name": "get_status",
            "arguments": "{}",
            "status": "completed",
        },
        {"type": "function_call_output", "call_id": "call_status", "output": "ready"},
        {"role": "user", "content": "Continue"},
    ]
    persisted = get_agent_session(db, "session")
    assert persisted is not None
    assert [run.to_dict() for run in persisted.runs or []] == original
    assert [run.to_dict() for run in session.runs or []] == original
    db.close()


def test_reused_responses_model_uses_current_replay_budget(tmp_path: Path) -> None:
    """An unbounded plan must not inherit portable replay from a prior bounded plan."""
    config, _ = _make_config(tmp_path)
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=True)
    resolved = resolve_agent_preparation_inputs(
        agent=_agent(model=model),
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    messages = [
        Message(role="assistant", content="Previous answer", provider_data={"response_id": "resp_previous"}),
        Message(role="user", content="Continue"),
    ]
    for budget in (1000, None, 2000):
        configure_native_history(
            model,
            plan=replace(resolved.execution_plan, hard_replay_budget_tokens=budget),
            history_settings=resolved.history_settings,
            session=None,
            allowed=False,
        )
        request = model.get_request_params(messages=messages)
        replay = model._format_messages(messages)
        if budget is None:
            assert request["previous_response_id"] == "resp_previous"
            assert replay == [{"role": "user", "content": "Continue"}]
        else:
            assert "previous_response_id" not in request
            assert replay[0] == {"role": "assistant", "content": "Previous answer"}


@pytest.mark.parametrize("change", ["none", "model", "endpoint", "summary", "disabled", "missing", "invalid_threshold"])
def test_restore_native_policy_requires_latest_compatible_response(change: str) -> None:
    """A rebuild must not recover stale policy from an older checkpoint or foreign route."""
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=1024)
    checkpoint = ModelResponse(content="Ready")
    record_native_checkpoint(
        checkpoint,
        [{"type": "compaction", "id": "cmp", "encrypted_content": "small-checkpoint"}],
        model.native_compaction,
    )
    latest = ModelResponse(content="Waiting for approval")
    record_native_checkpoint(latest, [], None if change == "disabled" else model.native_compaction)
    if change == "missing":
        latest.provider_data = None
    elif change == "invalid_threshold":
        latest.provider_data["mindroom_native_compaction"]["threshold"] = True
    messages = [
        Message(role="user", content="Canonical facts"),
        Message(role="assistant", content=checkpoint.content, provider_data=checkpoint.provider_data),
        Message(role="user", content="Continue"),
        Message(role="assistant", content=latest.content, provider_data=latest.provider_data),
    ]
    run = _completed_run("paused", messages=messages)
    session = _session("session", runs=[run])
    rebuilt = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    rebuilt.configure_native_compaction(threshold=2048)
    if change == "model":
        rebuilt.id = "unsupported-route"
    elif change == "endpoint":
        rebuilt.base_url = "https://other.example/v1"
    elif change == "summary":
        session.summary = SessionSummary(summary="New portable summary")
    restore_native_history(rebuilt, persisted_run=run, session=session)
    replay = rebuilt._format_messages(messages)
    if change == "none":
        assert rebuilt.native_compaction is not None
        assert rebuilt.native_compaction.threshold == 1024
        assert replay[0]["type"] == "compaction"
    else:
        assert rebuilt.native_compaction is None
        assert replay[0] == {"role": "user", "content": "Canonical facts"}


@pytest.mark.asyncio
async def test_native_budget_keeps_large_canonical_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Counting canonical history here would compact it before the checkpoint reaches the API."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=120000)
    response = ModelResponse(content="Ready")
    record_native_checkpoint(
        response,
        [{"type": "compaction", "id": "cmp", "encrypted_content": "small-checkpoint"}],
        model.native_compaction,
    )
    session = _session(
        "session",
        runs=[
            _completed_run(
                "old",
                messages=[
                    Message(role="user", content="large old transcript " * 50000),
                    Message(role="assistant", content="Ready", provider_data=response.provider_data),
                ],
            ),
        ],
    )
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    seed_session(db, session)
    agent = _agent(model=model, db=db)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    text_compact = AsyncMock(side_effect=AssertionError("Canonical history must remain intact"))
    monkeypatch.setattr("mindroom.history.runtime._run_scope_compaction_with_lifecycle", text_compact)
    resolved = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    prepared = await prepare_scope_history(
        agent=agent,
        agent_name="test_agent",
        resolved_inputs=resolved,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(scope, db, session),
    )
    final = finalize_history_preparation(prepared_scope_history=prepared, config=config)
    assert final.replay_plan is not None
    assert final.replay_plan.add_history_to_context
    assert final.replay_plan.num_history_runs is None
    assert final.replay_plan.num_history_messages is None
    assert final.replay_plan.estimated_tokens < 1000
    assert final.replays_persisted_history
    assert len(session.runs or []) == 1
    assert len(session.runs[0].messages[0].content) > 900000
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_compact", [False, True])
async def test_proxy_replay_budget_counts_tokens_and_discards_stored_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auto_compact: bool,
) -> None:
    """Dense text and hidden stored context must not bypass portable replay fitting."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(enabled=auto_compact, reserve_tokens=1000, replay_window_tokens=6000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=30000)},
    )
    monkeypatch.setattr("mindroom.model_loading.get_model_instance", lambda *_args, **_kwargs: FakeModel(id="summary"))
    monkeypatch.setattr(
        "mindroom.history.compaction.generate_compaction_summary",
        AsyncMock(return_value=SessionSummary(summary="Keep the recent instruction")),
    )
    model = MindRoomOpenAIResponses(id="gpt-6-astra", base_url="https://proxy.example/v1", store=True)
    latest = [
        Message(role="user", content="Keep the recent instruction"),
        Message(
            role="assistant",
            content="Ready",
            provider_data={"response_id": "resp_large_context"},
            metrics=MessageMetrics(input_tokens=9000, output_tokens=10),
        ),
    ]
    session = _session(
        "session",
        runs=[
            _completed_run("old", messages=[Message(role="user", content="0123456789" * 1800)]),
            _completed_run("recent", messages=latest),
        ],
    )
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    seed_session(db, session)
    agent = _agent(model=model, db=db)
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    resolved = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    prepared = await prepare_scope_history(
        agent=agent,
        agent_name="test_agent",
        resolved_inputs=resolved,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(scope, db, session),
    )
    final = finalize_history_preparation(prepared_scope_history=prepared, config=config)
    assert final.replay_plan is not None
    if auto_compact:
        assert prepared.compaction_reply_outcome == "success"
        assert session.summary is not None
        assert session.summary.summary == "Keep the recent instruction"
        assert len(session.runs or []) == 0
    else:
        assert final.replay_plan.mode == "limited"
        assert final.replay_plan.num_history_runs == 1
        assert final.replay_plan.estimated_tokens < 100
        assert len(session.runs or []) == 2
    assert model.native_compaction is None
    messages = [*latest, Message(role="user", content="Continue")]
    assert "previous_response_id" not in model.get_request_params(messages=messages)
    assert model._format_messages(messages)[0] == {"role": "user", "content": "Keep the recent instruction"}
    assert latest[-1].provider_data == {"response_id": "resp_large_context"}
    db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["normal", "manual", "bounded", "custom_summary", "disabled", "scheduled"])
@pytest.mark.parametrize("authored_claude", [False, True])
async def test_native_activation_respects_history_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    *,
    authored_claude: bool,
) -> None:
    """Manual, bounded, custom-model, disabled, and scheduled policies keep portable semantics."""
    compaction = CompactionConfig(threshold_tokens=120000, enabled=case != "disabled")
    if case == "custom_summary":
        compaction.model = "summary"
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=compaction,
        num_history_runs=2 if case == "bounded" else None,
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000),
            "summary": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000),
        },
    )
    model = (
        MindRoomAnthropicClaude(id="claude-sonnet-5", context_management={"edits": [{"type": "compact_20260112"}]})
        if authored_claude
        else MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    )
    session = _session("session", runs=[_completed_run("old")])
    scope = HistoryScope(kind="agent", scope_id="test_agent")
    if case == "manual":
        write_scope_state(session, scope, HistoryScopeState(force_compact_before_next_run=True))
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    seed_session(db, session)
    agent = _agent(model=model, db=db)
    if case == "manual":
        # The policy must reach the existing text owner, with native replay disabled.
        async def text_compact(**_kwargs: object) -> None:
            assert model.native_compaction is None
            message = "Reached portable compaction"
            raise RuntimeError(message)

        monkeypatch.setattr("mindroom.history.runtime._run_scope_compaction_with_lifecycle", text_compact)
    resolved = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    kwargs = dict(  # noqa: C408
        agent=agent,
        agent_name="test_agent",
        resolved_inputs=resolved,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(scope, db, session),
        allow_native_compaction=case != "scheduled",
    )
    if case == "manual":
        with pytest.raises(RuntimeError, match="Reached portable compaction"):
            await prepare_scope_history(**kwargs)
    else:
        await prepare_scope_history(**kwargs)
        assert (model.native_compaction is not None) is (case == "normal")
    db.close()


@pytest.mark.asyncio
async def test_text_summary_change_invalidates_checkpoint(tmp_path: Path) -> None:
    """A retained native checkpoint cannot hide a newer portable summary rewrite."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    model.configure_native_compaction(threshold=120000)
    old_route = model.native_compaction.route
    session = _session("session", summary=SessionSummary(summary="New portable summary."))
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    seed_session(db, session)
    agent = _agent(model=model, db=db)
    resolved = resolve_agent_preparation_inputs(
        agent=agent,
        agent_name="test_agent",
        full_prompt="Continue",
        config=config,
        static_prompt_tokens=100,
    )
    await prepare_scope_history(
        agent=agent,
        agent_name="test_agent",
        resolved_inputs=resolved,
        runtime_paths=paths,
        config=config,
        scope_context=ScopeSessionContext(HistoryScope(kind="agent", scope_id="test_agent"), db, session),
    )
    assert model.native_compaction is not None
    assert model.native_compaction.route != old_route
    db.close()


@pytest.mark.asyncio
async def test_team_native_activation_uses_team_model(tmp_path: Path) -> None:
    """A team's native policy belongs to its leader model, not its first member."""
    config, paths = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(threshold_tokens=120000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=200000)},
    )
    config.teams["team"] = TeamConfig(agents=["test_agent"], display_name="Team", role="Coordinate work.")
    member = _agent()
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=False)
    team = Team(id="team", model=model, members=[member])
    db = SqliteDb(db_file=str(tmp_path / "history.db"))
    session = _team_session("session", team_id="team", runs=[_completed_team_run("run", team_id="team")])
    seed_session(db, session)
    await prepare_bound_scope_history(
        agents=[member],
        team=team,
        team_name="team",
        full_prompt="Continue.",
        config=config,
        runtime_paths=paths,
        scope_context=ScopeSessionContext(HistoryScope(kind="team", scope_id="team"), db, session),
        active_model_name="default",
        active_context_window=200000,
        static_prompt_tokens=100,
    )
    assert model.native_compaction is not None
    assert model.native_compaction.threshold == 120000
    db.close()


@pytest.mark.parametrize(
    ("provider_data", "portable"),
    [
        pytest.param({"mindroom_portable_replay": True}, True, id="saved-canonical"),
        pytest.param(
            {
                "mindroom_portable_replay": False,
                "mindroom_response_output": [{"type": "reasoning", "encrypted_content": "opaque"}],
            },
            False,
            id="saved-continuation-wins",
        ),
        pytest.param({}, False, id="legacy-unknown-policy"),
        pytest.param({"mindroom_portable_replay": None}, False, id="null-policy"),
        pytest.param({"mindroom_portable_replay": 1}, False, id="integer-policy"),
        pytest.param({"mindroom_portable_replay": "true"}, False, id="string-policy"),
        pytest.param({"mindroom_portable_replay": {}}, False, id="object-policy"),
        pytest.param({"mindroom_response_stored": False}, True, id="legacy-unstored"),
        pytest.param(
            {"mindroom_native_compaction": {"route": "old-route", "threshold": 1024}},
            True,
            id="legacy-native",
        ),
        pytest.param(
            {"mindroom_native_compaction": {"route": "old-route", "threshold": True}},
            False,
            id="malformed-native",
        ),
        pytest.param(
            {"mindroom_native_compaction": {"route": "", "threshold": 1024}},
            False,
            id="empty-native-route",
        ),
        pytest.param(
            {"mindroom_response_output": [{"type": "reasoning", "encrypted_content": "opaque"}]},
            True,
            id="legacy-ordered-reasoning",
        ),
        pytest.param({"mindroom_response_output": []}, False, id="empty-ordered-output"),
        pytest.param({"mindroom_response_output": [None]}, False, id="malformed-ordered-output"),
        pytest.param(
            {"mindroom_response_output": [{"type": "reasoning", "encrypted_content": ""}]},
            False,
            id="empty-reasoning",
        ),
        pytest.param(
            {"mindroom_response_output": [{"type": "reasoning", "encrypted_content": True}]},
            False,
            id="malformed-reasoning",
        ),
    ],
)
def test_restore_portable_policy_requires_latest_provenance(provider_data: dict[str, Any], *, portable: bool) -> None:
    """Without legacy provenance the original budget is unknown; retain stored continuation."""
    messages = [
        Message(
            role="assistant",
            content="Older answer",
            provider_data={"response_id": "resp_older", "mindroom_portable_replay": not portable},
        ),
        Message(
            role="assistant",
            content="Latest answer",
            provider_data={"response_id": "resp_latest", "mindroom_response_stored": True, **provider_data},
        ),
        Message(role="user", content="Continue"),
    ]
    model = MindRoomOpenAIResponses(id="gpt-6-astra", store=True)
    model.configure_portable_replay(enabled=not portable)
    restore_native_history(model, persisted_run=_completed_run("paused", messages=messages), session=None)
    request = model.get_request_params(messages=messages)
    if portable:
        assert "previous_response_id" not in request
    else:
        assert request["previous_response_id"] == "resp_latest"
        assert model._format_messages(messages) == [{"role": "user", "content": "Continue"}]
