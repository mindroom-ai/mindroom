# Matrix Voice Message Tool Design

## Goal

Agents can send a Matrix voice message from one tool call by giving text to synthesize.
They should not need to call a TTS tool, persist an audio file, register it as an attachment, and send that attachment separately.

## Approach

Add a focused `matrix_voice_message` builtin tool.
It uses OpenAI text-to-speech for the audio generation step and the existing Matrix delivery stack for upload, encryption payloads, thread fallback, and conversation-cache notification.
This keeps provider scope narrow and avoids turning `matrix_message` into a TTS-aware tool.

## Tool API

`matrix_voice_message(text, room_id=None, thread_id=None, caption=None, companion_message=None)` sends synthesized speech to Matrix.
`text` is required and becomes the spoken content.
`caption` is optional and becomes the Matrix event body.
`companion_message` is optional and sends a normal Matrix text event to the same room and thread before the voice event.
This is useful when the agent wants both readable text and audio in one tool call.
`caption` is not a normal Matrix text message and should stay a short audio label or description.
When `caption` is omitted, the body is a short default filename-style label so clients display a normal audio event.
`room_id` defaults to the current room.
`thread_id` defaults to the current thread when one is active, and `thread_id="room"` forces a room-level voice message.

## Configuration

The builtin tool metadata exposes OpenAI API key, TTS model, voice, and format fields.
Defaults reuse `OPENAI_TTS`, `alloy`, and `mp3`.
The implementation stays OpenAI-only for this feature so the first version is small and testable.

## Matrix Delivery

Add an in-memory audio send helper in `matrix/client_delivery.py`.
It reuses the same upload and encryption logic used by `send_file_message`.
The sent content uses `msgtype: m.audio`, includes MIME and size metadata, and adds `org.matrix.msc3245.voice` so Matrix clients can render it as a voice note.
When `companion_message` is provided, the tool reuses the existing Matrix message operation path to send normal text to the same room/thread before sending the voice note.
Threaded sends use existing fallback relation behavior and require the same latest-thread-event lookup as file sends.

## Runtime Behavior

The tool resolves Matrix runtime context through `get_tool_runtime_context`.
It checks room authorization with existing `room_access_allowed`.
It rate-limits sends using the same `check_rate_limit` helper pattern as `matrix_message`.
Speech generation runs off the event loop because the OpenAI client call is synchronous.

## Errors

Missing runtime context returns a structured tool error.
Empty text returns a structured tool error.
Unauthorized rooms return a structured tool error.
TTS failures and Matrix delivery failures return structured tool errors with no secret values.
If companion text was sent before a later TTS or voice-delivery failure, the error payload includes `companion_event_id`.

## Tests

Add tests for the in-memory Matrix audio send helper.
Add tests for the `matrix_voice_message` tool covering validation, targeting, TTS invocation, Matrix send payload, and metadata docs.
Add tests for companion text delivery to the same thread and for voice-delivery failure after companion text succeeds.
Add metadata registration tests through the existing tool registry behavior where useful.
