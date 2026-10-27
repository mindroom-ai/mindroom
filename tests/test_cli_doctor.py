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
    _check_memory_llm,
    _check_pairing,
    _check_providers,
    _check_single_provider,
    _classify_vertexai_claude_error,
    _endpoint_settings,
    _endpoint_variables,
    _validate_provider_key,
    _validate_vertexai_claude_connection,
    doctor,
)
from mindroom.config.main import Config
from mindroom.config.matrix import MatrixSyncConfig
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import env_key_for_provider, resolve_primary_runtime_paths
from mindroom.credentials.sync import get_embedder_api_key, sync_env_to_credentials
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

    result = _check_single_provider("openai", config, runtime_paths)

    assert result == (0, 0, 1)
    assert validated == []
    assert "openai: OPENAI_API_KEY not set" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("provider", "url", "headers"),
    [
        # The request the dashboard's provider setup sends for the same key.
        ("openrouter", "https://openrouter.ai/api/v1/key", {"Authorization": "Bearer k"}),
        ("anthropic", "https://api.anthropic.com/v1/models", {"x-api-key": "k", "anthropic-version": "2023-06-01"}),
        ("openai", "https://api.openai.com/v1/models", {"Authorization": "Bearer k"}),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta/models?key=k", {}),
        ("groq", "https://api.groq.com/openai/v1/models", {"Authorization": "Bearer k"}),
    ],
)
def test_a_provider_key_is_checked_with_the_request_that_proves_it_valid(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    url: str,
    headers: dict[str, str],
) -> None:
    """Doctor sends the dashboard's key check, so an OpenRouter key is read back from its key endpoint."""
    sent: list[tuple[str, dict[str, str]]] = []

    def get(url: str, *, headers: dict[str, str], **_kwargs: object) -> httpx.Response:
        sent.append((url, headers))
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr("mindroom.cli.doctor.httpx.get", get)

    assert _validate_provider_key(provider, "k") == (True, "")
    assert sent == [(url, headers)]


@pytest.mark.parametrize("provider", ["zai", "huggingface"])
def test_a_provider_without_a_key_check_is_not_probed(monkeypatch: pytest.MonkeyPatch, provider: str) -> None:
    """Only a route with a known key check is probed, so an unknown key style cannot misreport a valid key."""

    def get(url: str, **_kwargs: object) -> httpx.Response:
        raise AssertionError(url)

    monkeypatch.setattr("mindroom.cli.doctor.httpx.get", get)

    assert _validate_provider_key(provider, "k") == (None, "no key check for this provider")


def test_doctor_checks_each_accepted_provider_spelling_as_the_route_the_runtime_uses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Spellings config load accepts group under the runtime's route and get that route's checks."""
    runtime_paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    sent = _record_key_checks(monkeypatch)
    vertex_models: list[str] = []

    def validate_vertex(model_config: ModelConfig, _runtime_paths: RuntimePaths) -> tuple[bool, str]:
        vertex_models.append(model_config.id)
        return True, ""

    monkeypatch.setattr("mindroom.cli.doctor._validate_vertexai_claude_connection", validate_vertex)
    config = Config(
        models={
            "default": ModelConfig(provider="Ollama", id="qwen3.8:27b", host="http://gpu.example:11434"),
            "small": ModelConfig(provider=" ollama ", id="qwen3.8:27b"),
            "claude": ModelConfig(provider="VertexAI-Claude", id="claude-sonnet-5-5"),
        },
        router=RouterConfig(model="default"),
    )

    assert _check_providers(config, runtime_paths) == (3, 0, 0)
    assert "Providers: ollama (2 models), vertexai_claude (1 model)" in capsys.readouterr().out
    assert [url for url, _headers in sent] == ["http://gpu.example:11434/api/tags", "http://localhost:11434/api/tags"]
    assert vertex_models == ["claude-sonnet-5-5"]


def test_doctor_checks_each_ollama_server_a_model_s_requests_reach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doctor checks every server the Ollama models reach, ``client_params.host`` first, once per server."""
    runtime_paths = _runtime_with_env(tmp_path, {"OLLAMA_HOST": "http://env-ollama:11434"})
    sync_env_to_credentials(runtime_paths=runtime_paths)
    sent = _record_key_checks(monkeypatch)
    config = Config(
        models={
            "default": ModelConfig(
                provider="ollama",
                id="gemma4",
                host="http://model-ollama:11434",
                extra_kwargs={"client_params": {"host": "http://client-ollama:11434"}},
            ),
            "plain": ModelConfig(provider="ollama", id="gemma4"),
            "other": ModelConfig(provider="Ollama", id="qwen3.8:27b"),
        },
        router=RouterConfig(model="default"),
    )

    assert _check_single_provider("ollama", config, runtime_paths) == (2, 0, 0)
    assert [url for url, _headers in sent] == [
        "http://client-ollama:11434/api/tags",
        "http://env-ollama:11434/api/tags",
    ]


