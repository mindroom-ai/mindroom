# Image Messages

MindRoom can process images sent to Matrix rooms, passing them to vision-capable agents and teams for analysis.

## Overview

When a user sends an image in a Matrix room:

1. The responder determines whether it should answer (via mention, thread participation, or DM)
2. The image is downloaded and decrypted (if E2E encrypted)
3. The image is wrapped as an `agno.media.Image` and passed to the AI model
4. The responder replies with its analysis

Image support works automatically for agents and teams -- no configuration is needed.
The selected model must support vision (e.g., Claude, GPT-5.6).

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
                                        │ Responder   │
                                        │ Replies     │
                                        └─────────────┘
```

## Usage

Send an image in a Matrix room and mention the agent or team in the caption:

- **With caption**: `@assistant What does this diagram show?` -- the caption is used as the prompt
- **Without caption**: The agent receives `[Attached image]` as the prompt and describes what it sees
- **Bare filename**: If the body is just a filename (e.g., `IMG_1234.jpg`), it is treated the same as no caption

Images work in both direct messages and threads, and with both individual agents and teams.

## Captions (MSC2530)

If the Matrix event's `filename` field differs from `body`, the `body` is used as a user caption.
This follows [MSC2530](https://github.com/matrix-org/matrix-spec-proposals/pull/2530) semantics and works with clients that set the caption in the body.

## Image Persistence

Image bytes are saved under `mindroom_data/incoming_media/`, while their attachment metadata is saved under `mindroom_data/attachments/`; both are subject to the attachment retention policy.
In addition to being passed to the AI model as vision input, each image is also registered as an `att_*` attachment ID so agents can reference it via tool calls.
See [Attachments](https://docs.mindroom.chat/attachments/) for details on retention and context scoping.

## Encryption

Both unencrypted and E2E encrypted images are supported. Encrypted images are decrypted transparently using the key material from the Matrix event.

## Media Fallback

If a model rejects inline media (images, audio, video, or documents), MindRoom automatically retries the request without the inline media.
The retried prompt includes `[Inline media unavailable for this model]` to inform the agent that attachments were dropped.
The note is added only when the outgoing request really did lose every copy of some kind, so a turn that pre-strips a kind from thread context while a fresh upload of that same kind survives says nothing — the model can see the attachment the note would claim was omitted.
Agents can still reference the files via attachment IDs and tools.

This fallback is transparent — no user action is required.
Any ordinary failure of a media-bearing request triggers one retry without media — no error wording decides whether to retry, so unknown provider prose degrades gracefully instead of surfacing a raw provider error.
Provider safeguard refusals are returned as refusals and are not retried without media.
When the retry succeeds, the model route learns that the dropped media kinds are unsupported, and later requests omit them up front instead of paying a failed API call.
This learned capability state is process-local and resets on restart.
Payload-size and context-overflow rejections never teach the capability state, since dropping media can shrink an oversized request for reasons unrelated to media support.
Transient failures never teach either, since their retry can succeed simply because the outage, rate limit, or timeout passed; the statuses that count as transient are listed in `TRANSIENT_PROVIDER_STATUS_CODES` (`error_handling.py`).

## Wire-Level Media Guard

The fallback above only covers the media MindRoom passes into a run.
Media can also reach a provider from thread history that agno replays, or from a tool that returns an image, and neither is visible to the run-input layer.
`model_media_guard.py` wraps the model's own `ainvoke`, `ainvoke_stream`, `aresponse`, and `aresponse_stream` so those messages get the same treatment at the last point before the wire.

When a guarded call fails with a client-error status the provider itself issued, the guard retries with media stripped from the outgoing messages and adds a short note to each message that lost attachments so the agent can say they were omitted.
Each retry removes exactly one more media kind than the previous one, so the attempt that finally succeeds differs from the one before it by a single kind, and that kind alone is recorded as unsupported.
A route that has already learned a kind strips it up front on every attempt, so the next turn spends one call instead of repeating the whole experiment.
Statuses that answer who is asking rather than what was asked — 401 and 403 — are passed straight through, since a rejected key or a forbidden resource would otherwise pay the whole ladder on every turn.
A 404 is not one of them: the failure this guard was written for is OpenRouter answering 404 with `No endpoints found that support image input`.
A retry that also gives an otherwise empty message its first text does not teach, because the added note could be what the provider accepted.
When a failure can teach nothing at all — a context overflow, an oversized payload, transient prose — the ladder stops isolating kinds one at a time and collapses to a single attempt that removes every kind the guard owns, so an unlearnable failure costs two calls however much media the history holds.

The guard keeps its own capability cache, separate from the run-input one.
A replayed attachment differs from a fresh upload in format, size, and encoding, so what a model refuses to replay never suppresses the next thing a user uploads, and vice versa.
A kind has to be isolated twice on the same route before it is cached: the first isolation is only a suspicion, and the second one turns it into a lesson.
The two are counted per route and media kind for the life of the process, and nothing else about them is compared.
They need not come from the same conversation, the same attachment, or the same hour, and a turn that succeeds with that kind still on the wire is not recorded and does not discharge a standing suspicion.
So the gate rules out a single isolated blip, and nothing beyond that.
In particular, a single corrupt attachment replayed from history is on the wire every turn of its conversation, so it supplies both isolations itself and does end up blinding the route to that modality until restart.
The gate delays that by one turn rather than preventing it, and rejecting the modality is the conservative end of the trade: the agent still answers, and the omission note tells it what it lost.
When the guard is what dropped media from the request that finally succeeded, the run-input layer is told, and it declines to record its own dropped kinds as unsupported, because the recovery was not its doing.
The separation is about what each layer may write, not about what it may read.
The guard only ever owns messages the caller did not put on the wire, so thread-context media pinned into a run input is never the guard's to strip and the run-input retry still removes it — but that success is never credited to the run-input cache, whatever either cache already holds, because context media is replayed history re-materialized and an experiment over it is an experiment over the guard's class of input.
It is credited as one isolation at the guard's two-strike gate instead, so a context-only turn on a route nothing has taught costs the same two strikes as the guard's own ladder rather than blinding the route to the user's next upload of that kind.
A fresh attachment on a turn that carried no replayed media is the case the run-input cache does answer: the retry removed the user's own upload and nothing else, which is evidence about exactly the input class that cache gates.
A turn carrying both is confounded — the layer performs one removal, and it takes the upload and the replayed attachment away together, so the success cannot say which the provider objected to.
Every kind such a turn uploaded fresh is credited to neither cache: the run-input cache closes on a single strike, and the guard's cache gates replayed media, which that kind never was.
Only the replayed kinds the turn did not also upload fresh reach the two-strike gate, which costs the route one extra doomed call before it converges and keeps one bad history attachment from suppressing fresh uploads of its kind for the life of the process.
A success only counts at all when the without-media attempt affirmatively finished *and answered*: a run output with any status other than `completed` — errored, cancelled, paused, or never settled — proves nothing, and neither does a `completed` run that answered nothing, which is the same shape the driver holding it discards and retries as an empty run.
What counts as an answer is whatever that driver already decided, handed to the shared bar rather than re-derived by it: an agent run answers through its own content or tool calls, and a team run answers through its members' too, so a team whose consensus is empty above a member that replied is a success the same way the delivered reply is.
A stream is held to the same bar in the facts a stream leaves behind: one that ended having emitted no text and called no tool proves nothing either, and at the wire that means a response whose chunks carried no content, no tool call, no generated image, video, audio, or file, and no structured output.
A model answers in media as readily as in text — Gemini returns an inline image with the content field left empty, and an OpenAI audio answer arrives the same way — so reading the wire for text alone would refuse the lesson a media-answering route just earned and make that route re-walk the whole ladder every turn.
Banking a lesson and having something to deliver part company on exactly that shape: the provider accepted the media-free request, so the lesson is real, while nothing in the delivery path renders a generated image, video, audio, or file, so the turn is still the empty run every driver discards, retries once, and finishes with the empty-response notice rather than in silence.
Reaching the consumer is not the same fact and does not stand in for it, since an empty completion still delivers a role-only first chunk and a usage-only last one.
That bar guards every site that banks a lesson, on the blocking and streaming drivers of the agent path and the team path alike, and on both of the guard's own paths.
Both streaming drivers count a tool call from either end of its life, whether the stream announced its start or only carried its completion, so a turn whose only tool call never returns clears both bars; they differ only in where the visible text comes from, since the agent path reads the text it streamed and the team path reads the member slots plus the coordinator's consensus.
Reading both ends is what keeps each bar from disagreeing with its own driver, which delivers any attempt carrying a completed tool execution as a real answer even when no start was ever tracked for it.
The guard's report to the layer above is not held to it: whether the attempt answered or not, the guard is still what took the media off, so the run-input retry never gets the credit for that turn.
The reading runs the other way too: once the guard's cache holds a kind, the run-input layer omits thread-context media of that kind before the call instead of shipping it and failing first, so the conversation pays for that lesson once rather than on every turn.
That read only narrows, and only for context media — a fresh upload is still filtered against the run-input cache alone, which the guard never writes.
Both halves of this — keeping fresh uploads out of the run input so a retry can tell them apart from context media, and the uncredited-success and pre-strip reads above — hold for individual agents and for teams alike; one shared attempt ladder (`ai_runtime.MediaAttempt`) drives both, so the two cannot drift apart.
Transient failures are not retried here at all: rate limits, outages, timeouts, and connection errors belong to the provider retry ladders that wrap the guard, and the guard passes them straight through.

## Limitations

- **Routing with multiple eligible responders** -- without an `@mention`, the router uses the image caption to select among candidates only when room configuration and reply permissions leave multiple eligible agents or teams.
- **Bridge mention detection** uses `m.mentions` in the event, falling back to parsing HTML pills from `formatted_body` when `m.mentions` is absent (e.g., mautrix-telegram). Bridges that set neither may not trigger agent responses.
- **Blank guard-owned messages never converge** -- the wire guard refuses to teach from a retry that also gave an otherwise empty message its first text, since the added note could be what the provider accepted. A replayed or tool-produced message whose only content is its attachment therefore fails that test on every attempt, so its route never learns the kind and every turn of that conversation pays the same two calls instead of settling to one. The turn still answers correctly; only the cost fails to converge.
- **Model support** -- vision input requires a model that supports it. Text-only models reject inline images, and the [media fallback](#media-fallback) retries without them so the agent still answers with a note that it cannot view the attachment.
