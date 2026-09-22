"""Caption styling with fake video rendering and no font lookup."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from mindroom.tools.moviepy_video_tools import moviepy_video_tools

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.custom_tools.agno_compat_moviepy import MindRoomMoviePyVideoTools


def _clip(**kwargs: object) -> MagicMock:
    clip = MagicMock()
    clip.size = kwargs.get("size", (20, 12))
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
) -> tuple[MindRoomMoviePyVideoTools, dict[str, MagicMock]]:
    """Patch captured factories only after real modules have finished importing."""
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
    return toolkit_class(), factories


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

    def reject_publication(_source: str, _destination: str) -> None:
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
    assert rendered_paths[0] != output_path
    assert rendered_paths[0].parent == output_path.parent
    assert not rendered_paths[0].exists()
    assert set(tmp_path.iterdir()) == {srt_path, output_path}
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
) -> None:
    """An explicit font path reaches words, spaces, and highlighted words."""
    toolkit, factories = caption_renderer
    line = {
        "start": 0,
        "end": 1,
        "textcontents": [{"word": "Hello", "start": 0, "end": 1}],
    }

    toolkit.create_caption_clips(line, (320, 180), font="custom-caption.ttf", font_size=24)

    assert [call.kwargs["font"] for call in factories["TextClip"].call_args_list] == ["custom-caption.ttf"] * 3
