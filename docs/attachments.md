---
icon: lucide/paperclip
---

# Attachments

MindRoom can process files, images, audio, and videos sent to Matrix rooms, passing them to agents and teams for analysis or action.
Supported attachment kinds: `audio`, `file`, `image`, `video`.

## Overview

When a user sends a file, image, audio message, or video in a Matrix room:

1. The responder determines whether it should answer (via mention, thread participation, or DM)
2. The media is downloaded and decrypted (if E2E encrypted), subject to the [incoming media size checks](#limitations)
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
An authorized response may inspect another participant's uploads when their attachment IDs are available in the same conversation.
When voice media download and registration succeed, raw-audio fallback uses the same attachment ID mechanism; see [Voice Fallback](voice.md#voice-fallback-no-stt-available).

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
For an eligible inline-media failure, MindRoom can retry without the media and explicitly tell the agent that the removed content was not inspected; see [Media Fallback](images.md#media-fallback) for retry limits and exclusions.
Known adapter omissions use the same guidance, so unsupported media is not silently dropped.
The agent can then call `get_attachment` without `view` to obtain metadata or save the file, and use other available extraction, transcription, or analysis tools.
This does not provision another model, grant credentials, or automatically delegate the task.
For worker-routed agents, prefer `get_attachment("att_...", mindroom_output_path="incoming/file.ext")` before processing an attachment with `file`, `coding`, `python`, or `shell`, because the runtime-local path may not exist inside the worker workspace.
`mindroom_output_path` must be a file path relative to the agent workspace.
It must not be empty, absolute, point at the workspace root, contain `..` or NUL bytes, start with `~`, or contain `$` or `%` characters.
When the save succeeds, the response includes `mindroom_tool_output` with `status: "saved_to_file"`, `path`, byte count, `format: "binary"`, and `sha256`.
In shell tools, that workspace is exposed as `$MINDROOM_AGENT_WORKSPACE`; in worker-routed shell and python tools it is also `~` and `$HOME`, so `incoming/file.ext` and `~/incoming/file.ext` refer to the same saved file.

`matrix_message` accepts one ordered `attachments` list containing context attachment IDs (`att_*`) and local file paths.
Local files are registered in the current context before sending.
With the default `file_access: workspace`, paths resolve from the agent workspace and must stay inside it; absolute paths must point into the workspace, and `~` expands to the MindRoom process home rather than the worker workspace.
With [`file_access`](architecture/security-posture.md#file-access) set to `unrestricted`, any existing file the MindRoom process can read is accepted.
Without a configured agent workspace, `workspace` mode only attaches `att_*` IDs.
Use `matrix_message(attachments=["att_example", "exports/report.csv"])` to send attachment IDs and file paths in order to the current conversation.

### Why use this tool?

Not all AI models support direct file inputs.
The `attachments` tool lets any model work with files by calling tools that operate on attachment IDs, even if the model itself cannot ingest the raw bytes.

## Encryption

Both unencrypted and E2E encrypted files, images, audio clips, and videos are supported.
Encrypted media is decrypted transparently using the key material from the Matrix event.

## Retention

MindRoom automatically prunes attachment metadata and eligible managed `incoming_media/` files older than 30 days.
Pruning runs opportunistically during new attachment registration, with a one-hour cleanup throttle.
Managed files with active attachment references are retained, and filesystem failures can delay deletion.
This is not an exact deletion deadline and does not delete unmanaged source or workspace files or Matrix homeserver copies.

## Limitations

- **Incoming media size** -- downloaded and decrypted payloads must each be at most 64 MiB (67,108,864 bytes).
  MindRoom checks the returned download body and, for encrypted media, the decrypted bytes before normal attachment or model use.
  This is a post-download check, so use a smaller file when it is rejected.
  Homeservers and model providers may impose lower limits; `get_attachment(view=True)` has a separate 20 MiB per-attachment limit.
- **Routing with multiple eligible responders** -- without an `@mention`, the router uses the file caption to select among candidates only when room configuration and reply permissions leave multiple eligible agents or teams.
- **Model support** -- the configured model must support file or video inputs for direct analysis. Models that do not can still use the `attachments` tool to inspect and process files via tool calls.
