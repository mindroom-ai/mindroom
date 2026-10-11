"""Caption styling and media paths with fake video rendering, plus FFmpeg's real format detection."""

from __future__ import annotations

import os
import shutil
import subprocess
import tracemalloc
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from moviepy.config import FFMPEG_BINARY
from PIL import ImageFont

from mindroom.tools.moviepy_video_tools import moviepy_video_tools

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.custom_tools.agno_compat_moviepy import MindRoomMoviePyVideoTools


def _clip(**kwargs: object) -> MagicMock:
    clip = MagicMock()
    width, height = kwargs.get("size", (20, 12))
    clip.size = (20 if width is None else width, 12 if height is None else height)
    clip.w, clip.h = clip.size
    clip.fps = 30
    clip.pos = lambda _time: (0, 0)

    def with_position(position: tuple[object, object] | Callable[[float], tuple[object, object]]) -> MagicMock:
        clip.pos = position if callable(position) else lambda _time: position
        return clip

    clip.with_position.side_effect = with_position
    for method in ("with_start", "with_duration", "with_opacity"):
        getattr(clip, method).return_value = clip
    clip.write_videofile.side_effect = lambda path, **_kwargs: Path(path).write_bytes(b"rendered")
    return clip


@pytest.fixture
def caption_renderer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]]:
    """Patch captured factories only after real modules have finished importing; ``tmp_path`` is the workspace."""
    from mindroom.custom_tools import agno_compat_moviepy as adapter  # noqa: PLC0415

    toolkit_class = moviepy_video_tools()
    factories = {
        "TextClip": MagicMock(side_effect=lambda **kwargs: _clip(**kwargs)),
        "ColorClip": MagicMock(side_effect=lambda **kwargs: _clip(**kwargs)),
        "CompositeVideoClip": MagicMock(side_effect=lambda _clips, **kwargs: _clip(**kwargs)),
        "VideoFileClip": MagicMock(side_effect=lambda _path: _clip(size=(1280, 720))),
    }
    for name, factory in factories.items():
        monkeypatch.setattr(adapter, name, factory)
    # The fake input is not real media, so skip FFmpeg's plain-media check as well as decoding.
    monkeypatch.setattr(adapter, "_require_plain_media", lambda _path: None)
    (tmp_path / "input.mp4").write_bytes(b"input video")
    return toolkit_class(tool_output_workspace_root=tmp_path), factories


@pytest.mark.parametrize(
    ("styles", "base_style", "highlight_style"),
    [
        ({}, {"font_size": 24}, {"font_size": 24}),
        ({"font_size": 42}, {"font_size": 42}, {"font_size": 42}),
        ({"font_color": "cyan"}, {"color": "cyan"}, {"color": "yellow"}),
        ({"stroke_color": "navy"}, {"stroke_color": "navy"}, {"stroke_color": "navy"}),
        ({"stroke_width": 4}, {"stroke_width": 4}, {"stroke_width": 4}),
        ({"stroke_width": 0}, {"stroke_width": 0}, {"stroke_width": 0}),
    ],
    ids=["default-font-size", "font-size", "text-color", "outline-color", "outline-width", "no-outline"],
)
def test_embed_captions_applies_styles_to_text_clips(
    caption_renderer: tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]],
    tmp_path: Path,
    styles: dict[str, object],
    base_style: dict[str, object],
    highlight_style: dict[str, object],
) -> None:
    """Dropping any advertised style must change the effective rendering inputs."""
    toolkit, factories = caption_renderer
    text_clip = factories["TextClip"]
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello world\n", encoding="utf-8")
    output_path = tmp_path / "captioned.mp4"

    result = toolkit.embed_captions("input.mp4", str(srt_path), str(output_path), **styles)

    assert result == str(output_path)
    assert output_path.read_bytes() == b"rendered"
    calls = [call.kwargs for call in text_clip.call_args_list]
    assert [call["text"] for call in calls] == ["Hello", " ", "Hello", "world", " ", "world"]
    for index in (0, 3):
        for key, expected in base_style.items():
            assert calls[index][key] == expected
    for index in (2, 5):
        for key, expected in highlight_style.items():
            assert calls[index][key] == expected
    if "font_size" in base_style:
        assert [calls[index]["font_size"] for index in (1, 4)] == [base_style["font_size"]] * 2


