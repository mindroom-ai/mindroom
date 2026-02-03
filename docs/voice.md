---
icon: lucide/mic
---

# Voice Messages

MindRoom can process voice messages sent to Matrix rooms, transcribing them and responding appropriately.

## Overview

When voice message handling is enabled:

1. Voice messages are detected in Matrix rooms
2. Audio is downloaded and decrypted (if E2E encrypted)
3. Audio is sent to an OpenAI-compatible speech-to-text (STT) service
4. Transcription is processed by an AI to recognize agent mentions and commands
5. The formatted message is sent to the room (prefixed with a microphone emoji)
6. The appropriate agent responds

## Configuration

Enable voice in `config.yaml`:

```yaml
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

1. **Agent mentions** - Converts spoken agent names to `@agent` format
2. **Command patterns** - Identifies and formats `!command` syntax
3. **Smart formatting** - Handles speech recognition errors and natural language variations

### Intelligence Model

The intelligence model processes raw transcriptions to recognize commands and agent names:

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

## Matrix Integration

Voice messages in Matrix are:

- Detected as `RoomMessageAudio` or `RoomEncryptedAudio` events
- Downloaded from the Matrix media server
- Decrypted if end-to-end encrypted (using the encryption key from the event)
- Saved temporarily as `.ogg` files for processing
- Sent to the STT service via the OpenAI-compatible API

The router agent handles all voice message processing to avoid duplicate transcriptions.

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | For OpenAI Whisper API (used as fallback if no `api_key` configured) |

## Text-to-Speech Tools

MindRoom also supports text-to-speech (TTS) through agent tools. These are separate from voice message transcription and allow agents to generate audio responses:

- **OpenAI** - Speech synthesis via `openai` tool
- **ElevenLabs** - High-quality AI voices and sound effects via `eleven_labs` tool
- **Cartesia** - Voice AI with optional voice localization via `cartesia` tool
- **Groq** - Fast speech generation via `groq` tool

See the [Tools documentation](/tools) for configuration details.

## Limitations

- Only OpenAI-compatible STT APIs are supported
- Audio quality affects transcription accuracy
- Very long messages may be truncated by the STT service
- Background noise can reduce accuracy
- Some accents may have lower accuracy

## Best Practices

1. **Speak clearly** - Enunciate agent names
2. **Reduce background noise** - Improves accuracy
3. **Say the agent name first** - "Hey @assistant, what's the weather?"
4. **Keep messages focused** - One request per message
5. **Use display names** - The AI will convert spoken names like "HomeAssistant" to the correct `@home` mention