def test_doctor_adds_up_every_ollama_server_s_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each server's pass, failure, or warning counts once in the route's totals."""
    runtime_paths = resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env={})
    statuses = {"http://up:11434": 200, "http://broken-a:11434": 500, "http://broken-b:11434": 500}

    def get(url: str, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("GET", url)
        host = url.removesuffix("/api/tags")
        if host not in statuses:
            msg = "unreachable"
            raise httpx.ConnectError(msg, request=request)
        return httpx.Response(statuses[host], request=request)

    monkeypatch.setattr("mindroom.cli.doctor.httpx.get", get)
    hosts = [*statuses, "http://down-a:11434", "http://down-b:11434"]
    config = Config(
        models={
            f"m{index}": ModelConfig(provider="ollama", id="gemma4", host=host) for index, host in enumerate(hosts)
        },
        router=RouterConfig(model="m0"),
    )

    assert _check_single_provider("ollama", config, runtime_paths) == (1, 2, 2)


@pytest.mark.parametrize(
    "llm",
    [
        {"provider": "ollama", "config": {"model": "gemma4", "host": "http://config-ollama:11434"}},
        {"provider": "ollama", "config": {"model": "gemma4"}},
        None,
    ],
    ids=["configured-host", "no-host", "default-llm"],
)
def test_doctor_checks_the_memory_llm_s_ollama_server_where_the_runtime_sends_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    llm: dict[str, object] | None,
) -> None:
    """A configured ``OLLAMA_HOST`` beats ``memory.llm.config.host``, and a chat model's host never applies."""
    runtime_paths = _runtime_with_env(tmp_path, {"OLLAMA_HOST": "http://env-ollama:11434"})
    sync_env_to_credentials(runtime_paths=runtime_paths)
    sent = _record_key_checks(monkeypatch)
    config = Config(
        models={"default": ModelConfig(provider="ollama", id="gemma4", host="http://chat-ollama:11434")},
        memory={"backend": "mem0", "llm": llm},
        router=RouterConfig(model="default"),
    )

    _check_memory_llm(config, runtime_paths)

    assert [url for url, _headers in sent] == ["http://env-ollama:11434/api/tags"]


