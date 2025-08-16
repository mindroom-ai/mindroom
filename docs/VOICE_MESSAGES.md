# Voice Message Support in MindRoom

MindRoom now supports automatic transcription and intelligent processing of voice messages! When users send voice messages in Matrix, MindRoom can:

1. **Transcribe the audio** using speech-to-text (STT) services
2. **Intelligently format** the transcription with proper commands and agent mentions
3. **Process the message** as if it was typed by the user

## Features

- 🎤 **Automatic Transcription**: Voice messages are automatically converted to text
- 🤖 **Smart Command Recognition**: Natural speech like "schedule a meeting tomorrow" becomes `!schedule meeting tomorrow`
- 👥 **Agent Name Detection**: "Ask the research agent" becomes `@research`
- 🏢 **Team Recognition**: "Get help from the code team" becomes `@code_team`
- 🔒 **Privacy-First**: Supports both cloud and self-hosted STT services
- 🌍 **Multi-Language**: Supports any language supported by your STT provider

## Configuration

Add the following to your `config.yaml`:

```yaml
voice:
  enabled: true

  # Speech-to-text configuration
  stt:
    provider: openai
    model: whisper-1
    # api_key: your-api-key  # Optional, uses OPENAI_API_KEY env var by default
    # host: http://localhost:8080  # For self-hosted STT services

  # Intelligence configuration for command recognition
  intelligence:
    model: default  # Uses your default AI model
    confidence_threshold: 0.7
```

### Using OpenAI Whisper (Cloud)

```yaml
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
    api_key: ${OPENAI_API_KEY}  # Or set directly
```

### Using Self-Hosted STT (LocalAI, Ollama, etc.)

Many self-hosted solutions provide OpenAI-compatible APIs:

```yaml
voice:
  enabled: true
  stt:
    provider: openai  # Most self-hosted solutions are OpenAI-compatible
    model: whisper-1
    host: http://localhost:8080/v1  # Your local STT service URL
```

### Using a Different AI Model for Intelligence

You can use a more powerful model for better command recognition:

```yaml
voice:
  enabled: true
  stt:
    provider: openai
    model: whisper-1
  intelligence:
    model: gpt4o  # Use a more capable model
    confidence_threshold: 0.8
```

## How It Works

1. **User sends a voice message** in any Matrix room where MindRoom is present
2. **MindRoom downloads** the audio file (supports both encrypted and unencrypted)
3. **Audio is transcribed** using your configured STT service
4. **AI processes the transcription** to:
   - Identify commands (`!schedule`, `!invite`, etc.)
   - Recognize agent names and convert to mentions (`@research`, `@code`)
   - Format the message properly for Matrix
5. **Formatted message is sent** as a reply to the original voice message
6. **Normal processing continues** as if the user had typed the message

## Examples

### Voice Input → Formatted Output

| What You Say | What MindRoom Sends |
|--------------|---------------------|
| "Hey research agent, can you help me?" | "🎤 @research can you help me?" |
| "Schedule a meeting tomorrow at 3pm" | "🎤 !schedule meeting tomorrow at 3pm" |
| "Ask the code team to review this" | "🎤 @code_team please review this" |
| "List all my scheduled tasks" | "🎤 !list_schedules" |
| "Can someone help me with math?" | "🎤 @calculator can someone help me with math?" |

## Privacy and Security

- **Audio files are deleted** immediately after transcription
- **Local processing option**: Use self-hosted STT for complete privacy
- **No audio storage**: Audio is never saved to disk permanently
- **Per-room control**: Can be enabled/disabled per room (coming soon)
- **Encrypted audio support**: Works with Matrix's end-to-end encryption

## Supported Audio Formats

MindRoom supports voice messages from:
- Matrix Element (audio/mp4)
- WhatsApp bridges (audio/ogg)
- Telegram bridges (various formats)
- Discord bridges (various formats)

## Troubleshooting

### Voice messages not being processed

1. Check that `voice.enabled: true` in your config
2. Verify your STT API credentials are correct
3. Check the logs for any error messages

### Poor transcription quality

1. Consider using a larger Whisper model (base, small, medium, large)
2. Ensure audio quality is good
3. Check if the language is supported by your STT model

### Commands not recognized properly

1. Increase the AI model capability in `intelligence.model`
2. Adjust `confidence_threshold` (lower = more permissive)
3. Speak more clearly when mentioning agent names or commands

## Limitations

- Voice messages are processed by the router agent only (to avoid duplicates)
- Transcription quality depends on audio quality and STT model
- Some accents or languages may require specific STT model configuration
- Very long voice messages may hit API limits

## Future Enhancements

- [ ] Text-to-speech responses (agents can respond with voice)
- [ ] Per-room voice settings
- [ ] Voice command shortcuts
- [ ] Speaker identification
- [ ] Real-time transcription for long messages
- [ ] Custom wake words for agent activation
