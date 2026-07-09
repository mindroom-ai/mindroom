# Voice Calls

MindRoom agents can join Element Call voice calls in their rooms and talk with you in real time.
The agent speaks through an OpenAI realtime speech-to-speech model (`gpt-realtime-2.1` by default), so there is no separate transcription and TTS pipeline and you can interrupt it naturally mid-sentence.

## How it works

Matrix group calls (MatrixRTC, used by Element Call, Element X, and recent Cinny releases) do not send media over Matrix itself.
Matrix only carries the signaling: participants publish `org.matrix.msc3401.call.member` state events, and media flows through a LiveKit SFU that is deployed next to the homeserver.

When a call starts in a room, the configured agent:

1. Sees the call membership state event and re-reads the room state.
2. Exchanges a Matrix OpenID token for a LiveKit JWT at the MatrixRTC authorization service (`lk-jwt-service`).
3. Connects to the LiveKit SFU and publishes its own call membership state event, so it appears in the call roster.
4. In encrypted rooms, distributes its media frame key over encrypted to-device messages and installs the other participants' keys, following the same per-sender key rotation policy as Element Call.
5. Runs an OpenAI realtime session on the call audio until everyone else leaves.

The voice agent uses the configured display name, role, and instructions with a spoken-style addendum.
Voice calls do not expose agent tools because MatrixRTC media does not identify an individual speaker as an authenticated Matrix requester, so MindRoom cannot safely apply requester-scoped authorization or credentials.

The agent leaves the call (and clears its membership state event) when the last other participant leaves, or when the bot shuts down.

## Configuration

```yaml
calls:
  enabled: true
  agents: [assistant]        # which agents may join calls in their rooms
  model: gpt-realtime-2.1    # OpenAI realtime model
  voice: marin               # optional voice preset
  # livekit_service_url: https://rtc.example.org   # override .well-known discovery
```

Voice calls require the `matrix_calls` extra (`pip install "mindroom[matrix_calls]"` or `uv sync --extra matrix_calls`) and an `OPENAI_API_KEY` in your credentials.
MindRoom enforces at most one calls-enabled agent per room.

## Server requirements

Your Matrix deployment needs the standard Element Call backend:

- A [LiveKit SFU](https://github.com/livekit/livekit) reachable by call participants.
- The [MatrixRTC authorization service](https://github.com/element-hq/lk-jwt-service) (`lk-jwt-service`) that exchanges Matrix OpenID tokens for LiveKit JWTs.
- The homeserver's `.well-known/matrix/client` must advertise the service:

```json
{
  "org.matrix.msc4143.rtc_foci": [
    { "type": "livekit", "livekit_service_url": "https://rtc.example.org" }
  ]
}
```

Element's [self-hosting guide](https://github.com/element-hq/element-call/blob/livekit/docs/self-hosting.md) covers the full setup, and [matrix-docker-ansible-deploy](https://github.com/spantaleev/matrix-docker-ansible-deploy) enables all of it with `matrix_rtc_enabled: true`.

MindRoom uses this configured or homeserver-discovered service URL only and never trusts a call member's advertised URL when exchanging its OpenID token.

The room's power levels must allow members to send `org.matrix.msc3401.call.member` state events (Element Call-capable clients set this up when they create rooms).

## Homeserver notes

- Synapse supports the full MatrixRTC stack, including MSC4140 delayed events for automatic membership cleanup.
- Tuwunel works with Element Call but does not support delayed events yet ([tuwunel#178](https://github.com/matrix-construct/tuwunel/issues/178)), so memberships of crashed clients linger until their `expires` window passes.

## Encrypted rooms

Element Call encrypts call media with per-sender frame keys distributed over olm-encrypted to-device messages.
MindRoom sends its own frame key this way, so participants can always hear the agent.
Hearing the participants in an encrypted room additionally requires a mindroom-nio release that surfaces unknown decrypted to-device events ([mindroom-nio#5](https://github.com/mindroom-ai/mindroom-nio/pull/5)); without it the agent joins but cannot decrypt inbound audio.
Calls in unencrypted rooms need none of this and work with plain SFU media.

## Transcripts and memory

Every call writes a markdown transcript incrementally: agents with a private workspace keep it in `calls/` inside their workspace (readable through their file tools later), other agents under `<storage>/calls/<agent>/`.
When the call ends, the agent appends a one-line summary with the transcript location to its daily memory.

## Limitations

- Audio only: the agent neither publishes nor consumes video and screen shares.
- Legacy 1:1 `m.call.*` calls (non-MatrixRTC) are not supported.
- Agent tools are unavailable in calls.