def test_doctor_checks_the_ollama_embedder_where_the_runtime_sends_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured ``OLLAMA_HOST`` beats ``memory.embedder.config.host`` in doctor, as in the embedder it builds."""
    runtime_paths = _runtime_with_env(tmp_path, {"OLLAMA_HOST": "http://env-ollama:11434"})
    sync_env_to_credentials(runtime_paths=runtime_paths)
    sent = _record_key_checks(monkeypatch)
    config = Config(
        memory={
            "backend": "mem0",
            "embedder": {
                "provider": "ollama",
                "config": {"model": "nomic-embed-text", "host": "http://config-ollama:11434"},
            },
        },
        router=RouterConfig(model="default"),
    )

    assert _check_memory_embedder(config, runtime_paths) == (1, 0, 0)
    assert [url for url, _headers in sent] == ["http://env-ollama:11434/api/tags"]


def _record_key_checks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, str]]]:
    """Answer every doctor HTTP request with 200 and return the requests it sent."""
    sent: list[tuple[str, dict[str, str]]] = []

    def get(url: str, *, headers: dict[str, str], **_kwargs: object) -> httpx.Response:
        sent.append((url, headers))
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr("mindroom.cli.doctor.httpx.get", get)
    return sent


def _runtime_with_env(tmp_path: Path, env_file: dict[str, str], exported: dict[str, str] | None = None) -> RuntimePaths:
    """Return runtime paths whose ``.env`` holds ``env_file`` and whose process exports ``exported``."""
    (tmp_path / ".env").write_text("".join(f"{name}={value}\n" for name, value in env_file.items()), encoding="utf-8")
    return resolve_primary_runtime_paths(config_path=tmp_path / "config.yaml", process_env=exported or {})


@pytest.mark.parametrize(
    ("value", "found"),
    [
        (
            {"base_url": "https://a", "client_params": {"api_base": "https://b"}},
            ["m.base_url", "m.client_params.api_base"],
        ),
        ({"client_params": {"http_options": {"baseUrl": "https://a"}}}, ["m.client_params.http_options.baseUrl"]),
        (
            {"client_params": {"apiBase": "https://a", "defaultHeaders": {"X": "y"}}},
            ["m.client_params.apiBase", "m.client_params.defaultHeaders"],
        ),
        ({"host": "https://a", "azure_endpoint": "https://b"}, ["m.host", "m.azure_endpoint"]),
        ({"client_params": {"default_headers": {"Authorization": "Bearer g"}}}, ["m.client_params.default_headers"]),
        ({"base_url": "", "host": None, "default_headers": {}, "temperature": 0.2, "api_key": "k"}, []),
        (None, []),
    ],
)
def test_every_set_base_host_endpoint_or_header_key_counts_as_an_endpoint_setting(
    value: object,
    found: list[str],
) -> None:
    """Doctor never learns which keys a client reads: any set key with an endpoint-like name counts, at any depth."""
    assert _endpoint_settings(value, "m") == found


def test_a_provider_family_s_set_endpoint_variables_count(tmp_path: Path) -> None:
    """Only a set variable of the key's own family whose name ends like an endpoint's counts."""
    runtime_paths = _runtime_with_env(
        tmp_path,
        {"OPENAI_BASE_URL": "https://a", "OPENAI_PROXY_HOST": "h", "GROQ_BASE_URL": " "},
        {
            "OPENAI_API_BASE": "https://b",
            "OPENAI_API_ENDPOINT": "https://c",
            "OPENAI_ORGANIZATION": "org",
            "GOOGLE_GMAIL_ALLOWED_HOSTED_DOMAINS": "example.com",
        },
    )

    assert _endpoint_variables("OPENAI_API_KEY", runtime_paths) == [
        "OPENAI_API_BASE",
        "OPENAI_API_ENDPOINT",
        "OPENAI_BASE_URL",
        "OPENAI_PROXY_HOST",
    ]
    assert _endpoint_variables("GROQ_API_KEY", runtime_paths) == []
    assert _endpoint_variables("GOOGLE_API_KEY", runtime_paths) == []


_GATEWAY = "https://gateway.example/v1"


@pytest.mark.parametrize(
    ("provider", "model_settings", "env_file", "exported", "reason"),
    [
        pytest.param(
            "openai",
            {"extra_kwargs": {"client_params": {"base_url": _GATEWAY}, "base_url": "https://other.example/v1"}},
            {},
            {},
            "models.gateway.extra_kwargs.client_params.base_url, models.gateway.extra_kwargs.base_url",
            id="client-params-base-url-over-base-url",
        ),
        pytest.param(
            "groq",
            {"extra_kwargs": {"base_url": "https://groq-proxy.example"}},
            {},
            {},
            "models.gateway.extra_kwargs.base_url",
            id="groq-host",
        ),
        pytest.param(
            "OpenAI",
            {"extra_kwargs": {"base_url": _GATEWAY}},
            {},
            {},
            "models.gateway.extra_kwargs.base_url",
            id="accepted-provider-spelling",
        ),
        pytest.param(
            "Anthropic",
            {"extra_kwargs": {"client_params": {"base_url": "https://gateway.example/anthropic"}}},
            {},
            {},
            "models.gateway.extra_kwargs.client_params.base_url",
            id="anthropic-client-params",
        ),
        pytest.param(
            "openrouter",
            {"extra_kwargs": {"client_params": {"base_url": "https://openrouter.ai/api/v1"}}},
            {},
            {},
            "models.gateway.extra_kwargs.client_params.base_url",
            id="openrouter-client-params",
        ),
        pytest.param("openai", {"host": _GATEWAY}, {}, {}, "models.gateway.host", id="model-host"),
        # The SDK sends a configured Authorization header in place of the key it is given.
        pytest.param(
            "openai",
            {
                "extra_kwargs": {
                    "client_params": {"base_url": _GATEWAY, "default_headers": {"Authorization": "Bearer gateway-key"}},
                },
            },
            {},
            {},
            "models.gateway.extra_kwargs.client_params.base_url, models.gateway.extra_kwargs.client_params.default_headers",
            id="authorization-header-at-a-gateway",
        ),
        pytest.param(
            "openai",
            {"extra_kwargs": {"default_headers": {"Authorization": "Bearer gateway-key"}}},
            {},
            {},
            "models.gateway.extra_kwargs.default_headers",
            id="authorization-header-alone",
        ),
        # google-genai takes the endpoint from http_options, in either spelling.
        pytest.param(
            "google",
            {"extra_kwargs": {"client_params": {"http_options": {"base_url": "https://gemini-gateway.example"}}}},
            {},
            {},
            "models.gateway.extra_kwargs.client_params.http_options.base_url",
            id="gemini-http-options",
        ),
        pytest.param(
            "gemini",
            {"extra_kwargs": {"client_params": {"http_options": {"baseUrl": "https://gemini-gateway.example"}}}},
            {},
            {},
            "models.gateway.extra_kwargs.client_params.http_options.baseUrl",
            id="gemini-http-options-camel-case",
        ),
        # client_params.vertexai moves the model to Vertex AI, which reads GOOGLE_VERTEX_BASE_URL.
        pytest.param(
            "google",
            {"extra_kwargs": {"client_params": {"vertexai": True}}},
            {"GOOGLE_VERTEX_BASE_URL": "https://vertex-gateway.example"},
            {},
            "GOOGLE_VERTEX_BASE_URL",
            id="vertexai-client-override",
        ),
        pytest.param(
            "gemini",
            {},
            {},
            {"GOOGLE_GEMINI_BASE_URL": "https://gemini-gateway.example"},
            "GOOGLE_GEMINI_BASE_URL",
            id="gemini-variable-exported",
        ),
        pytest.param("openai", {}, {"OPENAI_BASE_URL": _GATEWAY}, {}, "OPENAI_BASE_URL", id="openai-variable"),
        pytest.param(
            "anthropic",
            {},
            {},
            {"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
            "ANTHROPIC_BASE_URL",
            id="anthropic-variable-exported",
        ),
    ],
)
def test_doctor_skips_the_shared_key_check_wherever_an_endpoint_setting_or_variable_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    model_settings: dict[str, object],
    env_file: dict[str, str],
    exported: dict[str, str],
    reason: str,
) -> None:
    """The shared key goes nowhere, neither to the custom server nor to the provider, and doctor says why."""
    env_key = env_key_for_provider(provider)
    assert env_key is not None
    runtime_paths = _runtime_with_env(tmp_path, {env_key: "shared", **env_file}, exported)
    sent = _record_key_checks(monkeypatch)
    config = Config(
        models={"gateway": ModelConfig(provider=provider, id="m", **model_settings)},
        router=RouterConfig(model="gateway"),
    )

    assert _check_providers(config, runtime_paths) == (0, 0, 1)
    assert sent == []
    output = " ".join(capsys.readouterr().out.split())
    assert f"{env_key} not checked: {reason} may send it elsewhere or send another key in its place" in output


@pytest.mark.parametrize(
    ("provider", "env_file", "url"),
    [
        ("openai", {}, "https://api.openai.com/v1/models"),
        ("groq", {}, "https://api.groq.com/openai/v1/models"),
        # Another family's endpoint variable and a Google name containing HOST do not count.
        ("anthropic", {"OPENAI_BASE_URL": _GATEWAY}, "https://api.anthropic.com/v1/models"),
        (
            "google",
            {"GOOGLE_GMAIL_ALLOWED_HOSTED_DOMAINS": "example.com"},
            "https://generativelanguage.googleapis.com/v1beta/models?key=shared",
        ),
    ],
)
def test_doctor_checks_a_shared_key_at_the_provider_s_own_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    env_file: dict[str, str],
    url: str,
) -> None:
    """A model without an endpoint setting or variable gets the shared key checked at its provider's API."""
    env_key = env_key_for_provider(provider)
    assert env_key is not None
    runtime_paths = _runtime_with_env(tmp_path, {env_key: "shared", **env_file})
    sent = _record_key_checks(monkeypatch)
    config = Config(models={"default": ModelConfig(provider=provider, id="m")}, router=RouterConfig(model="default"))

    assert _check_providers(config, runtime_paths) == (1, 0, 0)
    assert [sent_url for sent_url, _headers in sent] == [url]


