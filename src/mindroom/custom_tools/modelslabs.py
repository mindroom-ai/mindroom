"""Native ModelsLab media generation toolkit with provider job ID polling."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast
from uuid import uuid4

import requests
from requests.exceptions import RequestException

from mindroom.llm.media import Audio, Image, Video
from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit
from mindroom.tool_system.tool_function import ToolValue

logger = get_logger(__name__)

_REQUEST_TIMEOUT_SECONDS = 60


class ModelsLabFileType(StrEnum):
    """Supported ModelsLab output file formats."""

    MP4 = "mp4"
    MP3 = "mp3"
    GIF = "gif"
    WAV = "wav"
    PNG = "png"
    JPG = "jpg"


_MODELS_LAB_URLS: dict[ModelsLabFileType, str] = {
    ModelsLabFileType.MP4: "https://modelslab.com/api/v6/video/text2video",
    ModelsLabFileType.MP3: "https://modelslab.com/api/v6/voice/music_gen",
    ModelsLabFileType.GIF: "https://modelslab.com/api/v6/video/text2video",
    ModelsLabFileType.WAV: "https://modelslab.com/api/v6/voice/sfx",
    ModelsLabFileType.PNG: "https://modelslab.com/api/v6/images/text2img",
    ModelsLabFileType.JPG: "https://modelslab.com/api/v6/images/text2img",
}

_MODELS_LAB_FETCH_URLS: dict[ModelsLabFileType, str] = {
    ModelsLabFileType.MP4: "https://modelslab.com/api/v6/video/fetch",
    ModelsLabFileType.MP3: "https://modelslab.com/api/v6/voice/fetch",
    ModelsLabFileType.GIF: "https://modelslab.com/api/v6/video/fetch",
    ModelsLabFileType.WAV: "https://modelslab.com/api/v6/voice/fetch",
    ModelsLabFileType.PNG: "https://modelslab.com/api/v6/images/fetch",
    ModelsLabFileType.JPG: "https://modelslab.com/api/v6/images/fetch",
}

_RETRYABLE_ERROR_CODES = frozenset({"rate_limited", "upstream_unavailable", "server_error"})


@dataclass(frozen=True, slots=True)
class _JobWaitResult:
    """Carry completed URLs and distinguish terminal rejection from an unresolved wait."""

    content: str
    rejected: bool = False
    output_urls: tuple[str, ...] = ()


def _check_provider_http_status(response: requests.Response, result: object) -> None:
    """Preserve provider rejection bodies before raising for HTTP status."""
    if isinstance(result, dict):
        provider_result = cast("dict[str, object]", result)
        if provider_result.get("status") == "error" or "error" in provider_result:
            return
    response.raise_for_status()


def _finite_provider_eta(value: object) -> int | float | None:
    """Accept finite provider estimates, including numeric strings, without booleans."""
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _resolve_file_type(file_type: ModelsLabFileType | str | None) -> ModelsLabFileType:
    if isinstance(file_type, ModelsLabFileType):
        return file_type
    if isinstance(file_type, str):
        normalized = file_type.strip().lower()
        return ModelsLabFileType(normalized) if normalized else ModelsLabFileType.MP4
    return ModelsLabFileType.MP4


class ModelsLabTools(Toolkit):
    """Generate images, videos, audio, and GIFs via the ModelsLab API."""

    def __init__(
        self,
        api_key: str | None = None,
        wait_for_completion: bool = False,
        add_to_eta: float = 15,
        max_wait_time: float = 60,
        file_type: ModelsLabFileType | str | None = ModelsLabFileType.MP4,
        model_id: str | None = None,
        width: int = 512,
        height: int = 512,
    ) -> None:
        resolved_type = _resolve_file_type(file_type)
        self.file_type = resolved_type
        self.url = _MODELS_LAB_URLS[resolved_type]
        self.fetch_url = _MODELS_LAB_FETCH_URLS[resolved_type]
        self.wait_for_completion = wait_for_completion
        self.add_to_eta = add_to_eta
        self.max_wait_time = max_wait_time
        self.model_id = model_id
        self.width = width
        self.height = height
        self.api_key = api_key or os.getenv("MODELS_LAB_API_KEY")

        if not self.api_key:
            logger.error("MODELS_LAB_API_KEY not set")

        super().__init__(name="modelslabs", tools=[self.generate_media])

    def _create_payload(self, prompt: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": self.api_key,
            "prompt": prompt,
            "webhook": None,
            "track_id": None,
        }
        if self.file_type in {ModelsLabFileType.PNG, ModelsLabFileType.JPG}:
            payload |= {
                "model_id": self.model_id or "flux",
                "width": self.width,
                "height": self.height,
                "samples": 1,
                "num_inference_steps": 30,
                "safety_checker": "no",
            }
        elif self.file_type in {ModelsLabFileType.MP4, ModelsLabFileType.GIF}:
            payload |= {
                "height": self.height,
                "width": self.width,
                "num_frames": 25,
                "negative_prompt": "low quality",
                "model_id": self.model_id or "cogvideox",
                "instant_response": False,
                "output_type": self.file_type.value,
            }
        elif self.file_type is ModelsLabFileType.WAV:
            payload |= {
                "duration": 10,
                "output_format": "wav",
                "temp": False,
            }
        else:
            payload |= {
                "base64": False,
                "temp": False,
            }
        return payload

    def _build_artifacts(
        self,
        url_links: list[str] | tuple[str, ...],
        eta_text: str | None,
    ) -> tuple[tuple[Image, ...], tuple[Video, ...], tuple[Audio, ...]]:
        images: list[Image] = []
        videos: list[Video] = []
        audios: list[Audio] = []
        for media_url in url_links:
            media_id = str(uuid4())
            if self.file_type in {ModelsLabFileType.PNG, ModelsLabFileType.JPG, ModelsLabFileType.GIF}:
                images.append(Image(id=media_id, url=media_url))
            elif self.file_type is ModelsLabFileType.MP4:
                videos.append(Video(id=media_id, url=media_url, eta=eta_text))
            else:
                audios.append(Audio(id=media_id, url=media_url))
        return tuple(images), tuple(videos), tuple(audios)

    def _wait_for_job(self, job_id: str, eta: float) -> _JobWaitResult:
        """Poll one provider job and report completion, retry-after stop, timeout, or terminal rejection."""
        attempt_count = min(max(1, math.ceil(eta + self.add_to_eta)), math.ceil(self.max_wait_time))
        last_fetch_error: str | None = None
        for attempt in range(attempt_count):
            response: requests.Response | None = None
            retryable = False
            try:
                response = requests.post(
                    f"{self.fetch_url}/{job_id}",
                    json={"key": self.api_key},
                    headers={"Content-Type": "application/json"},
                    timeout=_REQUEST_TIMEOUT_SECONDS,
                )
                retryable = response.status_code == 429 or response.status_code >= 500
                result = response.json()
                _check_provider_http_status(response, result)
                retryable = retryable or result.get("code") in _RETRYABLE_ERROR_CODES
                if retryable:
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
                last_fetch_error = str(exc)

            if retryable and response is not None:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    return _JobWaitResult(
                        content=(
                            "Stopped waiting because the provider requested a retry delay. "
                            f"Retry-After: {retry_after}. "
                            "The provider job may still complete; queued media links are retained. "
                            f"Last status-check error: {last_fetch_error}"
                        ),
                    )
            if attempt + 1 < attempt_count:
                time.sleep(1)

        content = "Waiting for media timed out. The provider job may still complete; queued media links are retained."
        if last_fetch_error:
            content += f" Last status-check error: {last_fetch_error}"
        return _JobWaitResult(content=content)

    def generate_media(self, prompt: str) -> ToolValue:
        """Generate an image, video, audio clip, or GIF from a text prompt using ModelsLab.

        Args:
            prompt: Text description of the media to generate.

        """
        if not self.api_key:
            return ToolValue(content="Please set the MODELS_LAB_API_KEY")

        try:
            response = requests.post(
                self.url,
                data=json.dumps(self._create_payload(prompt)),
                headers={"Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
            result = response.json()
            _check_provider_http_status(response, result)

            if result.get("status") == "error":
                return ToolValue(
                    content=f"Error: {result.get('message') or result.get('error') or 'Media generation failed'}",
                )
            if "error" in result:
                return ToolValue(content=f"Error: {result['error']}")

            return self._resolve_generation_result(result)
        except RequestException as exc:
            logger.exception("Network error generating ModelsLab media", file_type=self.file_type.value)
            return ToolValue(content=f"Error: Network error while generating {self.file_type.value}: {exc}")
        except Exception as exc:
            logger.exception("Unexpected error generating ModelsLab media", file_type=self.file_type.value)
            return ToolValue(content=f"Error: Unexpected error while generating {self.file_type.value}: {exc}")

    def _resolve_generation_result(self, result: dict[str, Any]) -> ToolValue:
        if result.get("status") == "success" or self.file_type in {
            ModelsLabFileType.PNG,
            ModelsLabFileType.JPG,
            ModelsLabFileType.WAV,
        }:
            url_links: list[str] | tuple[str, ...] = result.get("output") or result.get("future_links", [])
        else:
            url_links = result.get("future_links") or result.get("output", [])

        if self.wait_for_completion:
            eta = _finite_provider_eta(result.get("eta"))
            content, url_links, rejected = self._resolve_waiting_outcome(result, eta, url_links)
            if rejected:
                return ToolValue(content=content)
            eta_text = str(eta)
        else:
            raw_eta = result.get("eta")
            content = self._immediate_message(raw_eta)
            eta_text = str(raw_eta)

        images, videos, audios = self._build_artifacts(url_links, eta_text)
        return ToolValue(content=content, images=images, videos=videos, audios=audios)

    def _resolve_waiting_outcome(
        self,
        result: dict[str, Any],
        eta: float | None,
        url_links: list[str] | tuple[str, ...],
    ) -> tuple[str, list[str] | tuple[str, ...], bool]:
        if result.get("status") == "success":
            return f"{self.file_type.value.capitalize()} generated successfully", url_links, False
        if eta is None:
            return (
                "Cannot wait for media: provider response has no finite numeric ETA. The job may still complete.",
                url_links,
                False,
            )
        if result.get("id") is None:
            return (
                "Cannot wait for media: provider response has no job ID. The job may still complete.",
                url_links,
                False,
            )
        wait_result = self._wait_for_job(str(result["id"]), eta)
        resolved_urls = wait_result.output_urls or url_links
        return wait_result.content, resolved_urls, wait_result.rejected

    def _immediate_message(self, raw_eta: object) -> str:
        if self.file_type in {ModelsLabFileType.PNG, ModelsLabFileType.JPG} and raw_eta is None:
            return f"Image generated successfully ({self.file_type.value.upper()})"
        if raw_eta is not None:
            return (
                f"{self.file_type.value.capitalize()} has been generated successfully "
                f"and will be ready in {raw_eta} seconds"
            )
        return f"{self.file_type.value.capitalize()} generated successfully"
