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

Image support works automatically for all agents -- no configuration is needed. The AI model must support vision (e.g., Claude, GPT-4o).

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

## Encryption

Both unencrypted and E2E encrypted images are supported. Encrypted images are decrypted transparently using the key material from the Matrix event.

## Caching

AI response caching is automatically skipped when images are present, since image payloads are large and unlikely to repeat.

## Limitations

- **Router does not route image events** -- in multi-agent rooms, you must `@mention` the agent in the image caption. Without a mention, no agent will respond. Tracked in [#154](https://github.com/mindroom-ai/mindroom/issues/154).
- **Bridge mention detection** relies on `m.mentions` in the event, which some bridges (e.g., mautrix-telegram) do not set. Images sent from bridged platforms may not trigger agent responses.
- **Model support** -- the configured model must support vision. Text-only models will ignore the image or return an error.
