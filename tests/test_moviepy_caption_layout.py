"""Caption geometry with real pixels and no video decoding or encoding."""

from __future__ import annotations

import struct
from io import BytesIO
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pytest
from moviepy import ColorClip, CompositeVideoClip
from moviepy.tools import compute_position
from PIL import Image, ImageDraw, ImageFont

from mindroom.custom_tools import agno_compat_moviepy as adapter

if TYPE_CHECKING:
    from moviepy import TextClip
    from numpy.typing import NDArray


def _assert_complete_word_mask(
    clip: TextClip,
    font_size: int,
    stroke_width: int,
    font_path: str | None = None,
) -> int:
    """Compare a generous independent Pillow raster and return the clip baseline."""
    font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default(font_size)
    reference = Image.new("RGBA", (1024, 1024))
    ImageDraw.Draw(reference).text(
        (256, 512),
        clip.text,
        font=font,
        fill="white",
        stroke_width=stroke_width,
        stroke_fill="black",
        anchor="ls",
    )
    expected = np.asarray(reference)[:, :, 3] / 255
    actual = clip.mask.get_frame(0)
    expected_rows, expected_columns = np.nonzero(expected)
    actual_rows, actual_columns = np.nonzero(actual)
    assert len(expected_rows)
    assert len(actual_rows)
    # Prove the reference is complete without sharing production bbox arithmetic.
    assert 0 < expected_rows.min() <= expected_rows.max() < reference.height - 1
    assert 0 < expected_columns.min() <= expected_columns.max() < reference.width - 1
    baseline = int(actual_rows.min() - expected_rows.min() + 512)
    expected = expected[
        expected_rows.min() : expected_rows.max() + 1,
        expected_columns.min() : expected_columns.max() + 1,
    ]
    actual = actual[
        actual_rows.min() : actual_rows.max() + 1,
        actual_columns.min() : actual_columns.max() + 1,
    ]
    np.testing.assert_array_equal(actual, expected)
    return baseline


def _font_checksum(data: bytes | bytearray) -> int:
    """Compute the checksum used by TrueType table records."""
    padded = data + b"\0" * (-len(data) % 4)
    return sum(struct.unpack(f">{len(padded) // 4}I", padded)) & 0xFFFFFFFF


def _caption_font_file(tmp_path: Path, *, overhang: bool) -> str:
    """Export Pillow's bundled font, optionally narrowing its declared metrics."""
    bundled = ImageFont.load_default(24)
    assert isinstance(bundled.path, BytesIO)
    font_data = bytearray(bundled.path.getvalue())
    if overhang:
        table_count = struct.unpack_from(">H", font_data, 4)[0]
        tables: dict[bytes, tuple[int, int, int]] = {}
        for record in range(12, 12 + 16 * table_count, 16):
            tag, _checksum, offset, length = struct.unpack_from(">4sIII", font_data, record)
            tables[tag] = (record, offset, length)

        # Keep real glyph outlines but force ascender, descender, and right overhangs.
        hhea_offset = tables[b"hhea"][1]
        struct.pack_into(">hh", font_data, hhea_offset + 4, 350, -100)
        hmtx_offset = tables[b"hmtx"][1]
        metric_count = struct.unpack_from(">H", font_data, hhea_offset + 34)[0]
        for index in range(metric_count):
            struct.pack_into(">H", font_data, hmtx_offset + 4 * index, 100)

        # Preserve a valid font, including table and whole-file checksums.
        head_offset = tables[b"head"][1]
        struct.pack_into(">I", font_data, head_offset + 8, 0)
        for record, offset, length in tables.values():
            checksum = _font_checksum(font_data[offset : offset + length])
            struct.pack_into(">I", font_data, record + 4, checksum)
        adjustment = (0xB1B0AFBA - _font_checksum(font_data)) & 0xFFFFFFFF
        struct.pack_into(">I", font_data, head_offset + 8, adjustment)

    font_path = tmp_path / ("caption-overhang.ttf" if overhang else "caption.ttf")
    font_path.write_bytes(font_data)
    return str(font_path)


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
        ((320, 180), 72, 0, "jump"),
        ((320, 180), 72, 4, "jump"),
    ],
    ids=[
        "small-video",
        "large-font",
        "wide-word",
        "no-outline",
        "thick-outline",
        "wrapped-caption",
        "negative-bearing",
        "negative-bearing-outline",
    ],
)
def test_embed_captions_preserves_glyph_pixels_and_background(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    video_size: tuple[int, int],
    font_size: int,
    stroke_width: int,
    text: str,
) -> None:
    """Default-font glyphs fit both canvases and the timed background darkens video."""
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
            _assert_complete_word_mask(clip, font_size, stroke_width)
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


@pytest.mark.parametrize("font_source", ["default", "file", "overhang-file"])
@pytest.mark.parametrize("font_size", [24, 72])
@pytest.mark.parametrize("stroke_width", [0, 1, 4])
def test_word_rasters_preserve_complete_bounds_and_shared_baseline(
    tmp_path: Path,
    font_source: str,
    font_size: int,
    stroke_width: int,
) -> None:
    """Real default and explicit fonts retain all pixels, bearings, and baselines."""
    font_path = None
    if font_source != "default":
        font_path = _caption_font_file(tmp_path, overhang=font_source == "overhang-file")
    font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default(font_size)
    words = ["jump", "Agyp", "Ágyp", "j"]
    if font_source == "overhang-file":
        ascent, descent = font.getmetrics()
        bounds = [font.getbbox(word, anchor="ls") for word in words]
        assert min(top for _left, top, _right, _bottom in bounds) < -ascent
        assert max(bottom for _left, _top, _right, bottom in bounds) > descent
        assert font.getbbox("j", anchor="ls")[2] > font.getlength("j")
    if font_size == 72:
        assert font.getbbox("jump", anchor="ls")[0] < 0
    text_json = {
        "start": 0,
        "end": 2,
        "textcontents": [
            {"word": word, "start": index * 0.5, "end": (index + 1) * 0.5} for index, word in enumerate(words)
        ],
    }
    clips = adapter.MindRoomMoviePyVideoTools().create_caption_clips(
        text_json,
        (1280, 720),
        font=font_path,
        font_size=font_size,
        stroke_width=stroke_width,
    )
    try:
        baselines = []
        for base, highlight in zip(clips[::3], clips[2::3], strict=True):
            baselines.append(_assert_complete_word_mask(base, font_size, stroke_width, font_path))
            assert _assert_complete_word_mask(highlight, font_size, stroke_width, font_path) == baselines[-1]
            assert highlight.pos(0) == base.pos(0)
            assert highlight.size == base.size
        assert len(set(baselines)) == 1
        for left, top, right, bottom in (_ink_bounds(clip) for clip in clips[::3]):
            assert 0 <= left < right <= 1280
            assert 0 <= top < bottom <= 720
    finally:
        for clip in clips:
            clip.close()


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
