"""Tests for embedder health classification, state, and probes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, AuthenticationError, PermissionDeniedError

from mindroom import embedder_health
from mindroom.openai_embedder import MindRoomOpenAIEmbedder
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.embedder_health import (
    check_embedder_health,
    describe_embedder_error,
    embedder_in_use,
    get_embedder_failure,
    handle_embedder_config_reload,
    is_embedder_auth_failure_detail,
    probe_embedder,
    record_embedder_health,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

SECRET = "sk-super-secret-embedder-key"  # noqa: S105


@pytest.fixture(autouse=True)
def _reset_embedder_health() -> Iterator[None]:
    record_embedder_health(None)
    yield
    record_embedder_health(None)


def _status_error(status_code: int, message: str = "boom") -> APIStatusError:
    request = httpx.Request("POST", "http://embeddings.local/v1/embeddings")
    response = httpx.Response(
        status_code,
        request=request,
        json={"error": {"message": f"{message} key={SECRET}"}},
    )
    if status_code == 401:
        return AuthenticationError(message, response=response, body=None)
    if status_code == 403:
        return PermissionDeniedError(message, response=response, body=None)
    return APIStatusError(message, response=response, body=None)


def _runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")


def _config(
    memory: dict | str | None = None,
    knowledge_bases: dict | None = None,
    agents: dict | None = None,
) -> Config:
    return Config(
        memory=memory or {},
        knowledge_bases=knowledge_bases or {},
        agents=agents or {},
        router=RouterConfig(model="default"),
    )


class _HealthyEmbedder:
    def get_embedding(self, _text: str) -> list[float]:
        return [0.1, 0.2]


class _AuthFailingEmbedder:
    def get_embedding(self, _text: str) -> list[float]:
        raise _status_error(401)


class _EmptyVectorEmbedder:
    def get_embedding(self, _text: str) -> list[float]:
        return []


def test_authentication_error_maps_to_fixed_401_detail() -> None:
    """401 maps to the fixed auth-failed detail."""
    exc = _status_error(401)
    assert is_embedder_auth_failure_detail(describe_embedder_error(exc))
    assert describe_embedder_error(exc) == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL
    assert describe_embedder_error(exc) == "embedder authentication failed (HTTP 401)"


def test_permission_denied_maps_to_distinct_403_detail() -> None:
    """403 stays distinct from 401 so operators repair the right thing."""
    exc = _status_error(403)
    assert is_embedder_auth_failure_detail(describe_embedder_error(exc))
    assert describe_embedder_error(exc) == embedder_health._EMBEDDER_PERMISSION_DENIED_DETAIL
    assert describe_embedder_error(exc) != embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


def test_other_http_status_uses_fixed_message_without_body() -> None:
    """Non-auth HTTP failures use a fixed message without the response body."""
    exc = _status_error(500, message=f"internal error at http://user:{SECRET}@embeddings.local/v1")
    assert not is_embedder_auth_failure_detail(describe_embedder_error(exc))
    detail = describe_embedder_error(exc)
    assert detail == "embedder request failed (HTTP 500)"
    assert SECRET not in detail


def test_transport_error_uses_fixed_message_without_host() -> None:
    """Transport failures never leak the configured host."""
    request = httpx.Request("POST", "http://embeddings.internal.example/v1/embeddings")
    exc = APIConnectionError(request=request)
    detail = describe_embedder_error(exc)
    assert detail == "embedder endpoint unreachable"
    assert "embeddings.internal.example" not in detail


def test_generic_exception_maps_to_type_only_detail() -> None:
    """Unknown exception text never passes through, only the type name does."""
    exc = RuntimeError(f"provider rejected api_key={SECRET}")
    detail = describe_embedder_error(exc)
    assert detail == "embedder request failed (RuntimeError)"
    assert SECRET not in detail


def test_embedder_request_error_detail_passes_through() -> None:
    """Already-classified errors keep their exact detail."""
    exc = embedder_health.EmbedderRequestError("embedder authentication failed (HTTP 401)")
    assert describe_embedder_error(exc) == "embedder authentication failed (HTTP 401)"


def test_extract_classified_detail_from_refresh_summary() -> None:
    """The classified cause is extracted from an indexing summary."""
    summary = "Indexed 0 of 3 managed knowledge files (first error: embedder authentication failed (HTTP 401))"
    detail = embedder_health.extract_classified_embedder_detail(summary)
    assert detail == "embedder authentication failed (HTTP 401)"


def test_extract_classified_detail_exact_forms() -> None:
    """Exact classified strings extract unchanged; None stays None."""
    assert (
        embedder_health.extract_classified_embedder_detail("embedder endpoint unreachable")
        == "embedder endpoint unreachable"
    )
    assert (
        embedder_health.extract_classified_embedder_detail("embedder request failed (HTTP 503)")
        == "embedder request failed (HTTP 503)"
    )
    assert embedder_health.extract_classified_embedder_detail(None) is None


def test_extract_classified_detail_rejects_free_text() -> None:
    """Operator free text — including embedder-prefixed hostile text — never extracts."""
    assert embedder_health.extract_classified_embedder_detail("git sync failed: fatal: repo unreachable") is None
    # The type-name fallback form is deliberately not extractable: an
    # identifier-shaped token inside persisted free text could be a secret.
    assert embedder_health.extract_classified_embedder_detail(f"embedder request failed ({SECRET})") is None
    assert embedder_health.extract_classified_embedder_detail("embedder exploded near api_key=sk-secret") is None


def test_auth_failure_detail_membership() -> None:
    """Only the two fixed auth details classify as credential rejections."""
    assert is_embedder_auth_failure_detail(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)
    assert is_embedder_auth_failure_detail(embedder_health._EMBEDDER_PERMISSION_DENIED_DETAIL)
    assert not is_embedder_auth_failure_detail(None)
    assert not is_embedder_auth_failure_detail("embedder request failed (HTTP 500)")


def test_record_and_get_round_trip() -> None:
    """Recording a failure and clearing it round-trips."""
    assert get_embedder_failure() is None
    record_embedder_health(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)
    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL
    record_embedder_health(None)
    assert get_embedder_failure() is None


def test_non_openai_provider_is_never_in_use() -> None:
    """Keyless providers never count as keyed embedder use."""
    config = _config(memory={"backend": "mem0", "embedder": {"provider": "ollama"}})
    assert not embedder_in_use(config)


def test_mem0_backend_uses_embedder() -> None:
    """The mem0 backend always embeds."""
    assert embedder_in_use(_config(memory={"backend": "mem0"}))


def test_file_backend_keyword_mode_does_not_use_embedder() -> None:
    """Keyword-only file memory never embeds."""
    assert not embedder_in_use(_config(memory={"backend": "file"}))


def test_file_backend_semantic_mode_uses_embedder() -> None:
    """Semantic file memory embeds."""
    assert embedder_in_use(_config(memory={"backend": "file", "search": {"mode": "semantic"}}))


def test_semantic_knowledge_base_uses_embedder(tmp_path: Path) -> None:
    """Any semantic knowledge base embeds."""
    config = _config(
        memory="none",
        knowledge_bases={"docs": {"mode": "semantic", "path": str(tmp_path)}},
    )
    assert embedder_in_use(config)


def test_files_mode_knowledge_base_does_not_use_embedder(tmp_path: Path) -> None:
    """Files-mode knowledge bases skip embeddings."""
    config = _config(
        memory="none",
        knowledge_bases={"docs": {"mode": "files", "path": str(tmp_path)}},
    )
    assert not embedder_in_use(config)


def test_per_agent_file_semantic_override_uses_embedder() -> None:
    """A per-agent semantic override counts even when defaults do not embed."""
    config = _config(
        memory="none",
        agents={
            "helper": {
                "display_name": "Helper",
                "role": "test",
                "memory_backend": "file",
                "memory_search": {"mode": "semantic"},
            },
        },
    )
    assert embedder_in_use(config)


def test_probe_returns_none_on_non_empty_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful embedding round-trip reports healthy."""
    monkeypatch.setattr(
        "mindroom.embedding_factory.create_configured_embedder",
        lambda *_args: _HealthyEmbedder(),
    )
    assert probe_embedder(_config(), _runtime_paths(tmp_path)) is None


