"""Tests for doctor's Vertex AI Claude failure classification and embedder check."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
import typer
from anthropic import APIStatusError
from google.auth.exceptions import DefaultCredentialsError

from mindroom.cli.doctor import _check_memory_config, _check_memory_embedder, _classify_vertexai_claude_error, doctor
from mindroom.config.main import Config
from mindroom.config.models import RouterConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.credentials_sync import get_embedder_api_key

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _api_status_error(status_code: int, message: str) -> APIStatusError:
    request = httpx.Request("POST", "https://example.test/v1/messages")
    response = httpx.Response(status_code, request=request)
    return APIStatusError(message, response=response, body=None)


def test_publisher_model_not_found_explains_model_garden() -> None:
    """A 404 should point at per-project/region model availability, not just the code."""
    original_message = "Publisher model `claude-x` was not found or your project does not have access to it."
    valid, detail = _classify_vertexai_claude_error(_api_status_error(404, original_message))

    assert valid is False
    assert detail.startswith("HTTP 404: model not available in this project/region")
    assert "Model Garden" in detail
    assert original_message in detail


def test_service_disabled_explains_api_enablement() -> None:
    """A SERVICE_DISABLED 403 should name the API that needs enabling."""
    valid, detail = _classify_vertexai_claude_error(
        _api_status_error(403, "Agent Platform API has not been used... reason: SERVICE_DISABLED"),
    )

    assert valid is False
    assert detail == "HTTP 403: the Vertex AI API (aiplatform.googleapis.com) is not enabled in this project"


def test_plain_permission_denied_points_at_iam() -> None:
    """A non-SERVICE_DISABLED 403 should point at IAM access."""
    valid, detail = _classify_vertexai_claude_error(_api_status_error(403, "Permission denied on resource."))

    assert valid is False
    assert detail == "HTTP 403: permission denied — check the credentials' IAM access to Vertex AI in this project"


def test_other_status_codes_stay_compact() -> None:
    """Unclassified statuses keep the previous compact HTTP detail."""
    valid, detail = _classify_vertexai_claude_error(_api_status_error(500, "boom"))

    assert valid is False
    assert detail == "HTTP 500"


def test_missing_credentials_stay_inconclusive() -> None:
    """Missing ADC credentials remain a warning, not a failure."""
    valid, detail = _classify_vertexai_claude_error(DefaultCredentialsError("no ADC"))

    assert valid is None
    assert detail == "no ADC"


def _openai_embedder_config(host: str | None = None) -> Config:
    embedder: dict[str, object] = {"provider": "openai"}
    if host is not None:
        embedder["config"] = {"host": host}
    return Config(memory={"backend": "mem0", "embedder": embedder}, router=RouterConfig(model="default"))


def _doctor_runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")


def test_memory_embedder_check_passes_on_healthy_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A healthy embedding round-trip counts as a pass."""
    monkeypatch.setattr("mindroom.cli.doctor.probe_embedder", lambda *_args: None)

    assert _check_memory_embedder(_openai_embedder_config(), _doctor_runtime_paths(tmp_path)) == (1, 0, 0)


def test_memory_embedder_check_fails_on_auth_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A credential rejection from the shared probe is a hard failure."""
    monkeypatch.setattr(
        "mindroom.cli.doctor.probe_embedder",
        lambda *_args: "embedder authentication failed (HTTP 401)",
    )

    assert _check_memory_embedder(_openai_embedder_config(), _doctor_runtime_paths(tmp_path)) == (0, 1, 0)


def test_memory_embedder_check_warns_when_endpoint_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable endpoint stays an inconclusive warning, not a hard failure."""
    monkeypatch.setattr(
        "mindroom.cli.doctor.probe_embedder",
        lambda *_args: "embedder endpoint unreachable",
    )
    config = _openai_embedder_config(host="http://embeddings.local:9292/v1")

    assert _check_memory_embedder(config, _doctor_runtime_paths(tmp_path)) == (0, 0, 1)


def test_memory_config_probes_embedder_for_knowledge_only_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Semantic knowledge bases without mem0 still get the embedder preflight."""
    embedder_checks: list[Config] = []

    def fake_embedder_check(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
        del runtime_paths
        embedder_checks.append(config)
        return 1, 0, 0

    monkeypatch.setattr("mindroom.cli.doctor._check_memory_embedder", fake_embedder_check)
    docs_path = tmp_path / "docs"
    docs_path.mkdir()
    config = Config(
        memory={"backend": "none", "embedder": {"provider": "openai"}},
        knowledge_bases={"docs": {"mode": "semantic", "path": str(docs_path)}},
        router=RouterConfig(model="default"),
    )

    assert _check_memory_config(config, _doctor_runtime_paths(tmp_path)) == (2, 0, 0)
    assert embedder_checks


def test_memory_config_skips_embedder_without_semantic_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A config with no semantic consumers keeps the old single pass line."""

    def fail_embedder_check(config: Config, runtime_paths: RuntimePaths) -> tuple[int, int, int]:
        del config, runtime_paths
        msg = "embedder check must not run"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.cli.doctor._check_memory_embedder", fail_embedder_check)
    config = Config(memory={"backend": "none"}, router=RouterConfig(model="default"))

    assert _check_memory_config(config, _doctor_runtime_paths(tmp_path)) == (1, 0, 0)


def test_doctor_seeds_env_credentials_like_the_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor resolves fresh config-adjacent .env credentials the way `mindroom run` will."""
    monkeypatch.delenv("EMBEDDER_API_KEY", raising=False)
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    (config_dir / ".env").write_text("EMBEDDER_API_KEY=sk-doctor-embedder\n", encoding="utf-8")
    monkeypatch.setattr("mindroom.cli.doctor._check_matrix_homeserver", lambda **_kwargs: (1, 0, 0))

    # The config file is intentionally missing: the run still fails loudly on
    # that check, but the credential sync must already have happened.
    with pytest.raises(typer.Exit):
        doctor(config_path=config_dir / "config.yaml", storage_path=tmp_path / "storage")

    runtime_paths = resolve_primary_runtime_paths(
        config_path=config_dir / "config.yaml",
        storage_path=tmp_path / "storage",
    )
    assert get_embedder_api_key(runtime_paths) == "sk-doctor-embedder"
