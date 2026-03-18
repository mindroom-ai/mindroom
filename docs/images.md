---
icon: lucide/image
---

# Image Messages

MindRoom can process images sent to Matrix rooms, passing them to vision-capable AI models for analysis.

## Overview

When a user sends an image in a Matrix room:

1. The agent determines whether it should respond (via mention, thread participation, or DM)
2. The image is downloaded and decrypted (if E2E encrypted)
3. The image is wrapped as an `agno.media.Image` and passed to the AI model
4. The agent responds with its analysis

Image support works automatically for all agents -- no configuration is needed. The AI model must support vision (e.g., Claude, GPT-5.4).

## Supported Formats

MindRoom detects image format from file byte signatures:

- PNG
- JPEG
- GIF
- WebP
- BMP
- TIFF

If the declared MIME type in the Matrix event does not match the detected byte signature, MindRoom logs a warning and uses the detected type.

## How It Works

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│ Image Msg   │────>│ Download &  │────>│ Pass to AI  │
│ (Matrix)    │     │ Decrypt     │     │ Model       │
└─────────────┘     └─────────────┘     └─────────────┘
                                              │
                                              v
                                        ┌─────────────┐
                                        │ Agent       │
                                        │ Responds    │
                                        └─────────────┘
```

## Usage

Send an image in a Matrix room and mention the agent in the caption:

- **With caption**: `@assistant What does this diagram show?` -- the caption is used as the prompt
- **Without caption**: The agent receives `[Attached image]` as the prompt and describes what it sees
- **Bare filename**: If the body is just a filename (e.g., `IMG_1234.jpg`), it is treated the same as no caption

Images work in both direct messages and threads, and with both individual agents and teams.

## Captions (MSC2530)

If the Matrix event's `filename` field differs from `body`, the `body` is used as a user caption.
This follows [MSC2530](https://github.com/matrix-org/matrix-spec-proposals/pull/2530) semantics and works with clients that set the caption in the body.

## Image Persistence

Images are saved under `mindroom_data/attachments/` and `mindroom_data/incoming_media/` and registered as attachment records with 30-day retention.
In addition to being passed to the AI model as vision input, each image is also registered as an `att_*` attachment ID so agents can reference it via tool calls.
See [Attachments](attachments.md) for details on retention and context scoping.

## Encryption

Both unencrypted and E2E encrypted images are supported. Encrypted images are decrypted transparently using the key material from the Matrix event.

## Caching

AI response caching is automatically skipped when images are present, since image payloads are large and unlikely to repeat.

## Limitations

- **Routing in multi-agent rooms** -- in multi-agent rooms without an `@mention`, the router selects the best agent based on the image caption.
- **Bridge mention detection** uses `m.mentions` in the event, falling back to parsing HTML pills from `formatted_body` when `m.mentions` is absent (e.g., mautrix-telegram). Bridges that set neither may not trigger agent responses.
- **Model support** -- the configured model must support vision. Text-only models will ignore the image or return an error.