def test_probe_reports_auth_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A 401 during the probe surfaces the canonical auth detail."""
    monkeypatch.setattr(
        "mindroom.embedding_factory.create_configured_embedder",
        lambda *_args: _AuthFailingEmbedder(),
    )
    assert probe_embedder(_config(), _runtime_paths(tmp_path)) == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


def test_probe_rejects_empty_vector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty vector is a failure, never a silent success."""
    monkeypatch.setattr(
        "mindroom.embedding_factory.create_configured_embedder",
        lambda *_args: _EmptyVectorEmbedder(),
    )
    assert probe_embedder(_config(), _runtime_paths(tmp_path)) == "embedder returned an empty vector"


def test_probe_bounds_openai_client_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Probes cap the SDK client so a stalled endpoint fails in seconds, not minutes."""
    client = MagicMock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1, 0.2])],
        usage=None,
    )
    embedder = MindRoomOpenAIEmbedder(id="text-embedding-3-small", api_key="sk-x", openai_client=client)
    monkeypatch.setattr(
        "mindroom.embedding_factory.create_configured_embedder",
        lambda *_args: embedder,
    )

    assert probe_embedder(_config(), _runtime_paths(tmp_path)) is None
    assert embedder.client_params == {"timeout": 10.0, "max_retries": 0}


@pytest.mark.asyncio
async def test_check_embedder_health_records_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing probe records the failure detail."""
    monkeypatch.setattr(embedder_health, "probe_embedder", lambda *_args: embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)
    config = _config(memory={"backend": "mem0"})

    await check_embedder_health(config, _runtime_paths(tmp_path), reason="startup")

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_check_embedder_health_success_clears_previous_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy probe clears an earlier recorded failure (degrade then recover)."""
    record_embedder_health(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)
    monkeypatch.setattr(embedder_health, "probe_embedder", lambda *_args: None)

    await check_embedder_health(_config(memory={"backend": "mem0"}), _runtime_paths(tmp_path), reason="startup")

    assert get_embedder_failure() is None


@pytest.mark.asyncio
async def test_check_embedder_health_skips_probe_when_not_in_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No probe runs when the config cannot send keyed embedder requests."""

    def _fail(*_args: object) -> str:
        msg = "probe must not run"
        raise AssertionError(msg)

    monkeypatch.setattr(embedder_health, "probe_embedder", _fail)

    await check_embedder_health(_config(memory="none"), _runtime_paths(tmp_path), reason="startup")

    assert get_embedder_failure() is None


