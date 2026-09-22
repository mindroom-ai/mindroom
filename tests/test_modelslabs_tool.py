"""ModelsLab job polling through the real SDK and an isolated HTTP transport."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import pytest
import requests

from mindroom.tools.modelslabs import modelslabs_tools


@dataclass
class _ModelsLabTransport:
    payloads: list[dict[str, object] | requests.exceptions.RequestException] = field(default_factory=list)
    requests: list[requests.PreparedRequest] = field(default_factory=list)
    timeouts: list[object] = field(default_factory=list)

    def send(self, request: requests.PreparedRequest) -> requests.Response:
        """Return scripted provider responses without opening a connection."""
        self.requests.append(request)
        assert self.payloads, f"Unexpected provider request: {request.url}"
        payload = self.payloads.pop(0)
        if isinstance(payload, requests.exceptions.RequestException):
            raise payload
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(payload).encode()
        response.request = request
        response.url = request.url
        return response


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _ModelsLabTransport:
    """Keep SDK request serialization and artifact construction real."""
    transport = _ModelsLabTransport()

    def send(
        _session: requests.Session,
        request: requests.PreparedRequest,
        **kwargs: object,
    ) -> requests.Response:
        transport.timeouts.append(kwargs.get("timeout"))
        return transport.send(request)

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    return transport


@pytest.mark.parametrize(
    ("file_type", "media_field", "fetch_kind"),
    [
        ("png", "images", "images"),
        ("jpg", "images", "images"),
        ("gif", "images", "video"),
        ("mp4", "videos", "video"),
        ("mp3", "audios", "voice"),
        ("wav", "audios", "voice"),
    ],
)
def test_queued_media_fetches_provider_job_id(
    provider: _ModelsLabTransport,
    file_type: str,
    media_field: str,
    fetch_kind: str,
) -> None:
    """Queued jobs use provider identity while artifacts retain local identity."""
    media_url = f"https://media.example.test/result.{file_type}"
    fetch_url = f"https://modelslab.com/api/v6/{fetch_kind}/fetch/74123"
    provider.payloads = [
        {
            "status": "processing",
            "id": 74123,
            "eta": 1,
            "future_links": [media_url],
            "fetch_result": fetch_url,
        },
        {"status": "success", "id": 74123, "output": [media_url]},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type=file_type,
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=1,
    )

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 2
    assert provider.requests[1].url == fetch_url
    assert json.loads(provider.requests[1].body) == {"key": "test-api-key"}
    media = {"images": result.images, "videos": result.videos, "audios": result.audios}[media_field]
    assert media is not None
    assert [item.url for item in media] == [media_url]
    assert media[0].id != "74123"
    assert "success" in result.content.lower()
    assert "will be ready" not in result.content.lower()


def test_queued_media_wait_timeout_does_not_claim_generation_success(provider: _ModelsLabTransport) -> None:
    """Exhausting a requested wait is not proof the provider job failed."""
    media_url = "https://media.example.test/result.gif"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 1, "future_links": [media_url]},
        {"status": "processing", "id": 74123, "eta": 10},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=1,
    )

    result = tool.generate_media("Make a test pattern")

    assert "timed out" in result.content.lower()
    assert "success" not in result.content.lower()
    assert "may still" in result.content.lower()
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]


def test_queued_media_reports_provider_failure(provider: _ModelsLabTransport) -> None:
    """A terminal fetch error must reach the caller instead of a success claim."""
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 1, "future_links": ["https://media.example.test/result.gif"]},
        {"status": "error", "id": 74123, "message": "Generation rejected"},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=1,
    )

    result = tool.generate_media("Make a test pattern")

    assert "Generation rejected" in result.content
    assert "success" not in result.content.lower()


@pytest.mark.parametrize("status", ["success", "processing"])
def test_media_without_wait_keeps_artifacts_and_skips_fetch(provider: _ModelsLabTransport, status: str) -> None:
    """The polling repair preserves the nonwaiting media return path."""
    media_url = "https://media.example.test/result.gif"
    provider.payloads = [{"status": status, "id": 74123, "eta": 1, "future_links": [media_url]}]
    tool = modelslabs_tools()(api_key="test-api-key", file_type="gif", wait_for_completion=False)

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 1
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]


def test_initial_provider_error_remains_an_error(provider: _ModelsLabTransport) -> None:
    """Immediate provider rejection bypasses polling and returns its message."""
    provider.payloads = [{"status": "error", "message": "Invalid request"}]
    tool = modelslabs_tools()(api_key="test-api-key", file_type="gif", wait_for_completion=True)

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 1
    assert "Invalid request" in result.content
    assert not result.images


@pytest.mark.parametrize("stage", ["generation", "fetch"])
def test_transport_timeout_returns_an_honest_outcome(provider: _ModelsLabTransport, stage: str) -> None:
    """Bound network stalls without claiming a failed status check failed the job."""
    media_url = "https://media.example.test/result.gif"
    if stage == "fetch":
        provider.payloads.append(
            {"status": "processing", "id": 74123, "eta": 1, "future_links": [media_url]},
        )
    provider.payloads.append(requests.exceptions.ReadTimeout("Provider read timed out"))
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=1,
    )

    result = tool.generate_media("Make a test pattern")

    assert "success" not in result.content.lower()
    if stage == "generation":
        assert "Network error" in result.content
        assert not result.images
    else:
        assert "timed out" in result.content.lower()
        assert "may still" in result.content.lower()
        assert result.images is not None
        assert [item.url for item in result.images] == [media_url]
    assert provider.timeouts
    for timeout in provider.timeouts:
        assert isinstance(timeout, (int, float))
        assert 0 < timeout <= 60
