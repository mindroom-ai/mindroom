"""Native MoviePy video processing toolkit with confined media paths and safe caption rendering."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import closing, suppress
from functools import partial
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

try:
    from moviepy import ColorClip, CompositeVideoClip, TextClip, VideoClip, VideoFileClip
    from moviepy.config import FFMPEG_BINARY
except ImportError as exc:
    msg = "`moviepy` not installed. Install with: `pip install moviepy`"
    raise ImportError(msg) from exc

from PIL import ImageFont

from mindroom.file_access import resolve_agent_file, write_agent_file
from mindroom.logging_config import get_logger
from mindroom.tool_system.authoring import Toolkit

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO

    from mindroom.config.models import FileAccess

logger = get_logger(__name__)

_STAGING_PREFIX = "mindroom-moviepy-"
# Worker code can write sparse files whose logical size far exceeds disk usage.
# Caption parsing builds hundreds of bytes per word, so captions use a smaller cap;
# rendering keeps only one active caption line rasterized at a time.
_MAX_STAGED_VIDEO_BYTES = 1 << 30
_MAX_STAGED_CAPTION_BYTES = 1 << 20
# FFmpeg demuxers that read only the opened file; playlists/manifests (HLS, DASH) follow external paths/URLs.
_PLAIN_MEDIA_FORMATS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts,mpeg,flv,asf,gif,ogg,wav,mp3,flac,aac"

_MAX_LINE_CHARS = 30
_MAX_LINE_DURATION = 2.5
_MAX_WORD_GAP = 1.5


def _caption_layers(
    subtitle_lines: list[dict[str, Any]],
    render_line: Callable[[dict[str, Any]], CompositeVideoClip],
    active_line: dict[int, CompositeVideoClip],
) -> list[VideoClip]:
    """Return one bottom-centered clip per subtitle line backed by a single-entry render cache.

    ``active_line`` holds at most one rendered line so memory stays bounded regardless of caption length.
    Each line renders once here to size its clip and validate fit before encoding begins.
    """

    def canvas(index: int) -> CompositeVideoClip:
        if index not in active_line:
            for previous in active_line.values():
                previous.close()
            active_line.clear()
            active_line[index] = render_line(subtitle_lines[index])
        return active_line[index]

    layers = []
    for index, line in enumerate(subtitle_lines):
        start, duration = line["start"], line["end"] - line["start"]
        layer = VideoClip(lambda t, index=index, start=start: canvas(index).get_frame(start + t), duration=duration)
        mask = VideoClip(
            lambda t, index=index, start=start: cast("VideoClip", canvas(index).mask).get_frame(start + t),
            is_mask=True,
            duration=duration,
        )
        layers.append(layer.with_mask(mask).with_start(start).with_position(("center", "bottom")))
    return layers


def _require_plain_media(staged: str) -> None:
    """Refuse a staged input unless FFmpeg opens it as a plain media file referencing no external paths or URLs."""
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


def _srt_timestamp_to_seconds(timestamp: str) -> float:
    hours, minutes, seconds = timestamp.strip().replace(",", ".").split(":")
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)


class MoviePyVideoTools(Toolkit):
    """Process videos, extract audio, write SRT files, and embed word-highlighted captions."""

    def __init__(
        self,
        enable_process_video: bool = True,
        enable_generate_captions: bool = True,
        enable_embed_captions: bool = True,
        all: bool = False,  # noqa: A002
        *,
        tool_output_workspace_root: Path | None = None,
        file_access: FileAccess = "workspace",
    ) -> None:
        self._workspace_root = tool_output_workspace_root
        self._file_access = file_access
        tools: list[Callable[..., Any]] = []
        if enable_process_video or all:
            tools.append(self.extract_audio)
        if enable_generate_captions or all:
            tools.append(self.create_srt)
        if enable_embed_captions or all:
            tools.append(self.embed_captions)
        super().__init__(name="moviepy_video_tools", tools=tools)

    def _stage_input(self, raw_path: str, field_name: str, staging: Path, max_bytes: int) -> str:
        """Copy one authorized input into the staging directory without exceeding ``max_bytes``."""
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
        """Stage one plain media video input, refusing playlists and manifests."""
        staged = self._stage_input(raw_path, "video_path", staging, _MAX_STAGED_VIDEO_BYTES)
        _require_plain_media(staged)
        return staged

    def _publish(self, raw_path: str, payload: bytes | BinaryIO) -> None:
        """Write one output according to ``file_access``, atomically replacing workspace targets."""
        write_agent_file(raw_path, payload, workspace_root=self._workspace_root, file_access=self._file_access)

    @staticmethod
    def parse_srt(srt_content: str) -> list[dict[str, Any]]:
        """Convert SRT subtitle text into evenly distributed word-level timing entries."""
        words: list[dict[str, Any]] = []
        for block in srt_content.strip().split("\n\n"):
            if not block.strip():
                continue
            parts = block.split("\n")
            if len(parts) < 3:
                continue
            start_time, end_time = parts[1].split(" --> ")
            start = _srt_timestamp_to_seconds(start_time)
            end = _srt_timestamp_to_seconds(end_time)
            text_words = " ".join(parts[2:]).split()
            if not text_words:
                continue
            time_per_word = (end - start) / len(text_words)
            for idx, word in enumerate(text_words):
                word_start = start + idx * time_per_word
                words.append({"word": word, "start": word_start, "end": word_start + time_per_word})
        return words

    @staticmethod
    def split_text_into_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group word timing entries into caption lines bounded by length, duration, and pause gap."""
        subtitles: list[dict[str, Any]] = []
        line: list[dict[str, Any]] = []
        line_duration = 0.0

        for idx, word_data in enumerate(words):
            line.append(word_data)
            line_duration += word_data["end"] - word_data["start"]
            joined = " ".join(item["word"] for item in line)
            duration_exceeded = line_duration > _MAX_LINE_DURATION
            chars_exceeded = len(joined) > _MAX_LINE_CHARS
            maxgap_exceeded = idx > 0 and word_data["start"] - words[idx - 1]["end"] > _MAX_WORD_GAP
            if duration_exceeded or chars_exceeded or maxgap_exceeded:
                subtitles.append(
                    {
                        "word": joined,
                        "start": line[0]["start"],
                        "end": line[-1]["end"],
                        "textcontents": line,
                    },
                )
                line = []
                line_duration = 0.0

        if line:
            subtitles.append(
                {
                    "word": " ".join(item["word"] for item in line),
                    "start": line[0]["start"],
                    "end": line[-1]["end"],
                    "textcontents": line,
                },
            )
        return subtitles

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
        """Build positioned base, space, and highlighted word TextClips on a shared baseline."""
        word_clips: list[TextClip] = []
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
        # Share one baseline across the line, accommodating glyph overhangs and stroke.
        caption_top = min([-ascent - outline_width, *(bounds[1] for bounds in word_bounds)])
        caption_bottom = max([descent + outline_width, *(bounds[3] for bounds in word_bounds)])
        top_margin = -caption_top - ascent - outline_width
        word_canvas_height = ascent + outline_width + caption_bottom
        full_duration = text_json["end"] - text_json["start"]

        for word_data, (left, _top, right, _bottom) in zip(text_json["textcontents"], word_bounds, strict=True):
            duration = word_data["end"] - word_data["start"]
            word_size = (max(1, right + outline_width), word_canvas_height)
            word_margin = (max(0, -left - outline_width), top_margin, 0, 0)

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
                msg = "Caption word exceeds available width; use a smaller font_size."
                raise ValueError(msg)

            if x_pos and x_pos + word_width > max_line_width:
                x_pos = 0
                y_pos += line_height
                line_height = 0
            word_clip = word_clip.with_position((x_buffer + x_pos, y_pos))
            space_clip = space_clip.with_position((x_buffer + x_pos + word_width, y_pos))
            x_pos += word_width + space_width
            line_height = max(line_height, word_height, space_height)

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
            word_clips.extend((word_clip, space_clip, highlight_clip))

        return word_clips

    def _render_caption_line(
        self,
        line: dict[str, Any],
        frame_size: tuple[int, int],
        *,
        font_size: int,
        font_color: str,
        stroke_color: str,
        stroke_width: int,
    ) -> CompositeVideoClip:
        """Render one subtitle line over its translucent full-width background bar."""
        word_clips = self.create_caption_clips(
            line,
            frame_size,
            color=font_color,
            stroke_color=stroke_color,
            stroke_width=stroke_width,
            font_size=font_size,
        )
        width, height = frame_size
        bg_height = ceil(max(clip.pos(0)[1] + clip.h for clip in word_clips))
        if bg_height > height:
            msg = "Caption block exceeds video height; use a smaller font_size."
            raise ValueError(msg)
        bg_clip = (
            ColorClip(
                size=(width, bg_height),
                color=(0, 0, 0),
                duration=line["end"] - line["start"],
            )
            .with_opacity(0.6)
            .with_start(line["start"])
        )
        return CompositeVideoClip([bg_clip, *word_clips], size=(width, bg_height))

    def extract_audio(self, video_path: str, output_path: str) -> str:
        """Extract the audio track from a video file.

        Args:
            video_path: Input video path (confined to workspace unless `file_access: unrestricted`).
            output_path: Destination audio path (e.g. `.wav` or `.mp3`).

        """
        try:
            with tempfile.TemporaryDirectory(prefix=_STAGING_PREFIX) as staging_dir:
                staging = Path(staging_dir)
                staged_output = staging / f"output{Path(output_path).suffix}"
                with closing(VideoFileClip(self._stage_video(video_path, staging))) as video:
                    if video.audio is None:
                        msg = "Video has no audio track."
                        raise ValueError(msg)  # noqa: TRY301
                    video.audio.write_audiofile(str(staged_output))
                with staged_output.open("rb") as rendered:
                    self._publish(output_path, rendered)
        except Exception as exc:
            logger.exception("Failed to extract audio")
            return f"Failed to extract audio: {exc}"
        return output_path

    def create_srt(self, transcription: str, output_path: str) -> str:
        """Write SRT-formatted subtitle text to a file.

        Args:
            transcription: SRT subtitle text.
            output_path: Destination `.srt` path.

        """
        try:
            self._publish(output_path, transcription.encode("utf-8"))
        except Exception as exc:
            logger.exception("Failed to create SRT file")
            return f"Failed to create SRT file: {exc}"
        return output_path

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
        """Burn word-highlighted SRT captions onto a video.

        Args:
            video_path: Input video path.
            srt_path: Input `.srt` subtitle path.
            output_path: Optional output video path (defaults to `<video_stem>_captioned.mp4` beside input).
            font_size: Caption font size in pixels.
            font_color: Base text color.
            stroke_color: Outline color.
            stroke_width: Outline width in pixels.

        """
        video = None
        final_video = None
        active_line: dict[int, CompositeVideoClip] = {}
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX))
        try:
            if output_path is None:
                source = Path(video_path)
                output_path = str(source.with_name(f"{source.stem}_captioned.mp4"))

            video = VideoFileClip(self._stage_video(video_path, staging))
            srt_content = Path(self._stage_input(srt_path, "srt_path", staging, _MAX_STAGED_CAPTION_BYTES)).read_text(
                encoding="utf-8",
            )
            words = self.parse_srt(srt_content)
            subtitle_lines = self.split_text_into_lines(words)
            render_line = partial(
                self._render_caption_line,
                frame_size=(video.w, video.h),
                font_size=font_size,
                font_color=font_color,
                stroke_color=stroke_color,
                stroke_width=stroke_width,
            )
            final_video = CompositeVideoClip(
                [video, *_caption_layers(subtitle_lines, render_line, active_line)],
                size=video.size,
            )
            staged_output = staging / f"output{Path(output_path).suffix}"
            final_video.write_videofile(
                str(staged_output),
                codec="libx264",
                audio_codec="aac",
                temp_audiofile=str(staging / "audio.m4a") if final_video.audio is not None else None,
                fps=video.fps,
                preset="medium",
                threads=4,
            )
            with staged_output.open("rb") as rendered:
                self._publish(output_path, rendered)
        except Exception as exc:
            logger.exception("Failed to embed captions")
            return f"Failed to embed captions: {exc}"
        else:
            return output_path
        finally:
            for clip in active_line.values():
                with suppress(Exception):
                    clip.close()
            if final_video is not None:
                with suppress(Exception):
                    final_video.close()
            if video is not None:
                with suppress(Exception):
                    video.close()
            shutil.rmtree(staging, ignore_errors=True)
