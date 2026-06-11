"""Shared media-input container passed across bot, teams, and AI layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from agno.media import Audio, File, Image, Video

type MediaKind = Literal["audio", "image", "file", "video"]


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

    def kinds(self) -> frozenset[MediaKind]:
        """Return the media kinds with at least one item."""
        kinds: set[MediaKind] = set()
        if self.audio:
            kinds.add("audio")
        if self.images:
            kinds.add("image")
        if self.files:
            kinds.add("file")
        if self.videos:
            kinds.add("video")
        return frozenset(kinds)

    def merge(self, other: MediaInputs) -> MediaInputs:
        """Concatenate two media-input sets preserving order."""
        if not self.has_any():
            return other
        if not other.has_any():
            return self
        return MediaInputs(
            audio=(*self.audio, *other.audio),
            images=(*self.images, *other.images),
            files=(*self.files, *other.files),
            videos=(*self.videos, *other.videos),
        )
