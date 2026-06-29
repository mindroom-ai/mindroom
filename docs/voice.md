---
icon: lucide/mic
---

# Voice Messages

MindRoom can surface Matrix voice messages as attachment-aware prompts for agents and teams.
If STT is configured, MindRoom also transcribes the audio and routes it through the normal text pipeline.
If STT is unavailable, disabled, or fails, the audio still remains available as an attachment and falls back to `🎤 [Attached voice message]`.

## Overview

When a voice message is received:

1. The audio event is handled through the shared media pipeline.
2. Audio is downloaded and decrypted, if needed, and registered as a context-scoped attachment.
3. If STT is configured and succeeds, the audio is transcribed and lightly normalized for mentions and commands.
4. If STT is unavailable, disabled, or fails, MindRoom falls back to `🎤 [Attached voice message]`.
5. The normalized text plus attachment metadata is dispatched using the normal routing and thread logic.
6. If routing is ambiguous in a multi-responder room, the router posts a visible handoff message.
7. If `voice.visible_router_echo` is enabled and the router is present and allowed to reply, the router also posts the normalized voice text as a display-only message.
8. Otherwise, no extra router message is posted and the chosen agent or team replies directly.
9. The responding entity receives the original audio attachment alongside the normalized prompt.

## Configuration

Enable STT and voice-intelligence formatting in `config.yaml`:

```yaml
voice:
  enabled: true
  visible_router_echo: true
  stt:
    provider: openai
    model: whisper-1
    # Optional: custom endpoint (without /v1 suffix)
    # host: http://localhost:8080
  intelligence:
    model: default  # Model used for command recognition
```

Or use the dashboard's Voice tab.

With `voice.enabled: false`, audio messages are still surfaced as attachments with the fallback prompt.
Enabling voice adds STT and command-recognition on top of that attachment flow.
With `voice.visible_router_echo: true`, the router also posts the normalized transcript or fallback text for inspection when it is present in the room and allowed to reply.

## STT Providers

MindRoom uses the OpenAI-compatible transcription API. Any service that implements the `/v1/audio/transcriptions` endpoint will work.

### OpenAI Whisper (Cloud)

```yaml
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
```

Requires `OPENAI_API_KEY` environment variable.

### Self-Hosted Whisper

```yaml
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
    host: http://localhost:8080
```

Note: Do not include `/v1` in the host URL - MindRoom appends `/v1/audio/transcriptions` automatically.

