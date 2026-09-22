"""Preserve provider job identity while waiting for ModelsLab media."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import requests
from agno.models.response import FileType
from agno.tools.function import ToolResult
from agno.tools.models_labs import ModelsLabTools
from requests.exceptions import RequestException

_REQUEST_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class _JobWaitResult:
    """Carry completed URLs and distinguish rejection from an unresolved wait."""

    content: str
    rejected: bool = False
    output_urls: tuple[str, ...] = ()


def _check_provider_http_status(response: requests.Response, result: object) -> None:
    """Preserve provider rejection bodies before enforcing HTTP status."""
    if isinstance(result, dict):
        provider_result = cast("dict[str, object]", result)
        if provider_result.get("status") == "error" or "error" in provider_result:
            return
    response.raise_for_status()


# AGNO_COMPAT: ModelsLab polling loses provider identity and reports unsuccessful waits as success.
# Reason: Agno 3.0.9 sends the artifact UUID to fetch and discards the wait outcome and completed URLs.
# Upstream issue: Tracking gap; no matching issue found on September 22, 2026.
# Upstream PR: No matching fix identified.
# Remove when: Agno polls the provider ID and distinguishes completion, timeout,
# and terminal rejection, retains completed URLs, and accepts numeric ETAs;
# retain bounded transport waits, authored file-type normalization, and media helpers.
# Coverage: tests/test_modelslabs_tool.py::test_queued_media_fetches_provider_job_id,
# ::test_queued_media_wait_timeout_does_not_claim_generation_success,
# ::test_queued_media_reports_provider_failure,
# ::test_transport_timeout_returns_an_honest_outcome,
# ::test_http_provider_rejection_preserves_explanation,
# ::test_fractional_polling_settings_from_runtime_config,
# ::test_zero_eta_checks_once, ::test_completed_fetch_urls_replace_queued_links,
# ::test_numeric_provider_eta_is_polled, ::test_retryable_fetch_error_preserves_job, and nonwaiting/error controls.
class ModelsLabCompletionTools(ModelsLabTools):
    """Repair completion waiting while retaining upstream media construction."""

    def generate_media(self, prompt: str) -> ToolResult:
        """Generate media (video, image, or audio) given a prompt."""
        if not self.wait_for_completion or not self.api_key:
            return super().generate_media(prompt)
        try:
            return self._generate_waiting_media(prompt)
        except RequestException as exc:
            return ToolResult(content=f"Error: Network error while generating {self.file_type.value}: {exc}")

    def _generate_waiting_media(self, prompt: str) -> ToolResult:
        response = requests.post(
            self.url,
            data=json.dumps(self._create_payload(prompt)),
            headers={"Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        result = response.json()
        _check_provider_http_status(response, result)
        if result.get("status") == "error":
            return ToolResult(
                content=f"Error: {result.get('message') or result.get('error') or 'Media generation failed'}",
            )
        if "error" in result:
            return ToolResult(content=f"Error: {result['error']}")

        eta = result.get("eta")
        if self.file_type in (FileType.PNG, FileType.JPG, FileType.WAV):
            url_links = result.get("output") or result.get("future_links", [])
        else:
            url_links = result.get("future_links") or result.get("output", [])
        content = f"{self.file_type.value.capitalize()} generated successfully"
        if result.get("status") != "success":
            if isinstance(eta, int | float) and not isinstance(eta, bool) and math.isfinite(eta):
                job_id = result.get("id")
                if job_id is None:
                    content = "Cannot wait for media: provider response has no job ID. The job may still complete."
                else:
                    wait_result = self._wait_for_job(str(job_id), eta)
                    if wait_result.rejected:
                        return ToolResult(content=wait_result.content)
                    content = wait_result.content
                    if wait_result.output_urls:
                        url_links = wait_result.output_urls
            else:
                content = (
                    "Cannot wait for media: provider response has no finite numeric ETA. The job may still complete."
                )

        media_id = str(uuid4())
        all_images = []
        all_videos = []
        all_audios = []
        for media_url in url_links:
            artifacts = self._create_media_artifacts(media_id, media_url, str(eta))
            all_images.extend(artifacts["images"])
            all_videos.extend(artifacts["videos"])
            all_audios.extend(artifacts["audios"])

        return ToolResult(
            content=content,
            images=all_images or None,
            videos=all_videos or None,
            audios=all_audios or None,
        )

    def _wait_for_job(self, job_id: str, eta: float) -> _JobWaitResult:
        """Poll one provider job and describe the actual completion outcome."""
        attempt_count = min(max(1, math.ceil(eta + self.add_to_eta)), math.ceil(self.max_wait_time))
        last_fetch_error: str | None = None
        for attempt in range(attempt_count):
            try:
                response = requests.post(
                    f"{self.fetch_url}/{job_id}",
                    json={"key": self.api_key},
                    headers={"Content-Type": "application/json"},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
                result = response.json()
                _check_provider_http_status(response, result)
                if (
                    response.status_code == 429
                    or response.status_code >= 500
                    or result.get("code")
                    in (
                        "rate_limited",
                        "upstream_unavailable",
                        "server_error",
                    )
                ):
                    last_fetch_error = str(
                        result.get("message")
                        or result.get("error")
                        or result.get("code")
                        or "Provider status unavailable",
                    )
                elif result.get("status") == "error" or "error" in result:
                    return _JobWaitResult(
                        content=f"Error: {result.get('message') or result.get('error') or 'Media generation failed'}",
                        rejected=True,
                    )
                elif result.get("status") == "success":
                    return _JobWaitResult(
                        content=f"{self.file_type.value.capitalize()} generated successfully",
                        output_urls=tuple(result.get("output") or result.get("future_links") or ()),
                    )
            except RequestException as exc:
                # A failed status check does not establish the remote job's outcome.
                last_fetch_error = str(exc)
            if attempt + 1 < attempt_count:
                time.sleep(1)
        content = "Waiting for media timed out. The provider job may still complete; queued media links are retained."
        if last_fetch_error:
            content += f" Last status-check error: {last_fetch_error}"
        return _JobWaitResult(content=content)
