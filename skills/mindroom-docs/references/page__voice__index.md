# Voice Messages

MindRoom can surface Matrix voice messages as attachment-aware prompts for agents. If STT is configured, MindRoom also transcribes the audio and routes it through the normal text pipeline. If STT is unavailable, disabled, or fails, the audio still remains available as an attachment and falls back to `🎤 [Attached voice message]`.

## Overview

When a voice message is received:

1. The audio event is handled through the shared media pipeline.
1. Audio is downloaded and decrypted, if needed, and registered as a context-scoped attachment.
1. If STT is configured and succeeds, the audio is transcribed and lightly normalized for mentions and commands.
1. If STT is unavailable, disabled, or fails, MindRoom falls back to `🎤 [Attached voice message]`.
1. The normalized text plus attachment metadata is dispatched using the normal routing and thread logic.
1. The responding agent receives the original audio attachment alongside the normalized prompt.

## Configuration

Enable STT and voice-intelligence formatting in `config.yaml`:

```
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
    # Optional: custom endpoint (without /v1 suffix)
    # host: http://localhost:8080
  intelligence:
    model: default  # Model used for command recognition
```

Or use the dashboard's Voice tab.

With `voice.enabled: false`, audio messages are still surfaced as attachments with the fallback prompt. Enabling voice adds STT and command-recognition on top of that attachment flow.

## STT Providers

MindRoom uses the OpenAI-compatible transcription API. Any service that implements the `/v1/audio/transcriptions` endpoint will work.

### OpenAI Whisper (Cloud)

```
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
```

Requires `OPENAI_API_KEY` environment variable.

### Self-Hosted Whisper

```
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

```
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

1. **Agent mentions** - Converts spoken agent names to `@agent` format
1. **Command patterns** - Identifies and formats `!command` syntax
1. **Smart formatting** - Handles speech recognition errors and natural language variations

### Intelligence Model

The intelligence model processes raw transcriptions to recognize commands and agent names:

```
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
                                                            ┌─────────────┐
                                                            │ 🎤 Message  │
                                                            │ to Room     │
                                                            └─────────────┘
                                                                  │
                                                                  ▼
                                                            ┌─────────────┐
                                                            │ Agent       │
                                                            │ Responds    │
                                                            └─────────────┘
```

## Dispatch Behavior

### Single-agent rooms or explicitly targeted audio

If only one eligible agent is visible, that agent responds directly to the normalized audio event. If the audio caption or transcript explicitly mentions an agent, that targeted agent responds directly as well. In these cases, the router does not need to post an extra routing handoff.

### Multi-agent rooms where the router must choose

If multiple agents are available and the audio does not already target one of them, the router uses the normalized text to do the usual routing step. The router then posts a normal handoff message such as `@home could you help with this?`. The selected agent responds to that router handoff, and the handoff carries the original audio attachment metadata forward.

### No router, or router cannot reply

Audio still works when the router is absent. In that case, agents handle the normalized audio directly using the same mention, thread, and permission rules as normal text messages. The same direct handling also applies when the router is present but is not allowed to reply to the original sender. If multiple eligible agents remain and the audio does not already target one of them, there is no automatic handoff until the user mentions an agent.

### Attachment access

The original audio is always registered as a context-scoped attachment before dispatch continues. That means the responding agent can inspect the file directly, use audio-capable models, or fetch it later with the `attachments` tool. This is true whether the prompt came from a transcript, a fallback message, or a router handoff.

## Matrix Integration

Voice messages in Matrix are:

- Detected as `RoomMessageAudio` or `RoomEncryptedAudio` events
- Downloaded from the Matrix media server
- Decrypted if end-to-end encrypted (using the encryption key from the event)
- Registered as audio attachments before dispatch
- Sent to the STT service via the OpenAI-compatible API when transcription is enabled
- Normalized once per room and thread context, even though multiple bots may observe the event

Audio callbacks are registered on all bots because audio now follows the shared media pipeline. Shared normalization prevents repeated download and STT work for the same event. Reply-permission checks still use the original human sender, not a later router relay.

## Environment Variables

| Variable         | Description                                                          |
| ---------------- | -------------------------------------------------------------------- |
| `OPENAI_API_KEY` | For OpenAI Whisper API (used as fallback if no `api_key` configured) |

## Text-to-Speech Tools

MindRoom also supports text-to-speech (TTS) through agent tools. These are separate from voice message transcription and allow agents to generate audio responses:

- **OpenAI** - Speech synthesis via `openai` tool
- **ElevenLabs** - High-quality AI voices and sound effects via `eleven_labs` tool
- **Cartesia** - Voice AI with optional voice localization via `cartesia` tool
- **Groq** - Fast speech generation via `groq` tool

See the [Tools documentation](https://docs.mindroom.chat/tools/index.md) for configuration details.

## Voice Fallback (No STT Available)

When STT is unavailable, disabled, or transcription fails, MindRoom falls back to raw audio passthrough:

1. The voice message audio is downloaded and saved locally as an attachment
1. The normalized text becomes `🎤 [Attached voice message]`
1. The raw audio is registered as an attachment ID available to agents in the room or thread context
1. When an agent responds, it automatically receives the raw audio as an Agno `Audio` object

This means voice messages still reach agents even without STT. Agents with audio-capable models can process the raw audio directly, and tool-using agents can retrieve the file by attachment ID. Attachment IDs in this fallback path use the same context-scoping rules described in [File & Video Attachments](https://docs.mindroom.chat/attachments/index.md).

## Limitations

- Only OpenAI-compatible STT APIs are supported
- Audio quality and background noise affect transcription accuracy
- Without STT, routing has less textual context, so explicit `@mentions` or existing thread context are more reliable in multi-agent rooms
- Without STT, agents receive raw audio instead of transcription, so the model or tools must support audio inputs to process it

## Tips

- **Say the agent name first** - "Hey @assistant, what's the weather?"
- **Use display names** - The AI converts spoken names like "HomeAssistant" to the correct `@home` mention
