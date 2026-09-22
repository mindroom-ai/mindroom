"""Caption geometry with real pixels and no video decoding or encoding."""

from __future__ import annotations

from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest
from moviepy import ColorClip, CompositeVideoClip, TextClip
from moviepy.tools import compute_position

from mindroom.custom_tools import agno_compat_moviepy as adapter

if TYPE_CHECKING:
    from numpy.typing import NDArray


@pytest.fixture
def bundled_font(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real text rendering while using Pillow's bundled scalable font."""

    def text_clip(**kwargs: object) -> TextClip:
        kwargs["font"] = None
        return TextClip(**kwargs)

    monkeypatch.setattr(adapter, "TextClip", text_clip)


def _ink_bounds(clip: TextClip) -> tuple[int, int, int, int]:
    """Return positioned nontransparent glyph bounds, excluding transparent padding."""
    rows, columns = np.nonzero(clip.mask.get_frame(0))
    assert len(rows)
    x, y = clip.pos(0)
    return (
        int(x) + int(columns.min()),
        int(y) + int(rows.min()),
        int(x) + int(columns.max()) + 1,
        int(y) + int(rows.max()) + 1,
    )


@pytest.mark.parametrize(
    ("video_size", "font_size", "stroke_width", "text"),
    [
        ((320, 180), 24, 1, "Agyp"),
        ((1280, 720), 72, 1, "Agyp"),
        ((200, 180), 72, 1, "Agyp"),
        ((320, 180), 24, 0, "Ágyp"),
        ((320, 180), 24, 4, "Ágyp"),
        ((320, 180), 42, 1, "Hello wide words test"),
    ],
    ids=["small-video", "large-font", "wide-word", "no-outline", "thick-outline", "wrapped-caption"],
)
@pytest.mark.usefixtures("bundled_font")
def test_embed_captions_preserves_glyph_pixels_and_background(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    video_size: tuple[int, int],
    font_size: int,
    stroke_width: int,
    text: str,
) -> None:
    """Every glyph fits both canvases and the timed background darkens video."""
    video = ColorClip(video_size, color=(255, 255, 255), duration=3).with_fps(30)
    monkeypatch.setattr(adapter, "VideoFileClip", lambda _path: video)
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text(f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n", encoding="utf-8")
    output = tmp_path / "captioned.mp4"
    rendered_frames: list[NDArray[np.uint8]] = []

    def inspect_frame(final: CompositeVideoClip, path: str, **_kwargs: object) -> None:
        assert len(final.clips) == 2
        caption = final.clips[1]
        origin_x, origin_y = compute_position(caption.size, final.size, caption.pos(0))
        assert caption.h <= video_size[1]
        for clip in caption.clips[1:]:
            rows, columns = np.nonzero(clip.mask.get_frame(0))
            if not len(rows):  # Spaces have no visible pixels.
                continue
            x, y = compute_position(clip.size, caption.size, clip.pos(0))
            assert np.all((columns + x >= 0) & (columns + x < caption.w))
            assert np.all((rows + y >= 0) & (rows + y < caption.h))
            assert np.all((columns + x + origin_x >= 0) & (columns + x + origin_x < video_size[0]))
            assert np.all((rows + y + origin_y >= 0) & (rows + y + origin_y < video_size[1]))

        frame = final.get_frame(1.5)
        assert frame.shape == (video_size[1], video_size[0], 3)
        # Outside the text inset, the 60%-opaque black background covers white.
        assert frame[-1, 0].tolist() == pytest.approx([102, 102, 102], abs=1)
        # An active yellow word must still appear in the actual composed frame.
        assert np.any((frame[:, :, 0] > frame[:, :, 2]) & (frame[:, :, 1] > frame[:, :, 2]))
        for time in (0.5, 2.5):
            np.testing.assert_array_equal(final.get_frame(time), video.get_frame(time))
        rendered_frames.append(frame)
        Path(path).write_bytes(b"rendered frame")

    monkeypatch.setattr(CompositeVideoClip, "write_videofile", inspect_frame)

    result = adapter.MindRoomMoviePyVideoTools().embed_captions(
        "input.mp4",
        str(srt_path),
        str(output),
        font_size=font_size,
        stroke_width=stroke_width,
    )

    assert result == str(output)
    assert output.read_bytes() == b"rendered frame"
    assert len(rendered_frames) == 1


@pytest.mark.usefixtures("bundled_font")
def test_wrapped_words_advance_without_overlapping_glyphs() -> None:
    """The word after a wrap starts beyond the first word on its new row."""
    words = ["Hello", "wide", "words", "test"]
    text_json = {
        "start": 0,
        "end": 2,
        "textcontents": [
            {"word": word, "start": index * 0.5, "end": (index + 1) * 0.5} for index, word in enumerate(words)
        ],
    }
    clips = adapter.MindRoomMoviePyVideoTools().create_caption_clips(text_json, (320, 140), font_size=42)
    try:
        base_words = clips[::3]
        assert len(base_words) == 4
        for base, highlight in zip(base_words, clips[2::3], strict=True):
            assert highlight.pos(0) == base.pos(0)
            assert highlight.size == base.size
            np.testing.assert_array_equal(highlight.mask.get_frame(0), base.mask.get_frame(0))
        wrapped_x, wrapped_y = base_words[2].pos(0)
        following_x, following_y = base_words[3].pos(0)
        assert wrapped_y > base_words[1].pos(0)[1]
        assert following_y == wrapped_y
        assert following_x >= wrapped_x + base_words[2].w
        bounds = [_ink_bounds(clip) for clip in base_words]
        for left, top, right, bottom in bounds:
            assert 0 <= left < right <= 320
            assert 0 <= top < bottom <= 140
        for first, second in combinations(bounds, 2):
            left, top, right, bottom = first
            other_left, other_top, other_right, other_bottom = second
            assert max(left, other_left) >= min(right, other_right) or max(top, other_top) >= min(bottom, other_bottom)
    finally:
        for clip in clips:
            clip.close()


@pytest.mark.parametrize(
    ("text", "dimension"),
    [("AgypAgyp", "width"), ("Agyp Agyp Agyp", "height")],
)
@pytest.mark.usefixtures("bundled_font")
def test_oversized_captions_preserve_existing_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text: str,
    dimension: str,
) -> None:
    """An impossible requested size fails before rendering or replacing output."""
    video = ColorClip((320, 180), color=(255, 255, 255), duration=3).with_fps(30)
    monkeypatch.setattr(adapter, "VideoFileClip", lambda _path: video)
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text(f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n", encoding="utf-8")
    output = tmp_path / "captioned.mp4"
    output.write_bytes(b"existing video")

    def unexpected_render(_final: CompositeVideoClip, _path: str, **_kwargs: object) -> None:
        pytest.fail("Invalid caption geometry reached video encoding")

    monkeypatch.setattr(CompositeVideoClip, "write_videofile", unexpected_render)

    result = adapter.MindRoomMoviePyVideoTools().embed_captions(
        "input.mp4",
        str(srt_path),
        str(output),
        font_size=72,
    )

    assert result.startswith("Failed to embed captions:")
    assert dimension in result
    assert output.read_bytes() == b"existing video"
    assert set(tmp_path.iterdir()) == {srt_path, output}
