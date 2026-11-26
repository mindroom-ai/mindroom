"""Tests for outage auto-resume: homeserver corroboration, pipeline stage, fallback, opt-ins, and TLS errors."""
# ruff: noqa: D103

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from structlog.testing import capture_logs

from mindroom.agents import create_agent
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import RuntimePaths, resolve_runtime_paths
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
from mindroom.matrix_rtc.call_tools import _spoken
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
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from mindroom.llm.provider import ModelProvider, StreamEvent

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


def _runtime_paths(tmp_path: Path, homeserver: str = _REMOTE_HS, *, ssl_verify: bool = True) -> RuntimePaths:
    config_path = tmp_path / "config.yaml"
    if not config_path.exists():
        config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    env = {"MATRIX_HOMESERVER": homeserver}
    if not ssl_verify:
        env["MATRIX_SSL_VERIFY"] = "false"
    return resolve_runtime_paths(config_path=config_path, storage_path=tmp_path / "data", process_env=env)


def _stage(runtime_paths: RuntimePaths) -> partial[AsyncIterator[StreamEvent]]:
    return partial(connectivity_retry_stream, runtime_paths=runtime_paths)


def _chained(
    primary: ModelProvider,
    secondary: ModelProvider,
    runtime_paths: RuntimePaths,
    *,
    key: str,
) -> WrappedProvider:
    return WrappedProvider(
        WrappedProvider(
            primary,
            partial(
                fallback_stream,
                model_name="p",
                chain=(ChainModel("s", None, lambda: secondary, f"{key}-s"),),
                cooldown_key=f"{key}-p",
            ),
        ),
        _stage(runtime_paths),
    )


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


def _fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connectivity_stage, "_PROBE_BACKOFF_SECONDS", (0.005,))
    monkeypatch.setattr(connectivity_stage, "_JITTER_RANGE_SECONDS", (0.0, 0.0))


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [TRANSPORT_TIMEOUT, CONNECTION_ERROR, STREAM_STALLED])
async def test_outage_waits_resumes_and_logs_without_urls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _fast_backoff(monkeypatch)
    probes = iter([False, False, True])
    monkeypatch.setattr(
        connectivity_stage,
        "_http_probe_homeserver",
        lambda *_a, **_kw: asyncio.sleep(0, result=next(probes)),
    )

    inner = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint="https://gateway.example.com/opaque-path/v1"),
        [[StreamProgress(commits=False), _net_err(code, replay_stripped=True)], [_turn("resumed")]],
    )
    req = ModelRequest(messages=_req().messages, overrides=_req().overrides, retry_budget=object())
    with capture_logs() as cap_logs:
        events = await drain(WrappedProvider(inner, _stage(runtime_paths)), req)

    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].message.content == "resumed"
    assert len(inner.requests) == 2
    assert inner.requests[1].retry_budget is None
    assert inner.requests[1].strip_encrypted_replay is True
    assert inner.requests[1].messages == req.messages

    conn_logs = [entry for entry in cap_logs if str(entry.get("event", "")).startswith("provider_connectivity_")]
    assert [entry["event"] for entry in conn_logs] == ["provider_connectivity_wait", "provider_connectivity_resumed"]
    assert conn_logs[0]["remaining_seconds"] > 0
    assert all(
        (entry["provider"], entry["model_id"], entry["error_code"], entry["attempt"], entry["max_attempts"])
        == ("openai", "gpt-5", code, 1, 3)
        for entry in conn_logs
    )
    assert all("endpoint" not in entry and "gateway.example.com" not in str(entry) for entry in conn_logs)