@pytest.mark.parametrize(
    ("provider", "extra_kwargs", "env_file"),
    [
        # The documented Azure setup: AZURE_OPENAI_ENDPOINT in .env, and a model may also set azure_endpoint.
        (
            "azure",
            {"azure_endpoint": "https://res.openai.azure.com", "azure_deployment": "d"},
            {"AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com"},
        ),
        ("zai", {"base_url": "https://api.z.ai/api/coding/paas/v4"}, {}),
    ],
)
def test_a_provider_without_a_key_check_gets_one_no_key_check_warning_whatever_its_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    extra_kwargs: dict[str, str],
    env_file: dict[str, str],
) -> None:
    """Doctor never probes such a key, so its endpoint settings are no reason for a not-checked warning per model."""
    env_key = env_key_for_provider(provider)
    assert env_key is not None
    runtime_paths = _runtime_with_env(tmp_path, {env_key: "k", **env_file})
    sent = _record_key_checks(monkeypatch)
    config = Config(
        models={
            "first": ModelConfig(provider=provider, id="m", extra_kwargs=extra_kwargs),
            "second": ModelConfig(provider=provider, id="m", extra_kwargs=extra_kwargs),
        },
        router=RouterConfig(model="first"),
    )

    assert _check_providers(config, runtime_paths) == (0, 0, 1)
    assert sent == []
    output = " ".join(capsys.readouterr().out.split())
    assert f"{provider}: could not validate key (no key check for this provider)" in output
    assert "not checked" not in output


