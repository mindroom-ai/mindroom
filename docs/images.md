---
icon: lucide/image
---

# Image Messages

This page covers how agents and teams see images sent in Matrix rooms, which image formats they receive, and what happens when a model cannot accept an image.
Images follow the same rules as other attachments for captions, routing, encryption, size limits, attachment IDs, and retention; see [Attachments](attachments.md).

## Sending Images

Send an image in a room and mention the agent or team in the caption, for example `@assistant What does this diagram show?`.
No configuration is needed; the image is passed to the responder's model as vision input, so that model must support vision (for example Claude or GPT-6 Astra).
A model without vision answers through the [media fallback](#media-fallback).

Captions from bridges trigger a response when they mention the agent through Matrix mention metadata, an HTML mention pill, or plain `@name` text.

## Image Formats

MindRoom recognizes PNG, JPEG, GIF, WebP, BMP, and TIFF images from their content and sends the recognized type to the model, logging a warning when the client declared a different MIME type.
Other image formats are sent with the client's declared MIME type, and the provider may reject them.

## Media Fallback

When a provider rejects a request containing inline media (images, audio, video, or documents), MindRoom retries it once without that media.
The retry adds `[Inline media unavailable for this model]` and tells the agent the content was not inspected, so the agent can still open the file through the [`attachments` tool](attachments.md#the-attachments-tool) or explain the limitation.
If the provider error says the model does not support that kind of input, such as `does not support image input` or `is not a multimodal model`, later requests to the same model leave that media kind out from the start until MindRoom restarts.
Other rejections, such as an image the provider could not decode, drop the media from that one request only.
Once a streamed reply has produced text, tool calls, or reasoning, a later provider error is reported instead of retried without media, and provider safeguard refusals are never retried without media.
