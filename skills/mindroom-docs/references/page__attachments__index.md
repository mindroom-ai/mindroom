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
When a message with a file, image, or video is edited, later turns register its current revision under a new attachment ID, so a replaced file is used instead of the old one.

Attachment IDs are **context-scoped** -- an attachment registered in one room or thread is not accessible from another.
This prevents cross-room data leakage for ID-based access.
An authorized response may inspect another participant's uploads when their attachment IDs are available in the same conversation.
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

`register_attachment()` retains a copy of the file's current bytes in MindRoom's managed `incoming_media/` storage, and the attachment's `local_path` names that copy rather than the source file.
Later edits to the source file, including replacing it with a symbolic link, do not change what the attachment ID views, saves, or sends.
Workspace-relative registration opens the source without following symbolic links, so a linked file or directory below the workspace is rejected.
Registered files are limited to 64 MiB, the same cap that applies to incoming Matrix media.
Attachment IDs registered by earlier releases, which referenced the source file in place, are copied into managed storage on first use only when the source is still a regular file reached without symbolic links and its bytes match the recorded SHA-256; otherwise the ID stops resolving.
By default, `get_attachment()` returns the attachment metadata response, including the runtime-local `local_path`.
Use `get_attachment("att_...", view=True)` to inspect media from earlier in the conversation or a local file registered with `register_attachment(file_path)`.
This sends the attachment bytes to the configured model, using native image, audio, video, or document inputs rather than putting binary data in tool text.
It supports PNG, JPEG, GIF, and WebP images, audio, video, and Agno-supported document types including PDF and plain text, with a 20 MiB limit per attachment.
Image format is detected from the bytes; other media uses the attachment's MIME type and filename.
The selected model and its provider adapter must support the media type and may impose stricter format or size limits.
The attachment must be available in the current context and have a readable local file.
`view=True` cannot be combined with `mindroom_output_path`.
For an eligible inline-media failure, MindRoom can retry without the media and explicitly tell the agent that the removed content was not inspected; see [Media Fallback](https://docs.mindroom.chat/images/#media-fallback) for retry limits and exclusions.
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
With [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) set to `unrestricted`, any existing file the MindRoom process can read is accepted.
Without a configured agent workspace, `workspace` mode only attaches `att_*` IDs.
Use `matrix_message(attachments=["att_example", "exports/report.csv"])` to send attachment IDs and file paths in order to the current conversation.

`attachments` lets agents inspect and register files that are scoped to the current Matrix conversation.

### What It Does

`attachments` exposes `view_file(path=None, attachment_id=None)`, `list_attachments(target=None)`, `get_attachment()`, and `register_attachment()`.
`view_file(path="plots/result.png")` delivers an image directly to the calling model in one call.
`view_file(attachment_id="att_...")` views an authorized conversation attachment without a registration/fetch sequence.
Supply exactly one source; keep `read_file` for ordinary text and code.
Workspace paths resolve inside the selected worker, or inside the configured workspace in local execution mode.
Worker-routed viewing uses the same configured workspace as shell and file tools, even when the primary and worker mount storage at different paths.
PNG, JPEG, GIF and WebP inputs are supported up to 20 MiB and 40 million pixels.
The delivered image is bounded to 2048 pixels on its longest edge and 5 MiB; resizing, conversion, and first-frame-only animation handling are disclosed in metadata.
Transparent images retain their transparency; images that cannot fit the payload limit return an explicit error while preserving the source artifact.
Viewing preserves the source path and retains a reusable attachment handle when context storage is available.
A retained handle identifies the delivered image copy and follows existing attachment authority: it is available during the current tool run, or when supplied by conversation metadata.
For later turns, reopen the original workspace path; model history replays up to four recent viewed images within a 10 MiB aggregate limit.
Older or oversized replay images are omitted with an explicit notice; their saved artifacts remain available.
Viewing does not publish, upload to a separate vision service, open a user-facing panel, or post into Matrix.
Adapters that cannot deliver tool images return an explicit limitation while retaining the artifact.
Share only when requested, using `matrix_message(attachments=["att_..."])` with the returned handle.
`list_attachments()` returns the attachment IDs currently available in tool runtime context, the resolved metadata payloads, and any `missing_attachment_ids`.
Pass a context-available attachment ID as `target` to return only that attachment; an ID outside the current context returns an error.
`get_attachment()` returns a single attachment record, including the runtime-local path, when called with only an attachment ID.
`get_attachment(attachment_id, view=True)` sends image, audio, video, or document content (including PDF) to the model, including local files and attachments from earlier in the conversation.
Image viewing uses the same preparation, size limits, transformation disclosures, and history replay as `view_file` and browser screenshots.
Viewing requires a model and provider adapter that support the media type, and a readable, context-scoped file no larger than 20 MiB.
Rejected media requests retry without the media and give the agent explicit guidance to use the attachment ID/path with other available tools; known adapter omissions receive the same guidance.
It cannot be combined with `mindroom_output_path`.
`get_attachment(attachment_id, mindroom_output_path="relative/path")` saves the attachment bytes into the agent workspace and returns a `mindroom_tool_output` save receipt with the saved path, byte count, binary format, and SHA256 digest.
Use `mindroom_output_path` before handing attachments to worker-routed workspace tools such as `file`, `coding`, `python`, or `shell`, because the runtime-local path may not exist inside the worker workspace.
In shell tools, the agent workspace is exposed as `$MINDROOM_AGENT_WORKSPACE`; in worker-routed shell and python tools it is also `~` and `$HOME`, so a saved path like `incoming/file.txt` can also be read as `~/incoming/file.txt`.
The path must be relative to the workspace and must not be empty, absolute, point at the workspace root, contain `..` or NUL bytes, start with `~`, or contain `$` or `%` characters.
`register_attachment()` turns a local file path into a new context-scoped `att_*` ID and appends that ID to the current runtime context so later tool calls in the same run can reuse it.
`register_attachment()` paths follow the agent's [`file_access`](https://docs.mindroom.chat/architecture/security-posture/#file-access) setting: with the default `workspace`, they resolve from the agent workspace and must stay inside it, so pass workspace-relative paths such as `incoming/file.txt`.
Absolute paths must point into the workspace, and `~` expands to the MindRoom process home rather than the worker workspace, so `~/incoming/file.txt` is rejected here.
Without a configured agent workspace, `workspace` mode refuses path-based registration and only existing `att_*` IDs can be attached; `unrestricted` mode still accepts absolute paths.
Registration copies the file's current bytes into managed attachment storage without following symbolic links, so later edits or replacement of the source file do not change the attachment.
Attachment records include kind, filename, MIME type, room ID, thread ID, sender, creation time, and an `available` flag that reports whether the local file still exists.
This tool does not send files by itself, but its IDs can be passed to `matrix_message` for `send`.

### Configuration

This tool has no tool-specific inline configuration fields.

### Example

```yaml
agents:
  assistant:
    tools:
      - attachments
```

```python
list_attachments()
list_attachments(target="att_abc123")
get_attachment("att_abc123")
get_attachment("att_abc123", mindroom_output_path="incoming/plan.pdf")
register_attachment("incoming/plan.pdf")
matrix_message(message="Sharing the plan here.", attachments=["att_abc123"])
```

### Notes

- `attachment_id` values must be non-empty `att_*` IDs that are already present in the current tool runtime context.
- Registering a new file attaches it to the current `room_id` and `thread_id`, which prevents accidental reuse across unrelated conversations.
- For the full attachment lifecycle, media kinds, retention rules, and Matrix ingestion flow, use the dedicated [Attachments](https://docs.mindroom.chat/attachments/) guide.

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
Downloaded Matrix media is stored once per content and file type, so events that share one file keep their own attachment records but a single copy of its bytes.

## Limitations

- **Incoming media size** -- downloaded payloads must be at most 64 MiB (67,108,864 bytes), and encrypted media decrypts to the same size.
  MindRoom stops a download as soon as it passes that limit, so use a smaller file when it is rejected.
  Homeservers and model providers may impose lower limits; `get_attachment(view=True)` has a separate 20 MiB per-attachment limit.
- **Earlier media that fails** -- a file, image, or video earlier in the conversation that cannot be downloaded, decrypted, or stored is not tried again for an hour, so later turns do not fetch it each time.
- **Earlier media per turn** -- one turn downloads at most 10 earlier files, images, or videos that are not stored yet, newest first, and leaves the rest for later turns.
  The thread's opening message is not counted toward this limit or held back after a failure: each turn downloads it until it is stored.
- **Routing with multiple eligible responders** -- without an `@mention`, the router uses the file caption to select among candidates only when room configuration and reply permissions leave multiple eligible agents or teams.
- **Model support** -- the configured model must support file or video inputs for direct analysis. Models that do not can still use the `attachments` tool to inspect and process files via tool calls.