@pytest.mark.asyncio
async def test_homeserver_probe_coalesces_ssl_verify_and_classifies_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    real_connectivity_probe: None,
) -> None:
    del real_connectivity_probe
    runtime_paths = _runtime_paths(tmp_path)
    real_async_client = httpx.AsyncClient
    seen_verify: list[object] = []

    class _TruncatedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            err = httpx.RemoteProtocolError("body closed early")
            raise err
            yield b""

    def _mock_client(outcome: object) -> Callable[..., httpx.AsyncClient]:
        def _factory(
            *,
            verify: object = True,
            timeout: float = 3.0,
            follow_redirects: bool = False,
        ) -> httpx.AsyncClient:
            seen_verify.append(verify)

            def _handler(_request: httpx.Request) -> httpx.Response:
                if isinstance(outcome, BaseException):
                    raise outcome
                assert isinstance(outcome, httpx.Response)
                return outcome

            return real_async_client(
                transport=httpx.MockTransport(_handler),
                timeout=timeout,
                follow_redirects=follow_redirects,
            )

        return _factory

    # HTTP 200, HTTP 503 with truncated body, and SSLCertVerificationError all prove online; ConnectError proves offline.
    truncated_503 = httpx.Response(503, stream=_TruncatedStream())
    for online_outcome in (httpx.Response(200), truncated_503):
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client(online_outcome))
        assert await connectivity_stage._probe_homeserver_reachable(runtime_paths) is True
    assert isinstance(seen_verify[-1], ssl.SSLContext)

    cert_connect = httpx.ConnectError("cert failed")
    cert_connect.__context__ = ssl.SSLCertVerificationError(1, "untrusted")
    cert_connect.__suppress_context__ = True
    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(cert_connect))
    assert await connectivity_stage._probe_homeserver_reachable(runtime_paths) is True

    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(httpx.ConnectError("unreachable")))
    assert await connectivity_stage._probe_homeserver_reachable(runtime_paths) is False

    # MATRIX_SSL_VERIFY=false passes verify=False; invalid homeserver returns None (fast-fail).
    insecure_paths = _runtime_paths(tmp_path, ssl_verify=False)
    monkeypatch.setattr(httpx, "AsyncClient", _mock_client(httpx.Response(200)))
    assert await connectivity_stage._probe_homeserver_reachable(insecure_paths) is True
    assert seen_verify[-1] is False
    assert (
        await connectivity_stage._probe_homeserver_reachable(_runtime_paths(tmp_path, homeserver="not-a-url")) is None
    )

    # Concurrent callers coalesce into one HTTP probe.
    gate = asyncio.Event()
    http_calls = 0

    async def _slow_probe(_url: str, _verify: object, _timeout: float) -> bool:
        nonlocal http_calls
        http_calls += 1
        await gate.wait()
        return False

    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", _slow_probe)
    t1 = asyncio.create_task(connectivity_stage._probe_homeserver_reachable(runtime_paths))
    t2 = asyncio.create_task(connectivity_stage._probe_homeserver_reachable(runtime_paths))
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(t1, t2) == [False, False]
    assert http_calls == 1


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
async def test_non_network_errors_never_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: ProviderError,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    probed = False

    async def _probe(*_a: object, **_kw: object) -> bool:
        nonlocal probed
        probed = True
        return False

    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", _probe)
    inner = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD), [[error]])
    with pytest.raises(ProviderError) as exc_info:
        await drain(WrappedProvider(inner, _stage(runtime_paths)), _req())
    assert exc_info.value is error
    assert len(inner.requests) == 1
    assert not probed


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [CONNECTION_ERROR, STREAM_STALLED])
@pytest.mark.parametrize(
    ("homeserver", "endpoint"),
    [
        (_REMOTE_HS, "https://bad.invalid"),
        (_REMOTE_HS, _LOCAL),
        ("http://127.0.0.1:8008", _CLOUD),
    ],
)
async def test_reachable_homeserver_fails_fast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    homeserver: str,
    endpoint: str,
) -> None:
    runtime_paths = _runtime_paths(tmp_path, homeserver=homeserver)
    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", lambda *_a, **_kw: asyncio.sleep(0, result=True))
    inner = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=endpoint), [[_net_err(code)]])
    with pytest.raises(ProviderUnavailable):
        await drain(WrappedProvider(inner, _stage(runtime_paths)), _req())
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
async def test_committed_attempt_never_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    commit_event: StreamEvent,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _fast_backoff(monkeypatch)
    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", lambda *_a, **_kw: asyncio.sleep(0, result=False))
    inner = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[commit_event, _net_err()], [_turn("must not run")]],
    )
    with pytest.raises(ProviderUnavailable):
        await drain(WrappedProvider(inner, _stage(runtime_paths)), _req())
    assert len(inner.requests) == 1