def test_doctor_skips_an_endpoint_model_when_the_credential_store_is_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the store, a model with an endpoint setting may read the shared key, so it is not checked."""
    runtime_paths = _runtime_with_env(tmp_path, {"OPENAI_API_KEY": "shared"})
    sent = _record_key_checks(monkeypatch)

    def unreadable(*_args: object) -> bool:
        raise OSError

    monkeypatch.setattr("mindroom.cli.doctor.model_uses_own_credential", unreadable)
    gateway = {"client_params": {"base_url": _GATEWAY, "api_key": "proxy-key"}}
    config = Config(
        models={"gateway": ModelConfig(provider="openai", id="m", extra_kwargs=gateway)},
        router=RouterConfig(model="gateway"),
    )

    assert _check_providers(config, runtime_paths) == (0, 0, 1)
    assert sent == []


def test_doctor_checks_a_shared_key_at_the_provider_s_own_api_beside_a_custom_endpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A model on the provider's own API still gets the shared key checked there; the gateway model is not checked."""
    runtime_paths = _runtime_with_env(tmp_path, {"OPENAI_API_KEY": "shared"})
    sent = _record_key_checks(monkeypatch)
    gateway = {"client_params": {"base_url": _GATEWAY}}
    config = Config(
        models={
            "hosted": ModelConfig(provider="openai", id="gpt-6-astra"),
            "gateway": ModelConfig(provider="openai", id="m", extra_kwargs=gateway),
        },
        router=RouterConfig(model="hosted"),
    )

    assert _check_providers(config, runtime_paths) == (1, 0, 1)
    assert sent == [("https://api.openai.com/v1/models", {"Authorization": "Bearer shared"})]
    output = " ".join(capsys.readouterr().out.split())
    assert "OPENAI_API_KEY not checked: models.gateway.extra_kwargs.client_params.base_url may send" in output


