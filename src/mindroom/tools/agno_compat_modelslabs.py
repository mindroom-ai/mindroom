"""Preserve provider job identity while waiting for ModelsLab media."""

from __future__ import annotations

import json
import time
from uuid import uuid4

import requests
from agno.models.response import FileType
from agno.tools.function import ToolResult
from agno.tools.models_labs import ModelsLabTools
from requests.exceptions import RequestException

_REQUEST_TIMEOUT_SECONDS = 60


# AGNO_COMPAT: ModelsLab polling loses provider identity and reports unsuccessful waits as success.
# Reason: Agno 3.0.9 sends the artifact UUID to fetch and discards the wait outcome.
# Upstream issue: Tracking gap; no matching issue found on September 22, 2026.
# Upstream PR: No matching fix identified.
# Remove when: Agno polls the provider ID and distinguishes completion, timeout,
# and provider rejection; retain bounded transport waits, authored file-type normalization, and media helpers.
# Coverage: tests/test_modelslabs_tool.py::test_queued_media_fetches_provider_job_id,
# ::test_queued_media_wait_timeout_does_not_claim_generation_success,
# ::test_queued_media_reports_provider_failure,
# ::test_transport_timeout_returns_an_honest_outcome, and nonwaiting/error controls.
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
        response.raise_for_status()
        result = response.json()
        if result.get("status") == "error":
            return ToolResult(content=f"Error: {result.get('message')}")
        if "error" in result:
            return ToolResult(content=f"Error: {result['error']}")

        eta = result.get("eta")
        if self.file_type in (FileType.PNG, FileType.JPG, FileType.WAV):
            url_links = result.get("output") or result.get("future_links", [])
        else:
            url_links = result.get("future_links") or result.get("output", [])
        media_id = str(uuid4())
        all_images = []
        all_videos = []
        all_audios = []
        for media_url in url_links:
            artifacts = self._create_media_artifacts(media_id, media_url, str(eta))
            all_images.extend(artifacts["images"])
            all_videos.extend(artifacts["videos"])
            all_audios.extend(artifacts["audios"])

        content = f"{self.file_type.value.capitalize()} generated successfully"
        if result.get("status") != "success":
            if isinstance(eta, int):
                job_id = result.get("id")
                if job_id is None:
                    content = "Cannot wait for media: provider response has no job ID. The job may still complete."
                else:
                    content = self._wait_for_job(str(job_id), eta)
            else:
                content = "Cannot wait for media: provider response has no integer ETA. The job may still complete."
        return ToolResult(
            content=content,
            images=all_images or None,
            videos=all_videos or None,
            audios=all_audios or None,
        )

    def _wait_for_job(self, job_id: str, eta: int) -> str:
        """Poll one provider job and describe the actual completion outcome."""
        time_to_wait = min(eta + self.add_to_eta, self.max_wait_time)
        for _ in range(time_to_wait):
            try:
                response = requests.post(
                    f"{self.fetch_url}/{job_id}",
                    json={"key": self.api_key},
                    headers={"Content-Type": "application/json"},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                result = response.json()
            except RequestException:
                # A failed status check does not establish the remote job's outcome.
                time.sleep(1)
                continue
            if result.get("status") == "success":
                return f"{self.file_type.value.capitalize()} generated successfully"
            if result.get("status") == "error":
                return f"Error: {result.get('message') or result.get('error') or 'Media generation failed'}"
            if "error" in result:
                return f"Error: {result['error']}"
            time.sleep(1)
        return "Waiting for media timed out. The provider job may still complete; queued media links are retained."
