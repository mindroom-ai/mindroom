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

The voice agent is the same agent you chat with: it carries the agent's regular system prompt (with a spoken-style addendum) and the same tools it has in text conversations.
Tool calls run in the configured agent's room-scoped runtime context.
MatrixRTC does not identify an individual speaker as a Matrix requester, so calls requiring `tool_approval` are refused with a spoken explanation instead of executing.

The agent leaves the call (and clears its membership state event) when the last other participant leaves, or when the bot shuts down.

## Configuration

```yaml
calls:
  enabled: true
  agents: [assistant]        # shared agents that may join calls in their configured rooms
  model: gpt-realtime-2.1    # OpenAI realtime model
  voice: marin               # optional voice preset
  # livekit_service_url: https://rtc.example.org   # same-server .well-known override
```

Voice calls require the `matrix_calls` extra (`pip install "mindroom[matrix_calls]"` or `uv sync --extra matrix_calls`) and an `OPENAI_API_KEY` in your credentials.
MindRoom enforces at most one calls-enabled agent per room.
Calls only join rooms configured for that agent and only while every call participant passes the normal room and per-agent reply permissions.
Requester-private agents cannot join calls because MatrixRTC cannot bind each spoken turn to a Matrix requester.

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

MindRoom follows MatrixRTC's sticky oldest-membership focus selection, including a focus inherited from a departed founder.
Same-server focus URLs must match trusted local discovery or the configured override, while federated focuses require HTTPS and use server-side fetch guards that reject private, local, and metadata endpoints before sending the short-lived Matrix OpenID identity token.

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

Every call writes a markdown transcript incrementally: agents using file-backed memory keep it in `calls/` inside their canonical workspace (readable through their file tools later), while other agents use `<storage>/calls/<agent>/`.
When the call ends, the agent appends a one-line summary with the transcript location to its daily memory.

## Limitations

- Audio only: the agent neither publishes nor consumes video and screen shares.
- Legacy 1:1 `m.call.*` calls (non-MatrixRTC) are not supported.
