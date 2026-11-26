"""Tests for outage auto-resume: connectivity state, corroboration, pipeline stage, fallback, and errors."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from functools import partial
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom import connectivity
from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.llm.errors import (
    CONNECTION_ERROR,
    QUOTA_EXHAUSTED,
    STREAM_STALLED,
    TLS_CERTIFICATE_ERROR,
    TRANSPORT_TIMEOUT,
    AuthenticationFailed,
    ContextWindowExceeded,
    ProviderError,
    ProviderUnavailable,
    RateLimited,
)
from mindroom.llm.media import Image
from mindroom.llm.messages import AGGREGATE_USAGE_KEY, Message
from mindroom.llm.provider import (
    Capabilities,
    ModelIdentity,
    ModelRequest,
    ReasoningDelta,
    RequestOverrides,
    StreamProgress,
    StreamTracker,
    TextDelta,
    ToolCallReady,
    TurnCompleted,
    UsageReported,
)
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.matrix.health import (
    clear_matrix_sync_state,
    mark_matrix_sync_failure,
    mark_matrix_sync_loop_started,
    mark_matrix_sync_success,
    reset_matrix_sync_health,
)
from mindroom.matrix_rtc.call_tools import _spoken
from mindroom.orchestration import runtime as orchestration_runtime
from mindroom.providers.errors import provider_failure
from mindroom.providers.pipeline import connectivity as connectivity_stage
from mindroom.providers.pipeline import fallback as fallback_stage
from mindroom.providers.pipeline.connectivity import connectivity_retry_stream
from mindroom.providers.pipeline.fallback import ChainModel, fallback_stream
from mindroom.providers.pipeline.media import media_fallback_projection, media_fallback_stream
from mindroom.providers.pipeline.wrapped import WrappedProvider
from mindroom.tool_system.runtime_context import current_plugins
from mindroom.tool_system.worker_routing import ToolExecutionIdentity
from mindroom.turn_model import TurnModel
from tests.conftest import bind_runtime_paths
from tests.identity_helpers import persist_entity_accounts
from tests.providers.bare_adapter import drain

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from mindroom.llm.provider import StreamEvent

_CLOUD = "https://api.openai.com/v1"
_FALLBACK = "https://api.anthropic.com"
_LOCAL = "http://127.0.0.1:11434"
_REMOTE_HS = "https://matrix.example.com"


@dataclass
class _Scripted:
    identity: ModelIdentity
    outcomes: list[list[StreamEvent | BaseException]]
    requests: list[ModelRequest] = field(default_factory=list)
    capabilities: Capabilities = field(default_factory=Capabilities)
    native_history_route: Any = None
    token_counter: Any = None
    model_params: dict[str, Any] = field(default_factory=dict)

    def request_tokens(self, _request: ModelRequest, *, start: int = 0) -> int:
        del start
        return 10

    def context_key(self, _request: ModelRequest) -> str:
        return self.identity.id

    async def stream(self, request: ModelRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        for item in self.outcomes.pop(0):
            if isinstance(item, BaseException):
                raise item
            yield item


def _turn(text: str = "ok", *, inp: int = 10, out: int = 5) -> TurnCompleted:
    return TurnCompleted(
        message=Message(
            role="assistant",
            content=text,
            metrics=MessageMetrics(tokens=TokenCounts(input_tokens=inp, output_tokens=out)),
        ),
        stop_reason="end_turn",
    )


def _req(*, wait: bool = True) -> ModelRequest:
    return ModelRequest(
        messages=(Message(role="user", content="hello"),),
        overrides=RequestOverrides(wait_for_connectivity=wait),
    )


def _net_err(code: str = CONNECTION_ERROR, *, replay_stripped: bool = False) -> ProviderUnavailable:
    err = ProviderUnavailable("network down", provider="openai", code=code)
    err.replay_stripped = replay_stripped
    return err


def _fast_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    real = connectivity.wait_for_connectivity

    async def _fast(*, deadline: float) -> bool:
        return await real(deadline=deadline, probe_backoff=(0.005,), jitter_range=(0.0, 0.0))

    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", _fast)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [TRANSPORT_TIMEOUT, CONNECTION_ERROR, STREAM_STALLED])
async def test_remote_matrix_outage_waits_and_resumes_on_sync_recovery(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    connectivity.register_matrix_homeserver(_REMOTE_HS)
    mark_matrix_sync_loop_started("agent")
    mark_matrix_sync_failure("agent")
    _fast_wait(monkeypatch)
    monkeypatch.setattr(connectivity, "_http_probe_homeserver", lambda *_a, **_kw: asyncio.sleep(0, result=True))

    inner = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[StreamProgress(commits=False), _net_err(code, replay_stripped=True)], [_turn("resumed")]],
    )
    activities: list[tuple[str, str | None]] = []
    waiting = asyncio.Event()

    def _record_activity(wid: str, text: str | None) -> None:
        activities.append((wid, text))
        if text is not None:
            waiting.set()

    async def _recover() -> None:
        await waiting.wait()
        mark_matrix_sync_success("agent")

    with connectivity.stream_activity_scope(_record_activity):
        req = ModelRequest(messages=_req().messages, overrides=_req().overrides, retry_budget=object())
        task = asyncio.create_task(_recover())
        events = await drain(WrappedProvider(inner, connectivity_retry_stream), req)
        await task

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].message.content == "resumed"
    assert len(inner.requests) == 2
    assert inner.requests[1].retry_budget is None
    assert inner.requests[1].strip_encrypted_replay is True
    assert inner.requests[1].messages == req.messages
    assert [text for _, text in activities] == [connectivity_stage._WAITING_STATUS_TEXT, None]


@pytest.mark.asyncio
async def test_homeserver_probe_coalesces_classifies_responses_and_recovers_early_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Unregistered homeserver without matrix_offline() never corroborates offline.
    assert await connectivity.corroborate_offline() is False

    connectivity.register_matrix_homeserver(_REMOTE_HS)
    gate = asyncio.Event()
    http_calls = 0

    async def _fake_probe(_cfg: object, _timeout: float) -> bool:
        nonlocal http_calls
        http_calls += 1
        await gate.wait()
        return False

    real_http_probe = connectivity._http_probe_homeserver
    monkeypatch.setattr(connectivity, "_http_probe_homeserver", _fake_probe)
    # Early outage (matrix_offline() is still False): two concurrent waiters share ONE probe.
    t1 = asyncio.create_task(connectivity.corroborate_offline())
    t2 = asyncio.create_task(connectivity.corroborate_offline())
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(t1, t2) == [True, True]
    assert http_calls == 1

    # Early outage end-to-end recovers via /versions probe once homeserver responds again.
    _fast_wait(monkeypatch)
    probes = iter([False, False, True])
    monkeypatch.setattr(
        connectivity,
        "_http_probe_homeserver",
        lambda *_a, **_kw: asyncio.sleep(0, result=next(probes)),
    )
    early = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[_net_err()], [_turn("probe recovered")]],
    )
    events = await drain(WrappedProvider(early, connectivity_retry_stream), _req())
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].message.content == "probe recovered"
    assert len(early.requests) == 2

    # HTTP 200, HTTP 503, and SSLCertVerificationError all count as online; ConnectError counts as offline.
    class _FakeClient:
        def __init__(self, outcome: object) -> None:
            self._outcome = outcome

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def get(self, _url: str) -> object:
            if isinstance(self._outcome, BaseException):
                raise self._outcome
            return self._outcome

    cfg = (_REMOTE_HS, True)
    for online_outcome in (httpx.Response(200), httpx.Response(503)):
        monkeypatch.setattr(httpx, "AsyncClient", lambda outcome=online_outcome, **_kw: _FakeClient(outcome))
        assert await real_http_probe(cfg, 1.0) is True
    cert_connect = httpx.ConnectError("cert failed")
    cert_connect.__context__ = ssl.SSLCertVerificationError(1, "untrusted")
    cert_connect.__suppress_context__ = True
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _FakeClient(cert_connect))
    assert await real_http_probe(cfg, 1.0) is True
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _FakeClient(httpx.ConnectError("network unreachable")))
    assert await real_http_probe(cfg, 1.0) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        AuthenticationFailed("bad key", provider="openai", status_code=401),
        ProviderUnavailable("quota", provider="openai", code=QUOTA_EXHAUSTED),
        RateLimited("slow down", provider="openai", status_code=429),
        ContextWindowExceeded("too big", provider="openai", status_code=400),
        ProviderUnavailable("503 overloaded", provider="openai", status_code=503, code=None),
        provider_failure(ssl.SSLCertVerificationError(1, "cert failed"), provider="openai"),
    ],
)
async def test_non_network_errors_never_wait(monkeypatch: pytest.MonkeyPatch, error: ProviderError) -> None:
    connectivity.register_matrix_homeserver(_REMOTE_HS)
    mark_matrix_sync_failure("agent")
    waited = False

    async def _no_wait(*_a: object, **_kw: object) -> bool:
        nonlocal waited
        waited = True
        return True

    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", _no_wait)
    inner = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD), [[error]])
    with pytest.raises(ProviderError) as exc_info:
        await drain(WrappedProvider(inner, connectivity_retry_stream), _req())
    assert exc_info.value is error
    assert len(inner.requests) == 1
    assert not waited


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [CONNECTION_ERROR, STREAM_STALLED])
@pytest.mark.parametrize(
    ("homeserver", "endpoint"),
    [
        (_REMOTE_HS, "https://bad.invalid"),
        (_REMOTE_HS, _LOCAL),
        ("http://127.0.0.1:8008", _CLOUD),
        ("http://tuwunel:8008", _CLOUD),
    ],
)
async def test_reachable_homeserver_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    homeserver: str,
    endpoint: str,
) -> None:
    connectivity.register_matrix_homeserver(homeserver)
    monkeypatch.setattr(connectivity, "_http_probe_homeserver", lambda *_a, **_kw: asyncio.sleep(0, result=True))
    inner = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=endpoint), [[_net_err(code)]])
    with pytest.raises(ProviderUnavailable):
        await drain(WrappedProvider(inner, connectivity_retry_stream), _req())
    assert len(inner.requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "commit_event",
    [
        TextDelta("hi"),
        ReasoningDelta("hmm"),
        ToolCallReady(call={"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}),
        StreamProgress(commits=True),
    ],
)
async def test_committed_attempt_never_retries(monkeypatch: pytest.MonkeyPatch, commit_event: StreamEvent) -> None:
    connectivity.register_matrix_homeserver(_REMOTE_HS)
    mark_matrix_sync_failure("agent")
    _fast_wait(monkeypatch)
    inner = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[commit_event, _net_err()], [_turn("must not run")]],
    )
    with pytest.raises(ProviderUnavailable):
        await drain(WrappedProvider(inner, connectivity_retry_stream), _req())
    assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_fallback_chain_cloud_to_local_and_cloud_to_cloud_cooldown_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_stage._COOLDOWN_UNTIL.clear()
    connectivity.register_matrix_homeserver(_REMOTE_HS)
    mark_matrix_sync_failure("agent")
    wait_calls = 0

    async def _track_wait(*_a: object, **_kw: object) -> bool:
        nonlocal wait_calls
        wait_calls += 1
        return True

    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", _track_wait)

    # Cloud primary -> Local fallback succeeds immediately without waiting.
    cloud = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD), [[_net_err()]])
    local = _Scripted(ModelIdentity(id="llama", provider="ollama", endpoint=_LOCAL), [[_turn("local")]])
    chained = WrappedProvider(
        WrappedProvider(
            cloud,
            partial(
                fallback_stream,
                model_name="c",
                chain=(ChainModel("l", None, lambda: local, "lk"),),
                cooldown_key="ck",
            ),
        ),
        connectivity_retry_stream,
    )
    events = await drain(chained, _req())
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].message.content == "local"
    assert wait_calls == 0

    # Cloud primary -> Cloud fallback: both offline -> rollback cooldowns, wait once, retry primary first
    # WITHOUT stripping primary's encrypted replay state even though fallback's error had replay_stripped=True.
    fallback_stage._COOLDOWN_UNTIL.clear()
    primary = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[_net_err()], [_turn("primary")]],
    )
    secondary = _Scripted(
        ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK),
        [[_net_err(TRANSPORT_TIMEOUT, replay_stripped=True)]],
    )
    chained_cloud = WrappedProvider(
        WrappedProvider(
            primary,
            partial(
                fallback_stream,
                model_name="p",
                chain=(ChainModel("s", None, lambda: secondary, "sk"),),
                cooldown_key="pk",
            ),
        ),
        connectivity_retry_stream,
    )
    events = await drain(chained_cloud, _req())
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].message.content == "primary"
    assert (wait_calls, len(primary.requests), len(secondary.requests)) == (1, 2, 1)
    assert primary.requests[1].strip_encrypted_replay is False
    assert "pk" not in fallback_stage._COOLDOWN_UNTIL
    assert "sk" not in fallback_stage._COOLDOWN_UNTIL

    # Mixed RateLimited + CONNECTION_ERROR does NOT roll back primary's rate-limit cooldown.
    fallback_stage._COOLDOWN_UNTIL.clear()
    rl_primary = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[RateLimited("429", provider="openai", retry_after_seconds=90)]],
    )
    rl_secondary = _Scripted(ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK), [[_net_err()]])
    mixed = WrappedProvider(
        rl_primary,
        partial(
            fallback_stream,
            model_name="p",
            chain=(ChainModel("s", None, lambda: rl_secondary, "rl-s"),),
            cooldown_key="rl-p",
        ),
    )
    with pytest.raises(ProviderUnavailable):
        await drain(mixed, _req())
    assert "rl-p" in fallback_stage._COOLDOWN_UNTIL


@pytest.mark.asyncio
async def test_cooldown_rollback_guards_tls_failover_and_no_urls_in_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connectivity.register_matrix_homeserver(_REMOTE_HS)

    # 1. Without corroborated outage (wait=False or online probe), cooldown is NOT rolled back.
    fallback_stage._COOLDOWN_UNTIL.clear()
    monkeypatch.setattr(connectivity, "_http_probe_homeserver", lambda *_a, **_kw: asyncio.sleep(0, result=True))
    for wait_flag, key in ((False, "no-wait-pk"), (True, "online-pk")):
        p = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD), [[_net_err()]])
        s = _Scripted(ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK), [[_net_err()]])
        chained = WrappedProvider(
            WrappedProvider(
                p,
                partial(
                    fallback_stream,
                    model_name="p",
                    chain=(ChainModel("s", None, lambda s=s: s, f"{key}-s"),),
                    cooldown_key=key,
                ),
            ),
            connectivity_retry_stream,
        )
        with pytest.raises(ProviderUnavailable):
            await drain(chained, _req(wait=wait_flag))
        assert key in fallback_stage._COOLDOWN_UNTIL

    # 2. Concurrent newer cooldown on same key is preserved during corroborated rollback.
    fallback_stage._COOLDOWN_UNTIL.clear()
    mark_matrix_sync_failure("agent")
    newer_until = 9e12

    async def _corroborate_with_concurrent_write() -> bool:
        fallback_stage._COOLDOWN_UNTIL["conc-pk"] = newer_until
        return True

    monkeypatch.setattr(connectivity_stage, "corroborate_offline", _corroborate_with_concurrent_write)
    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", lambda *_a, **_kw: asyncio.sleep(0, result=False))
    p_conc = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD), [[_net_err()]])
    s_conc = _Scripted(ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK), [[_net_err()]])
    chained_conc = WrappedProvider(
        WrappedProvider(
            p_conc,
            partial(
                fallback_stream,
                model_name="p",
                chain=(ChainModel("s", None, lambda: s_conc, "conc-sk"),),
                cooldown_key="conc-pk",
            ),
        ),
        connectivity_retry_stream,
    )
    with pytest.raises(ProviderUnavailable):
        await drain(chained_conc, _req())
    assert fallback_stage._COOLDOWN_UNTIL["conc-pk"] == newer_until
    assert "conc-sk" not in fallback_stage._COOLDOWN_UNTIL

    # 3. Expired TLS cert primary with inline image skips media retry and fails over with the image intact.
    tls_err = provider_failure(ssl.SSLCertVerificationError(1, "certificate expired"), provider="openai")
    assert tls_err is not None
    assert (tls_err.retryable, tls_err.code) == (False, TLS_CERTIFICATE_ERROR)
    bad_tls = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD), [[tls_err]])
    healthy = _Scripted(ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK), [[_turn("backup ok")]])
    media_primary = WrappedProvider(
        bad_tls,
        partial(media_fallback_stream, fallback_prompt="no media"),
        media_fallback_projection("no media"),
    )
    tls_chain = WrappedProvider(
        WrappedProvider(
            media_primary,
            partial(
                fallback_stream,
                model_name="p",
                chain=(ChainModel("s", None, lambda: healthy, "tls-s"),),
                cooldown_key="tls-p",
            ),
        ),
        connectivity_retry_stream,
    )
    img = Image(id="att_img1", content=b"pngbytes", mime_type="image/png")
    img_req = ModelRequest(
        messages=(Message(role="user", content="describe", images=(img,)),),
        overrides=RequestOverrides(wait_for_connectivity=True),
    )
    tls_events = await drain(tls_chain, img_req)
    assert isinstance(tls_events[-1], TurnCompleted)
    assert tls_events[-1].message.content == "backup ok"
    assert (len(bad_tls.requests), len(healthy.requests)) == (1, 1)
    assert bad_tls.requests[0].messages[0].images == (img,)
    assert healthy.requests[0].messages[0].images == (img,)

    # 4. Reconnect logs record only structured metadata (provider/model_id/error_code/attempt/max_attempts), never URLs.
    monkeypatch.setattr(connectivity_stage, "corroborate_offline", connectivity.corroborate_offline)
    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", lambda *_a, **_kw: asyncio.sleep(0, result=True))
    gateway_endpoint = "https://gateway.example.com/opaque-path-xyz/v1"
    gw = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=gateway_endpoint),
        [[_net_err()], [_turn("ok")]],
    )
    with capture_logs() as cap_logs:
        await drain(WrappedProvider(gw, connectivity_retry_stream), _req())
    conn_logs = [entry for entry in cap_logs if str(entry.get("event", "")).startswith("provider_connectivity_")]
    assert [entry["event"] for entry in conn_logs] == ["provider_connectivity_wait", "provider_connectivity_resumed"]
    assert conn_logs[0]["remaining_seconds"] > 0
    assert all(
        (entry["provider"], entry["model_id"], entry["error_code"], entry["attempt"], entry["max_attempts"])
        == ("openai", "gpt-5", CONNECTION_ERROR, 1, 3)
        for entry in conn_logs
    )
    assert all("endpoint" not in entry and "gateway.example.com" not in str(entry) for entry in conn_logs)


@pytest.mark.asyncio
async def test_deadline_expiry_reconnect_cap_and_immediate_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    connectivity.register_matrix_homeserver(_REMOTE_HS)
    mark_matrix_sync_failure("agent")

    # 1. Deadline expiry re-raises original ProviderUnavailable and clears activity.
    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", lambda *_a, **_kw: asyncio.sleep(0, result=False))
    original = _net_err()
    seen: list[str | None] = []
    with (
        connectivity.stream_activity_scope(lambda _wid, text: seen.append(text)),
        pytest.raises(ProviderUnavailable) as exc_info,
    ):
        await drain(
            WrappedProvider(
                _Scripted(ModelIdentity(id="m", provider="openai", endpoint=_CLOUD), [[original]]),
                connectivity_retry_stream,
            ),
            _req(),
        )
    assert exc_info.value is original
    assert seen == [connectivity_stage._WAITING_STATUS_TEXT, None]

    # 2. Max reconnect cap (3 -> 4 total attempts).
    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", lambda *_a, **_kw: asyncio.sleep(0, result=True))
    cap = _Scripted(ModelIdentity(id="m", provider="openai", endpoint=_CLOUD), [[_net_err()] for _ in range(6)])
    with pytest.raises(ProviderUnavailable):
        await drain(WrappedProvider(cap, connectivity_retry_stream), _req())
    assert len(cap.requests) == 4

    # 3. User Stop cancels immediately during wait without shielding.
    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", connectivity.wait_for_connectivity)
    waiting = asyncio.Event()
    cancel_seen: list[str | None] = []

    def _on_act(_wid: str, text: str | None) -> None:
        cancel_seen.append(text)
        if text is not None:
            waiting.set()

    async def _run() -> None:
        with connectivity.stream_activity_scope(_on_act):
            await drain(
                WrappedProvider(
                    _Scripted(ModelIdentity(id="m", provider="openai", endpoint=_CLOUD), [[_net_err()]]),
                    connectivity_retry_stream,
                ),
                _req(),
            )

    task = asyncio.create_task(_run())
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert cancel_seen == [connectivity_stage._WAITING_STATUS_TEXT, None]


@pytest.mark.asyncio
async def test_opt_out_builders_and_usage_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connectivity.register_matrix_homeserver(_REMOTE_HS)
    mark_matrix_sync_failure("agent")

    # Opt-out default passes straight through.
    opt_out = _Scripted(ModelIdentity(id="m", provider="openai", endpoint=_CLOUD), [[_net_err()]])
    with pytest.raises(ProviderUnavailable):
        await drain(WrappedProvider(opt_out, connectivity_retry_stream), _req(wait=False))
    assert len(opt_out.requests) == 1

    # create_agent enables wait_for_connectivity only for Matrix turns; _spoken disables it for voice.
    config_path = tmp_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    runtime_paths = resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env={})
    config = bind_runtime_paths(
        Config(
            models={"default": ModelConfig(provider="synthetic", id="test-model")},
            agents={"helper": AgentConfig(display_name="Helper", model="default")},
        ),
        runtime_paths,
    )
    persist_entity_accounts(config, runtime_paths, usernames={"router": "router", "helper": "helper"})
    model = TurnModel(model_name="default", requested_model_name="default", context_window=None, overridden=False)
    plugins = current_plugins(config, runtime_paths)
    matrix_build = create_agent(
        "helper",
        config,
        runtime_paths,
        None,
        knowledge=None,
        model=model,
        plugins=plugins,
        outside_matrix=False,
    )
    headless_build = create_agent(
        "helper",
        config,
        runtime_paths,
        None,
        knowledge=None,
        model=model,
        plugins=plugins,
        outside_matrix=True,
    )
    mcp_identity = ToolExecutionIdentity(
        channel="mcp",
        agent_name="helper",
        requester_id="@user:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="mcp-1",
    )
    mcp_build = create_agent(
        "helper",
        config,
        runtime_paths,
        mcp_identity,
        knowledge=None,
        model=model,
        plugins=plugins,
        outside_matrix=False,
    )
    assert matrix_build.request_template.overrides.wait_for_connectivity is True
    assert headless_build.request_template.overrides.wait_for_connectivity is False
    assert mcp_build.request_template.overrides.wait_for_connectivity is False
    assert _spoken(matrix_build).request_template.overrides.wait_for_connectivity is False

    # Usage attribution: single provider (sums attempts) AND fallback chain (no double-counting).
    monkeypatch.setattr(connectivity_stage, "wait_for_connectivity", lambda *_a, **_kw: asyncio.sleep(0, result=True))
    u1 = MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=0))
    u2 = MessageMetrics(tokens=TokenCounts(input_tokens=50, output_tokens=0))
    single = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[UsageReported(u1, final=False), _net_err()], [_turn("done", inp=100, out=20)]],
    )
    single_turn = (await drain(WrappedProvider(single, connectivity_retry_stream), _req()))[-1]
    assert isinstance(single_turn, TurnCompleted)
    assert (single_turn.message.metrics.tokens.input_tokens, single_turn.message.metrics.tokens.output_tokens) == (
        200,
        20,
    )
    assert (single_turn.message.provider_data or {}).get(AGGREGATE_USAGE_KEY) is True

    fallback_stage._COOLDOWN_UNTIL.clear()
    p_id = ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD)
    s_id = ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK)
    primary = _Scripted(p_id, [[UsageReported(u1, final=False), _net_err()], [_turn("ok", inp=80, out=15)]])
    secondary = _Scripted(s_id, [[UsageReported(u2, final=False), _net_err()]])
    chained = WrappedProvider(
        WrappedProvider(
            primary,
            partial(
                fallback_stream,
                model_name="p",
                chain=(ChainModel("s", None, lambda: secondary, "su"),),
                cooldown_key="pu",
            ),
        ),
        connectivity_retry_stream,
    )
    tracker = StreamTracker(identity=p_id)
    events = await drain(chained, _req())
    for ev in events:
        tracker.observe(ev)
    assert isinstance(events[-1], TurnCompleted)
    assert [(ident.id, m.tokens.input_tokens) for ident, m in tracker.prior_usage] == [("gpt-5", 100), ("claude", 50)]
    assert (events[-1].message.metrics.tokens.input_tokens, events[-1].message.metrics.tokens.output_tokens) == (80, 15)


@pytest.mark.asyncio
async def test_matrix_health_sync_transitions_and_ssl_cert_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_matrix_sync_health()
    mark_matrix_sync_loop_started("a")
    mark_matrix_sync_loop_started("b")
    mark_matrix_sync_success("a")
    assert not connectivity.matrix_offline()
    mark_matrix_sync_failure("a")
    assert connectivity.matrix_offline()
    mark_matrix_sync_success("b")
    assert not connectivity.matrix_offline()
    mark_matrix_sync_failure("b")
    clear_matrix_sync_state("b")
    assert not connectivity.matrix_offline()

    async def _fail_sync(_bot: object, *, attempt: int) -> orchestration_runtime._SyncIterationOutcome:
        del attempt
        return orchestration_runtime._SyncIterationOutcome(restart_reason_category="sync_failure")

    monkeypatch.setattr(orchestration_runtime, "_run_sync_iteration", _fail_sync)
    fake_bot = SimpleNamespace(
        agent_name="sync_bot",
        running=True,
        ingestion_quiesced=False,
        in_flight_response_count=0,
        invalidate_agent_reply_memberships=lambda **_kw: None,
    )
    await orchestration_runtime.sync_forever_with_restart(fake_bot, max_retries=1)  # type: ignore[arg-type]
    assert connectivity.matrix_offline()

    cert_err = ssl.SSLCertVerificationError(1, "self-signed")
    wrapped = httpx.ConnectError("TLS failed")
    wrapped.__context__ = cert_err
    wrapped.__suppress_context__ = True
    for raw in (cert_err, wrapped):
        mapped = provider_failure(raw, provider="openai")
        assert type(mapped) is ProviderError
        assert (mapped.retryable, mapped.code) == (False, TLS_CERTIFICATE_ERROR)