@pytest.mark.asyncio
async def test_fallback_chain_and_tls_failover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _fast_backoff(monkeypatch)
    fallback_stage._COOLDOWN_UNTIL.clear()

    # 1. Cloud primary -> Local fallback succeeds immediately without probing or waiting.
    probe_calls = 0

    async def _fail_probe(*_a: object, **_kw: object) -> bool:
        nonlocal probe_calls
        probe_calls += 1
        return False

    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", _fail_probe)
    cloud = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD), [[_net_err()]])
    local = _Scripted(ModelIdentity(id="llama", provider="ollama", endpoint=_LOCAL), [[_turn("local")]])
    events = await drain(_chained(cloud, local, runtime_paths, key="c2l"), _req())
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].message.content == "local"
    assert probe_calls == 0

    # 2. Cloud primary -> Cloud fallback: both fail during outage -> waits once, retries primary first
    # without stripping primary's encrypted replay state even though fallback's error had replay_stripped=True.
    fallback_stage._COOLDOWN_UNTIL.clear()
    probes = iter([False, True])
    monkeypatch.setattr(
        connectivity_stage,
        "_http_probe_homeserver",
        lambda *_a, **_kw: asyncio.sleep(0, result=next(probes)),
    )
    primary = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[_net_err()], [_turn("primary")]],
    )
    secondary = _Scripted(
        ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK),
        [[_net_err(TRANSPORT_TIMEOUT, replay_stripped=True)]],
    )
    events = await drain(_chained(primary, secondary, runtime_paths, key="c2c"), _req())
    assert isinstance(events[-1], TurnCompleted)
    assert events[-1].message.content == "primary"
    assert (len(primary.requests), len(secondary.requests)) == (2, 1)
    assert primary.requests[1].strip_encrypted_replay is False

    # 3. Expired TLS cert primary with inline image skips media retry and fails over with image intact.
    cert_err = ssl.SSLCertVerificationError(1, "certificate expired")
    wrapped_cert = httpx.ConnectError("TLS failed")
    wrapped_cert.__context__ = cert_err
    wrapped_cert.__suppress_context__ = True
    for raw in (cert_err, wrapped_cert):
        mapped = provider_failure(raw, provider="openai")
        assert type(mapped) is ProviderError
        assert (mapped.retryable, mapped.code) == (False, TLS_CERTIFICATE_ERROR)

    tls_err = provider_failure(cert_err, provider="openai")
    assert tls_err is not None
    bad_tls = _Scripted(ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD), [[tls_err]])
    healthy = _Scripted(ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK), [[_turn("backup ok")]])
    media_primary = WrappedProvider(
        bad_tls,
        partial(media_fallback_stream, fallback_prompt="no media"),
        media_fallback_projection("no media"),
    )
    img = Image(id="att_img1", content=b"pngbytes", mime_type="image/png")
    img_req = ModelRequest(
        messages=(Message(role="user", content="describe", images=(img,)),),
        overrides=RequestOverrides(wait_for_connectivity=True),
    )
    tls_events = await drain(_chained(media_primary, healthy, runtime_paths, key="tls"), img_req)
    assert isinstance(tls_events[-1], TurnCompleted)
    assert tls_events[-1].message.content == "backup ok"
    assert (len(bad_tls.requests), len(healthy.requests)) == (1, 1)
    assert healthy.requests[0].messages[0].images == (img,)