def test_doctor_keeps_the_shared_key_from_an_endpoint_that_gets_the_model_s_client_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``client_params`` set an endpoint and a key, so the shared key goes only to the provider."""
    runtime_paths = _runtime_with_env(tmp_path, {"OPENAI_API_KEY": "shared"})
    sent = _record_key_checks(monkeypatch)
    client_params = {"base_url": "https://proxy.example/v1", "api_key": "proxy-key"}
    config = Config(
        models={
            "default": ModelConfig(provider="openai", id="gpt-6-astra", extra_kwargs={"client_params": client_params}),
        },
        router=RouterConfig(model="default"),
    )

    assert _check_single_provider("openai", config, runtime_paths) == (1, 0, 0)
    assert sent == [("https://api.openai.com/v1/models", {"Authorization": "Bearer shared"})]


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


@pytest.mark.parametrize(
    ("provider", "llm_config", "exported", "key_name", "reason"),
    [
        # Mem0 sends the shared key to the memory LLM's configured endpoint.
        (
            "openai",
            {"model": "m", "openai_base_url": "https://mem-gateway.example/v1"},
            {},
            "OPENAI_API_KEY",
            "memory.llm.config.openai_base_url",
        ),
        # Mem0's OpenAI client reads OPENAI_BASE_URL itself, for the shared key and a configured one alike.
        (
            "openai",
            {"model": "m"},
            {"OPENAI_BASE_URL": "https://mem-gateway.example/v1"},
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
        ),
        (
            "openai",
            {"model": "m", "api_key": "sk-litellm"},
            {"OPENAI_BASE_URL": "https://litellm.internal/v1"},
            "memory.llm.config.api_key",
            "OPENAI_BASE_URL",
        ),
        # Mem0's Anthropic client leaves the endpoint to the SDK, which reads ANTHROPIC_BASE_URL.
        (
            "anthropic",
            {"model": "m", "api_key": "sk-litellm"},
            {"ANTHROPIC_BASE_URL": "https://litellm.internal"},
            "memory.llm.config.api_key",
            "ANTHROPIC_BASE_URL",
        ),
        # A configured key is skipped too: doctor never works out which endpoint Mem0 sends it to.
        (
            "openai",
            {"model": "m", "api_key": "sk-mem", "openai_base_url": "https://mem.example/v1"},
            {},
            "memory.llm.config.api_key",
            "memory.llm.config.openai_base_url",
        ),
        # An OpenRouter gateway: an exported OPENROUTER_API_KEY sends Mem0's OpenAI client, for openrouter and openai
        # alike, to openrouter_base_url with that key.
        (
            "openrouter",
            {"model": "m", "api_key": "gateway-key", "openrouter_base_url": "https://gateway.example/v1"},
            {"OPENROUTER_API_KEY": "gateway-key"},
            "memory.llm.config.api_key",
            "memory.llm.config.openrouter_base_url, OPENROUTER_API_KEY",
        ),
        (
            "openai",
            {"model": "m", "api_key": "gateway-key", "openrouter_base_url": "https://gateway.example/v1"},
            {"OPENROUTER_API_KEY": "gateway-key"},
            "memory.llm.config.api_key",
            "memory.llm.config.openrouter_base_url, OPENROUTER_API_KEY",
        ),
        # The exported OPENROUTER_API_KEY alone takes an openai memory LLM to OpenRouter, and it replaces a configured
        # openrouter key.
        ("openai", {"model": "m"}, {"OPENROUTER_API_KEY": "or-key"}, "OPENAI_API_KEY", "OPENROUTER_API_KEY"),
        (
            "openrouter",
            {"model": "m", "api_key": "sk-or-config"},
            {"OPENROUTER_API_KEY": "or-key"},
            "memory.llm.config.api_key",
            "OPENROUTER_API_KEY",
        ),
    ],
)
def test_memory_llm_key_is_not_checked_wherever_an_endpoint_setting_or_variable_may_send_it_elsewhere(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    llm_config: dict[str, str],
    exported: dict[str, str],
    key_name: str,
    reason: str,
) -> None:
    """A memory LLM key that Mem0 may send to another server is sent nowhere, neither there nor to the provider."""
    env_key = env_key_for_provider(provider)
    assert env_key is not None
    runtime_paths = _runtime_with_env(tmp_path, {env_key: "shared"}, exported)
    sent = _record_key_checks(monkeypatch)
    config = Config(
        memory={"backend": "mem0", "llm": {"provider": provider, "config": llm_config}},
        router=RouterConfig(model="default"),
    )

    assert _check_memory_llm(config, runtime_paths) == (0, 0, 1)
    assert sent == []
    output = " ".join(capsys.readouterr().out.split())
    assert (
        f"Memory LLM: {key_name} not checked: {reason} may send it elsewhere or send another key in its place" in output
    )


@pytest.mark.parametrize(
    ("provider", "llm_config", "exported", "sent_request"),
    [
        (
            "openai",
            {"model": "m", "api_key": "sk-mem"},
            {},
            ("https://api.openai.com/v1/models", {"Authorization": "Bearer sk-mem"}),
        ),
        # Mem0 sends the exported OPENROUTER_API_KEY itself, which doctor checks at OpenRouter's own API.
        (
            "openrouter",
            {"model": "m"},
            {"OPENROUTER_API_KEY": "or-key"},
            ("https://openrouter.ai/api/v1/key", {"Authorization": "Bearer or-key"}),
        ),
    ],
)
def test_memory_llm_key_is_checked_at_the_provider_s_own_api_without_endpoint_settings_or_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    llm_config: dict[str, str],
    exported: dict[str, str],
    sent_request: tuple[str, dict[str, str]],
) -> None:
    """A memory LLM key with nothing that could redirect it is checked at its provider's own API."""
    runtime_paths = _runtime_with_env(tmp_path, {}, exported)
    sent = _record_key_checks(monkeypatch)
    config = Config(
        memory={"backend": "mem0", "llm": {"provider": provider, "config": llm_config}},
        router=RouterConfig(model="default"),
    )

    assert _check_memory_llm(config, runtime_paths) == (1, 0, 0)
    assert sent == [sent_request]


@pytest.mark.parametrize(
    ("env_file", "expected", "urls"),
    [
        ({}, (1, 0, 0), ["https://generativelanguage.googleapis.com/v1beta/models?key=shared"]),
        ({"GOOGLE_GEMINI_BASE_URL": "https://embed-gateway.example"}, (0, 0, 1), []),
    ],
)
def test_memory_embedder_shared_key_is_checked_only_without_an_endpoint_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env_file: dict[str, str],
    expected: tuple[int, int, int],
    urls: list[str],
) -> None:
    """A Mem0 gemini embedder reads GOOGLE_GEMINI_BASE_URL through google-genai, so doctor skips its key then."""
    runtime_paths = _runtime_with_env(tmp_path, {"GOOGLE_API_KEY": "shared", **env_file})
    sent = _record_key_checks(monkeypatch)
    embedder = {"provider": "gemini", "config": {"model": "gemini-embedding-2"}}
    config = Config(memory={"backend": "mem0", "embedder": embedder}, router=RouterConfig(model="default"))

    assert _check_memory_embedder(config, runtime_paths) == expected
    assert [url for url, _headers in sent] == urls


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
