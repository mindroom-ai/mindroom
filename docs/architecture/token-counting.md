# Token counting

This page records the assumptions, decisions, and evidence behind counting tokens per provider route.
User-facing behavior is in [How tokens are counted](../configuration/history.md#how-tokens-are-counted).

## Evidence

Token counts depend on both the tokenizer and the content, so no single ratio works.
Measured against Anthropic's token counting endpoint on six samples, the 8–9 tokens of message framing subtracted (issue #3482):

| Sample | Characters | chars / 4 | o200k | Claude Opus 5.5 | Claude Haiku 4.5 |
|---|---:|---:|---:|---:|---:|
| English prose | 15,840 | 3,960 | 3,001 | 4,559 | 3,120 |
| Markdown notes | 21,600 | 5,400 | 7,400 | 9,998 | 7,798 |
| Python source | 3,543 | 885 | 800 | 1,355 | 1,022 |
| Markdown docs | 20,000 | 5,000 | 4,675 | 7,118 | 5,283 |
| JSON tool schemas | 14,350 | 3,587 | 3,842 | 5,360 | 3,960 |
| Japanese text | 5,400 | 1,350 | 4,080 | 4,919 | 4,920 |

Characters / 4 missed Claude Opus 5.5 by 13–73% and overcounted Claude Haiku 4.5 prose by 27%.
o200k undercounted Claude Opus 5.5 by 17–41%, and UTF-8 bytes were 2.6–3.5 times the real count.
A provider count of a 1 KB to 400 KB text took 0.2–0.5 s.

A live run on 2026-10-31 counted six similar samples through each provider's token counting endpoint and through a second, independently framed request to the same endpoint (system text or instructions instead of a user message); the two agreed within 0.07% on Claude Opus 5.5 and exactly on Gemini 3.8 Flash, and GPT-6 Astra's input-token endpoint counted every sample exactly as o200k_base did.
Provider tokens per o200k_base token on those samples:

| Model | Prose | Notes | Python | Docs | JSON | Japanese |
|---|---:|---:|---:|---:|---:|---:|
| Claude Opus 5.5 | 1.587 | 1.225 | 1.696 | 1.523 | 1.798 | 1.276 |
| Gemini 3.8 Flash | 1.022 | 1.309 | 1.256 | 1.119 | 1.042 | 0.803 |
| GPT-6 Astra | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

A live run on 2026-11-02 sent three replies per model through the reply path, a first prompt of about 26,000 characters of docs, JSON, and Python, then two short follow-ups, and compared each reply's prepared first-request size with the size the provider reported:

| Model | Reply 1 (estimated) | Reply 2 (reported size) | Reply 3 (reported size) |
|---|---:|---:|---:|
| GPT-6 Luna | −0.2% | +1.3% | 0.0% |
| Gemini 3.8 Flash | +20.7% | +0.3% | +0.2% |
| Claude Sonnet 5.5 (Vertex AI) | +19.6% | +0.2% | +0.2% |

Gemini bills a replayed thought signature about as the reasoning its reply reported: before its route's replay estimate added it, replies that reported 476 and 389 reasoning tokens grew the next requests by 447 and 385 tokens beyond their text, and replies 2 and 3 came out 4.7% and 3.7% low.

## Assumptions

- tiktoken's mapping of a model id to its encoding (`o200k_base`, `o200k_harmony`, or `cl100k_base` for older models such as GPT-4) is that model's tokenizer only on a route that serves OpenAI's own models (OpenAI's endpoint, Azure, Codex, OpenRouter `openai/` ids); a llama.cpp or other OpenAI-compatible server can serve any model under an OpenAI name.
- tiktoken downloads its vocabulary on first use with no timeout.

## Decisions

- Each route declares a `TokenCounter` whose estimate runs locally: the model's own tiktoken encoding on a route that serves OpenAI's models, and o200k_base scaled by a margin per family elsewhere.
  No count goes over the network, so no reply waits for one.
