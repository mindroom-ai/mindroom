"""Agno MoviePy caption rendering with explicit per-call styling."""

from __future__ import annotations

from contextlib import suppress
from math import ceil
from pathlib import Path
from typing import Any, cast, override

from agno.tools import moviepy_video as agno_moviepy
from moviepy import ColorClip, CompositeVideoClip, TextClip, VideoFileClip
from PIL import ImageFont

# AGNO_COMPAT: MoviePyVideoTools drops caption styles and derives font size unconditionally.
# Reason: Agno's embed_captions accepts four style arguments but never forwards
# them; create_caption_clips has no explicit font-size parameter.
# Upstream issue: Tracking gap; no matching issue identified for caption style forwarding.
# Upstream PR: None identified. The two copied methods retain the pinned SDK's
# parsing, media settings, and temporary output publication.
# Remove when: The pinned SDK applies all four embed_captions style arguments to
# normal and highlighted clips, preserving layout and safe output publication.
# Coverage: tests/test_moviepy_video_tools.py::test_embed_captions_applies_styles_to_text_clips.

# AGNO_COMPAT: MoviePy caption geometry clips text and overlaps wrapped words.
# Reason: The SDK uses fixed video-height fractions for caption size/position
# and leaves the horizontal cursor at zero after placing a wrapped word.
# MoviePy drops glyph bbox origins and can undercount height on Pillow 11.
# Explicit canvas bounds and margins preserve bearings, accents, and outlines.
# Upstream issue: Tracking gap; caption-layout tracking has not been verified.
# Upstream PR: None identified.
# Remove when: The SDK sizes and positions captions from rendered clip bounds,
# advances wrapped words without overlap, and rejects text that cannot fit.
# MoviePy must also retain complete word rasters across supported Pillow versions.
# Coverage: tests/test_moviepy_caption_layout.py.

# AGNO_COMPAT: MoviePy captions assume an Arial font file is installed.
# Reason: MoviePy resolves explicit font files, while the shipped Linux image
# provides Liberation fonts. Pillow supplies a bundled font when font is None.
# Upstream issue: Tracking gap; font-default tracking has not been verified.
# Upstream PR: None identified.
# Remove when: The SDK uses a portable default while preserving explicit fonts.
# Coverage: tests/test_moviepy_caption_layout.py and
# tests/test_moviepy_video_tools.py::test_create_caption_clips_preserves_explicit_font.


# AGNO_COMPAT: MoviePy leaves temporary audio behind when caption encoding fails.
# Reason: write_videofile removes generated audio only after successful video encoding.
# Upstream issue: Tracking gap; temporary-audio cleanup tracking has not been verified.
# Upstream PR: None identified.
# Remove when: MoviePy cleans temporary audio on every encoding exit.
# Coverage: tests/test_moviepy_caption_output.py.


