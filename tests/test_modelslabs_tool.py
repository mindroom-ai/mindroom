"""ModelsLab job polling through the real SDK and an isolated HTTP transport."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
import requests

from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.metadata import get_tool_by_name
from mindroom.tools.agno_compat_modelslabs import ModelsLabCompletionTools
from mindroom.tools.modelslabs import modelslabs_tools

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class _ModelsLabReply:
    status_code: int
    body: dict[str, object] | bytes
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class _ModelsLabTransport:
    payloads: list[dict[str, object] | _ModelsLabReply | requests.exceptions.RequestException] = field(
        default_factory=list,
    )
    requests: list[requests.PreparedRequest] = field(default_factory=list)
    timeouts: list[object] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)

    def send(self, request: requests.PreparedRequest) -> requests.Response:
        """Return scripted provider responses without opening a connection."""
        self.requests.append(request)
        assert self.payloads, f"Unexpected provider request: {request.url}"
        payload = self.payloads.pop(0)
        if isinstance(payload, requests.exceptions.RequestException):
            raise payload
        response = requests.Response()
        reply = payload if isinstance(payload, _ModelsLabReply) else _ModelsLabReply(200, payload)
        response.status_code = reply.status_code
        response.headers.update(reply.headers)
        response._content = reply.body if isinstance(reply.body, bytes) else json.dumps(reply.body).encode()
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
    monkeypatch.setattr(time, "sleep", transport.sleeps.append)
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


@pytest.mark.parametrize(
    ("file_type", "media_field"),
    [("png", "images"), ("jpg", "images"), ("gif", "images"), ("mp4", "videos"), ("mp3", "audios"), ("wav", "audios")],
)
def test_inline_success_uses_completed_urls(
    provider: _ModelsLabTransport,
    file_type: str,
    media_field: str,
) -> None:
    """Immediate completion returns finished artifacts without polling queued links."""
    completed = [f"https://media.example.test/completed-{index}.{file_type}" for index in range(2)]
    provider.payloads = [
        {
            "status": "success",
            "id": 74123,
            "eta": 1,
            "output": completed,
            "future_links": [f"https://media.example.test/queued.{file_type}"],
        },
    ]
    tool = modelslabs_tools()(api_key="test-api-key", file_type=file_type, wait_for_completion=True)

    result = tool.generate_media("Make a test pattern")

    media = {"images": result.images, "videos": result.videos, "audios": result.audios}[media_field]
    assert media is not None
    assert [item.url for item in media] == completed
    assert "success" in result.content.lower()
    assert len(provider.requests) == 1
    assert provider.sleeps == []


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


@pytest.mark.parametrize("file_type", ["gif", "mp4", "mp3"])
@pytest.mark.parametrize(
    "error_response",
    [
        {"status": "error", "id": 74123, "message": "Generation rejected"},
        {"id": 74123, "error": "Generation rejected"},
    ],
    ids=["error-status", "error-field"],
)
def test_queued_media_reports_provider_failure(
    provider: _ModelsLabTransport,
    file_type: str,
    error_response: dict[str, object],
) -> None:
    """Terminal rejection returns its message without unavailable media artifacts."""
    media_url = f"https://media.example.test/result.{file_type}"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 1, "future_links": [media_url]},
        error_response,
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type=file_type,
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=1,
    )

    result = tool.generate_media("Make a test pattern")

    assert "Generation rejected" in result.content
    assert "success" not in result.content.lower()
    assert not result.images
    assert not result.videos
    assert not result.audios


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


@pytest.mark.parametrize("stage", ["generation", "fetch"])
@pytest.mark.parametrize("http_status", [200, 400, 401, 404])
@pytest.mark.parametrize(
    ("body", "explanation"),
    [
        ({"status": "error", "message": "Generation rejected"}, "Generation rejected"),
        ({"status": "error", "code": "content_moderated", "message": "Generation rejected"}, "Generation rejected"),
        ({"status": "error", "error": "Generation rejected"}, "Generation rejected"),
        ({"status": "error", "message": "", "error": "Generation rejected"}, "Generation rejected"),
        ({"error": "Generation rejected"}, "Generation rejected"),
        ({"status": "error"}, "Media generation failed"),
    ],
)
def test_http_provider_rejection_preserves_explanation(
    provider: _ModelsLabTransport,
    stage: str,
    http_status: int,
    body: dict[str, object],
    explanation: str,
) -> None:
    """Non-retryable provider rejections preserve their explanation and stop."""
    media_url = "https://media.example.test/result.gif"
    if stage == "fetch":
        provider.payloads.append({"status": "processing", "id": 74123, "eta": 2, "future_links": [media_url]})
    provider.payloads.append(_ModelsLabReply(http_status, body))
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=2,
    )

    result = tool.generate_media("Make a test pattern")

    assert result.content == f"Error: {explanation}"
    assert len(provider.requests) == (2 if stage == "fetch" else 1)
    assert provider.sleeps == []
    assert not result.images
    assert not result.videos
    assert not result.audios


@pytest.mark.parametrize("stage", ["generation", "fetch"])
@pytest.mark.parametrize("body", [b"<html>Unavailable</html>", b"", b"[]", b"null", {}, {"status": "success"}])
def test_unknown_http_failure_does_not_claim_success(
    provider: _ModelsLabTransport,
    stage: str,
    body: dict[str, object] | bytes,
) -> None:
    """HTTP failures without structured rejection cannot establish completion."""
    media_url = "https://media.example.test/result.gif"
    if stage == "fetch":
        provider.payloads.append({"status": "processing", "id": 74123, "eta": 1, "future_links": [media_url]})
    provider.payloads.append(_ModelsLabReply(503, body))
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=1,
    )

    result = tool.generate_media("Make a test pattern")

    assert "success" not in result.content.lower()
    assert len(provider.requests) == (2 if stage == "fetch" else 1)
    if stage == "fetch":
        assert "timed out" in result.content.lower()
        assert "may still" in result.content.lower()
        assert result.images is not None
        assert [item.url for item in result.images] == [media_url]
    else:
        assert "Network error" in result.content
        assert not result.images


@pytest.mark.parametrize(
    "failure",
    [
        _ModelsLabReply(503, {}),
        _ModelsLabReply(502, b"<html>Bad Gateway</html>"),
        requests.exceptions.ReadTimeout("Status unavailable"),
    ],
)
def test_failed_status_check_can_recover(
    provider: _ModelsLabTransport,
    failure: _ModelsLabReply | requests.exceptions.RequestException,
) -> None:
    """An inconclusive fetch is retried within the configured attempt budget."""
    media_url = "https://media.example.test/result.mp4"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 2},
        failure,
        {"status": "success", "output": [media_url]},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=2,
    )

    result = tool.generate_media("Make a test pattern")

    assert "success" in result.content.lower()
    assert len(provider.requests) == 3
    assert provider.sleeps == [1]
    assert result.videos is not None
    assert [item.url for item in result.videos] == [media_url]


@pytest.mark.parametrize(
    ("add_to_eta", "max_wait_time", "attempt_count"),
    [(0.5, 60, 2), (0, 0.5, 1)],
)
@pytest.mark.parametrize("final_status", ["success", "processing"])
def test_fractional_polling_settings_from_runtime_config(
    provider: _ModelsLabTransport,
    tmp_path: Path,
    add_to_eta: float,
    max_wait_time: float,
    attempt_count: int,
    final_status: str,
) -> None:
    """Dashboard numbers retain fractional values and round up to polling slots."""
    media_url = "https://media.example.test/result.gif"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 1, "future_links": [media_url]},
        *[{"status": "processing"} for _ in range(attempt_count - 1)],
        {"status": final_status},
    ]
    tool = get_tool_by_name(
        "modelslabs",
        resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage"),
        credential_overrides={"api_key": "test-api-key"},
        tool_config_overrides={
            "file_type": "gif",
            "wait_for_completion": True,
            "add_to_eta": add_to_eta,
            "max_wait_time": max_wait_time,
        },
        disable_sandbox_proxy=True,
        worker_target=None,
    )
    assert isinstance(tool, ModelsLabCompletionTools)

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == attempt_count + 1
    assert all(request.url.endswith("/fetch/74123") for request in provider.requests[1:])
    assert provider.sleeps == [1] * (attempt_count - 1)
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]
    if final_status == "success":
        assert "success" in result.content.lower()
    else:
        assert "timed out" in result.content.lower()
        assert "may still" in result.content.lower()
        assert "success" not in result.content.lower()


@pytest.mark.parametrize("max_wait_time", [1, 60])
@pytest.mark.parametrize("outcome", ["success", "processing", "transport_error"])
def test_zero_eta_checks_once(
    provider: _ModelsLabTransport,
    max_wait_time: int,
    outcome: str,
) -> None:
    """Zero ETA still checks once under a positive cap, without a trailing sleep."""
    media_url = "https://media.example.test/result.gif"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 0, "future_links": [media_url]},
        requests.exceptions.ReadTimeout("Status unavailable") if outcome == "transport_error" else {"status": outcome},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=max_wait_time,
    )

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 2
    assert provider.requests[1].url.endswith("/fetch/74123")
    assert provider.sleeps == []
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]
    if outcome == "success":
        assert "success" in result.content.lower()
    else:
        assert "timed out" in result.content.lower()
        assert "may still" in result.content.lower()
        assert "success" not in result.content.lower()


def test_zero_poll_cap_skips_fetch_and_retains_queued_links(provider: _ModelsLabTransport) -> None:
    """A zero cap permits no status requests and leaves completion unresolved."""
    media_url = "https://media.example.test/result.gif"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 0, "future_links": [media_url]},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=0,
    )

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 1
    assert provider.sleeps == []
    assert "timed out" in result.content.lower()
    assert "may still" in result.content.lower()
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]


@pytest.mark.parametrize(
    ("file_type", "media_field"),
    [("png", "images"), ("jpg", "images"), ("gif", "images"), ("mp4", "videos"), ("mp3", "audios"), ("wav", "audios")],
)
@pytest.mark.parametrize("queued_links", [False, True])
def test_completed_fetch_urls_replace_queued_links(
    provider: _ModelsLabTransport,
    file_type: str,
    media_field: str,
    queued_links: bool,
) -> None:
    """Completed outputs are returned even when queued URLs are absent or differ."""
    processing: dict[str, object] = {"status": "processing", "id": 74123, "eta": 1}
    if queued_links:
        processing["future_links"] = [f"https://media.example.test/queued.{file_type}"]
    output_urls = [f"https://media.example.test/completed-{index}.{file_type}" for index in range(2)]
    provider.payloads = [processing, {"status": "success", "id": 74123, "output": output_urls}]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type=file_type,
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=1,
    )

    result = tool.generate_media("Make a test pattern")

    media = {"images": result.images, "videos": result.videos, "audios": result.audios}[media_field]
    assert media is not None
    assert [item.url for item in media] == output_urls
    assert all(item.id != "74123" for item in media)
    assert "success" in result.content.lower()
    assert len(provider.requests) == 2


@pytest.mark.parametrize("eta", [1.5, 10.0, "1.5", "10", " 2.0 "])
def test_numeric_provider_eta_is_polled(provider: _ModelsLabTransport, eta: float | str) -> None:
    """Finite numeric ETAs reach image polling and honor the existing attempt cap."""
    media_url = "https://media.example.test/result.png"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": eta},
        {"status": "processing"},
        {"status": "success", "output": [media_url]},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="png",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=2,
    )

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 3
    assert all(request.url == "https://modelslab.com/api/v6/images/fetch/74123" for request in provider.requests[1:])
    assert all(json.loads(request.body) == {"key": "test-api-key"} for request in provider.requests[1:])
    assert provider.sleeps == [1]
    assert "success" in result.content.lower()
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]


@pytest.mark.parametrize("eta", [None, True, "unknown", "", "nan", "inf", float("nan"), float("inf"), -float("inf")])
def test_invalid_provider_eta_does_not_start_polling(provider: _ModelsLabTransport, eta: object) -> None:
    """Non-numeric and non-finite ETAs cannot become a polling attempt count."""
    provider.payloads = [{"status": "processing", "id": 74123, "eta": eta}]
    tool = modelslabs_tools()(api_key="test-api-key", file_type="png", wait_for_completion=True)

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 1
    assert "success" not in result.content.lower()
    assert provider.sleeps == []


@pytest.mark.parametrize("http_status", [500, 503])
def test_initial_service_error_preserves_explanation(provider: _ModelsLabTransport, http_status: int) -> None:
    """An initial submission failure preserves its message without resubmitting."""
    provider.payloads = [
        _ModelsLabReply(http_status, {"status": "error", "message": "Provider temporarily unavailable"}),
    ]
    tool = modelslabs_tools()(api_key="test-api-key", wait_for_completion=True)

    result = tool.generate_media("Make a test pattern")

    assert result.content == "Error: Provider temporarily unavailable"
    assert len(provider.requests) == 1
    assert provider.sleeps == []
    assert not result.images
    assert not result.videos
    assert not result.audios


@pytest.mark.parametrize(
    ("http_status", "error_code"),
    [
        (500, None),
        (503, None),
        (429, None),
        (200, "server_error"),
        (200, "upstream_unavailable"),
        (200, "rate_limited"),
    ],
)
@pytest.mark.parametrize("recovers", [False, True])
def test_retryable_fetch_error_preserves_job(
    provider: _ModelsLabTransport,
    http_status: int,
    error_code: str | None,
    recovers: bool,
) -> None:
    """Service errors retry within budget and preserve queued media if unresolved."""
    queued_url = "https://media.example.test/queued.gif"
    output_url = "https://media.example.test/completed.gif"
    error_body: dict[str, object] = {"status": "error", "message": "Provider temporarily unavailable"}
    if error_code is not None:
        error_body["code"] = error_code
    failure = _ModelsLabReply(http_status, error_body)
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 2, "future_links": [queued_url]},
        failure,
        {"status": "success", "output": [output_url]} if recovers else failure,
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=2,
    )

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 3
    assert provider.sleeps == [1]
    assert result.images is not None
    if recovers:
        assert "success" in result.content.lower()
        assert [item.url for item in result.images] == [output_url]
        assert "Provider temporarily unavailable" not in result.content
    else:
        assert "timed out" in result.content.lower()
        assert "may still" in result.content.lower()
        assert "Provider temporarily unavailable" in result.content
        assert "success" not in result.content.lower()
        assert [item.url for item in result.images] == [queued_url]


@pytest.mark.parametrize(
    ("http_status", "error_code", "unreadable"),
    [
        (429, None, False),
        (503, None, False),
        (200, "rate_limited", False),
        (200, "upstream_unavailable", False),
        (200, "server_error", False),
        (429, None, True),
        (503, None, True),
    ],
)
@pytest.mark.parametrize("retry_after", ["30", "Wed, 23 Sep 2026 12:00:00 GMT", "not-a-delay", "0", " "])
def test_provider_retry_after_stops_wait(
    provider: _ModelsLabTransport,
    http_status: int,
    error_code: str | None,
    unreadable: bool,
    retry_after: str,
) -> None:
    """Explicit provider cooldowns end this wait without early polling or sleeping."""
    media_url = "https://media.example.test/queued.gif"
    error_body: dict[str, object] | bytes = {"status": "error", "message": "Provider temporarily unavailable"}
    if error_code is not None:
        error_body["code"] = error_code
    if unreadable:
        error_body = b"<html>Temporarily unavailable</html>"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 2, "future_links": [media_url]},
        _ModelsLabReply(http_status, error_body, {"retry-after": retry_after}),
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=2,
    )

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 2
    assert provider.sleeps == []
    assert "Stopped waiting" in result.content
    assert f"Retry-After: {retry_after}" in result.content
    assert "may still" in result.content.lower()
    assert "success" not in result.content.lower()
    if not unreadable:
        assert "Provider temporarily unavailable" in result.content
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]


@pytest.mark.parametrize("http_status", [200, 429])
@pytest.mark.parametrize("retry_after", [None, ""])
def test_rate_limit_without_retry_after_can_recover(
    provider: _ModelsLabTransport,
    http_status: int,
    retry_after: str | None,
) -> None:
    """Absent or empty cooldown headers preserve the existing bounded retry path."""
    media_url = "https://media.example.test/completed.gif"
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 2},
        _ModelsLabReply(
            http_status,
            {"status": "error", "code": "rate_limited", "message": "Provider rate limit reached"},
            headers,
        ),
        {"status": "success", "output": [media_url]},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=2,
    )

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 3
    assert provider.sleeps == [1]
    assert "success" in result.content.lower()
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]


def test_retry_after_without_retryable_failure_does_not_stop_polling(provider: _ModelsLabTransport) -> None:
    """Account-limit headers alone do not turn a processing result into refusal."""
    media_url = "https://media.example.test/completed.gif"
    provider.payloads = [
        {"status": "processing", "id": 74123, "eta": 2},
        _ModelsLabReply(200, {"status": "processing"}, {"Retry-After": "30"}),
        {"status": "success", "output": [media_url]},
    ]
    tool = modelslabs_tools()(
        api_key="test-api-key",
        file_type="gif",
        wait_for_completion=True,
        add_to_eta=0,
        max_wait_time=2,
    )

    result = tool.generate_media("Make a test pattern")

    assert len(provider.requests) == 3
    assert provider.sleeps == [1]
    assert "success" in result.content.lower()
    assert result.images is not None
    assert [item.url for item in result.images] == [media_url]
