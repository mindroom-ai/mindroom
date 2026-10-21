"""History budgets size Responses history as its explicit replay sends it, pricing images by their visual patches."""

from __future__ import annotations

import base64
import json
from base64 import b64encode
from copy import deepcopy
from datetime import UTC, datetime
from io import BytesIO
from random import Random
from typing import TYPE_CHECKING, Any

import pytest
from PIL import Image as PillowImage
from structlog.testing import capture_logs

from mindroom.config.models import CompactionConfig, ModelConfig
from mindroom.history.policy import classify_compaction_decision
from mindroom.history.replay import estimate_prompt_visible_history_tokens
from mindroom.history.runtime import (
    finalize_history_preparation,
    prepare_scope_history,
    resolve_entity_preparation_inputs,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryPolicy, HistoryScope, ResolvedHistorySettings, ScopeSummary
from mindroom.llm.media import Image
from mindroom.llm.messages import Message
from mindroom.llm.provider import NativeCompactionReplay
from mindroom.native_compaction import checkpoint_estimated_tokens, record_native_checkpoint
from mindroom.providers.openai.responses_wire import responses_input
from mindroom.session_storage.agent_storage import create_session_storage
from mindroom.token_budget import approximate_o200k_tokens, stable_serialize
from tests.conftest import seed_session
from tests.history_helpers import _completed_run, _make_config, _session
from tests.providers.golden_support import pipeline_provider
from tests.providers.openai_family_support import responses_adapter

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.providers.openai.responses import ResponsesAdapter

_SCOPE = HistoryScope(kind="agent", scope_id="test_agent")
_ALL_HISTORY = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)


def _replayed_input(adapter: ResponsesAdapter, messages: list[Message]) -> list[Any]:
    """Return the ``input`` an explicitly replayed request on ``adapter`` sends for ``messages``."""
    return responses_input(
        messages,
        store=False,
        portable_replay=True,
        native_compaction=None,
        cache_system_prompt=adapter.settings.cache_system_prompt,
        explicit_breakpoint=adapter.wire.explicit_breakpoint,
    )


def _png(size: tuple[int, int] = (256, 256), *, compress_level: int = 6) -> bytes:
    buffer = BytesIO()
    PillowImage.new("RGB", size, "blue").save(buffer, format="PNG", compress_level=compress_level)
    return buffer.getvalue()


@pytest.mark.parametrize("representation", ["bytes", "path", "data_url", "content_block"])
def test_image_history_after_summary_does_not_repeat_compaction_for_encoded_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    session = _session(
        "session",
        runs=[_completed_run("recent", messages=[message])],
        summary=ScopeSummary(text="Earlier work is complete.", updated_at=datetime.now(UTC)),
    )
    config, _ = _make_config(
        tmp_path,
        defaults_compaction=CompactionConfig(reserve_tokens=1000, replay_window_tokens=10_000),
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra", context_window=20_000)},
    )
    adapter = responses_adapter(tmp_path, store=True)
    resolved = resolve_entity_preparation_inputs(
        config=config,
        entity_name="test_agent",
        static_prompt_tokens=100,
        active_model_name=None,
        active_context_window=None,
    )
    before = deepcopy(session)
    request_before = _replayed_input(adapter, [message])
    serialized_sizes = []
    serialize = json.dumps

    def record_serialization(value: object, **kwargs: Any) -> str:  # noqa: ANN401
        result = serialize(value, **kwargs)
        serialized_sizes.append(len(result))
        return result

    monkeypatch.setattr(json, "dumps", record_serialization)
    for _ in range(2):
        tokens = estimate_prompt_visible_history_tokens(
            session=session,
            scope=_SCOPE,
            history_settings=resolved.history_settings,
            replay_model=adapter,
        )
        decision = classify_compaction_decision(
            plan=resolved.execution_plan,
            force_compact_before_next_run=False,
            current_history_tokens=tokens,
        )
        assert decision.mode == "none", (tokens, decision.reason)
        assert 77 < tokens < 2000
    monkeypatch.undo()
    assert serialized_sizes
    # Estimation must not serialize image transport even for a discarded fallback.
    assert max(serialized_sizes) < 10_000
    assert session == before
    assert _replayed_input(adapter, [message]) == request_before


