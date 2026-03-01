# File & Video Attachments

MindRoom can process files and videos sent to Matrix rooms, passing them to agents for analysis or action.

## Overview

When a user sends a file or video in a Matrix room:

1. The agent determines whether it should respond (via mention, thread participation, or DM)
1. The media is downloaded and decrypted (if E2E encrypted)
1. The file is saved locally and registered as a context-scoped attachment
1. The agent receives the media as an Agno `File` or `Video` object plus an attachment ID it can reference in tool calls
1. The agent responds with its analysis or takes action on the file

File and video support works automatically for all agents -- no configuration is needed.

## How It Works

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ File/Video  │────>│ Download &  │────>│ Register    │────>│ Pass to AI  │
│ (Matrix)    │     │ Decrypt     │     │ Attachment  │     │ Model       │
└─────────────┘     └─────────────┘     └─────────────┘     └─────────────┘
                                                                  │
                                                                  v
                                                            ┌─────────────┐
                                                            │ Agent       │
                                                            │ Responds    │
                                                            └─────────────┘
```

## Usage

Send a file or video in a Matrix room and mention the agent in the caption:

- **With caption**: `@assistant Summarize this document` -- the caption is used as the prompt
- **Without caption**: The agent receives `[Attached file]` or `[Attached video]` as the prompt
- **Bare filename**: If the body is just the filename (e.g., `report.pdf`), it is treated the same as no caption

Files and videos work in both direct messages and threads, and with both individual agents and teams.

## Attachment IDs

Each uploaded file or video is assigned a stable attachment ID (e.g., `att_abc123`). The agent's prompt is augmented with the available IDs:

```
Available attachment IDs: att_abc123. Use tool calls to inspect or process them.
```

Attachment IDs are **context-scoped** -- an attachment registered in one room or thread is not accessible from another. This prevents cross-room data leakage for ID-based access. Voice raw-audio fallback uses the same attachment ID mechanism; see [Voice Fallback](https://docs.mindroom.chat/voice/#voice-fallback-no-stt-configured).

## The `attachments` Tool

Agents can use the optional `attachments` tool to interact with context-scoped attachments programmatically.

### Enabling

Add `attachments` to the agent's tool list:

```
agents:
  assistant:
    tools:
      - attachments
```

### Operations

| Operation                                                | Description                                                                          |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `list_attachments(target?)`                              | List metadata for attachments in the current context (ID, filename, MIME type, size) |
| `send_attachments(attachment_ids, room_id?, thread_id?)` | Upload and send context attachment IDs to a Matrix room or thread                    |

`send_attachments` accepts only attachment IDs from the current context. Direct local file path references are not supported.

### Why use this tool?

Not all AI models support direct file inputs. The `attachments` tool lets any model work with files by calling tools that operate on attachment IDs, even if the model itself cannot ingest the raw bytes.

## Encryption

Both unencrypted and E2E encrypted files and videos are supported. Encrypted media is decrypted transparently using the key material from the Matrix event.

## Caching

AI response caching is automatically skipped when files, videos, or audio are present, since media payloads are large and unlikely to repeat.

## Limitations

- **Routing in multi-agent rooms** -- in multi-agent rooms without an `@mention`, the router selects the best agent based on the file caption.
- **Model support** -- the configured model must support file or video inputs for direct analysis. Models that do not can still use the `attachments` tool to inspect and process files via tool calls.
