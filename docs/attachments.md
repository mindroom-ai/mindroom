---
icon: lucide/paperclip
---

# Attachments

MindRoom can process files, images, audio, and videos sent to Matrix rooms, passing them to agents for analysis or action.
Supported attachment kinds: `audio`, `file`, `image`, `video`.

## Overview

When a user sends a file, image, audio message, or video in a Matrix room:

1. The agent determines whether it should respond (via mention, thread participation, or DM)
2. The media is downloaded and decrypted (if E2E encrypted)
3. The file is saved locally and registered as a context-scoped attachment
4. The agent receives the media as an Agno `File`, `Video`, `Audio`, or `Image` object plus an attachment ID it can reference in tool calls
5. The agent responds with its analysis or takes action on the file

Attachment support works automatically for all agents -- no configuration is needed.

## How It Works

```
┌──────────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ File/Image/Audio │────>│ Download &  │────>│ Register    │────>│ Pass to AI  │
│ /Video (Matrix)  │     │ Decrypt     │     │ Attachment  │     │ Model       │
└──────────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                                                  │
                                                                  v
                                                            ┌─────────────┐
                                                            │ Agent       │
                                                            │ Responds    │
                                                            └─────────────┘
```

## Usage

Send a file, image, audio message, or video in a Matrix room and mention the agent in the caption:

- **With caption**: `@assistant Summarize this document` -- the caption is used as the prompt
- **Without caption**: The agent receives `[Attached file]`, `[Attached image]`, `[Attached audio]`, or `[Attached video]` as the prompt
- **Bare filename**: If the body is just the filename (e.g., `report.pdf`), it is treated the same as no caption

Attachments work in both direct messages and threads, and with both individual agents and teams.

## Attachment IDs

Each uploaded file or video is assigned a stable attachment ID (e.g., `att_abc123`).
The agent's prompt is augmented with the available IDs:

```
Available attachment IDs: att_abc123. Use tool calls to inspect or process them.
```

Attachment IDs are **context-scoped** -- an attachment registered in one room or thread is not accessible from another.
This prevents cross-room data leakage for ID-based access.
Voice raw-audio fallback uses the same attachment ID mechanism; see [Voice Fallback](voice.md#voice-fallback-no-stt-available).

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
| `list_attachments(target?)` | List metadata for attachments in the current context (ID, kind, local_path, filename, MIME type, size, room_id, thread_id, sender, created_at) |
| `get_attachment(attachment_id, mindroom_output_path?)` | Return one context attachment record, or save its bytes to a workspace-relative path and return a save receipt |
| `register_attachment(file_path)` | Register a local file path as a context attachment ID (`att_*`) |

When `mindroom_output_path` is omitted, `get_attachment()` returns the attachment metadata response, including the runtime-local `local_path`.
For worker-routed agents, prefer `get_attachment("att_...", mindroom_output_path="incoming/file.ext")` before processing an attachment with `file`, `coding`, `python`, or `shell`, because the runtime-local path may not exist inside the worker workspace.
`mindroom_output_path` must be a file path relative to the agent workspace.
It must not be empty, absolute, point at the workspace root, contain `..` or NUL bytes, or use environment or user expansion.
When the save succeeds, the response includes `mindroom_tool_output` with `status: "saved_to_file"`, `path`, byte count, `format: "binary"`, and `sha256`.

`attachment_ids` accepts only context attachment IDs (`att_*`).
`attachment_file_paths` accepts local file paths and auto-registers them in the current context before sending.
Use `matrix_message(action="send"|"reply"|"thread-reply", attachment_ids=..., attachment_file_paths=...)` to send attachments.

### Why use this tool?

Not all AI models support direct file inputs.
The `attachments` tool lets any model work with files by calling tools that operate on attachment IDs, even if the model itself cannot ingest the raw bytes.

## Encryption

Both unencrypted and E2E encrypted files and videos are supported.
Encrypted media is decrypted transparently using the key material from the Matrix event.

## Caching

AI response caching is automatically skipped when files, images, audio, or videos are present, since media payloads are large and unlikely to repeat.

## Retention

MindRoom automatically prunes attachment metadata and managed `incoming_media/` files older than 30 days.
Pruning runs opportunistically during new attachment registration.

## Limitations

- **Routing in multi-agent rooms** -- in multi-agent rooms without an `@mention`, the router selects the best agent based on the file caption.
- **Model support** -- the configured model must support file or video inputs for direct analysis. Models that do not can still use the `attachments` tool to inspect and process files via tool calls.