@pytest.mark.parametrize("floor", ["canonical", "provider"])
@pytest.mark.parametrize("native", [False, True])
def test_unknown_image_model_preserves_transport_floors(tmp_path: Path, floor: str, *, native: bool) -> None:
    """Unknown models retain content-block floors and conservatively price viewed images."""
    buffer = BytesIO()
    # Seeded pixels keep the provider-dominant transport fixture reproducible.
    pixels = (
        PillowImage.new("RGB", (256, 256), "blue")
        if floor == "canonical"
        else PillowImage.frombytes("RGB", (256, 256), Random(0).randbytes(256 * 256 * 3))  # noqa: S311
    )
    pixels.save(buffer, format="PNG", compress_level=0)
    if floor == "canonical":
        block = {"type": "input_image", "image_url": "data:image/png;base64," + b64encode(buffer.getvalue()).decode()}
        message = Message(role="user", content=[block])
        expected_tokens = (len(stable_serialize(block)) + (3 if native else 0)) // 4
    else:
        message = Message(
            role="user",
            content="Describe.",
            images=[Image(id="mindroom_viewed_fixture", content=buffer.getvalue(), format="png")],
        )
    adapter = responses_adapter(tmp_path, "unrecognized-image-model")
    request_before = _replayed_input(adapter, [message])
    provider_tokens = approximate_o200k_tokens(stable_serialize(request_before))
    if floor == "canonical":
        assert expected_tokens > provider_tokens
    else:
        # Historical viewed images use a bounded visual fallback rather than encoded transport text.
        assert provider_tokens > 100_000
        expected_tokens = adapter.estimate_portable_replay_tokens([Message(role="user", content="Describe.")]) + 5_000
    messages = [message]
    route = "unknown-image-route" if native else None
    if native:
        messages.insert(
            0,
            Message(
                role="assistant",
                provider_data={
                    "mindroom_native_compaction": {
                        "route": route,
                        "items": [{"type": "compaction", "encrypted_content": "checkpoint"}],
                    },
                },
            ),
        )
        expected_tokens += 15  # Serialized checkpoint is 59 characters, rounded up.
    session = _session("session", runs=[_completed_run("recent", messages=messages)])
    before = deepcopy(session)
    tokens = estimate_prompt_visible_history_tokens(
        session=session,
        scope=_SCOPE,
        history_settings=_ALL_HISTORY,
        native_route=route,
        replay_model=adapter,
    )
    assert tokens == expected_tokens
    assert session == before
    assert _replayed_input(adapter, [message]) == request_before


def test_responses_image_budget_preserves_visual_cost_and_ignores_png_encoding_size(tmp_path: Path) -> None:
    """The same pixels cost the same budget across lossless encodings."""
    adapter = responses_adapter(tmp_path)
    estimates = [
        adapter.estimate_portable_replay_tokens(
            [
                Message(
                    role="user",
                    content="Describe.",
                    images=[Image(content=_png(compress_level=compression), format="png")],
                ),
            ],
        )
        for compression in (0, 9)
    ]
    assert estimates[0] == estimates[1]
    assert 77 <= estimates[0] < 500
    assert estimates[0] > adapter.estimate_portable_replay_tokens([Message(role="user", content="Describe.")])


