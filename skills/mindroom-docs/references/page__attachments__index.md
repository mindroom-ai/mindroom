# Attachments

MindRoom can process files, images, audio, and videos sent to Matrix rooms, passing them to agents and teams for analysis or action.
Supported attachment kinds: `audio`, `file`, `image`, `video`.

## Overview

When a user sends a file, image, audio message, or video in a Matrix room:

1. The responder determines whether it should answer (via mention, thread participation, or DM)
2. The media is downloaded and decrypted (if E2E encrypted)
3. The file is saved locally and registered as a context-scoped attachment
4. The responder receives the media as an Agno `File`, `Video`, `Audio`, or `Image` object plus an attachment ID it can reference in tool calls
5. The responder replies with its analysis or takes action on the file

Attachment support works automatically for agents and teams -- no configuration is needed.

## How It Works

```
┌──────────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ File/Image/Audio │────>│ Download &  │────>│ Register    │────>│ Pass to AI  │
│ /Video (Matrix)  │     │ Decrypt     │     │ Attachment  │     │ Model       │
└──────────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                                                  │
                                                                  v
                                                            ┌─────────────┐
                                                            │ Responder   │
                                                            │ Replies     │
                                                            └─────────────┘
```

## Usage

Send a file, image, audio message, or video in a Matrix room and mention the agent or team in the caption:

- **With caption**: `@assistant Summarize this document` -- the caption is used as the prompt
- **Without caption**: The agent receives `[Attached file]`, `[Attached image]`, `[Attached audio]`, or `[Attached video]` as the prompt
- **Bare filename**: If the body is just the filename (e.g., `report.pdf`), it is treated the same as no caption

Attachments work in both direct messages and threads, and with both individual agents and teams.

## Attachment IDs

Each registered file, image, audio clip, or video is assigned a stable attachment ID (e.g., `att_abc123`).
Attachments sent with the current message are listed in the prompt with full provenance (kind, filename, sender, send time, and originating event ID):

```
Attachments sent with the current message (use tool calls to inspect or process them by ID):
- att_abc123 (image, "car.jpg", from @user:example.org, sent 2026-06-06 09:00 UTC, event $abc)
```

Current-turn attachments are offered to the model as inline media.
Earlier attachments stay in chronological position through inline annotations, but their media bytes are not replayed to the provider:

```text
@user:example.org: check this out
[attachments: att_def456 (image, "house.jpg")]
```

Agents can use the annotation's attachment ID with tools when they need to inspect historical media again.

Attachment IDs are **context-scoped** -- an attachment registered in one room or thread is not accessible from another.
This prevents cross-room data leakage for ID-based access.
When voice media download and registration succeed, raw-audio fallback uses the same attachment ID mechanism; see [Voice Fallback](https://docs.mindroom.chat/voice/#voice-fallback-no-stt-available).

## The `attachments` Tool

Agents can use the optional `attachments` tool to interact with context-scoped attachments programmatically.

### Enabling

Add `attachments` to the agent's tool list:

```yaml
agents:
  assistant:
    tools:
      - attachments
```

### Operations

| Operation | Description |
|-----------|-------------|
| `list_attachments(target?)` | List metadata for attachments in the current context (ID, kind, local_path, filename, MIME type, size, room_id, thread_id, sender, event_timestamp, created_at) |
| `get_attachment(attachment_id, mindroom_output_path?, view=False)` | Return metadata, save bytes to a workspace-relative path, or send media/document content to the model with `view=True` |
| `register_attachment(file_path)` | Register a local file path as a context attachment ID (`att_*`) |

By default, `get_attachment()` returns the attachment metadata response, including the runtime-local `local_path`.
Use `get_attachment("att_...", view=True)` to inspect media from earlier in the conversation or a local file registered with `register_attachment(file_path)`.
This sends the attachment bytes to the configured model, using native image, audio, video, or document inputs rather than putting binary data in tool text.
It supports PNG, JPEG, GIF, and WebP images, audio, video, and Agno-supported document types including PDF and plain text, with a 20 MiB limit per attachment.
Image format is detected from the bytes; other media uses the attachment's MIME type and filename.
The selected model and its provider adapter must support the media type and may impose stricter format or size limits.
The attachment must be available in the current context and have a readable local file.
`view=True` cannot be combined with `mindroom_output_path`.
If the provider rejects inline media, MindRoom retries the request without it and explicitly tells the agent that the removed content was not inspected.
Known adapter omissions use the same guidance, so unsupported media is not silently dropped.
The agent can then call `get_attachment` without `view` to obtain metadata or save the file, and use other available extraction, transcription, or analysis tools.
This does not provision another model, grant credentials, or automatically delegate the task.
For worker-routed agents, prefer `get_attachment("att_...", mindroom_output_path="incoming/file.ext")` before processing an attachment with `file`, `coding`, `python`, or `shell`, because the runtime-local path may not exist inside the worker workspace.
`mindroom_output_path` must be a file path relative to the agent workspace.
It must not be empty, absolute, point at the workspace root, contain `..` or NUL bytes, start with `~`, or contain `$` or `%` characters.
When the save succeeds, the response includes `mindroom_tool_output` with `status: "saved_to_file"`, `path`, byte count, `format: "binary"`, and `sha256`.
In shell tools, that workspace is exposed as `$MINDROOM_AGENT_WORKSPACE`; in worker-routed shell and python tools it is also `~` and `$HOME`, so `incoming/file.ext` and `~/incoming/file.ext` refer to the same saved file.

`attachment_ids` accepts only context attachment IDs (`att_*`).
`attachment_file_paths` accepts local file paths and auto-registers them in the current context before sending.
Relative paths resolve from the agent workspace when one is available.
Relative paths must stay inside the workspace.
Use `matrix_message(action="send"|"reply"|"thread-reply", attachment_ids=..., attachment_file_paths=...)` to send attachments.

### Why use this tool?

Not all AI models support direct file inputs.
The `attachments` tool lets any model work with files by calling tools that operate on attachment IDs, even if the model itself cannot ingest the raw bytes.

## Encryption

Both unencrypted and E2E encrypted files, images, audio clips, and videos are supported.
Encrypted media is decrypted transparently using the key material from the Matrix event.

## Retention

MindRoom automatically prunes attachment metadata and managed `incoming_media/` files older than 30 days.
Pruning runs opportunistically during new attachment registration.

## Limitations

- **Routing with multiple eligible responders** -- without an `@mention`, the router uses the file caption to select among candidates only when room configuration and reply permissions leave multiple eligible agents or teams.
- **Model support** -- the configured model must support file or video inputs for direct analysis. Models that do not can still use the `attachments` tool to inspect and process files via tool calls.