Use with [faster-whisper-server](https://github.com/fedirz/faster-whisper-server) or similar OpenAI-compatible STT servers.

### Custom API Key

For self-hosted solutions that require authentication:

```yaml
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
    host: http://localhost:8080
    api_key: your-custom-api-key
```

If `api_key` is not set, MindRoom falls back to the `OPENAI_API_KEY` environment variable.

## Command Recognition

The intelligence component uses an AI model to analyze transcriptions and format them properly:

1. **Agent and team mentions** - Converts spoken agent or team names to listed `@agent` or `@team` mentions
2. **Mention sanitization** - Mentions of agents or teams not available in the current room have their `@` stripped so the responder is not falsely targeted
3. **Command patterns** - Identifies and formats `!command` syntax
4. **Speculative command rejection** - Commands the AI invents that were not in the original transcription are rejected to prevent false positives
5. **Smart formatting** - Handles speech recognition errors and natural language variations

### Intelligence Model

The intelligence model processes raw transcriptions to recognize commands, agent names, and team names:

```yaml
voice:
  intelligence:
    model: default  # Uses the default model from your models config
```

You can specify a different model for faster or more accurate command recognition.

## How It Works

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Voice Msg   │────▶│ Download &  │────▶│ Transcribe  │────▶│ Format with │
│ (Audio)     │     │ Decrypt     │     │ (STT)       │     │ AI (LLM)    │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                                                  │
                                                                  ▼
                                                         ┌──────────────────┐
                                                         │ Normal Dispatch  │
                                                         │ Decision         │
                                                         └──────────────────┘
                                                           │            │
                                                           │            │
                                                           ▼            ▼
                                                 ┌──────────────┐  ┌──────────────┐
                                                 │ Visible      │  │ No Visible   │
                                                 │ Router       │  │ Router       │
                                                 │ Handoff      │  │ Handoff      │
                                                 └──────────────┘  └──────────────┘
                                                           │            │
                                                           └──────┬─────┘
                                                                  ▼
                                                           ┌─────────────┐
                                                           │ Responder   │
                                                           │ Answers     │
                                                           └─────────────┘
```

## Dispatch Behavior

### Single-responder rooms or explicitly targeted audio

If only one eligible agent or team is visible, that responder answers the normalized audio event directly.
If the audio caption or transcript explicitly mentions an agent or team, that targeted responder answers directly as well.
In these cases, the router does not post an extra visible routing handoff.
The transcript or fallback text is used internally for dispatch, not echoed to the room as a separate message.
If `voice.visible_router_echo` is enabled, the router still posts a display-only copy of the normalized voice text, but responders ignore that echo and continue responding to the original audio event.

### Multi-responder rooms where the router must choose

If multiple agents or teams are available and the audio does not already target one of them, the router uses the normalized text to do the usual routing step.
The router then posts a normal handoff message such as `@home could you help with this?`.
The selected agent or team responds to that router handoff, and the handoff carries the original audio attachment metadata forward.
This is the case where a visible router message appears.
If `voice.visible_router_echo` is also enabled, the router first posts the normalized voice text as a display-only echo and then posts the normal handoff.

### No router, or router cannot reply

Audio still works when the router is absent.
In that case, agents and teams handle the normalized audio directly using the same mention, thread, and permission rules as normal text messages.
The same direct handling also applies when the router is present but is not allowed to reply to the original sender.
In these cases, there is no visible router echo because the router does not handle the event.
If multiple eligible responders remain and the audio does not already target one of them, there is no automatic handoff until the user mentions an agent or team.

### Visibility rule

By default, MindRoom posts a display-only router echo of normalized voice text when the router is allowed to process the event.
The router handoff message appears only when the router must disambiguate between multiple eligible responders.
If the responder is already clear from room shape, thread context, or explicit targeting, the chosen agent or team replies directly to the original audio event.
Set `voice.visible_router_echo: false` to suppress the display-only echo without changing which event responders actually answer.

### Attachment access

The original audio is always registered as a context-scoped attachment before dispatch continues.
That means the responding agent or team can inspect the file directly, use audio-capable models, or fetch it later with the `attachments` tool.
This is true whether the prompt came from a transcript, a fallback message, or a router handoff.
For successful STT turns, MindRoom adds hidden model-facing guidance that says the `🎤` text is already the transcript and the raw audio attachment is optional.
For raw fallback turns, MindRoom does not add that guidance because the audio attachment remains the primary content.

## Matrix Integration

Voice messages in Matrix are:

- Detected as `RoomMessageAudio` or `RoomEncryptedAudio` events
- Downloaded from the Matrix media server
- Decrypted if end-to-end encrypted (using the encryption key from the event)
- Registered as audio attachments before dispatch
- Sent to the STT service via the OpenAI-compatible API when transcription is enabled
- Normalized once per room and thread context, even though multiple bots may observe the event

Audio callbacks are registered on all bots because audio now follows the shared media pipeline.
Shared normalization prevents repeated download and STT work for the same event.
Reply-permission checks still use the original human sender, not a later router relay.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | For OpenAI Whisper API (used as fallback if no `api_key` configured) |

## Text-to-Speech Tools

MindRoom also supports text-to-speech (TTS) through agent tools.
These are separate from voice message transcription and allow agents to generate audio responses:

- **Matrix Voice Message** - One-call OpenAI TTS delivery to the current Matrix room or thread via `matrix_voice_message`
- **OpenAI** - Speech synthesis via `openai` tool
- **ElevenLabs** - High-quality AI voices and sound effects via `eleven_labs` tool
- **Cartesia** - Voice AI with optional voice localization via `cartesia` tool
- **Groq** - Fast speech generation via `groq` tool

Use `matrix_voice_message` when an agent should send a playable Matrix voice note directly.
When `room_id` and `thread_id` are omitted, it sends to the current room and active thread.
Pass `thread_id="room"` to send at room level.
Use `caption` for the audio event body, and use `companion_message` for a separate readable text message in the same target.
The default Opus output includes duration and waveform metadata for Matrix voice-note rendering.
Other response formats may send as regular `m.audio` when duration metadata cannot be derived.

See the [Tools documentation](tools/index.md) for configuration details.

## Voice Fallback (No STT Available)

When STT is unavailable, disabled, or transcription fails, MindRoom falls back to raw audio passthrough:

1. The voice message audio is downloaded and saved locally as an attachment
2. The normalized text becomes `🎤 [Attached voice message]`
3. The raw audio is registered as an attachment ID available to agents and teams in the room or thread context
4. When an agent or team responds, it automatically receives the raw audio as an Agno `Audio` object

This means voice messages still reach responders even without STT.
Agents or teams with audio-capable models can process the raw audio directly, and tool-using responders can retrieve the file by attachment ID.
Attachment IDs in this fallback path use the same context-scoping rules described in [File & Video Attachments](attachments.md).

## Limitations

- Only OpenAI-compatible STT APIs are supported
- Audio quality and background noise affect transcription accuracy
- Without STT, routing has less textual context, so explicit `@mentions` or existing thread context are more reliable in multi-responder rooms
- Without STT, responders receive raw audio instead of transcription, so the model or tools must support audio inputs to process it

## Tips

- **Say the agent or team name first** - "Hey @assistant, what's the weather?"
- **Use display names** - The AI converts spoken names like "HomeAssistant" to the correct `@home` mention
