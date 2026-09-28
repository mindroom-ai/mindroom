"""Tests for the first-run AI provider setup API."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mindroom import constants
from mindroom.api import main, provider_setup
from mindroom.api.main import app, initialize_api_app
from mindroom.config.main import Config
from mindroom.credentials import CredentialsManager, get_runtime_credentials_manager

_OPENROUTER_KEY = "sk-or-v1-secret-test-key"


def _hosted_config() -> Config:
    """Mirror the hosted seed: one agent and the router on an OpenRouter default model."""
    return Config.model_validate(
        {
            "models": {
                "default": {"provider": "openrouter", "id": "google/gemini-3.8-flash"},
                "sonnet": {"provider": "anthropic", "id": "claude-sonnet-5"},
                "local": {"provider": "ollama", "id": "qwen3.8:27b"},
            },
            "agents": {"mind": {"display_name": "Mind", "role": "assistant", "model": "default"}},
        },
    )


def _publish_config(config: Config) -> None:
    context = main._app_context(app)
    context.config_data = config.authored_model_dump()
    context.runtime_config = config
    context.config_load_result = main.ConfigLoadResult(success=True)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create an API client with the hosted seed config committed."""
    initialize_api_app(
        app,
        constants.resolve_primary_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "mindroom_data",
            process_env={constants.OWNER_MATRIX_USER_ID_ENV: "@alice:example.org"},
        ),
    )
    _publish_config(_hosted_config())
    return TestClient(app, base_url="http://localhost")


def _credentials(client: TestClient) -> CredentialsManager:
    return get_runtime_credentials_manager(main._app_runtime_paths(client.app))