def test_responses_image_budget_reads_only_bounded_header_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Estimating a large image must not allocate another full decoded image."""
    payload = "data:image/png;base64," + b64encode(_png(compress_level=0)).decode()
    decoded_sizes = []
    decode = base64.b64decode

    def record_decode(value: str, *, validate: bool = False) -> bytes:
        result = decode(value, validate=validate)
        decoded_sizes.append(len(result))
        return result

    adapter = responses_adapter(tmp_path)
    monkeypatch.setattr(base64, "b64decode", record_decode)
    tokens = adapter.estimate_portable_replay_tokens(
        [Message(role="user", content=[{"type": "input_image", "image_url": payload}])],
    )
    assert 77 <= tokens < 200
    assert decoded_sizes
    assert max(decoded_sizes) <= 64 * 1024


def test_responses_jpeg_header_beyond_scan_budget_uses_conservative_allowance(tmp_path: Path) -> None:
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
    tokens = responses_adapter(tmp_path).estimate_portable_replay_tokens(
        [Message(role="user", content="Describe.", images=[Image(content=image, format="jpeg")])],
    )
    assert 36_000 <= tokens < 36_100


def test_responses_unrecognized_webp_header_uses_conservative_allowance(tmp_path: Path) -> None:
    """A WebP chunk with no known dimensions is priced at the ceiling, never at made-up dimensions."""
    header = b"RIFF\x10\x00\x00\x00WEBPJUNK\x04\x00\x00\x00test"
    tokens = responses_adapter(tmp_path).estimate_portable_replay_tokens(
        [
            Message(
                role="user",
                content=[{"type": "input_image", "image_url": "data:image/webp;base64," + b64encode(header).decode()}],
            ),
        ],
    )
    assert 36_000 <= tokens < 36_100


def test_native_checkpoint_tail_images_keep_visual_cost_without_transport_bytes(tmp_path: Path) -> None:
    """A checkpoint keeps its opaque budget while new image input uses visual tokens."""
    adapter = responses_adapter(tmp_path, store=False)
    native = adapter.native_compaction_settings(threshold=1024)
    assert native is not None
    checkpoint = Message(role="assistant", content="Ready")
    record_native_checkpoint(
        checkpoint,
        [{"type": "compaction", "id": "cmp", "encrypted_content": "opaque-checkpoint" * 100}],
        native,
    )
    estimates = []
    for compression in (0, 9):
        tail = Message(
            role="user",
            content=[
                {
                    "type": "input_image",
                    "image_url": "data:image/png;base64," + b64encode(_png(compress_level=compression)).decode(),
                },
            ],
        )
        session = _session("session", runs=[_completed_run("recent", messages=[checkpoint, tail])])
        estimates.append(
            estimate_prompt_visible_history_tokens(
                session=session,
                scope=_SCOPE,
                history_settings=_ALL_HISTORY,
                native_route=native.route,
                replay_model=adapter,
            ),
        )
    assert estimates[0] == estimates[1]
    assert 500 < estimates[0] < 2000


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "GIF", "WEBP"])
@pytest.mark.parametrize("detail", ["low", "high", "original", "auto"])
def test_responses_image_header_dimensions_keep_nonzero_visual_budget(
    tmp_path: Path,
    image_format: str,
    detail: str,
) -> None:
    """Supported image headers all describe the same visual patch area."""
    buffer = BytesIO()
    PillowImage.new("RGB", (1024, 1024), "blue").save(buffer, format=image_format)
    tokens = responses_adapter(tmp_path).estimate_portable_replay_tokens(
        [
            Message(
                role="user",
                content="Describe.",
                images=[Image(content=buffer.getvalue(), format=image_format.lower(), detail=detail)],
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
    tmp_path: Path,
    source: str,
    detail: str,
    visual_tokens: int,
) -> None:
    """Remote or unreadable images retain cost without fetching or parsing arbitrary text."""
    tokens = responses_adapter(tmp_path).estimate_portable_replay_tokens(
        [Message(role="user", content=[{"type": "input_image", "image_url": source, "detail": detail}])],
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
    tmp_path: Path,
    model_id: str,
    size: tuple[int, int],
    detail: str,
    visual_tokens: int,
) -> None:
    """Documented patch examples and default detail policies constrain image sizing."""
    tokens = responses_adapter(tmp_path, model_id).estimate_portable_replay_tokens(
        [Message(role="user", content="Describe.", images=[Image(content=_png(size), format="png", detail=detail)])],
    )
    assert visual_tokens <= tokens < visual_tokens + 100


def test_responses_image_file_id_retains_unknown_dimension_allowance(tmp_path: Path) -> None:
    """An uploaded image has visual cost even without locally available bytes."""
    tokens = responses_adapter(tmp_path).estimate_portable_replay_tokens(
        [Message(role="user", content=[{"type": "input_image", "file_id": "file_image", "detail": "high"}])],
    )
    assert 3000 <= tokens < 3100


def test_responses_budget_preserves_literal_data_urls_files_and_tool_arguments(tmp_path: Path) -> None:
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
    adapter = responses_adapter(tmp_path, store=False)
    assert adapter.estimate_portable_replay_tokens(messages) == approximate_o200k_tokens(
        stable_serialize(_replayed_input(adapter, messages)),
    )


def test_models_without_a_patch_budget_count_the_replayed_input_as_text(tmp_path: Path) -> None:
    """A model with no documented patch budget prices the replayed input, images included, as serialized text."""
    adapter = responses_adapter(tmp_path, "unrecognized-image-model")
    messages = [Message(role="user", content="Describe.", images=[Image(content=_png(), format="png")])]

    assert adapter.portable_replay_uses_visual_tokens() is False
    assert responses_adapter(tmp_path).portable_replay_uses_visual_tokens() is True
    assert adapter.estimate_portable_replay_tokens(messages) == approximate_o200k_tokens(
        stable_serialize(_replayed_input(adapter, messages)),
    )


def test_native_checkpoint_estimate_does_not_count_the_summary_it_replaced() -> None:
    """A checkpoint already covers the summary message before it, which the projection drops."""
    route = "route-1"
    checkpoint = Message(
        role="assistant",
        content="",
        provider_data={
            "mindroom_native_compaction": {
                "route": route,
                "threshold": 100,
                "checkpoint_prefix": True,
                "items": [{"type": "compaction", "content": "provider checkpoint"}],
            },
        },
    )
    runs = [_completed_run("run-1", messages=[Message(role="user", content="question"), checkpoint])]

    without_summary = estimate_prompt_visible_history_tokens(
        session=_session("session", runs=runs),
        scope=_SCOPE,
        history_settings=_ALL_HISTORY,
        native_route=route,
    )
    with_summary = estimate_prompt_visible_history_tokens(
        session=_session("session", runs=runs, summary=ScopeSummary(text="S" * 400, updated_at=datetime.now(UTC))),
        scope=_SCOPE,
        history_settings=_ALL_HISTORY,
        native_route=route,
    )

    assert with_summary == without_summary


@pytest.mark.parametrize(
    ("items", "logged"),
    [
        ([{"type": "compaction", "content": "provider checkpoint"}, {"type": "text", "text": "Done"}], True),
        ([{"type": "compaction", "content": None}, {"type": "text", "text": "Done"}], False),
        ([{"type": "text", "text": "Done"}], False),
    ],
)
def test_provider_compaction_logs_once_per_checkpoint(items: list[dict[str, Any]], *, logged: bool) -> None:
    """Only a response that carries a real checkpoint reports that the provider compacted."""
    native = NativeCompactionReplay(route="route-1", threshold=60_000)

    with capture_logs() as logs:
        record_native_checkpoint(Message(role="assistant", content="Done"), items, native)

    events = [log for log in logs if log["event"] == "Native compaction checkpoint"]
    expected = {
        "event": "Native compaction checkpoint",
        "log_level": "info",
        "threshold_tokens": 60_000,
        "checkpoint_tokens": checkpoint_estimated_tokens(items[:1]),
    }
    assert events == ([expected] if logged else [])


@pytest.mark.asyncio
async def test_reply_preparation_sizes_and_fits_history_with_the_route_estimate(tmp_path: Path) -> None:
    """A reply on a Responses route sizes its stored history and fits its replay with the route's own count."""
    config, runtime_paths = _make_config(tmp_path)
    storage = create_session_storage("test_agent", config, runtime_paths, execution_identity=None)
    spaced_answer = Message(role="assistant", content="x " * 2000)
    session = seed_session(
        storage,
        _session(
            "session-1",
            runs=[_completed_run("run-1", messages=[Message(role="user", content="Hi"), spaced_answer])],
        ),
    )
    adapter = responses_adapter(tmp_path)
    inputs = resolve_entity_preparation_inputs(
        config=config,
        entity_name="test_agent",
        static_prompt_tokens=0,
        active_model_name=None,
        active_context_window=None,
    )
    route_estimate = estimate_prompt_visible_history_tokens(
        session=session,
        scope=_SCOPE,
        history_settings=inputs.history_settings,
        replay_model=adapter,
    )
    canonical_estimate = estimate_prompt_visible_history_tokens(
        session=session,
        scope=_SCOPE,
        history_settings=inputs.history_settings,
    )

    prepared = await prepare_scope_history(
        agent_name="test_agent",
        scope=_SCOPE,
        model=pipeline_provider(adapter),
        resolved_inputs=inputs,
        non_prompt_tokens=0,
        runtime_paths=runtime_paths,
        config=config,
        scope_context=ScopeSessionContext(scope=_SCOPE, storage=storage, session=session, session_id="session-1"),
        allow_native_compaction=False,
    )
    final = finalize_history_preparation(prepared_scope_history=prepared, config=config, static_prompt_tokens=0)

    assert route_estimate > canonical_estimate
    assert prepared.compaction_decision.current_history_tokens == route_estimate
    assert final.replay_plan is not None
    assert final.replay_plan.estimated_tokens == route_estimate
