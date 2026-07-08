"""Tests for doctor's Vertex AI Claude failure classification."""

from __future__ import annotations

import httpx
from anthropic import APIStatusError
from google.auth.exceptions import DefaultCredentialsError

from mindroom.cli.doctor import _classify_vertexai_claude_error


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