def _mock_provider(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        provider_setup.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_status_reports_provider_used_by_agents_without_key(client: TestClient) -> None:
    """The router and the Mind agent share the OpenRouter default model, which has no key yet."""
    response = client.get("/api/provider-setup/status")

    assert response.status_code == 200
    assert response.json() == {"missing": [{"provider": "openrouter", "models": ["default"]}]}


def test_status_accepts_key_saved_under_env_var_name(client: TestClient) -> None:
    """A key saved as ``OPENROUTER_API_KEY`` counts, exactly like runtime model loading."""
    _credentials(client).save_credentials("OPENROUTER_API_KEY", {"api_key": _OPENROUTER_KEY, "_source": "ui"})

    response = client.get("/api/provider-setup/status")

    assert response.json() == {"missing": []}


def test_status_accepts_per_model_key(client: TestClient) -> None:
    """A dashboard key saved for one model config satisfies that model."""
    _credentials(client).save_credentials("model:default", {"api_key": _OPENROUTER_KEY, "_source": "ui"})

    assert client.get("/api/provider-setup/status").json() == {"missing": []}


def test_status_accepts_key_in_model_config(client: TestClient) -> None:
    """An explicit ``extra_kwargs.api_key`` satisfies the model, like at runtime."""
    config = _hosted_config()
    config.models["default"].extra_kwargs = {"api_key": _OPENROUTER_KEY}
    _publish_config(config)

    assert client.get("/api/provider-setup/status").json() == {"missing": []}


@pytest.mark.parametrize(
    "model",
    [
        {"provider": "anthropic", "id": "claude-sonnet-5", "extra_kwargs": {"auth_token": "anthropic-oauth-token"}},
        {"provider": "azure", "id": "gpt-6-astra", "extra_kwargs": {"azure_ad_token": "azure-ad-token"}},
        {"provider": "azure", "id": "gpt-6-astra", "extra_kwargs": {"azure_ad_token_provider": "token-provider"}},
        {"provider": "gemini", "id": "gemini-3.8-flash", "extra_kwargs": {"vertexai": True}},
        {"provider": "google", "id": "gemini-3.8-flash", "extra_kwargs": {"vertexai": True}},
        {"provider": "bedrock_claude", "id": "anthropic.claude-sonnet-5"},
        {"provider": "vertexai_claude", "id": "claude-sonnet-5"},
        {"provider": "codex", "id": "gpt-6-astra"},
        {"provider": "ollama", "id": "qwen3.8:27b"},
    ],
)
def test_status_accepts_non_api_key_authentication(client: TestClient, model: dict[str, object]) -> None:
    """Models authenticated by a token or a non-API-key runtime never ask for a provider key."""
    _publish_config(
        Config.model_validate(
            {
                "models": {"default": model},
                "agents": {"mind": {"display_name": "Mind", "role": "assistant", "model": "default"}},
            },
        ),
    )

    assert client.get("/api/provider-setup/status").json() == {"missing": []}


def _config_with_unknown_model_reference() -> Config:
    """Config validation allows the router and agents to name a model missing from ``models``."""
    return Config.model_validate(
        {
            "models": {"sonnet": {"provider": "openrouter", "id": "anthropic/claude-sonnet-5"}},
            "agents": {"mind": {"display_name": "Mind", "role": "assistant", "model": "ghost"}},
        },
    )


def test_status_skips_model_references_missing_from_models(client: TestClient) -> None:
    """An unknown model reference (here the implicit router ``default``) never breaks the status check."""
    _publish_config(_config_with_unknown_model_reference())

    response = client.get("/api/provider-setup/status")

    assert response.status_code == 200
    assert response.json() == {"missing": []}


def test_connect_succeeds_with_model_references_missing_from_models(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saving a verified key still reports success when config names an unknown model."""
    _publish_config(_config_with_unknown_model_reference())
    _mock_provider(monkeypatch, lambda _request: httpx.Response(200, json={"data": {}}))

    response = client.post("/api/provider-setup/connect", json={"provider": "openrouter", "api_key": _OPENROUTER_KEY})

    assert response.status_code == 200
    assert response.json() == {"service": "openrouter", "missing": []}
    assert _credentials(client).load_credentials("openrouter") == {"api_key": _OPENROUTER_KEY, "_source": "ui"}


def test_status_ignores_unused_and_keyless_models(client: TestClient) -> None:
    """Unused models and providers without API keys never ask for setup."""
    config = _hosted_config()
    config.agents["mind"].model = "local"
    config.router.model = "local"
    _publish_config(config)

    assert client.get("/api/provider-setup/status").json() == {"missing": []}


def test_connect_verifies_key_and_saves_canonical_service(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key the provider accepts is saved under ``openrouter`` as a UI credential."""
    requests: list[httpx.Request] = []

    def openrouter(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": {"label": "sk-or-v1-sec...key"}})

    _mock_provider(monkeypatch, openrouter)

    response = client.post(
        "/api/provider-setup/connect",
        json={"provider": "openrouter", "api_key": f"  {_OPENROUTER_KEY}\n"},
    )

    assert response.status_code == 200
    assert response.json() == {"service": "openrouter", "missing": []}
    assert _OPENROUTER_KEY not in response.text
    assert [(request.method, str(request.url)) for request in requests] == [
        ("GET", "https://openrouter.ai/api/v1/key"),
    ]
    assert requests[0].headers["Authorization"] == f"Bearer {_OPENROUTER_KEY}"
    assert _credentials(client).load_credentials("openrouter") == {"api_key": _OPENROUTER_KEY, "_source": "ui"}


def test_connect_anthropic_uses_models_endpoint_and_reports_remaining_openrouter_models(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An Anthropic key is saved, but the OpenRouter default model still needs its own key."""
    requests: list[httpx.Request] = []

    def anthropic(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    _mock_provider(monkeypatch, anthropic)

    response = client.post("/api/provider-setup/connect", json={"provider": "anthropic", "api_key": "sk-ant-test"})

    assert response.status_code == 200
    assert response.json() == {
        "service": "anthropic",
        "missing": [{"provider": "openrouter", "models": ["default"]}],
    }
    assert str(requests[0].url) == "https://api.anthropic.com/v1/models"
    assert requests[0].headers["x-api-key"] == "sk-ant-test"
    assert requests[0].headers["anthropic-version"] == "2023-06-01"


@pytest.mark.parametrize("status_code", [401, 403])
def test_connect_rejected_key_is_not_saved_or_echoed(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    status_code: int,
) -> None:
    """A key the provider rejects returns a clear error and is never stored, logged, or echoed."""
    _mock_provider(monkeypatch, lambda _request: httpx.Response(status_code, json={"error": "invalid key"}))

    response = client.post("/api/provider-setup/connect", json={"provider": "openai", "api_key": _OPENROUTER_KEY})

    assert response.status_code == 400
    assert response.json() == {
        "detail": "OpenAI rejected this API key. Check that you copied the whole key and that it is active.",
    }
    assert _credentials(client).load_credentials("openai") is None
    captured = capsys.readouterr()
    assert _OPENROUTER_KEY not in caplog.text + captured.out + captured.err


def test_connect_reports_unreachable_provider_without_saving(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Network failures ask the user to retry and never store an unverified key."""

    def unreachable(request: httpx.Request) -> httpx.Response:
        message = "connection refused"
        raise httpx.ConnectError(message, request=request)

    _mock_provider(monkeypatch, unreachable)

    response = client.post("/api/provider-setup/connect", json={"provider": "openrouter", "api_key": _OPENROUTER_KEY})

    assert response.status_code == 502
    assert response.json() == {"detail": "Could not reach OpenRouter to verify the key. Try again in a moment."}
    assert _credentials(client).load_credentials("openrouter") is None


def test_connect_reports_provider_outage_without_saving(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-side failures other than auth rejections are reported as temporary."""
    _mock_provider(monkeypatch, lambda _request: httpx.Response(503))

    response = client.post("/api/provider-setup/connect", json={"provider": "openrouter", "api_key": _OPENROUTER_KEY})

    assert response.status_code == 502
    assert "HTTP 503" in response.json()["detail"]
    assert _credentials(client).load_credentials("openrouter") is None


def test_connect_rejects_blank_key_and_unknown_provider(client: TestClient) -> None:
    """Blank keys and providers outside the setup list never reach a provider."""
    blank = client.post("/api/provider-setup/connect", json={"provider": "openrouter", "api_key": "   "})
    unknown = client.post("/api/provider-setup/connect", json={"provider": "groq", "api_key": "gsk-test"})

    assert blank.status_code == 400
    assert blank.json() == {"detail": "Paste an API key first."}
    assert unknown.status_code == 422
