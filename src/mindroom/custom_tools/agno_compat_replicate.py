"""Replicate credential handoff repair for Agno's media toolkit."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import override

import replicate
from agno.agent import Agent  # noqa: TC002  # resolved by Agno function schema introspection
from agno.media import Image, Video
from agno.team.team import Team  # noqa: TC002  # resolved by Agno function schema introspection
from agno.tools.function import ToolResult
from agno.tools.replicate import ReplicateTools
from replicate.helpers import FileOutput

from mindroom.logging_config import get_logger

logger = get_logger(__name__)


# AGNO_COMPAT: ReplicateTools checks its key but calls an unrelated default SDK client.
# Reason: Agno 3.0.9 calls module-level replicate.run, so its resolved api_key
# never reaches SDK authentication. Keep media parsing while binding each call.
# Upstream issue: Tracking gap; no issue has been verified for this key handoff.
# Upstream PR: No fix or client-injection proposal has been verified.
# Remove when: Agno uses the toolkit key for its request client and owns its
# lifetime; preserve the configured-key guard, Agent/Team injection, and media results.
# Coverage: tests/test_replicate_tools.py.
class MindRoomReplicateTools(ReplicateTools):
    """Generate Replicate media using this toolkit's resolved credentials."""

    @override
    def generate_media(self, agent: Agent | Team, prompt: str) -> ToolResult:
        """Generate an image or video using the configured Replicate model.

        Args:
            agent: Calling agent or team, injected by Agno.
            prompt: Text describing the media to generate.

        """
        del agent
        if not self.api_key:
            logger.error("replicate_api_key_missing")
            return ToolResult(content="API key is not set.")

        try:
            client = replicate.Client(api_token=self.api_key)
            # Replicate 1.0.7 has no public close/context-manager API. Its private
            # HTTPX client owns the transport; keep it open through iterator parsing.
            with client._client:
                outputs = client.run(ref=self.model, input={"prompt": prompt})
                if isinstance(outputs, FileOutput):
                    outputs = [outputs]
                elif isinstance(outputs, (Iterable, Iterator)) and not isinstance(outputs, str):
                    outputs = list(outputs)
                else:
                    logger.error("replicate_unexpected_output_type", output_type=type(outputs).__name__)
                    return ToolResult(content=f"Unexpected output type: {type(outputs)}")

                images: list[Image] = []
                videos: list[Video] = []
                results: list[str] = []
                for output in outputs:
                    if not isinstance(output, FileOutput):
                        logger.error("replicate_unexpected_output_type", output_type=type(output).__name__)
                        return ToolResult(content=f"Unexpected output type: {type(output)}")

                    result_msg, media_artifact = self._parse_output(output)
                    results.append(result_msg)
                    if isinstance(media_artifact, Image):
                        images.append(media_artifact)
                    elif isinstance(media_artifact, Video):
                        videos.append(media_artifact)

                return ToolResult(
                    content="\n".join(results),
                    images=images or None,
                    videos=videos or None,
                )
        except Exception as exc:
            logger.exception("replicate_generation_failed")
            return ToolResult(content=f"Error: {exc}")
