# Voice Calls

MindRoom agents can join Element Call voice calls in their rooms and talk with you in real time.
The `realtime` backend provides OpenAI realtime speech-to-speech.
The `cascaded` backend combines independently configured speech-to-text and text-to-speech services with the agent's normal MindRoom response path.

## How it works

Matrix group calls (MatrixRTC, used by Element Call, Element X, and recent Cinny releases) do not send media over Matrix itself.
Matrix only carries the signaling: participants publish `org.matrix.msc3401.call.member` state events, and media flows through a LiveKit SFU that is deployed next to the homeserver.

When a call starts in a room, the configured agent:

1. Sees the call membership state event and re-reads the room state.
2. Exchanges a Matrix OpenID token for a LiveKit JWT at the MatrixRTC authorization service (`lk-jwt-service`).
3. Connects to the LiveKit SFU and publishes its own call membership state event, so it appears in the call roster.
4. In encrypted rooms, distributes its media frame key over encrypted to-device messages and installs the other participants' keys, following the same per-sender key rotation policy as Element Call.
5. Runs either the OpenAI realtime session or the cascaded speech pipeline until the managed session ends.

The voice agent is the same agent you chat with.
Realtime carries the agent's rendered prompt and effective tools into OpenAI Realtime.
Cascaded sends each finalized transcript through the normal MindRoom agent response path, preserving model resolution, the rendered system prompt, instructions, knowledge, skills, hooks, tools, requester identity, history storage, and tool execution behavior.
A cascaded profile may explicitly override the resolved LLM while preserving all other agent behavior.
MindRoom keeps a managed call active only while its devices belong to one Matrix user, and uses that user's ID as the real requester for the room-scoped tool runtime.
Multiple devices belonging to that same user are supported.
Tools requiring confirmation, user input, external execution, or `tool_approval` are hidden in both backends because a voice call has no approval UI.

The agent leaves the call and clears its membership state event when the caller leaves, a second distinct Matrix user joins, the voice session terminates, or the bot shuts down.

## Configuration

```yaml
calls:
  enabled: true
  profiles:
    openai-realtime:
      backend: realtime
      model: gpt-realtime-2.1
      credentials_service: openai-voice
      voice: marin
  agents:
    assistant: openai-realtime
  # livekit_service_url: https://rtc.example.org   # same-server .well-known override
```

Voice calls require the `matrix_calls` extra (`pip install "mindroom[matrix_calls]"` or `uv sync --extra matrix_calls`).
`calls.profiles` defines reusable, complete voice pipelines.
`calls.agents` maps each enabled agent to exactly one profile name.
The `model` in a realtime profile is the provider-specific OpenAI realtime speech model ID.
Cascaded profiles require complete STT and TTS settings, and each speech leg selects its own credential service.
The optional `model` in a cascaded profile references a named entry from the top-level `models` mapping.
An explicit cascaded call model takes precedence over room and agent models for the calls-enabled agent.
Omitting it keeps the existing room and agent model resolution.
There are no inherited backend defaults or credential fallbacks.
`enabled` and `livekit_service_url` remain global and cannot be overridden per agent.
Set a dedicated credential service when voice and chat use different OpenAI credentials.
A missing selected service does not fall back to another key.
For example, these two agents use independent voice pipelines:

```yaml
calls:
  enabled: true
  profiles:
    openai-realtime:
      backend: realtime
      model: gpt-realtime-2.1
      credentials_service: openai-voice
      voice: marin
    local-cascaded:
      backend: cascaded
      stt:
        provider: openai_compatible
        model: whisper-large-v3
        host: http://127.0.0.1:9000
      tts:
        provider: openai_compatible
        model: kokoro
        host: http://127.0.0.1:9001
        extra_kwargs:
          voice: af_heart
  agents:
    concierge: openai-realtime
    local_assistant: local-cascaded
```

MindRoom enforces at most one calls-enabled agent per room.
Calls only join rooms configured for that agent and only while the sole caller passes the normal room and per-agent reply permissions.
Calls-enabled agents also join calls in ad-hoc rooms they accepted through their normal authorized-invite policy. This lets Matrix clients create a private, temporary voice room and invite one agent without adding that room to `config.yaml` first.
Calls-enabled agents advertise `📞 Voice calls` in their Matrix presence status only when their MatrixRTC runtime is available, so clients can show the call action only where it will be answered.
Requester-private agents use the sole authorized caller's verified Matrix user ID to resolve their workspace, memory, credentials, history, knowledge, and tool execution scope.
The same caller scope applies in configured rooms and authorized ad-hoc invite rooms.

## Cascaded LLM model override

This example uses a large model for text conversations and a faster model for cascaded call turns.
The override changes only the calls-enabled agent's LLM selection, while STT, TTS, prompts, history, memory, tools, and delegated-agent model selection remain unchanged.

```yaml
models:
  chat:
    provider: anthropic
    id: claude-opus-4-8
  call_fast:
    provider: anthropic
    id: claude-haiku-4-5

agents:
  assistant:
    display_name: Assistant
    model: chat

calls:
  enabled: true
  profiles:
    fast-cascaded:
      backend: cascaded
      model: call_fast
      stt:
        provider: openai
        model: gpt-4o-transcribe
        credentials_service: openai-voice
      tts:
        provider: openai
        model: tts-1
        credentials_service: openai-voice
        extra_kwargs:
          voice: ash
  agents:
    assistant: fast-cascaded
```