class MindRoomMoviePyVideoTools(agno_moviepy.MoviePyVideoTools):
    """Apply advertised caption styles without shared rendering state."""

    @override
    def create_caption_clips(
        self,
        text_json: dict[str, Any],
        frame_size: tuple[int, int],
        font: str | None = None,
        color: str = "white",
        highlight_color: str = "yellow",
        stroke_color: str = "black",
        stroke_width: float = 1.5,
        *,
        font_size: int | None = None,
    ) -> list[TextClip]:
        """Create word-level caption clips with highlighting effects.

        Args:
            text_json: Dictionary containing text and timing information
            frame_size: Tuple of (width, height) for the video frame
            font: Font file path, or None to use Pillow's bundled default font.
            color: Base text color
            highlight_color: Color for highlighted words
            stroke_color: Color for text outline
            stroke_width: Width of text outline
            font_size: Explicit pixel size, or derive it from frame height when omitted.

        Returns:
            List of MoviePy TextClip objects for each word and highlight.

        """
        word_clips = []
        x_pos = 0
        y_pos = 0
        line_height = 0

        frame_width, frame_height = frame_size
        x_buffer = frame_width * 0.1
        max_line_width = frame_width - (2 * x_buffer)
        fontsize = int(frame_height * 0.30) if font_size is None else font_size
        pil_font = cast(
            "ImageFont.FreeTypeFont",
            ImageFont.truetype(font, int(fontsize)) if font else ImageFont.load_default(int(fontsize)),
        )
        ascent, descent = pil_font.getmetrics()
        text_height = ascent + descent
        outline_width = int(stroke_width)
        word_bounds = [
            pil_font.getbbox(word["word"], anchor="ls", stroke_width=outline_width)
            for word in text_json["textcontents"]
        ]
        # One baseline for the batch, including glyphs beyond the font metrics.
        caption_top = min([-ascent - outline_width, *(bounds[1] for bounds in word_bounds)])
        caption_bottom = max([descent + outline_width, *(bounds[3] for bounds in word_bounds)])
        top_margin = -caption_top - ascent - outline_width
        word_canvas_height = ascent + outline_width + caption_bottom

        full_duration = text_json["end"] - text_json["start"]

        for word_data, (left, _top, right, _bottom) in zip(text_json["textcontents"], word_bounds, strict=True):
            duration = word_data["end"] - word_data["start"]
            # TextClip draws at (left margin + stroke, top margin + ascent + stroke).
            # Its size excludes margins, so include the bbox's right edge directly.
            word_size = (max(1, right + outline_width), word_canvas_height)
            word_margin = (max(0, -left - outline_width), top_margin, 0, 0)

            # Create base word clip using official TextClip parameters
            word_clip = (
                TextClip(
                    text=word_data["word"],
                    font=font,
                    font_size=int(fontsize),
                    color=color,
                    stroke_color=stroke_color,
                    stroke_width=outline_width,
                    size=word_size,
                    margin=word_margin,
                    horizontal_align="left",
                    vertical_align="top",
                    method="label",
                )
                .with_start(text_json["start"])
                .with_duration(full_duration)
            )

            # Create space clip
            space_clip = (
                TextClip(
                    text=" ",
                    font=font,
                    font_size=int(fontsize),
                    color=color,
                    size=(None, text_height),
                    horizontal_align="left",
                    vertical_align="top",
                    method="label",
                )
                .with_start(text_json["start"])
                .with_duration(full_duration)
            )

            word_width, word_height = word_clip.size
            space_width, space_height = space_clip.size
            if x_buffer + word_width > frame_width:
                message = "Caption word exceeds available width; use a smaller font_size."
                raise ValueError(message)

            # Clip bounds include font metrics, glyph overhangs, and stroke.
            # The cursor already includes the preceding space; trailing spaces
            # must not force an otherwise fitting word onto another row.
            if x_pos and x_pos + word_width > max_line_width:
                x_pos = 0
                y_pos += line_height
                line_height = 0
            word_clip = word_clip.with_position((x_buffer + x_pos, y_pos))
            space_clip = space_clip.with_position((x_buffer + x_pos + word_width, y_pos))
            x_pos += word_width + space_width
            line_height = max(line_height, word_height, space_height)

            word_clips.append(word_clip)
            word_clips.append(space_clip)

            # Create highlighted version
            highlight_clip = (
                TextClip(
                    text=word_data["word"],
                    font=font,
                    font_size=int(fontsize),
                    color=highlight_color,
                    stroke_color=stroke_color,
                    stroke_width=outline_width,
                    size=word_size,
                    margin=word_margin,
                    horizontal_align="left",
                    vertical_align="top",
                    method="label",
                )
                .with_start(word_data["start"])
                .with_duration(duration)
                .with_position(word_clip.pos)
            )

            word_clips.append(highlight_clip)

        return word_clips

    @override
    def embed_captions(
        self,
        video_path: str,
        srt_path: str,
        output_path: str | None = None,
        font_size: int = 24,
        font_color: str = "white",
        stroke_color: str = "black",
        stroke_width: int = 1,
    ) -> str:
        """Create a new video with embedded captions and word-level highlighting.

        Args:
            video_path: Path to the input video file
            srt_path: Path to the SRT caption file
            output_path: Path for the output video (optional)
            font_size: Size of caption text
            font_color: Color of caption text
            stroke_color: Color of text outline
            stroke_width: Width of text outline

        Returns:
            Path to the captioned video file, or error message if failed.

        """
        video = None
        final_video = None
        all_caption_clips = []
        temp_output_path: str | None = None
        temp_audio_path: str | None = None
        try:
            # If no output path provided, create one based on input video
            if output_path is None:
                output_path = video_path.rsplit(".", 1)[0] + "_captioned.mp4"

            # Load video
            video = VideoFileClip(video_path)

            # Read caption file and parse SRT
            srt_content = Path(srt_path).read_text(encoding="utf-8")

            # Parse SRT and get word timing
            words = self.parse_srt(srt_content)

            # Split into lines
            subtitle_lines = self.split_text_into_lines(words)

            # Create caption clips for each line
            for line in subtitle_lines:
                word_clips = self.create_caption_clips(
                    line,
                    (video.w, video.h),
                    color=font_color,
                    stroke_color=stroke_color,
                    stroke_width=stroke_width,
                    font_size=font_size,
                )
                bg_height = ceil(max(clip.pos(0)[1] + clip.h for clip in word_clips))
                if bg_height > video.h:
                    message = "Caption block exceeds video height; use a smaller font_size."
                    raise ValueError(message)  # noqa: TRY301 - preserve SDK error results and cleanup.

                # Children keep absolute video times and local canvas positions.
                # Only the outer composite is positioned against the video.
                bg_clip = (
                    ColorClip(
                        size=(video.w, bg_height),
                        color=(0, 0, 0),
                        duration=line["end"] - line["start"],
                    )
                    .with_opacity(0.6)
                    .with_start(line["start"])
                )
                caption_composite = CompositeVideoClip(
                    [bg_clip, *word_clips],
                    size=(video.w, bg_height),
                ).with_position(("center", "bottom"))

                all_caption_clips.append(caption_composite)

            # Combine video with all captions
            final_video = CompositeVideoClip([video, *all_caption_clips], size=video.size)

            # Write output with optimized settings
            temp_output_path = agno_moviepy._make_temp_output_path(output_path)
            if final_video.audio is not None:
                temp_audio_path = agno_moviepy._make_temp_output_path(str(Path(output_path).with_suffix(".m4a")))
            final_video.write_videofile(
                temp_output_path,
                codec="libx264",
                audio_codec="aac",
                temp_audiofile=temp_audio_path,
                fps=video.fps,
                preset="medium",
                threads=4,
                # Disable default progress bar
            )
            Path(temp_output_path).replace(output_path)
            temp_output_path = None

        except Exception as exc:
            agno_moviepy._remove_file_if_exists(temp_output_path)
            agno_moviepy.logger.exception("Failed to embed captions")
            return f"Failed to embed captions: {exc}"
        else:
            return output_path
        finally:
            for clip in all_caption_clips:
                with suppress(Exception):
                    clip.close()
            if final_video is not None:
                with suppress(Exception):
                    final_video.close()
            if video is not None:
                with suppress(Exception):
                    video.close()
            agno_moviepy._remove_file_if_exists(temp_audio_path)
