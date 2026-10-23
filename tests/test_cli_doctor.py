"""Tests for doctor's Vertex AI Claude failure classification and embedder check."""

from __future__ import annotations

from typing import TYPE_CHECKING

import google.auth
import httpx
import pytest
import typer
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.oauth2.credentials import Credentials

from mindroom.cli.doctor import (
    _check_matrix_homeserver,
    _check_memory_config,
    _check_memory_embedder,
    _check_pairing,
    _check_single_provider,
    _classify_vertexai_claude_error,
    _validate_vertexai_claude_connection,
    doctor,
)
from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import resolve_primary_runtime_paths
from mindroom.credentials.sync import get_embedder_api_key
from mindroom.llm.errors import ProviderError, ProviderUnavailable
from mindroom.providers.anthropic import messages as claude_messages

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths


def _api_status_error(status_code: int, message: str) -> ProviderError:
    return ProviderError(message, provider="VertexAI", status_code=status_code)


def test_the_vertex_claude_probe_asks_for_one_output_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe caps its reply through the model's own ``max_tokens``, over any value the model authored."""
    built: list[ModelConfig] = []

    def build_provider(model_config: ModelConfig, _runtime_paths: RuntimePaths) -> None:
        built.append(model_config)
        msg = "probe stopped here"
        raise ValueError(msg)

    monkeypatch.setattr(claude_messages, "build_provider", build_provider)
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    authored = {"project_id": "test-project", "region": "us-east5", "max_tokens": 4096}
    model = ModelConfig(provider="vertexai_claude", id="claude-sonnet-5-5", extra_kwargs=authored)

    assert _validate_vertexai_claude_connection(model, runtime_paths) == (None, "probe stopped here")
    assert [config.extra_kwargs for config in built] == [{"timeout": 10, **authored, "max_tokens": 1}]


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


@pytest.mark.parametrize(
    "error",
    [
        DefaultCredentialsError("no ADC"),
        ProviderUnavailable("token service unreachable", provider="VertexAI", code="connection_error"),
        ProviderError("connection refused", provider="VertexAI"),
    ],
    ids=["missing-credentials", "token-service-unreachable", "endpoint-unreachable"],
)
def test_unverifiable_checks_stay_inconclusive(error: Exception) -> None:
    """Missing credentials and unreachable services remain a warning, not a failure."""
    valid, detail = _classify_vertexai_claude_error(error)

    assert valid is None
    assert detail == str(error)


class _RejectedCredentials(Credentials):
    """Google credentials whose token endpoint rejects every refresh, as for a deleted service-account key."""

    def __init__(self) -> None:
        super().__init__(token=None)

    def refresh(self, request: object) -> None:  # noqa: ARG002 - google-auth passes its transport
        msg = "invalid_grant: Invalid JWT Signature."
        raise RefreshError(msg)


def test_rejected_vertex_credentials_fail_with_the_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials the token endpoint rejects fail the check with the token endpoint's reason, not a bare status."""
    monkeypatch.setattr(google.auth, "default", lambda scopes: (_RejectedCredentials(), "adc-project"))  # noqa: ARG005
    runtime_paths = resolve_primary_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={},
    )
    model = ModelConfig(
        provider="vertexai_claude",
        id="claude-sonnet-5-5",
        extra_kwargs={"project_id": "test-project", "region": "us-east5"},
    )

    valid, detail = _validate_vertexai_claude_connection(model, runtime_paths)

    assert valid is False
    assert detail == "Google credentials were rejected: invalid_grant: Invalid JWT Signature."


def _openai_embedder_config(host: str | None = None) -> Config:
    embedder: dict[str, object] = {"provider": "openai"}
    if host is not None:
        embedder["config"] = {"host": host}
    return Config(memory={"backend": "mem0", "embedder": embedder}, router=RouterConfig(model="default"))


def _doctor_runtime_paths(tmp_path: Path) -> RuntimePaths:
    return resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")


