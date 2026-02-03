---
icon: lucide/mic
---

# Voice Messages

MindRoom can process voice messages sent to Matrix rooms, transcribing them and responding appropriately.

## Overview

When voice message handling is enabled:

1. Voice messages are detected in Matrix rooms
2. Audio is sent to a speech-to-text (STT) service
3. Transcription is analyzed for agent mentions and commands
4. The appropriate agent responds

## Configuration

Enable voice in `config.yaml`:

```yaml
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
    # Optional: custom endpoint
    # host: http://localhost:8080/v1
  intelligence:
    model: default
    confidence_threshold: 0.7
```

Or use the dashboard's Voice tab.

## STT Providers

### OpenAI Whisper (Cloud)

```yaml
voice:
  stt:
    provider: openai
    model: whisper-1
```

Requires `OPENAI_API_KEY` environment variable.

### Self-Hosted Whisper

```yaml
voice:
  stt:
    provider: openai
    model: whisper-1
    host: http://localhost:8080/v1
```

Use with [faster-whisper-server](https://github.com/fedirz/faster-whisper-server) or similar.

## Command Recognition

The intelligence component analyzes transcriptions:

1. **Agent mentions** - Detects "@agent" patterns
2. **Command patterns** - Identifies `!command` syntax
3. **Confidence scoring** - Filters low-confidence transcriptions

### Confidence Threshold

```yaml
voice:
  intelligence:
    confidence_threshold: 0.7  # 0.0 to 1.0
```

Lower values accept more transcriptions (including potential errors). Higher values require clearer speech.

## How It Works

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Voice Msg   │────▶│ Transcribe  │────▶│ Analyze     │
│ (Audio)     │     │ (STT)       │     │ (LLM)       │
└─────────────┘     └─────────────┘     └─────────────┘
                                              │
                                              ▼
                                        ┌─────────────┐
                                        │ Route to    │
                                        │ Agent       │
                                        └─────────────┘
```

## Matrix Integration

Voice messages in Matrix are:

- Detected by file type (`audio/ogg`, `audio/webm`, etc.)
- Downloaded from the Matrix media server
- Decrypted if end-to-end encrypted
- Sent to the STT service

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENAI_API_KEY` | For OpenAI Whisper API |

## Limitations

- Audio quality affects transcription accuracy
- Very long messages may be truncated
- Background noise can reduce accuracy
- Some accents may have lower accuracy

## Best Practices

1. **Speak clearly** - Enunciate agent names
2. **Reduce background noise** - Improves accuracy
3. **Say the agent name first** - "Hey @assistant, what's the weather?"
4. **Keep messages focused** - One request per message