The configured `call_fast` alias is validated when MindRoom loads the configuration.
An unknown alias fails configuration loading instead of falling back to the normal agent model.

## Cascaded cloud example

This example omits the cascaded `model` override and uses OpenAI speech services while the agent keeps its normally resolved model.
The agent can therefore use Anthropic, Gemini, OpenAI, or any other MindRoom model provider without changing the call configuration.

```yaml
calls:
  enabled: true
  profiles:
    openai-cascaded:
      backend: cascaded
      stt:
        provider: openai
        model: gpt-4o-transcribe
        credentials_service: openai-voice
        extra_kwargs:
          language: en
      tts:
        provider: openai
        model: tts-1
        credentials_service: openai-voice
        extra_kwargs:
          voice: ash
  agents:
    assistant: openai-cascaded
```

Each speech component has its own `provider`, `model`, `credentials_service`, `host`, and `extra_kwargs`.
The two speech legs may select the same named credential or different ones.
The current OpenAI model catalog documents [`gpt-4o-transcribe`](https://developers.openai.com/api/docs/models/gpt-4o-transcribe) for transcription and [`tts-1`](https://developers.openai.com/api/docs/models/tts-1) for text-to-speech.

## Completely local example

This configuration sends STT, LLM, and TTS requests only to separately managed localhost services and needs no real cloud API key.
The STT service must expose an OpenAI-compatible `/v1/audio/transcriptions` endpoint.
The TTS service must expose an OpenAI-compatible `/v1/audio/speech` endpoint, such as a narrow local Kokoro server using a configured model alias.

```yaml
models:
  default:
    provider: openai
    id: local-chat-model
    extra_kwargs:
      api_key: sk-no-key-required
      base_url: http://127.0.0.1:9292/v1

memory: none

agents:
  assistant:
    display_name: Assistant
    role: Help the caller.
    rooms: [lobby]

calls:
  enabled: true
  profiles:
    local-cascaded:
      backend: cascaded
      stt:
        provider: openai_compatible
        model: whisper-large-v3
        host: http://127.0.0.1:9000
        extra_kwargs:
          language: en
      tts:
        provider: openai_compatible
        model: tts-1
        host: http://127.0.0.1:9001
        extra_kwargs:
          voice: ash
  agents:
    assistant: local-cascaded
```

`host` accepts either the service root or its `/v1` base URL.
MindRoom supplies a non-secret placeholder key when an OpenAI-compatible speech endpoint has no configured key.
LiveKit's local Silero VAD controls turn boundaries and barge-in, and preemptive agent generation stays disabled so an interrupted or speculative turn cannot execute tools twice.

## Server requirements

Your Matrix deployment needs the standard Element Call backend:

- A [LiveKit SFU](https://github.com/livekit/livekit) reachable by call participants.
- The [MatrixRTC authorization service](https://github.com/element-hq/lk-jwt-service) (`lk-jwt-service`) that exchanges Matrix OpenID tokens for LiveKit JWTs.
- The Matrix server-name domain's `.well-known/matrix/client` must advertise the service:

```json
{
  "org.matrix.msc4143.rtc_foci": [
    { "type": "livekit", "livekit_service_url": "https://rtc.example.org" }
  ]
}
```

Element's [self-hosting guide](https://github.com/element-hq/element-call/blob/livekit/docs/self-hosting.md) covers the full setup, and [matrix-docker-ansible-deploy](https://github.com/spantaleev/matrix-docker-ansible-deploy) enables all of it with `matrix_rtc_enabled: true`.

MindRoom joins only calls whose oldest membership advertises the locally configured or discovered MatrixRTC focus.
It does not connect the server-hosted agent to participant-selected remote focuses; remaining participants may still inherit and advertise the trusted local focus after the original founder leaves.

The room's power levels must allow members to send `org.matrix.msc3401.call.member` state events (Element Call-capable clients set this up when they create rooms).

## Homeserver notes

- Synapse supports the full MatrixRTC stack, including MSC4140 delayed events for automatic membership cleanup.
- Tuwunel works with Element Call but does not support delayed events yet ([tuwunel#178](https://github.com/matrix-construct/tuwunel/issues/178)), so memberships of crashed clients linger until their `expires` window passes.

## Encrypted rooms

Element Call encrypts call media with per-sender frame keys distributed over olm-encrypted to-device messages.
MindRoom sends its own frame key this way, so participants can always hear the agent.
Hearing the participants in an encrypted room requires mindroom-nio 0.27.0 or newer, which surfaces unknown decrypted to-device events and is required by MindRoom's dependency metadata ([mindroom-nio#5](https://github.com/mindroom-ai/mindroom-nio/pull/5)).
Calls in unencrypted rooms need none of this and work with plain SFU media.

## Transcripts and memory

Every call writes a markdown transcript incrementally.
Shared file-memory agents keep it under `calls/` in their canonical workspace, where their file tools can read it later; other shared agents keep it under `<storage>/calls/<agent>/`.
Requester-private file-memory agents keep transcripts in their requester-scoped workspace, while other requester-private agents keep them in their requester-scoped state archive.
When the call ends, file memory stores a relative transcript reference, Mem0 stores the transcript as recallable context, and disabled memory leaves only the transcript file.

## Limitations

- Audio only: the agent neither publishes nor consumes video and screen shares.
- Managed agent calls support one distinct human Matrix user at a time, although that user may join from multiple devices.
- Cascaded speech services currently use OpenAI-compatible transcription and speech endpoints.
- Legacy 1:1 `m.call.*` calls (non-MatrixRTC) are not supported.
