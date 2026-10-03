"""Agno MoviePy caption rendering with explicit per-call styling and media paths that follow ``file_access``."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import closing, suppress
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast, override

from agno.tools import moviepy_video as agno_moviepy
from moviepy import ColorClip, CompositeVideoClip, TextClip, VideoFileClip
from moviepy.config import FFMPEG_BINARY
from PIL import ImageFont

from mindroom.file_access import resolve_agent_file
from mindroom.tools.path_safety import write_agent_file

if TYPE_CHECKING:
    from typing import BinaryIO

    from mindroom.config.models import FileAccess

_STAGING_PREFIX = "mindroom-moviepy-"
# Worker code can write the workspace, including sparse files whose logical size far exceeds the disk they use.
# Parsing builds a few hundred bytes of objects per caption word, so captions get a much smaller cap.
_MAX_STAGED_VIDEO_BYTES = 1 << 30
_MAX_STAGED_CAPTION_BYTES = 1 << 20
# FFmpeg demuxers that read only the file they open; playlists and manifests such as HLS and DASH open other files and URLs.
_PLAIN_MEDIA_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts,mpeg,flv,asf,gif,ogg,wav,mp3,flac,aac"

# AGNO_COMPAT: MoviePyVideoTools drops caption styles and derives font size unconditionally.
# Reason: Agno's embed_captions accepts four style arguments but never forwards
# them; create_caption_clips has no explicit font-size parameter.
# Upstream issue: Tracking gap; no matching issue identified for caption style forwarding.
# Upstream PR: None identified. The two copied methods retain the pinned SDK's
# parsing and media settings; outputs render in private staging and publish through write_agent_file.
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

# AGNO_COMPAT: MoviePyVideoTools reads and writes model-chosen media paths by name.
# Reason: Agno 3.0.9 hands video, caption, and output paths to open(), os.replace, and FFmpeg
# in whichever process runs the toolkit, so a prompt could replace MindRoom's config.yaml with
# create_srt, read any file, or point FFmpeg at a URL, whatever the agent's file_access.
# FFmpeg also follows the paths and URLs inside an HLS playlist or DASH manifest, which it
# detects by content, so a staged input must be refused unless it is a plain media file.
# Upstream issue: Tracking gap; upstream tracking has not been verified.
# Upstream PR: None identified.
# Remove when: the toolkit accepts caller-supplied input readers and output writers;
# retain resolution under file_access, private FFmpeg staging capped against worker-written sparse files,
# the plain-media check, and streamed no-follow publication.
# Coverage: tests/test_moviepy_video_tools.py::test_media_paths_follow_file_access,
# tests/test_moviepy_video_tools.py::test_oversized_inputs_fail_before_staging_past_the_limit,
# tests/test_moviepy_video_tools.py::test_video_inputs_refuse_playlists_and_manifests,
# tests/test_moviepy_video_tools.py::test_outputs_publish_without_buffering_the_rendered_file, and
# tests/test_file_access_contract.py::test_outside_files_follow_file_access.


def _require_plain_media(staged: str) -> None:
    """Refuse a staged input unless FFmpeg opens it as a plain media file that references no other file or URL.

    FFmpeg picks the format from the private copy's fixed content and name, so MoviePy later opens the same format.
    """
    probe = subprocess.run(
        [
            FFMPEG_BINARY,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            _PLAIN_MEDIA_FORMATS,
            "-i",
            staged,
            "-t",
            "0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if probe.returncode != 0:
        msg = "Video input must be a supported plain media file; playlists and manifests are refused."
        raise ValueError(msg)


class MindRoomMoviePyVideoTools(agno_moviepy.MoviePyVideoTools):
    """Apply advertised caption styles without shared rendering state, with media paths that follow ``file_access``.

    Inputs are copied through no-follow descriptors into a private staging directory, video inputs must be
    plain media files rather than playlists or manifests, MoviePy and FFmpeg read and write only there,
    and outputs below the workspace are streamed into place by atomic replacement.
    """

    def __init__(
        self,
        enable_process_video: bool = True,
        enable_generate_captions: bool = True,
        enable_embed_captions: bool = True,
        all: bool = False,  # noqa: A002 - upstream option name
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
        **kwargs: Any,  # noqa: ANN401
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        super().__init__(
            enable_process_video=enable_process_video,
            enable_generate_captions=enable_generate_captions,
            enable_embed_captions=enable_embed_captions,
            all=all,
            **kwargs,
        )

    def _stage_input(self, raw_path: str, field_name: str, staging: Path, max_bytes: int) -> str:
        """Copy one authorized input into the staging directory, keeping its suffix for format detection.

        The copy fails before it writes more than ``max_bytes``.
        """
        authorized = resolve_agent_file(
            raw_path,
            workspace_root=self._workspace_root,
            file_access=self._file_access,
            field_name=field_name,
        )
        staged = staging / f"{field_name}{Path(authorized.name).suffix}"
        with authorized.open() as source, staged.open("xb") as target:
            while chunk := source.read(1 << 16):
                if target.tell() + len(chunk) > max_bytes:
                    msg = f"{field_name} '{raw_path}' exceeds the {max_bytes >> 20} MiB input limit."
                    raise ValueError(msg)
                target.write(chunk)
        return str(staged)

    def _stage_video(self, raw_path: str, staging: Path) -> str:
        """Stage one video input that FFmpeg reads as a plain media file, never as a playlist or manifest."""
        staged = self._stage_input(raw_path, "video_path", staging, _MAX_STAGED_VIDEO_BYTES)
        _require_plain_media(staged)
        return staged

    def _publish(self, raw_path: str, payload: bytes | BinaryIO) -> None:
        """Write one output where the agent's file_access allows; below the workspace by atomic no-follow replacement."""
        write_agent_file(raw_path, payload, workspace_root=self._workspace_root, file_access=self._file_access)

    @override
    def extract_audio(self, video_path: str, output_path: str) -> str:
        """Converts video to audio using MoviePy.

        Args:
            video_path: Path to the video file; with ``file_access: workspace`` it must be inside the agent workspace
            output_path: Path where the audio will be saved; with ``file_access: workspace`` it must be inside the agent workspace

        Returns:
            str: Path to the extracted audio file

        """
        try:
            with tempfile.TemporaryDirectory(prefix=_STAGING_PREFIX) as staging_dir:
                staging = Path(staging_dir)
                staged_output = staging / f"output{Path(output_path).suffix}"
                with closing(VideoFileClip(self._stage_video(video_path, staging))) as video:
                    if video.audio is None:
                        message = "Video has no audio track."
                        raise ValueError(message)  # noqa: TRY301 - preserve SDK error results and cleanup.
                    video.audio.write_audiofile(str(staged_output))
                with staged_output.open("rb") as rendered:
                    self._publish(output_path, rendered)
        except Exception as exc:
            agno_moviepy.logger.exception("Failed to extract audio")
            return f"Failed to extract audio: {exc}"
        return output_path

    @override
    def create_srt(self, transcription: str, output_path: str) -> str:
        """Save transcription text to SRT formatted file.

        Args:
            transcription: Text transcription in SRT format
            output_path: Path where the SRT file will be saved; with ``file_access: workspace`` it must be inside the agent workspace

        Returns:
            str: Path to the created SRT file, or error message if failed

        """
        try:
            self._publish(output_path, transcription.encode("utf-8"))
        except Exception as exc:
            agno_moviepy.logger.exception("Failed to create SRT file")
            return f"Failed to create SRT file: {exc}"
        return output_path

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
            video_path: Path to the input video file; with ``file_access: workspace`` it must be inside the agent workspace
            srt_path: Path to the SRT caption file; with ``file_access: workspace`` it must be inside the agent workspace
            output_path: Path for the output video (optional); with ``file_access: workspace`` it must be inside the agent workspace
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
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX))
        try:
            # If no output path provided, create one based on input video
            if output_path is None:
                output_path = video_path.rsplit(".", 1)[0] + "_captioned.mp4"

            # Load video
            video = VideoFileClip(self._stage_video(video_path, staging))

            # Read caption file and parse SRT
            srt_content = Path(self._stage_input(srt_path, "srt_path", staging, _MAX_STAGED_CAPTION_BYTES)).read_text(
                encoding="utf-8",
            )

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

            # Write output with optimized settings inside the staging directory, then publish it complete
            staged_output = staging / f"output{Path(output_path).suffix}"
            final_video.write_videofile(
                str(staged_output),
                codec="libx264",
                audio_codec="aac",
                temp_audiofile=str(staging / "audio.m4a") if final_video.audio is not None else None,
                fps=video.fps,
                preset="medium",
                threads=4,
                # Disable default progress bar
            )
            with staged_output.open("rb") as rendered:
                self._publish(output_path, rendered)

        except Exception as exc:
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
            shutil.rmtree(staging, ignore_errors=True)
