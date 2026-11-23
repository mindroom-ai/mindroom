"""Re-export WAV validation helpers shared with voice dictation."""

from mindroom.voice.wav import WavError, wav_seconds

__all__ = ["WavError", "wav_seconds"]
