# Voice Messages

This page covers Matrix voice messages sent to agents and teams: speech-to-text (STT) setup, transcript cleanup, what appears in the room, and what happens without STT.
For live spoken conversations in Matrix calls, see [Voice Calls](https://docs.mindroom.chat/voice-calls/).

<video controls playsinline preload="metadata" aria-label="A voice message becomes a reminder" style="width: 100%" poster="https://github.com/user-attachments/assets/d8829350-12c9-4045-aed0-4a5a5127213c" data-poster-light="https://github.com/user-attachments/assets/d8829350-12c9-4045-aed0-4a5a5127213c" data-poster-dark="https://github.com/user-attachments/assets/9ec1837a-3566-4b0c-8274-7af7c30b988c">
  <source src="https://github.com/user-attachments/assets/823df173-50ae-4304-b652-36a61bfbc911" type="video/mp4" media="(prefers-color-scheme: dark)">
  <source src="https://github.com/user-attachments/assets/f048c8cc-3384-41e2-be59-000d3ab22099" type="video/mp4">
</video>

With voice enabled, MindRoom transcribes each voice message, cleans up the transcript, and handles it like a text message prefixed with `🎤`.
The responder also receives the original audio as an attachment.
Without STT, voice messages still reach responders as raw audio; see [Voice Fallback](#voice-fallback-no-stt-available).

## Setup

Configure voice in `config.yaml` or in the dashboard's Voice tab.

### OpenAI transcription

```yaml
voice:
  enabled: true
  stt:
    provider: openai
    model: gpt-transcribe
  intelligence:
    model: default  # Model used for mention normalization and light ASR cleanup
```

Cloud OpenAI transcription uses the `openai` credential service by default, which resolves a stored OpenAI credential or `OPENAI_API_KEY`.
Set `voice.stt.api_key` or a different `voice.stt.credentials_service` to use another key.

### Self-hosted Whisper

Any service that implements the OpenAI-compatible `/v1/audio/transcriptions` endpoint works, such as [faster-whisper-server](https://github.com/fedirz/faster-whisper-server):

```yaml
voice:
  enabled: true
  stt:
    provider: openai_compatible
    model: whisper-1
    host: http://localhost:8080
    api_key: your-custom-api-key  # Optional
```

`host` may be the service root or its `/v1` base URL.
Without `api_key` or `credentials_service`, MindRoom sends a non-secret placeholder key, so keyless local servers work and your OpenAI key is never sent to them.

## Configuration Reference

| Field | Default | Description |
|-------|---------|-------------|
| `voice.enabled` | `false` | Transcribe voice messages; when `false`, voice messages use the [raw-audio fallback](#voice-fallback-no-stt-available). |
| `voice.visible_router_echo` | `true` | Let the router show transcription progress and the transcript in the room; see [Voice Message Processing](#voice-message-processing). |
| `voice.stt.provider` | `openai` | `openai` or `openai_compatible`. |
| `voice.stt.model` | `gpt-transcribe` | STT model name. |
| `voice.stt.host` | none | Service root or `/v1` base URL; must be an HTTP(S) URL. Required for `openai_compatible`. |
| `voice.stt.api_key` | none | Inline API key. |
| `voice.stt.credentials_service` | `openai` for `provider: openai`, otherwise none | Named credential service holding the API key. |
| `voice.stt.extra_kwargs` | `{}` | Extra form fields sent with each transcription request, such as `language`; must not set `api_key`, `base_url`, `client`, or `model`. |
| `voice.intelligence.model` | `default` | Name of a `models` entry used for transcript cleanup. |

`provider: openai` requires `api_key` or `credentials_service`, and `provider: openai_compatible` requires `host`.

Each transcription request has a 60-second limit and is not retried.
On timeout or service error, the message uses the raw-audio fallback.

## Voice Message Processing

What appears in the room depends on whether the router is present and whether the responder is already clear.
Voice messages follow the same mention, thread, and access rules as text from the original sender.

**Router echo.**
With `voice.enabled: true` and `voice.visible_router_echo: true`, the router immediately posts `Router agent is transcribing…` and then replaces it with the cleaned transcript, or with the fallback text if transcription failed.
With `voice.enabled: false`, the router posts the fallback text directly instead.
The echo is display-only: agents answer the original voice message, not the echo.
The echo appears only when the router is in the room and allowed to reply to the sender.
Set `voice.visible_router_echo: false` to hide it; this does not change who answers.

**Responder already clear.**
If only one agent or team can respond, the transcript or caption mentions one, or thread context picks one, that responder answers the voice message directly without a router handoff.

**Router must choose.**
If several agents or teams can respond and none is targeted, the router routes on the transcript and posts a normal handoff such as `@home could you help with this?`.
The chosen responder answers that handoff and still receives the original audio attachment.

**No router, or router cannot reply to the sender.**
Agents and teams handle the voice message directly with the same rules as text, and no router echo appears.
If several responders remain and none is targeted, nobody answers until the user mentions an agent or team.

## Transcript Normalization

The `voice.intelligence.model` cleans up each raw transcript before dispatch:

- Spoken agent or team names become `@agent` or `@team` mentions, using display names such as "HomeAssistant" to produce `@home`.
- Mentions of agents or teams not available to the sender in the room lose their `@`, so they do not trigger a response.
- Spoken text is never rewritten into `!command` syntax.
- Light speech-recognition errors are corrected.

If cleanup fails or times out, MindRoom uses the raw transcript.
Choose a faster or stronger model in `voice.intelligence.model` to trade cleanup speed against accuracy.

## Voice Fallback (No STT Available)

When voice is disabled, STT is unavailable, or transcription fails, the voice message is dispatched as `🎤 [Attached voice message]`, or as `🎤` followed by the message caption when it has one.
The audio is registered as an attachment, so responders with audio-capable models receive it directly and tool-using responders can fetch it with the `attachments` tool.
Attachment IDs follow the context-scoping rules in [File & Video Attachments](https://docs.mindroom.chat/attachments/).
If the audio cannot be downloaded from Matrix, the fallback text is dispatched without an attachment.

Without a transcript, routing has little text to work with, so use explicit `@mentions` or reply in an existing thread in rooms with several responders.

## Limitations and Tips

- Only OpenAI-compatible STT APIs are supported.
- Audio quality and background noise affect transcription accuracy.
- Say the agent or team name first, for example "Hey assistant, what's the weather?"

## Text-to-Speech

Agents can also send spoken replies, which is separate from voice message transcription.
The `matrix_voice_message` tool sends a playable Matrix voice note to the current room or thread; see [Matrix & Attachments tools](https://docs.mindroom.chat/tools/matrix-and-attachments/).
The `openai`, `eleven_labs`, `cartesia`, and `groq` tools generate speech audio; see [AI & Generation tools](https://docs.mindroom.chat/tools/ai-and-generation/).
