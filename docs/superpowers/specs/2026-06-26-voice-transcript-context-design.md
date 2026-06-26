# Voice Transcript Context Design

## Goal

Reduce agent confusion on voice turns where MindRoom provides both a normalized transcript and the original audio attachment.
Agents should treat the transcript as the current user message and treat the raw audio attachment as optional source material.

## Current Behavior

Voice audio is normalized into a `PreparedTextEvent` with `SOURCE_KIND_KEY` set to `voice`.
When STT succeeds, the visible prompt starts with `🎤` and contains the normalized transcript.
The original audio is registered as a context-scoped attachment and is included in the current turn media payload.
The normal attachment prompt only says the audio attachment is available by ID, so some agents infer that they should transcribe it again.
When STT fails or voice is disabled, MindRoom uses `🎤 [Attached voice message]` and marks `VOICE_RAW_AUDIO_FALLBACK_KEY` so the agent can inspect or transcribe the audio.

## Chosen Approach

Use the existing hidden `model_prompt` channel to add voice-specific guidance for successful STT turns.
The Matrix-visible message and stored raw prompt remain unchanged.
The guidance is scoped to current-turn audio attachment IDs and does not create a new Matrix event.
It should say that MindRoom has already transcribed the voice message, that the listed audio attachment is the original raw audio, and that the agent should only re-transcribe or inspect it when the user asks or when the transcript seems wrong.

## Data Flow

Voice normalization keeps setting `SOURCE_KIND_KEY`, `ATTACHMENT_IDS_KEY`, and `VOICE_RAW_AUDIO_FALLBACK_KEY` on synthetic voice events.
Response payload assembly already receives trusted current attachment IDs and resolves them to current-turn attachment records.
The attachment prompt builder can render a voice transcript guidance block when current records include audio and the event was a successful voice transcript.
Raw audio fallback turns skip this guidance because those turns genuinely need the attachment as the primary content.

## Testing

Add focused payload tests for successful voice transcript turns and raw fallback turns.
The successful-transcript test should assert that `payload.prompt` remains only the transcript, `payload.model_prompt` includes the guidance and attachment ID, and inline audio is still attached.
The fallback test should assert that the normal attachment prompt remains available but the "already transcribed" guidance is absent.

## Scope

No Matrix event shape changes are required.
No changes are required to router visible echo behavior.
No changes are required to attachment storage or tool access.
