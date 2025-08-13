"""MLX Transcribe tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tools_metadata import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolStatus,
    register_tool_with_metadata,
)

if TYPE_CHECKING:
    from agno.tools.mlx_transcribe import MLXTranscribeTools


@register_tool_with_metadata(
    name="mlx_transcribe",
    display_name="MLX Transcribe",
    description="Audio transcription using Apple's MLX Whisper framework",
    category=ToolCategory.DEVELOPMENT,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    icon="Mic",
    icon_color="text-blue-600",
    config_fields=[
        # Core configuration
        ConfigField(
            name="base_dir",
            label="Base Directory",
            type="text",
            required=False,
            placeholder="/path/to/audio/files",
            description="Base directory for audio files (defaults to current directory)",
        ),
        ConfigField(
            name="read_files_in_base_dir",
            label="Enable File Reading",
            type="boolean",
            required=False,
            default=True,
            description="Enable the read_files function to list audio files in base directory",
        ),
        ConfigField(
            name="path_or_hf_repo",
            label="Model Path or HuggingFace Repo",
            type="text",
            required=False,
            default="mlx-community/whisper-large-v3-turbo",
            placeholder="mlx-community/whisper-large-v3-turbo",
            description="Path or HuggingFace repository for the Whisper model",
        ),
        
        # Audio processing parameters
        ConfigField(
            name="verbose",
            label="Verbose Output",
            type="boolean",
            required=False,
            description="Enable verbose output during transcription",
        ),
        ConfigField(
            name="temperature",
            label="Temperature",
            type="number",
            required=False,
            placeholder="0.0",
            description="Temperature for sampling (can be float or tuple of floats)",
        ),
        ConfigField(
            name="compression_ratio_threshold",
            label="Compression Ratio Threshold",
            type="number",
            required=False,
            placeholder="2.4",
            description="Threshold for compression ratio detection",
        ),
        ConfigField(
            name="logprob_threshold",
            label="Log Probability Threshold",
            type="number",
            required=False,
            placeholder="-1.0",
            description="Log probability threshold for token filtering",
        ),
        ConfigField(
            name="no_speech_threshold",
            label="No Speech Threshold",
            type="number",
            required=False,
            placeholder="0.6",
            description="Threshold for detecting silence/no speech",
        ),
        
        # Text processing options
        ConfigField(
            name="condition_on_previous_text",
            label="Condition on Previous Text",
            type="boolean",
            required=False,
            description="Whether to condition transcription on previous text context",
        ),
        ConfigField(
            name="initial_prompt",
            label="Initial Prompt",
            type="text",
            required=False,
            placeholder="The following is a recording of...",
            description="Initial prompt to guide transcription style and context",
        ),
        ConfigField(
            name="word_timestamps",
            label="Word Timestamps",
            type="boolean",
            required=False,
            description="Enable word-level timestamps in transcription output",
        ),
        
        # Punctuation handling
        ConfigField(
            name="prepend_punctuations",
            label="Prepend Punctuations",
            type="text",
            required=False,
            placeholder="\"'([{-",
            description="Punctuation characters to prepend to words",
        ),
        ConfigField(
            name="append_punctuations",
            label="Append Punctuations",
            type="text",
            required=False,
            placeholder="\"'.。,，!！?？:：\")]}、",
            description="Punctuation characters to append to words",
        ),
        
        # Advanced options
        ConfigField(
            name="clip_timestamps",
            label="Clip Timestamps",
            type="text",
            required=False,
            placeholder="0,30",
            description="Clip timestamps (string format like '0,30' or comma-separated list)",
        ),
        ConfigField(
            name="hallucination_silence_threshold",
            label="Hallucination Silence Threshold",
            type="number",
            required=False,
            placeholder="2.0",
            description="Threshold for detecting hallucinated content in silence",
        ),
        ConfigField(
            name="decode_options",
            label="Decode Options",
            type="text",
            required=False,
            placeholder='{"beam_size": 5}',
            description="Additional decoding options as JSON string (e.g., beam search parameters)",
        ),
    ],
    dependencies=["mlx-whisper"],
    docs_url="https://docs.agno.com/tools/toolkits/others/mlx_transcribe",
)
def mlx_transcribe_tools() -> type[MLXTranscribeTools]:
    """Return MLX Transcribe tools for audio transcription using Apple's MLX framework."""
    from agno.tools.mlx_transcribe import MLXTranscribeTools  # noqa: PLC0415

    return MLXTranscribeTools