- Provider counting endpoints (Anthropic `count_tokens`, Gemini `countTokens`, OpenAI's input-token count, llama.cpp `/tokenize`) were tried and removed.
  A reported request size sizes a request only while everything before the answer it describes is unchanged, the system prompt and tools included, so an exact count of the system prompt and tools would change no decision a reported size makes.
  What exact counts still bought was a fuller `MEMORY.md` preload on Claude and Gemini and exactness in one-off cases without a reported size, at the cost of waits of 0.2–0.5 s whenever stable text changed, a timeout, a pause after failures, a count cache, framing subtraction, and endpoint failure modes.
  OpenClaw and Hermes Agent, checked on 2026-11-01, also size context from the provider's reported usage plus a local estimate of newer messages and call no counting endpoint; their reported estimate bugs (2–7 times off) came from character heuristics and from estimating text the request never sends, which a tokenizer-based estimate with margins avoids.
- A request is sized from the request size the provider reported for the latest answer it extends ([compaction](compaction.md)), because that is a provider count already paid for.
- Each provider route sizes a request from the body its own builder renders for it, through the function it sends with (`ModelProvider.request_tokens`), and the request pipeline sizes through the rewrites it applies when sending, such as portable tool search and the media fallback's note.
  That makes whatever a route sends, such as `reasoning_content` on the DeepSeek and Kimi routes, earlier thinking on Claude, Codex instructions, or a forced output tool, part of the size by construction, where a separate model of each request had to be taught every wire rule a second time and missed several.
  The rejected alternative was the request builder returning each request with its size: the builder never holds the final request, because per-request hooks add tool parameters and the approval receipt, folds rewrite messages, and pipeline stages rewrite media and deferred tools, so a size carried beside a request went stale (an approval resume left the receipt out, and a continuation re-added history a fold had summarized); a correct size needs the route on the request that is sent.
  Text the model reads counts as text, values it reads as JSON (tool arguments, schemas, function responses) count serialized, and transport (ids, roles, signatures, media bytes) counts nothing; ciphertext counts what its answer reported (encrypted reasoning and Gemini thought signatures their reasoning tokens, replayed Claude thinking the larger of its text and its answer's output beyond the visible part), ciphertext no answer reports, such as a native checkpoint or a Claude server tool's encrypted search result, a quarter token per character, and media counts a fixed allowance, or visual patches on the Responses routes that price them, except that a file of UTF-8 text counts its text, which every route sends as text or as a document its provider reads as text, and a PDF counts the allowance per page, as Claude and Gemini price it.
  PDF pages are counted by scanning the file for page objects, inflating its object streams, where pdfTeX keeps them, instead of with a PDF parser: pypdf is not a core dependency, and it took 125 ms to count a 110-page pdfTeX file, which every size of a request that holds the file would repeat, while the scan took 3 ms.
  A Responses request that continues a stored response is sized as the whole history it continues, since the model reads it all.
- Each answer records the identity of the request it answered: a digest of the route's context key (the model, its endpoint, and what the route renders besides the messages, such as the system prompt, tools, and instructions), the inputs of the pipeline's rewrites, and each message's sent fields, with media by their content (inline bytes by digest, local files by size and modification time).
  A later request whose content before an answer has that identity extends that answer's request, so the answer's reported size plus the route's size of the answer and what follows it is the request's size; that one rule sizes a fresh reply's first request, each request of a tool loop, an approval resume, and a team member's resume.
  It replaced a digest of each run's system prompt, tools, summary, and history, checked separately before a reply, a positional rule inside a tool loop, and a size passed from preparation to the first request.
  Queued-message notices are left out of the identity, because a reply only removes one or moves it last, which leaves the reported size counting it a few tokens high.
  An answer whose attempt a pipeline stage retried as another request, such as one without media the provider rejected, records no identity, because its reported size describes that other request; nor does a request holding media at a URL, whose content can change between requests unseen.
  The latest native checkpoint bounds the answers that anchor, because projection replaced the transcript their reported sizes describe.
  Each request's identities are digested afresh, without a cache of message digests: a cache kept per message object would need every edit made in place, such as a fold marking its prompt, to drop it, and digesting 0.7 MB of history took about 8.5 ms, off the event loop.
- Budgets bound a whole request, the way providers report its size: compaction triggers when the first request of a reply exceeds the trigger, and no request may exceed the replay window minus the reserve; the history's share, the request beyond what it holds besides its history, appears only in notices and as the room a summary or a trimmed replay may take.
  A history's or a summary's share is the route's size of a request holding it less that of a request holding no messages, because a route sends some content with every request, such as Codex's default instructions or tools authored in `request_params`, which the request without its history already counts; for the same reason the size from an answer on never counts that content.
  The size from an answer on is the route's rendering of that answer and what follows it, not the whole request's estimate less the estimate of the request the answer answered: estimates are padded upper bounds, so the difference would subtract what a later request drops, such as earlier thinking a route stops replaying, by its margin and undercount, while a rendering of the tail can only overcount what it repeats.
- Estimates are o200k_base scaled by a margin per family: Claude 1.8, Gemini 1.35, OpenAI 1.0, any other model 1.25.
  The margins are upper bounds, because a cap or a compaction input that undercounts fails a request, while one that overcounts only wastes room; the Claude and Gemini margins cover the largest ratios measured above (JSON 1.798, Markdown notes 1.309), and the other margin covers a Qwen thread where o200k undercounted by about 10% (see [compaction](compaction.md)).
- Reported sizes are not turned into a route-wide ratio for other text: the ratio depends on the content (Claude Opus 5.5 ranges from 1.225 to 1.798 above), so a ratio learned from one text misjudges another, and a moving ratio would move the `MEMORY.md` cut of an unchanged file and break the conversation's cached prompt.
- Every local encode on a reply's path runs in a worker thread, and text is encoded in pieces of at most 8,192 characters, cut just before a word: tiktoken's cost grows with the square of a run without whitespace (200,000 such characters took 18 s in one piece), while the pieces counted 829 repository files exactly as one encode did.
  A stretch of 8,192 characters with no word start is cut anyway, which can add a token per cut.
- While o200k_base loads, and for good when it cannot, estimates count a quarter token per character, and sizes that must not undercount (compaction input, a compaction summary before it is stored, the `MEMORY.md` preload) count UTF-8 bytes.
  The byte count is an upper bound but triples history estimates, which would compact conversations that fit on the first reply after a start.
- The `MEMORY.md` preload counts each line with its newline and keeps the curated head, then the newest entries, within the token cap.
  Counting line by line rounds each line up, so the preload can stop a little under the cap; that keeps the cut a single pass whose result never changes for an unchanged file.

## Limitations

- Caps and compaction on Claude and Gemini act early, because their estimates are upper bounds: on the samples above, a Claude Opus 5.5 estimate is 1.0 to 1.47 times the real count, a Claude Haiku 4.5 estimate up to 1.75 times, and a Gemini estimate 1.03 to 1.68 times, so a 50,000-token `MEMORY.md` cap preloads 34,000 to 50,000 real Claude Opus 5.5 tokens.
- The margins come from six samples; a future tokenizer that counts more than its family margin times o200k_base undercounts until the margin is measured again.
- OpenAI models tiktoken does not map, such as the GPT-6 family, estimate with o200k_base at margin 1.0, which GPT-6 Astra's input-token endpoint matched exactly in the live run above.
- Without a usable reported size, such as after a replay limit dropped or cut part of the history, another model answered, the system prompt or tools changed, or the latest answer created a native checkpoint, a request is sized by its estimate, so a Claude or Gemini conversation can compact early; so is a team member's first request, which follows no answer, and, once after upgrading to the release that records request identities, each conversation's first reply.
- A reported size can count a little high: Claude without a thinking binding drops earlier turns' thinking the reported request still held, and a queued-message notice the reply moved counts at both places.