def test_provider_key_placeholder_warns_not_set_and_is_never_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A skipped first-run key leaves the starter placeholder, which doctor reports as not set instead of invalid."""
    (tmp_path / ".env").write_text("OPENAI_API_KEY=your-openai-key-here\n", encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    validated: list[object] = []
    monkeypatch.setattr(
        "mindroom.cli.doctor._validate_provider_key",
        lambda *args: validated.append(args) or (False, "HTTP 401"),
    )

    config = Config(
        models={"default": ModelConfig(provider="openai", id="gpt-6-astra")},
        router=RouterConfig(model="default"),
    )

    result = _check_single_provider("openai", config, set(), runtime_paths)

    assert result == (0, 0, 1)
    assert validated == []
    assert "openai: OPENAI_API_KEY not set" in capsys.readouterr().out


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


def test_doctor_counts_credential_sync_value_error_as_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Malformed credential encryption config is reported without aborting checks."""
    config_path = tmp_path / "missing" / "config.yaml"

    def fail_credential_sync(**_kwargs: object) -> None:
        msg = "invalid encryption key"
        raise ValueError(msg)

    monkeypatch.setattr(
        "mindroom.cli.doctor.sync_env_to_credentials",
        fail_credential_sync,
    )
    monkeypatch.setattr("mindroom.cli.doctor._check_matrix_homeserver", lambda **_kwargs: (1, 0, 0))

    with pytest.raises(typer.Exit):
        doctor(config_path=config_path, storage_path=tmp_path / "storage")

    output = capsys.readouterr().out
    assert "Could not sync env credentials into the store" in output
    assert "1 warning" in output


_NOT_PAIRED_LINE = "Not paired yet: `mindroom run` will print a link to approve with your MindRoom Chat account"


@pytest.mark.parametrize(
    ("env_file", "expected", "line"),
    [
        ("", (1, 0, 0), _NOT_PAIRED_LINE),
        ("MATRIX_HOMESERVER=http://localhost:8008\n", (0, 0, 0), None),
        ("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", (1, 0, 0), _NOT_PAIRED_LINE),
        (
            "MINDROOM_PROVISIONING_URL=https://mindroom.chat\n"
            "MINDROOM_LOCAL_CLIENT_ID=id\nMINDROOM_LOCAL_CLIENT_SECRET=secret\n",
            (1, 0, 0),
            "Paired with MindRoom Chat",
        ),
        (
            "MINDROOM_PROVISIONING_URL=https://mindroom.chat\nMINDROOM_LOCAL_CLIENT_ID=id\n",
            (0, 1, 0),
            "Provisioning credentials are incomplete",
        ),
        ("MINDROOM_PROVISIONING_URL=https://mindroom.chat\nMATRIX_REGISTRATION_TOKEN=t\n", (0, 0, 0), None),
    ],
)
def test_pairing_check_reports_hosted_pairing_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    env_file: str,
    expected: tuple[int, int, int],
    line: str | None,
) -> None:
    """An unpaired hosted install before its first run is informational, so the macOS Check step can pass."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    runtime_paths = resolve_primary_runtime_paths(config_path=config_path, process_env={})

    assert _check_pairing(runtime_paths) == expected

    output = " ".join(capsys.readouterr().out.split())
    if line is None:
        assert output == ""
    else:
        assert line in output


def test_doctor_summary_keeps_an_unpaired_first_run_free_of_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Doctor includes the pairing line without adding a warning to the summary the macOS app parses."""
    for name in ("MINDROOM_PROVISIONING_URL", "MINDROOM_LOCAL_CLIENT_ID", "MINDROOM_LOCAL_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    (config_dir / ".env").write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")
    monkeypatch.setattr("mindroom.cli.doctor._check_matrix_homeserver", lambda **_kwargs: (1, 0, 0))

    with pytest.raises(typer.Exit):
        doctor(config_path=config_dir / "config.yaml", storage_path=tmp_path / "storage")

    output = " ".join(capsys.readouterr().out.split())
    assert _NOT_PAIRED_LINE in output
    assert output.endswith("4 passed, 1 failed, 0 warnings")


def _versions_response(payload: dict[str, object]) -> httpx.Response:
    request = httpx.Request("GET", "https://matrix.test/_matrix/client/versions")
    return httpx.Response(200, request=request, json=payload)


def test_homeserver_check_ignores_msc4186_for_classic_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default classic mode does not require MSC4186 support."""
    monkeypatch.setattr(
        "mindroom.cli.doctor.httpx.get",
        lambda *_args, **_kwargs: _versions_response({"versions": ["v1.11"], "unstable_features": {}}),
    )

    assert _check_matrix_homeserver(_doctor_runtime_paths(tmp_path), config=Config()) == (1, 0, 0)


@pytest.mark.parametrize("advertised", [True, False, "true", None])
def test_homeserver_check_requires_advertised_sliding_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    advertised: object,
) -> None:
    """Sliding configuration reports incompatible servers before bot startup."""
    config = Config()
    config.matrix_sync = MatrixSyncConfig.model_construct(mode="sliding")
    monkeypatch.setattr(
        "mindroom.cli.doctor.httpx.get",
        lambda *_args, **_kwargs: _versions_response(
            {"versions": ["v1.11"], "unstable_features": {"org.matrix.simplified_msc3575": advertised}},
        ),
    )
    expected = (1, 0, 0) if advertised is True else (0, 1, 0)
    assert _check_matrix_homeserver(_doctor_runtime_paths(tmp_path), config=config) == expected