@pytest.mark.parametrize("failure", [None, "render", "publish"], ids=["success", "render-failure", "publish-failure"])
def test_embed_captions_publishes_complete_output_and_closes_media(
    caption_renderer: tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    """Partial renders never replace existing output or leave temporary files or open media."""
    toolkit, factories = caption_renderer
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello world\n", encoding="utf-8")
    output_path = tmp_path / "captioned.mp4"
    output_path.write_bytes(b"existing video")
    video = _clip(size=(1280, 720))
    composites: list[MagicMock] = []
    rendered_paths: list[Path] = []
    output_during_render: list[bytes] = []

    def render(path: str, **_kwargs: object) -> None:
        temporary_path = Path(path)
        rendered_paths.append(temporary_path)
        output_during_render.append(output_path.read_bytes())
        temporary_path.write_bytes(b"partial" if failure == "render" else b"complete video")
        if failure == "render":
            message = "encoder failed"
            raise OSError(message)

    def composite(_clips: list[object], **kwargs: object) -> MagicMock:
        clip = _clip(**kwargs)
        clip.write_videofile.side_effect = render
        composites.append(clip)
        return clip

    def reject_publication(*_args: object, **_kwargs: object) -> None:
        message = "destination locked"
        raise OSError(message)

    factories["VideoFileClip"].side_effect = lambda _path: video
    factories["CompositeVideoClip"].side_effect = composite
    if failure == "publish":
        monkeypatch.setattr(os, "replace", reject_publication)

    result = toolkit.embed_captions("input.mp4", str(srt_path), str(output_path))

    if failure is None:
        assert result == str(output_path)
        assert output_path.read_bytes() == b"complete video"
    else:
        assert result.startswith("Failed to embed captions:")
        assert ("encoder failed" if failure == "render" else "destination locked") in result
        assert output_path.read_bytes() == b"existing video"
    assert output_during_render == [b"existing video"]
    assert len(rendered_paths) == 1
    assert rendered_paths[0].parent != output_path.parent
    assert not rendered_paths[0].parent.exists()
    assert set(tmp_path.iterdir()) == {tmp_path / "input.mp4", srt_path, output_path}
    assert composites
    for clip in [video, *composites]:
        clip.close.assert_called_once()


def test_embed_captions_keeps_styles_independent_between_calls(
    caption_renderer: tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]],
    tmp_path: Path,
) -> None:
    """One toolkit applies each call's full style and restores defaults on a later call."""
    toolkit, factories = caption_renderer
    text_clip = factories["TextClip"]
    srt_path = tmp_path / "captions.srt"
    srt_path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    cases = [
        (
            {"font_size": 42, "font_color": "cyan", "stroke_color": "navy", "stroke_width": 4},
            [(42, "cyan", "navy", 4), (42, "cyan", None, None), (42, "yellow", "navy", 4)],
        ),
        (
            {"font_size": 18, "font_color": "red", "stroke_color": "green", "stroke_width": 0},
            [(18, "red", "green", 0), (18, "red", None, None), (18, "yellow", "green", 0)],
        ),
        ({}, [(24, "white", "black", 1), (24, "white", None, None), (24, "yellow", "black", 1)]),
    ]
    for index, (style, expected) in enumerate(cases):
        text_clip.reset_mock()
        output_path = tmp_path / f"captioned-{index}.mp4"

        result = toolkit.embed_captions("input.mp4", str(srt_path), str(output_path), **style)

        assert result == str(output_path)
        assert output_path.read_bytes() == b"rendered"
        actual = [
            (
                call.kwargs["font_size"],
                call.kwargs["color"],
                call.kwargs.get("stroke_color"),
                call.kwargs.get("stroke_width"),
            )
            for call in text_clip.call_args_list
        ]
        assert actual == expected


