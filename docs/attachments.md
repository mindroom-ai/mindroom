---
icon: lucide/paperclip
---

# Attachments

This page covers how agents and teams receive files, images, and videos sent in Matrix rooms, how attachment IDs work, how long attachments are kept, and how to use the `attachments` tool to view, save, and register files.
Attachment handling works automatically for agents and teams in direct messages and threads; no configuration is needed.
Image-specific behavior is on [Image Messages](images.md), and audio messages are transcribed as described on [Voice Messages](voice.md).

## Sending Files to an Agent

Send a file, image, or video in a Matrix room and mention the agent or team in the caption:

- **With caption**: `@assistant Summarize this document` uses the caption as the prompt.
- **Without caption**: the responder receives `[Attached file]`, `[Attached image]`, or `[Attached video]` as the prompt.

A media event's `body` counts as a caption only when the event also has a `filename` field with a different value, following [MSC2530](https://github.com/matrix-org/matrix-spec-proposals/pull/2530), so a body that only repeats the filename is not a caption.

The responder answers only when it would answer a text message there (mention, thread participation, or DM).
Without an `@mention`, the router uses the caption to choose among several eligible agents or teams.
Both unencrypted and end-to-end encrypted media are supported.

The responder's model receives the media directly, so it must support that input type (vision for images, file or video input for documents and videos).
Models without that support can still work with the file through the [`attachments` tool](#the-attachments-tool).
When a provider rejects inline media, MindRoom retries without it and tells the agent the content was not inspected; see [Media Fallback](images.md#media-fallback).
Some provider integrations never send certain media kinds, so MindRoom leaves those out from the start with the same note.
Only models using the `google` provider receive video, Claude and OpenAI Responses models (including Codex) also receive no audio, Ollama and Groq models receive only images, and Cerebras models receive no media.

## Attachment IDs

Each received or registered file gets an attachment ID such as `att_abc123`.
Attachments sent with the current message are listed in the prompt with their kind, filename, sender, send time, and event ID:

```
Attachments sent with the current message (use tool calls to inspect or process them by ID):
- att_abc123 (image, "car.jpg", from @user:example.org, sent 2026-06-06 09:00 UTC, event $abc)
```

Only current-message attachments are sent to the model as media.
Earlier attachments appear in the conversation history as annotations, and the agent can open them again by ID with the `attachments` tool:

```text
@user:example.org: check this out
[attachments: att_def456 (image, "house.jpg")]
```

Attachment IDs are scoped to their room and thread, so an ID from one conversation cannot be used in another.
Within a conversation, an authorized response can open files uploaded by any participant.
When a message's file is replaced by an edit, later turns use the new file under a new attachment ID.

## The `attachments` Tool

The optional `attachments` tool lets an agent list, view, save, and register files in the current conversation, including with models that cannot take file input directly.
Add it to the agent's tools; it has no configuration fields, and enabling [`matrix_message`](tools/matrix-message.md) also enables it:

```yaml
agents:
  assistant:
    tools:
      - attachments
```

| Function | What it does |
|----------|--------------|
| `list_attachments(target=None)` | List the attachment IDs available in the current context with their metadata, plus any `missing_attachment_ids`; `target` returns one attachment |
| `get_attachment(attachment_id, mindroom_output_path=None, view=False)` | Return metadata, save the file into the workspace, or show its content to the model |
| `view_file(path=None, attachment_id=None)` | Show an image from the workspace or an attachment to the model |
| `register_attachment(file_path)` | Turn a local file into a new attachment ID for this conversation |

```python
list_attachments()
get_attachment("att_abc123")
get_attachment("att_abc123", view=True)
get_attachment("att_abc123", mindroom_output_path="incoming/plan.pdf")
view_file(path="plots/result.png")
register_attachment("exports/report.csv")
matrix_message(message="Here is the report.", attachments=["att_abc123", "exports/report.csv"])
```

Attachment arguments must be `att_*` IDs available in the current conversation or registered earlier in the same tool run.
Metadata includes the kind, filename, MIME type, size, room ID, thread ID, sender, timestamps, and an `available` flag that reports whether the file still exists.
It also includes the runtime-local path, except for agents whose workspace tools run on a worker, where that path does not exist; their metadata has a `usage` note saying to save the file with `mindroom_output_path` instead.
To post files into Matrix, pass attachment IDs or file paths to `matrix_message`; see [`matrix_message` attachments](tools/matrix-message.md#attachments).

### Viewing Content

`get_attachment(attachment_id, view=True)` sends an image, audio, video, or document (including PDF and plain text) to the model, up to 20 MiB.
The model and its provider must support that media type and may have stricter limits.
`view=True` cannot be combined with `mindroom_output_path`.
If the model cannot take the media, the agent is told so and can save the file and use other extraction or transcription tools instead.

`view_file` shows one image to the calling model in one call; supply exactly one of `path` or `attachment_id`, and use `read_file` for ordinary text and code.
Workspace paths resolve inside the agent's worker when it has one, otherwise inside its workspace.
It accepts PNG, JPEG, GIF, and WebP images up to 20 MiB and 40 million pixels.
Images larger than 2048 pixels on the longest edge or 5 MiB are scaled down, animated images show only the first frame, and the result metadata reports these changes.
Image viewing through `get_attachment(view=True)` follows the same limits.
Viewing does not post anything to Matrix; to share the image, pass the returned attachment ID to `matrix_message`.
That ID works within the same tool run; in later turns, view the original path again.
Conversation history keeps up to four recently viewed images within 10 MiB in total, and older ones are replaced by a notice.

### Saving Files to the Workspace

`get_attachment(attachment_id, mindroom_output_path="incoming/file.ext")` saves the file into the agent workspace and returns a receipt with the path, byte count, and SHA-256.
Save attachments this way before processing them with `file`, `coding`, `python`, or `shell` on a worker, because attachment storage is not visible inside the worker.
The path must be a workspace-relative file path; it cannot be absolute, contain `..`, start with `~`, or contain `$` or `%`.
Saves into a worker are limited by [`MINDROOM_ATTACHMENT_INLINE_SAVE_MAX_BYTES`](deployment/sandbox-proxy.md#environment-variable-reference), which defaults to 64 MiB so that every attachment MindRoom accepts can be saved.
See [Sandbox Proxy](deployment/sandbox-proxy.md) for how the workspace appears as `$MINDROOM_AGENT_WORKSPACE` and `~` inside worker tools.

### Registering Local Files

`register_attachment(file_path)` copies a local file into attachment storage and returns a new attachment ID for the current room and thread, usable by later calls in the same run.
Later changes to the source file do not change the attachment.
Registered files are limited to 64 MiB.
Paths, including the targets of symbolic links, follow the agent's [`file_access`](architecture/security-posture.md#file-access) setting:

- With the default `workspace`, paths resolve from the agent workspace and must stay inside it, so pass workspace-relative paths such as `incoming/file.txt`.
  `~` expands to the MindRoom process home, not the worker workspace, so `~/incoming/file.txt` is rejected.
  Without a configured workspace, path registration is refused.
- With `unrestricted`, any file the MindRoom process can read is accepted, including absolute paths.

## Retention

Attachment bytes are stored under `incoming_media/` and metadata under `attachments/` in the storage directory (`mindroom_data/` by default).
MindRoom prunes attachments older than 30 days; deletion is not immediate at the 30-day mark.
Pruning never deletes workspace files, the source files of registered attachments, or copies on the Matrix homeserver.

## Limits

- **Incoming media size**: received files must be at most 64 MiB (67,108,864 bytes), including after decryption; send a smaller file if one is rejected.
  Homeservers and model providers may set lower limits.
- **Earlier media per turn**: a turn fetches at most 10 earlier files, images, or videos that are not stored yet, newest first, and leaves the rest for later turns.
  An earlier file that fails to download is not tried again for an hour.
