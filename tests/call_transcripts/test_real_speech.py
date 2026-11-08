"""A real recording through real speech-to-text; runs only with ``OPENAI_API_KEY`` set, and then reaches OpenAI."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from mindroom.call_transcripts.wav import wav_seconds
from mindroom.config.main import Config
from mindroom.config.voice import VoiceConfig, VoiceSTTConfig
from mindroom.voice.handler import transcribe_speech
from tests import conftest
from tests.conftest import test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Callable

# "We will ship the release on Friday, and I will write the notes.", spoken, as MindRoom Chat uploads a stretch:
# a 16 kHz mono 16-bit WAV.
_RECORDING = Path(__file__).parents[1] / "fixtures" / "call_transcripts" / "speech.wav"
_OPENAI_API_HOST = "api.openai.com"


@pytest.fixture
def openai_reachable(monkeypatch: pytest.MonkeyPatch) -> str:
    """Return the key, letting this test reach OpenAI's API host and nothing else beyond the machine."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        pytest.skip("Set OPENAI_API_KEY to transcribe a real recording.")
    refused: Callable[[str, int], bool] = conftest._dial_leaves_the_test
    monkeypatch.setattr(
        conftest,
        "_dial_leaves_the_test",
        lambda host, port: host != _OPENAI_API_HOST and refused(host, port),
    )
    return key


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_a_real_recording_is_transcribed_with_the_words_spoken_in_it(
    openai_reachable: str,
    tmp_path: Path,
) -> None:
    """A stretch shaped like Chat's uploads, through the default speech-to-text model, comes back as its words."""
    audio = _RECORDING.read_bytes()
    assert 3.0 < wav_seconds(audio) < 5.0
    config = Config(voice=VoiceConfig(stt=VoiceSTTConfig(api_key=openai_reachable)))

    transcript = await transcribe_speech(audio, config, test_runtime_paths(tmp_path), language="en")

    assert transcript is not None
    words = set(re.findall(r"[a-z]+", transcript.text.lower()))
    assert {"ship", "release", "friday", "write", "notes"} <= words, transcript.text
