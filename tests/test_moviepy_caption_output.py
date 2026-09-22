"""Caption-output cleanup through MoviePy's real audio/video write orchestration."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from moviepy import AudioClip, ColorClip, CompositeVideoClip
from moviepy.video import VideoClip

from mindroom.custom_tools import agno_compat_moviepy as adapter


@pytest.mark.parametrize(
    "failure",
    [None, "audio", "video", "publish"],
    ids=["success", "audio-failure", "video-failure", "publish-failure"],
)
def test_embed_captions_cleans_owned_audio_and_video_files(  # noqa: PLR0915
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str | None,
) -> None:
    """Real write_videofile failures preserve output and clean only owned media paths."""
    working = tmp_path / "working"
    output_dir = tmp_path / "output"
    working.mkdir()
    output_dir.mkdir()
    monkeypatch.chdir(working)
    source = tmp_path / "input.mp4"
    source.write_bytes(b"user input video")
    captions = tmp_path / "captions.srt"
    # Empty captions isolate audio publication from independently tested font rendering.
    captions.write_text("", encoding="utf-8")
    output = output_dir / "captioned.mp4"
    output.write_bytes(b"existing output")
    user_audio = output_dir / "captioned.m4a"
    user_audio.write_bytes(b"user audio")
    working_audio = working / "unrelatedTEMP_MPY_wvf_snd.mp4"
    working_audio.write_bytes(b"unrelated working audio")
    audio = AudioClip(lambda _time: 0.0, duration=1, fps=8000)
    video = ColorClip((32, 24), color=(0, 0, 0), duration=1).with_fps(1).with_audio(audio)
    monkeypatch.setattr(adapter, "VideoFileClip", lambda _path: video)
    audio_paths: list[Path] = []
    video_paths: list[Path] = []

    def encode_audio(_clip: AudioClip, filename: str, *_args: object, **_kwargs: object) -> None:
        path = Path(filename).resolve()
        audio_paths.append(path)
        assert output.read_bytes() == b"existing output"
        path.write_bytes(b"encoded audio")
        if failure == "audio":
            message = "audio encoder failed"
            raise OSError(message)

    def encode_video(
        final: CompositeVideoClip,
        filename: str,
        _fps: float,
        _codec: str,
        *,
        audiofile: str,
        audio_codec: str,
        **_kwargs: object,
    ) -> None:
        assert final.audio is not None
        assert audio_codec == "copy"
        assert Path(audiofile).resolve() == audio_paths[0]
        assert Path(audiofile).read_bytes() == b"encoded audio"
        assert output.read_bytes() == b"existing output"
        path = Path(filename).resolve()
        video_paths.append(path)
        path.write_bytes(b"partial video" if failure == "video" else b"complete video")
        if failure == "video":
            message = "video encoder failed"
            raise OSError(message)

    def reject_publication(_source: object, _destination: object) -> None:
        message = "destination locked"
        raise OSError(message)

    # Keep write_videofile and composite audio propagation real; replace encoder boundaries only.
    monkeypatch.setattr(AudioClip, "write_audiofile", encode_audio)
    monkeypatch.setattr(VideoClip, "ffmpeg_write_video", encode_video)
    if failure == "publish":
        monkeypatch.setattr(os, "replace", reject_publication)

    result = adapter.MindRoomMoviePyVideoTools().embed_captions(str(source), str(captions), str(output))

    if failure is None:
        assert result == str(output)
        assert output.read_bytes() == b"complete video"
    else:
        expected = "destination locked" if failure == "publish" else f"{failure} encoder failed"
        assert result == f"Failed to embed captions: {expected}"
        assert output.read_bytes() == b"existing output"
    assert len(audio_paths) == 1
    assert len(video_paths) == (0 if failure == "audio" else 1)
    for path in [*audio_paths, *video_paths]:
        assert path.parent == output_dir
        assert path not in {source, output, user_audio, working_audio}
        assert not path.exists()
    assert source.read_bytes() == b"user input video"
    assert user_audio.read_bytes() == b"user audio"
    assert working_audio.read_bytes() == b"unrelated working audio"
    assert set(output_dir.iterdir()) == {output, user_audio}
    assert set(working.iterdir()) == {working_audio}
