"""Shared media-input container passed across bot, teams, and AI layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.media import Audio, File, Image, Video


@dataclass(frozen=True)
class MediaInputs:
    """Optional multimodal inputs for a single model run."""

    audio: Sequence[Audio] = ()
    images: Sequence[Image] = ()
    files: Sequence[File] = ()
    videos: Sequence[Video] = ()

    @classmethod
    def from_optional(
        cls,
        *,
        audio: Sequence[Audio] | None = None,
        images: Sequence[Image] | None = None,
        files: Sequence[File] | None = None,
        videos: Sequence[Video] | None = None,
    ) -> MediaInputs:
        """Create a normalized media container from optional collections."""
        return cls(
            audio=tuple(audio or ()),
            images=tuple(images or ()),
            files=tuple(files or ()),
            videos=tuple(videos or ()),
        )

    def has_any(self) -> bool:
        """Return whether any media collection contains items."""
        return bool(self.audio or self.images or self.files or self.videos)