@pytest.mark.asyncio
async def test_reload_with_embedder_change_resets_health_and_reprobes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing memory.embedder on reload clears stale health and probes again."""
    record_embedder_health(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)
    probes: list[str] = []

    async def fake_check(_config: Config, _runtime_paths: RuntimePaths, *, reason: str) -> None:
        probes.append(reason)

    monkeypatch.setattr(embedder_health, "check_embedder_health", fake_check)
    current = _config(memory={"backend": "mem0"})
    new = _config(memory={"backend": "mem0", "embedder": {"config": {"model": "other-model"}}})

    handle_embedder_config_reload(current, new, _runtime_paths(tmp_path))
    await wait_for_background_tasks(timeout=5)

    assert get_embedder_failure() is None
    assert probes == ["config_reload"]


@pytest.mark.asyncio
async def test_reload_without_embedder_change_keeps_recorded_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated reload never clears a recorded embedder failure."""
    record_embedder_health(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)

    async def fake_check(_config: Config, _runtime_paths: RuntimePaths, *, reason: str) -> None:
        msg = f"no probe expected: {reason}"
        raise AssertionError(msg)

    monkeypatch.setattr(embedder_health, "check_embedder_health", fake_check)

    handle_embedder_config_reload(
        _config(memory={"backend": "mem0"}),
        _config(memory={"backend": "mem0"}),
        _runtime_paths(tmp_path),
    )

    assert get_embedder_failure() == embedder_health._EMBEDDER_AUTH_FAILED_DETAIL


@pytest.mark.asyncio
async def test_reload_enabling_embedder_use_probes_without_embedder_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning on the first semantic consumer probes even with an identical embedder block."""
    probes: list[str] = []

    async def fake_check(_config: Config, _runtime_paths: RuntimePaths, *, reason: str) -> None:
        probes.append(reason)

    monkeypatch.setattr(embedder_health, "check_embedder_health", fake_check)

    handle_embedder_config_reload(
        _config(memory="none"),
        _config(memory={"backend": "mem0"}),
        _runtime_paths(tmp_path),
    )
    await wait_for_background_tasks(timeout=5)

    assert probes == ["config_reload"]


@pytest.mark.asyncio
async def test_reload_disabling_embedder_use_clears_health_without_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling the last semantic consumer clears stale health instead of pinning it forever."""
    record_embedder_health(embedder_health._EMBEDDER_AUTH_FAILED_DETAIL)

    async def fake_check(_config: Config, _runtime_paths: RuntimePaths, *, reason: str) -> None:
        msg = f"no probe expected: {reason}"
        raise AssertionError(msg)

    monkeypatch.setattr(embedder_health, "check_embedder_health", fake_check)

    handle_embedder_config_reload(
        _config(memory={"backend": "mem0"}),
        _config(memory="none"),
        _runtime_paths(tmp_path),
    )

    assert get_embedder_failure() is None


@pytest.mark.asyncio
async def test_stale_probe_result_is_discarded_after_generation_bump(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe finishing after a reload bump cannot overwrite the newer health state."""

    def slow_probe(_config: Config, _runtime_paths: RuntimePaths) -> str:
        # A reload lands while this probe is still in flight.
        embedder_health._reset_embedder_health_generation()
        return embedder_health._EMBEDDER_AUTH_FAILED_DETAIL

    monkeypatch.setattr(embedder_health, "probe_embedder", slow_probe)

    await check_embedder_health(_config(memory={"backend": "mem0"}), _runtime_paths(tmp_path), reason="startup")

    assert get_embedder_failure() is None
