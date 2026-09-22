"""Agno MoviePy caption rendering with explicit per-call styling."""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any, override

from agno.tools import moviepy_video as agno_moviepy
from moviepy import ColorClip, CompositeVideoClip, TextClip, VideoFileClip

# AGNO_COMPAT: MoviePyVideoTools drops caption styles and derives font size unconditionally.
# Reason: Agno's embed_captions accepts four style arguments but never forwards
# them; create_caption_clips has no explicit font-size parameter.
# Upstream issue: Tracking gap; no matching issue identified for caption style forwarding.
# Upstream PR: None identified. The two copied methods retain the pinned SDK's
# parsing, layout, media settings, temporary output publication, and cleanup.
# Remove when: The pinned SDK applies all four embed_captions style arguments to
# normal and highlighted clips, preserving layout and safe output publication.
# Coverage: tests/test_moviepy_video_tools.py::test_embed_captions_applies_styles_to_text_clips.


class MindRoomMoviePyVideoTools(agno_moviepy.MoviePyVideoTools):
    """Apply advertised caption styles without shared rendering state."""

    @override
    def create_caption_clips(
        self,
        text_json: dict[str, Any],
        frame_size: tuple[int, int],
        font: str = "Arial",
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
            font: Font family to use for captions
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
        line_width = 0

        frame_width, frame_height = frame_size
        x_buffer = frame_width * 0.1
        max_line_width = frame_width - (2 * x_buffer)
        fontsize = int(frame_height * 0.30) if font_size is None else font_size

        full_duration = text_json["end"] - text_json["start"]

        for word_data in text_json["textcontents"]:
            duration = word_data["end"] - word_data["start"]

            # Create base word clip using official TextClip parameters
            word_clip = (
                TextClip(
                    text=word_data["word"],
                    font=font,
                    font_size=int(fontsize),
                    color=color,
                    stroke_color=stroke_color,
                    stroke_width=int(stroke_width),
                    method="label",
                )
                .with_start(text_json["start"])
                .with_duration(full_duration)
            )

            # Create space clip
            space_clip = (
                TextClip(text=" ", font=font, font_size=int(fontsize), color=color, method="label")
                .with_start(text_json["start"])
                .with_duration(full_duration)
            )

            word_width, word_height = word_clip.size
            space_width = space_clip.size[0]

            # Handle line wrapping
            if line_width + word_width + space_width <= max_line_width:
                word_clip = word_clip.with_position((x_pos + x_buffer, y_pos))
                space_clip = space_clip.with_position((x_pos + word_width + x_buffer, y_pos))
                x_pos += word_width + space_width
                line_width += word_width + space_width
            else:
                x_pos = 0
                y_pos += word_height + 10
                line_width = word_width + space_width
                word_clip = word_clip.with_position((x_buffer, y_pos))
                space_clip = space_clip.with_position((word_width + x_buffer, y_pos))

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
                    stroke_width=int(stroke_width),
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
                # Increase background height to accommodate larger text
                bg_height = int(video.h * 0.15)
                bg_clip = ColorClip(
                    size=(video.w, bg_height),
                    color=(0, 0, 0),
                    duration=line["end"] - line["start"],
                ).with_opacity(0.6)

                # Position background even closer to bottom (90% instead of 85%)
                bg_position = ("center", int(video.h * 0.90))
                bg_clip = bg_clip.with_start(line["start"]).with_position(bg_position)

                # Create word clips
                word_clips = self.create_caption_clips(
                    line,
                    (video.w, bg_height),
                    color=font_color,
                    stroke_color=stroke_color,
                    stroke_width=stroke_width,
                    font_size=font_size,
                )

                # Combine background and words
                caption_composite = CompositeVideoClip([bg_clip, *word_clips], size=bg_clip.size).with_position(
                    bg_position,
                )

                all_caption_clips.append(caption_composite)

            # Combine video with all captions
            final_video = CompositeVideoClip([video, *all_caption_clips], size=video.size)

            # Write output with optimized settings
            temp_output_path = agno_moviepy._make_temp_output_path(output_path)
            final_video.write_videofile(
                temp_output_path,
                codec="libx264",
                audio_codec="aac",
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
