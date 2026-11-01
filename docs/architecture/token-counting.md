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

A live run on 2026-10-31 counted six similar samples through each route's counting path and through a second, independently framed request to the same endpoint (system text or instructions instead of a user message); the two agreed within 0.07% on Claude Opus 5.5 and exactly on Gemini 3.8 Flash, and GPT-6 Astra's input-token endpoint counted every sample exactly as o200k_base did.
Provider tokens per o200k_base token on those samples:

| Model | Prose | Notes | Python | Docs | JSON | Japanese |
|---|---:|---:|---:|---:|---:|---:|
| Claude Opus 5.5 | 1.587 | 1.225 | 1.696 | 1.523 | 1.798 | 1.276 |
| Gemini 3.8 Flash | 1.022 | 1.309 | 1.256 | 1.119 | 1.042 | 0.803 |
| GPT-6 Astra | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

## Assumptions

- The counting endpoints are free and rate-limited apart from generation: Anthropic `count_tokens` (also Vertex AI `count-tokens:rawPredict` and Bedrock), Gemini `countTokens`, OpenAI's input-token count, and llama.cpp `/tokenize`.
- Each endpoint except llama.cpp counts a whole message, so a count includes the message framing; a one-character message counts that framing plus one token, because every such route's tokenizer encodes `"a"` as one token.
- tiktoken's mapping of a model id to its encoding (`o200k_base`, `o200k_harmony`, or `cl100k_base` for older models such as GPT-4) is that model's tokenizer only on a route that serves OpenAI's own models (OpenAI's endpoint, Azure, Codex, OpenRouter `openai/` ids); a llama.cpp or other OpenAI-compatible server can serve any model under an OpenAI name.
- tiktoken downloads its vocabulary on first use with no timeout.

## Decisions

- Each route declares a `TokenCounter`; every count carries its kind: exact (the model's own tokenizer ran locally, or the provider counted the same text) or estimated.
- A reply waits for the provider to count text it sizes that the provider has not counted yet: the system prompt's messages, the tool definitions, `MEMORY.md` and its preload when its estimate exceeds the token cap, and history that no reported request size describes, except history a native checkpoint stands in for, whose ciphertext no provider counts as text.
  Each count has a five-second limit, and a failed count pauses that route's counting for five minutes, so one broken endpoint costs a reply at most one wait.
  The rejected alternative counted in the background and estimated until the count arrived: replies never waited, but filling the gap took calibration from earlier counts, a per-file rate memory, and frozen counters, and every reply until a count arrived ran on an estimate.
- History is sized from the request size the provider reported for the latest answer ([compaction](compaction.md)), because it changes every reply, so counting it would make every reply wait, and the reported size is a provider count already paid for.
- Text that changes every reply and stays small (the current prompt, the newest answer) and text whose size only bounds a request (compaction input, the skill review transcript, a voice call's brief, mid-turn sizing) keeps the estimate: a wait for each would cost more than its error.
- Estimates are o200k_base scaled by a margin per family: Claude 1.8, Gemini 1.35, OpenAI 1.0, any other model 1.25.
  The margins are upper bounds, because a cap or a compaction input that undercounts fails a request, while one that overcounts only wastes room; the Claude and Gemini margins cover the largest ratios measured above (JSON 1.798, Markdown notes 1.309), and the other margin covers a Qwen thread where o200k undercounted by about 10% (see [compaction](compaction.md)).
- Provider counts are not turned into a route-wide ratio for other text: the ratio depends on the content (Claude Opus 5.5 ranges from 1.225 to 1.798 above), so a ratio learned from the system prompt misjudged history and compaction input, and a moving ratio moved the `MEMORY.md` cut of an unchanged file and broke the conversation's cached prompt.
- Every local encode on a reply's path runs in a worker thread, and text is encoded in pieces of at most 8,192 characters, cut just before a word: tiktoken's cost grows with the square of a run without whitespace (200,000 such characters took 18 s in one piece), while the pieces counted 829 repository files exactly as one encode did.
  A stretch of 8,192 characters with no word start is cut anyway, which can add a token per cut; such counts keep the exact label, since the error stays far below the provider's own framing.
- While o200k_base loads, and for good when it cannot, estimates count a quarter token per character, and compaction input its UTF-8 bytes.
  The byte count is an upper bound but triples history estimates, which would compact conversations that fit on the first reply after a start.
- The provider counts the whole `MEMORY.md`; a file over the token cap keeps that share of its characters, chosen as whole lines (the curated head, then the newest entries), and the preload those lines make is counted in turn and shrunk until it fits.
  The rejected alternative estimated each line, which only fit the cap closely once each line's estimate was scaled by the file's measured rate and that rate was remembered across appends.
- Counting may send text a reply withholds, such as the part of `MEMORY.md` past the preload cap, when it goes to the provider of the agent's own model: that model reads those files through its tools anyway.
- Exact counts stay in memory; after a restart, the first reply that sizes each stable input waits for its count once more.
- Parts, not whole requests, are counted: each part caches on its own and changes on its own schedule.

## Limitations

- Ollama, OpenRouter models other than OpenAI's, and other OpenAI-compatible servers have no counting endpoint wired, so their counts stay estimated with fixed margins; vLLM's `/tokenize` is not used because no route identifies a vLLM server.
- OpenAI models tiktoken does not map, such as the GPT-6 family, count exactly only through the input-token endpoint of OpenAI's own Responses route; on Codex, Azure, and Chat Completions they stay estimated.
- `prompt_curation` measures file sizes by the model's estimate, not provider counts: its checks run in a worker thread, apart from the event loop the provider clients use, and verify must compare the edited files on the basis the plan measured.
  It judges whether the `MEMORY.md` preload withholds lines from the provider's count of the file once a reply has counted it.
- A scope whose history no reported request size describes, such as one under a replay limit, waits for a count of its whole history on every reply.