def test_create_caption_clips_preserves_explicit_font(
    caption_renderer: tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit font path reaches words, spaces, and highlighted words."""
    toolkit, factories = caption_renderer
    font_loader = MagicMock(return_value=ImageFont.load_default(24))
    monkeypatch.setattr(ImageFont, "truetype", font_loader)
    line = {
        "start": 0,
        "end": 1,
        "textcontents": [{"word": "Hello", "start": 0, "end": 1}],
    }

    toolkit.create_caption_clips(line, (320, 180), font="custom-caption.ttf", font_size=24)

    font_loader.assert_called_once_with("custom-caption.ttf", 24)
    assert [call.kwargs["font"] for call in factories["TextClip"].call_args_list] == ["custom-caption.ttf"] * 3


def test_media_paths_follow_file_access(
    caption_renderer: tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-chosen media paths stay in the workspace and never reach primary files or FFmpeg URLs."""
    toolkit, factories = caption_renderer
    primary = tmp_path_factory.mktemp("primary")
    config = primary / "config.yaml"
    config.write_text("administrators: []\n", encoding="utf-8")
    monkeypatch.chdir(primary)
    (tmp_path / "captions.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
    (tmp_path / "linked.srt").symlink_to(config)

    assert toolkit.create_srt("administrators: [attacker]\n", "config.yaml") == "config.yaml"
    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == "administrators: [attacker]\n"
    for output_path in (str(config), os.path.relpath(config, tmp_path), "linked.srt"):
        assert toolkit.create_srt("x", output_path).startswith("Failed to create SRT file:"), output_path
    assert toolkit.extract_audio(str(config), "audio.wav").startswith("Failed to extract audio:")
    assert toolkit.embed_captions("http://127.0.0.1:8765/api/config", "captions.srt").startswith("Failed")
    assert toolkit.embed_captions("input.mp4", str(config)).startswith("Failed to embed captions:")

    assert config.read_text(encoding="utf-8") == "administrators: []\n"
    assert [entry.name for entry in primary.iterdir()] == ["config.yaml"]
    assert [Path(call.args[0]).name for call in factories["VideoFileClip"].call_args_list] == ["video_path.mp4"]


@pytest.mark.parametrize(
    ("video_path", "expected_output"),
    [
        ("input.mp4", "input_captioned.mp4"),
        ("v1.2/clip", "v1.2/clip_captioned.mp4"),
        ("./v1.2/clip", "v1.2/clip_captioned.mp4"),
        ("v1.2/.clip", "v1.2/.clip_captioned.mp4"),
    ],
)
def test_default_caption_output_lands_next_to_the_input(
    caption_renderer: tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]],
    tmp_path: Path,
    video_path: str,
    expected_output: str,
) -> None:
    """Without an output path, the captioned video is named after the input file and written beside it."""
    toolkit, _factories = caption_renderer
    (tmp_path / "v1.2").mkdir()
    for name in ("v1.2/clip", "v1.2/.clip"):
        (tmp_path / name).write_bytes(b"input video")
    (tmp_path / "empty.srt").write_text("", encoding="utf-8")

    assert toolkit.embed_captions(video_path, "empty.srt") == expected_output
    assert (tmp_path / expected_output).read_bytes() == b"rendered"
    assert sorted(path.name for path in tmp_path.glob("*_captioned.mp4")) == (
        ["input_captioned.mp4"] if expected_output == "input_captioned.mp4" else []
    )