@pytest.mark.asyncio
async def test_deadline_expiry_suspend_cap_and_immediate_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _fast_backoff(monkeypatch)
    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", lambda *_a, **_kw: asyncio.sleep(0, result=False))

    # 1. Deadline expiry (including jump past deadline on suspend/wake before reissue) re-raises original error.
    now = 100.0
    with monkeypatch.context() as mp:
        mp.delattr(connectivity_stage.time, "CLOCK_BOOTTIME", raising=False)
        mp.setattr(connectivity_stage.sys, "platform", "darwin")
        mp.setattr(connectivity_stage.time, "CLOCK_MONOTONIC", 99, raising=False)
        seen_clocks: list[int] = []
        mp.setattr(
            connectivity_stage.time,
            "clock_gettime",
            lambda cid: (seen_clocks.append(cid), 42.0)[1],
            raising=False,
        )
        assert connectivity_stage._now() == 42.0
        assert seen_clocks == [99]
    monkeypatch.setattr(connectivity_stage, "_now", lambda: now)
    monkeypatch.setattr(connectivity_stage, "_CONNECTIVITY_WAIT_LIMIT_SECONDS", 10.0)
    probes = iter([False, True])

    async def _probe_with_suspend(*_a: object, **_kw: object) -> bool:
        nonlocal now
        res = next(probes)
        if res:
            now += 20.0
        return res

    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", _probe_with_suspend)
    original = _net_err()
    with capture_logs() as cap_logs, pytest.raises(ProviderUnavailable) as exc_info:
        await drain(
            WrappedProvider(
                _Scripted(ModelIdentity(id="m", provider="openai", endpoint=_CLOUD), [[original]]),
                _stage(runtime_paths),
            ),
            _req(),
        )
    assert exc_info.value is original
    assert any(entry.get("event") == "provider_connectivity_wait_expired" for entry in cap_logs)

    # 2. Max reconnect cap (3 reconnects -> 4 total attempts).
    monkeypatch.setattr(connectivity_stage, "_CONNECTIVITY_WAIT_LIMIT_SECONDS", 1800.0)
    cap_probes = iter([False, True, False, True, False, True, False])
    monkeypatch.setattr(
        connectivity_stage,
        "_http_probe_homeserver",
        lambda *_a, **_kw: asyncio.sleep(0, result=next(cap_probes)),
    )
    cap = _Scripted(ModelIdentity(id="m", provider="openai", endpoint=_CLOUD), [[_net_err()] for _ in range(6)])
    with pytest.raises(ProviderUnavailable):
        await drain(WrappedProvider(cap, _stage(runtime_paths)), _req())
    assert len(cap.requests) == 4

    # 3. User Stop cancels immediately during backoff sleep without shielding.
    monkeypatch.setattr(connectivity_stage, "_PROBE_BACKOFF_SECONDS", (60.0,))
    probed = asyncio.Event()

    async def _offline_probe(*_a: object, **_kw: object) -> bool:
        probed.set()
        return False

    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", _offline_probe)
    task = asyncio.create_task(
        drain(
            WrappedProvider(
                _Scripted(ModelIdentity(id="m", provider="openai", endpoint=_CLOUD), [[_net_err()]]),
                _stage(runtime_paths),
            ),
            _req(),
        ),
    )
    await probed.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_opt_in_and_opt_out_builders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    monkeypatch.setattr(connectivity_stage, "_http_probe_homeserver", lambda *_a, **_kw: asyncio.sleep(0, result=False))

    # Opt-out default (wait=False) and runtime_paths=None pass straight through.
    for stage, req in ((_stage(runtime_paths), _req(wait=False)), (connectivity_retry_stream, _req(wait=True))):
        opt_out = _Scripted(ModelIdentity(id="m", provider="openai", endpoint=_CLOUD), [[_net_err()]])
        with pytest.raises(ProviderUnavailable):
            await drain(WrappedProvider(opt_out, stage), req)
        assert len(opt_out.requests) == 1

    # create_agent enables wait_for_connectivity only for Matrix turns; _spoken disables it for voice.
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
    mcp_identity = ToolExecutionIdentity(
        channel="mcp",
        agent_name="helper",
        requester_id="@user:example.org",
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id="mcp-1",
    )
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


@pytest.mark.asyncio
async def test_usage_attribution_single_and_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_paths = _runtime_paths(tmp_path)
    _fast_backoff(monkeypatch)
    probes = iter([False, True, False, True])
    monkeypatch.setattr(
        connectivity_stage,
        "_http_probe_homeserver",
        lambda *_a, **_kw: asyncio.sleep(0, result=next(probes)),
    )
    u1 = MessageMetrics(tokens=TokenCounts(input_tokens=100, output_tokens=0))
    u2 = MessageMetrics(tokens=TokenCounts(input_tokens=50, output_tokens=0))

    # Single provider sums attempts and marks AGGREGATE_USAGE_KEY.
    single = _Scripted(
        ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD),
        [[UsageReported(u1, final=False), _net_err()], [_turn("done", inp=100, out=20)]],
    )
    single_turn = (await drain(WrappedProvider(single, _stage(runtime_paths)), _req()))[-1]
    assert isinstance(single_turn, TurnCompleted)
    assert (single_turn.message.metrics.tokens.input_tokens, single_turn.message.metrics.tokens.output_tokens) == (
        200,
        20,
    )
    assert (single_turn.message.provider_data or {}).get(AGGREGATE_USAGE_KEY) is True

    # Fallback chain attributes prior attempts per model without double-counting.
    fallback_stage._COOLDOWN_UNTIL.clear()
    p_id = ModelIdentity(id="gpt-5", provider="openai", endpoint=_CLOUD)
    s_id = ModelIdentity(id="claude", provider="anthropic", endpoint=_FALLBACK)
    primary = _Scripted(p_id, [[UsageReported(u1, final=False), _net_err()], [_turn("ok", inp=80, out=15)]])
    secondary = _Scripted(s_id, [[UsageReported(u2, final=False), _net_err()]])
    tracker = StreamTracker(identity=p_id)
    events = await drain(_chained(primary, secondary, runtime_paths, key="usage"), _req())
    for ev in events:
        tracker.observe(ev)
    assert isinstance(events[-1], TurnCompleted)
    assert [(ident.id, m.tokens.input_tokens) for ident, m in tracker.prior_usage] == [("gpt-5", 100), ("claude", 50)]
    assert (events[-1].message.metrics.tokens.input_tokens, events[-1].message.metrics.tokens.output_tokens) == (80, 15)
