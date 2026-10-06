---
icon: lucide/phone
---

# Voice Calls

MindRoom agents can join Element Call voice calls (MatrixRTC, also used by Element X and recent Cinny releases) in their rooms and talk with the caller in real time.
Use this page to set up calls, choose a voice backend, and find out why an agent did or did not join a call.
Voice messages (recorded audio clips) are covered on [Voice Messages](voice.md).

Each calls-enabled agent uses one of three backends:

| Backend | What it does |
|---------|--------------|
| `realtime` | OpenAI realtime speech-to-speech runs the conversation with the agent's prompt and tools. |
| `live` | OpenAI Live handles speech and delegates substantive requests to the normal MindRoom agent. |
| `cascaded` | Separate speech-to-text and text-to-speech services wrap the agent's normal MindRoom response, so any model provider works, including fully local setups. |

## Setup

1. Install the `matrix_calls` extra (`pip install "mindroom[matrix_calls]"` or `uv sync --extra matrix_calls`).
2. Make sure your Matrix deployment has a MatrixRTC backend; see [Server requirements](#server-requirements).
3. Define call profiles and assign one to each calls-enabled agent:

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
  # livekit_service_url: https://rtc.example.org
```

Calls can also be configured in the dashboard's [Voice tab](dashboard.md#voice).
A calls-enabled agent shows `📞 Voice calls` in its Matrix presence status when the extra is installed and its profile's credentials resolve, so clients can offer the call action only where it will be answered.

## Calls configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `calls.enabled` | bool | `false` | Lets agents join voice calls. |
| `calls.profiles` | map of name to profile | `{}` | Reusable, complete voice pipelines; each has a `backend` and the fields below. |
| `calls.agents` | map of agent name to profile name | `{}` | Each calls-enabled agent uses exactly one profile. |
| `calls.livekit_service_url` | string | discovered | MatrixRTC authorization service URL to use instead of discovering it from the agent homeserver's `.well-known/matrix/client`. |

`enabled` and `livekit_service_url` are global and cannot be set per agent.
Profiles have no inherited defaults and credentials never fall back to another key, so a missing credential service leaves calls unavailable rather than using a different OpenAI key.
Set a dedicated credential service when voice and chat use different OpenAI credentials.

Configuration loading fails with one of these errors when references are wrong:

- `calls.agents references unknown profile(s): ...`
- `calls.agents references unknown agent(s): ...`
- `calls.agents configures multiple agents for room(s): ...`, because a room can have at most one calls-enabled agent.
- `calls.profiles references unknown cascaded model(s): ...` or `calls.profiles references unknown Live agent model(s): ...`, when a profile's model alias is not in the top-level `models` mapping.

### Realtime profile fields

| Field | Required | Description |
|-------|----------|-------------|
| `backend` | yes | `realtime` |
| `model` | yes | OpenAI realtime speech-to-speech model ID. |
| `credentials_service` | yes | Named credential service holding the OpenAI API key. |
| `voice` | yes | Realtime voice preset. |

### Live profile fields

| Field | Required | Description |
|-------|----------|-------------|
| `backend` | yes | `live` |
| `model` | yes | OpenAI Live speech model ID, independent of `agent_model`. |
| `credentials_service` | yes | Named credential service holding the OpenAI API key; Live needs no separate STT or TTS credentials. |
| `voice` | yes | Live voice preset. |
| `agent_model` | no | Name of a top-level `models` entry used for delegated agent turns; omitted keeps normal room and agent model resolution. |

### Cascaded profile fields

| Field | Required | Description |
|-------|----------|-------------|
| `backend` | yes | `cascaded` |
| `model` | no | Name of a top-level `models` entry for call turns; it takes precedence over room and agent models, and omitting it keeps normal model resolution. |
| `stt` | yes | Speech-to-text service. |
| `tts` | yes | Text-to-speech service. |

Each speech service (`stt` and `tts`) has these fields:

| Field | Default | Description |
|-------|---------|-------------|
| `provider` | `openai` | `openai` or `openai_compatible`. |
| `model` | required | Speech model name. |
| `credentials_service` | none | Named credential service holding the API key. |
| `api_key` | none | Inline API key. |
| `host` | none | Service root or its `/v1` base URL; must be an HTTP(S) URL. |
| `extra_kwargs` | `{}` | Provider options such as `language` for STT or `voice` for TTS; must not set `api_key`, `base_url`, `client`, or `model`. |

`openai` services require `credentials_service` or `api_key`.
`openai_compatible` services require `host` and may omit credentials, in which case MindRoom sends a non-secret placeholder key.
STT services must expose `/v1/audio/transcriptions` and TTS services `/v1/audio/speech`.
The two legs may use the same credential service or different ones.

## Choosing a backend

### Realtime

The realtime model runs the whole conversation itself, using the agent's rendered prompt and its tools.
See the example in [Setup](#setup).

### Live with agent delegation

Live answers from the agent's full rendered system prompt, including its context files and caller-specific workspace, and delegates requests that need tools, research, more memory, or actions to the normal MindRoom agent, then relays the result conversationally.
If the agent's prompt plus voice guidance exceeds about 16,000 tokens (a local estimate, not a provider limit), Live instead delegates every substantive request, including questions about identity and preferences, to the normal agent.
Delegated turns keep their history while the call lasts, and each new call starts a fresh delegate session.

This example uses GPT-Live for speech and the `chat` model for delegated turns, with a voice credential independent of the agent's model provider:

```yaml
models:
  chat:
    provider: anthropic
    id: claude-opus-5-5

agents:
  assistant:
    display_name: Assistant
    model: chat

calls:
  enabled: true
  profiles:
    openai-live:
      backend: live
      model: gpt-live-1
      credentials_service: openai-live
      voice: marin
      agent_model: chat
  agents:
    assistant: openai-live
```

### Cascaded

Each finalized transcript goes through the agent's normal response path, with the same model resolution, system prompt, instructions, knowledge, skills, hooks, tools, requester identity, and history.
The profile's `model` changes only the LLM; speech services, prompts, history, memory, and tools stay the same.
Turn ends are detected locally, and the caller can interrupt the agent by speaking.

This example uses OpenAI speech services and a faster model for call turns than for text chat:

```yaml
models:
  chat:
    provider: anthropic
    id: claude-opus-5-5
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
        model: gpt-transcribe
        credentials_service: openai-voice
        extra_kwargs:
          language: en
      tts:
        provider: openai
        model: gpt-4o-mini-tts
        credentials_service: openai-voice
        extra_kwargs:
          voice: ash
  agents:
    assistant: fast-cascaded
```

OpenAI documents [`gpt-transcribe`](https://developers.openai.com/api/docs/models/gpt-transcribe) for transcription and [`gpt-4o-mini-tts`](https://developers.openai.com/api/docs/models/gpt-4o-mini-tts) for text-to-speech.

### Completely local cascaded setup

This configuration sends STT, LLM, and TTS requests only to localhost services and needs no cloud API key:

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
        model: kokoro
        host: http://127.0.0.1:9001
        extra_kwargs:
          voice: af_heart
  agents:
    assistant: local-cascaded
```

Different agents can use different profiles, for example one agent on `realtime` and another on this local profile.

## When an agent joins a call

An agent joins a call when it starts in one of the agent's configured rooms, or in an ad-hoc room whose invitation the agent accepted under its [`accept_invites`](configuration/agents.md) setting.
Ad-hoc rooms let a Matrix client create a private voice room and invite one agent without adding the room to `config.yaml`.
A configured room assignment wins over invites; if several calls-enabled agents were only invited to the same room, none of them answers calls there.

The agent stays in a call only while one Matrix user is in it (that user may join from several devices) and only while that user passes the normal room and per-agent reply permissions.
That user is the requester for tools, and requester-private agents use that user's workspace, memory, credentials, history, and knowledge.

Tools that need confirmation, user input, external execution, or [`tool_approval`](tool-approval.md) are hidden during calls in every backend, because a call has no approval UI.
[Deferred tools](tools/dynamic-tools.md) are available from the start of a call in every backend, with no search or loading step.

The agent leaves the call when the caller leaves, a second Matrix user joins, the voice session ends, or MindRoom shuts down.
A restart also drops it from the call, and so does any configuration change applied while the agent is in a call.

If the agent joins but cannot hear or speak, look for a `Voice call error:` notice in the room, which names the failing service and how to fix it.
For a rejected credential, update the credential MindRoom uses, restart MindRoom, then leave and rejoin the call; retrying with the same credential does not help.

## Calling an agent about a thread

In MindRoom Chat, start the call from a thread's header, or from the agent's profile while a thread is open.
When the agent picks up, it receives a snapshot of that conversation, plus the thread title when the thread has one.
The snapshot is the newest part of the thread that fits the call budget (about 6,000 tokens), with a note when older messages were left out.
Each message is cut to 2,000 characters.
Starting a call from a room's main timeline gives the agent the newest unthreaded messages of that room instead.

The snapshot is taken once, when the agent joins; messages sent during the call are not added.
It works with all three call profiles; with `live`, the voice model skips it when the agent's prompt leaves too little room, but the delegated agent always gets it.

The agent gets the snapshot only when you are a member of the room the call came from, the agent's `access` admits you there, and the agent has joined it.
Otherwise, or when reading the conversation takes longer than 5 seconds, the call starts without it.

When you hang up, the agent posts the call back into that conversation: as a reply in the same thread, or as a new room message for a call started from the main timeline.
The message reads `📞 Voice call · N min` with the spoken transcript collapsed underneath, so later replies in the thread know what was said.
Calls shorter than 10 seconds, calls in which the caller said nothing, calls that started without a conversation snapshot, and calls cut off by a MindRoom restart post nothing.
The transcript holds only what was said; tool use is left out.
Before posting, the agent checks the caller's access to the origin room again, and posts nothing if the caller lost that access during the call.
If the agent's media connection drops and it rejoins the same call, each part of the call posts its own message.

An agent with the `matrix_message` tool is also told how to start longer work during a call.
It sends a self-contained task to the thread or room the call came from, and names itself or another agent as `recipient`, so the work runs there while you keep talking.
A call without an origin makes it ask you which room to use.

Other Matrix clients ask for a conversation snapshot with the `origin` field of the call room's `io.mindroom.agent_call` state event:

```json
{
  "version": 1,
  "agent_user_id": "@mindroom_assistant:example.org",
  "creator_user_id": "@alice:example.org",
  "ephemeral": false,
  "origin": { "room_id": "!abc:example.org", "thread_id": "$root" }
}
```

`thread_id` is `null` when the call starts from the room's main timeline, and `origin` may be left out entirely.
The agent uses `origin` only when the caller sent the event with an empty state key and `version: 1`, with `creator_user_id` and `agent_user_id` naming the caller and this agent, `thread_id` is the thread's root event, and `room_id` is not the call room.
Write `origin` before each call, because the agent reads it when it joins.

## Transcripts and memory

Every call writes a markdown transcript as it goes.

| Agent | Transcript location |
|-------|---------------------|
| Shared, file memory | `calls/` in the agent's workspace, readable later by its file tools |
| Shared, other memory | `<storage>/calls/<agent>/` |
| Requester-private, file memory | `calls/` in the requester's workspace |
| Requester-private, other memory | `calls/` in the requester-scoped state directory |

When the call ends, file memory stores a reference to the transcript, Mem0 stores the transcript as recallable context, and `memory: none` keeps only the transcript file.

## Server requirements

Matrix carries only call signaling; audio flows through a LiveKit SFU deployed next to the homeserver.
Your Matrix deployment needs the standard Element Call backend:

- A [LiveKit SFU](https://github.com/livekit/livekit) reachable by call participants.
- The [MatrixRTC authorization service](https://github.com/element-hq/lk-jwt-service) (`lk-jwt-service`).
- A `.well-known/matrix/client` on the Matrix server-name domain that advertises the service:

```json
{
  "org.matrix.msc4143.rtc_foci": [
    { "type": "livekit", "livekit_service_url": "https://rtc.example.org" }
  ]
}
```

On Kubernetes, the [MatrixRTC chart](https://github.com/mindroom-ai/mindroom/tree/main/cluster/k8s/matrixrtc) deploys LiveKit and the authorization service, and the [client chart](https://github.com/mindroom-ai/mindroom/tree/main/cluster/k8s/client) publishes the `.well-known` announcement and proxies `/livekit/jwt` and `/livekit/sfu` to them.
LiveKit media still needs a static public IP and open TCP and UDP media ports, which the chart README describes.
Element's [self-hosting guide](https://github.com/element-hq/element-call/blob/main/docs/self_hosting.md) covers the full setup, and [matrix-docker-ansible-deploy](https://github.com/spantaleev/matrix-docker-ansible-deploy) enables it with `matrix_rtc_enabled: true`.

The agent joins only calls started on your own MatrixRTC backend, meaning the one in `calls.livekit_service_url` or the agent homeserver's `.well-known`; calls started through a different backend are ignored.
[Managed rooms](rooms.md#room-management) already let every member join calls.
In other rooms, such as ad-hoc rooms, the power levels must let members send `org.matrix.msc3401.call.member` state events, which Element Call-capable clients set up when they create rooms.
Calls work in both encrypted and unencrypted rooms.

Synapse supports the full MatrixRTC stack, including automatic cleanup of stale call memberships.
Tuwunel works with Element Call but does not yet support delayed events ([tuwunel#178](https://github.com/matrix-construct/tuwunel/issues/178)), so memberships of crashed clients linger until they expire.

## Limitations

- Audio only: the agent neither sends nor receives video or screen shares.
- One human Matrix user per call, possibly on several devices.
- Cascaded speech services must be OpenAI-compatible transcription and speech endpoints.
- Legacy 1:1 `m.call.*` calls (non-MatrixRTC) are not supported.