def test_oversized_inputs_fail_before_staging_past_the_limit(
    caption_renderer: tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sparse worker-written inputs far above the staging caps are refused instead of copied to their full size."""
    from mindroom.custom_tools import agno_compat_moviepy as adapter  # noqa: PLC0415

    toolkit, factories = caption_renderer
    monkeypatch.setattr(adapter, "_MAX_STAGED_VIDEO_BYTES", 1 << 20)
    for name in ("big.mp4", "big.srt"):
        with (tmp_path / name).open("wb") as sparse:
            sparse.truncate(8 << 20)

    assert "video_path 'big.mp4' exceeds the 1 MiB input limit" in toolkit.extract_audio("big.mp4", "audio.wav")
    assert "srt_path 'big.srt' exceeds the 1 MiB input limit" in toolkit.embed_captions("input.mp4", "big.srt")
    assert [Path(call.args[0]).name for call in factories["VideoFileClip"].call_args_list] == ["video_path.mp4"]
    assert not (tmp_path / "audio.wav").exists()
    assert not (tmp_path / "input_captioned.mp4").exists()


def test_outputs_publish_without_buffering_the_rendered_file(
    caption_renderer: tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]],
    tmp_path: Path,
) -> None:
    """Rendered audio and video reach the workspace in chunks instead of being read into memory whole."""
    toolkit, factories = caption_renderer
    size = 4 * 1024 * 1024
    (tmp_path / "empty.srt").write_text("", encoding="utf-8")

    def render(path: str, **_kwargs: object) -> None:
        # A sparse file, so the fake encoder itself allocates nothing.
        with Path(path).open("wb") as output:
            output.truncate(size)

    def composite(_clips: list[object], **kwargs: object) -> MagicMock:
        clip = _clip(**kwargs)
        clip.write_videofile.side_effect = render
        return clip

    video = _clip(size=(1280, 720))
    video.audio.write_audiofile.side_effect = render
    factories["VideoFileClip"].side_effect = lambda _path: video
    factories["CompositeVideoClip"].side_effect = composite

    tracemalloc.start()
    try:
        assert toolkit.extract_audio("input.mp4", "audio.wav") == "audio.wav"
        assert toolkit.embed_captions("input.mp4", "empty.srt", "captioned.mp4") == "captioned.mp4"
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < size // 4
    assert (tmp_path / "audio.wav").stat().st_size == size
    assert (tmp_path / "captioned.mp4").stat().st_size == size


def _encode_clip(path: Path) -> None:
    """Encode one second of tiny video with silent audio through MoviePy's FFmpeg binary."""
    subprocess.run(
        [
            FFMPEG_BINARY,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=16x16:duration=1:rate=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=22050:cl=mono",
            "-t",
            "1",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize("suffix", [".mpg", ".vob", ".flv", ".wmv", ".gif"])
def test_video_inputs_accept_self_contained_formats(tmp_path: Path, suffix: str) -> None:
    """Common formats whose FFmpeg demuxers read only the opened file stay accepted, as before the plain-media check."""
    from mindroom.custom_tools.agno_compat_moviepy import _require_plain_media  # noqa: PLC0415

    clip = tmp_path / f"clip{suffix}"
    _encode_clip(clip)

    _require_plain_media(str(clip))


def test_video_inputs_refuse_playlists_and_manifests(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A playlist or manifest the agent wrote never makes FFmpeg read media outside the workspace."""
    toolkit = moviepy_video_tools()(tool_output_workspace_root=tmp_path)
    secret = tmp_path_factory.mktemp("outside") / "secret.mp4"
    _encode_clip(secret)
    shutil.copyfile(secret, tmp_path / "clip.mp4")
    (tmp_path / "empty.srt").write_text("", encoding="utf-8")
    playlist = f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1.0,\n{secret}\n#EXT-X-ENDLIST\n"
    manifest = (
        '<?xml version="1.0"?>\n'
        '<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011"'
        ' type="static" mediaPresentationDuration="PT1S" minBufferTime="PT1S"><Period>'
        '<AdaptationSet mimeType="audio/mp4"><Representation id="secret" bandwidth="128000">'
        f"<BaseURL>{secret}</BaseURL></Representation></AdaptationSet></Period></MPD>\n"
    )

    # Plain media still works, so only inputs FFmpeg would open as manifests are refused.
    assert toolkit.extract_audio("clip.mp4", "clip.wav") == "clip.wav"
    # FFmpeg detects DASH by content, so a manifest named like a video is refused too.
    for name, text in (("playlist.m3u8", playlist), ("manifest.mp4", manifest)):
        assert toolkit.create_srt(text, name) == name
        assert toolkit.extract_audio(name, "leak.wav").startswith("Failed to extract audio:"), name
        assert toolkit.embed_captions(name, "empty.srt", "leak.mp4").startswith("Failed to embed captions:"), name

    assert sorted(entry.name for entry in tmp_path.iterdir()) == [
        "clip.mp4",
        "clip.wav",
        "empty.srt",
        "manifest.mp4",
        "playlist.m3u8",
    ]
