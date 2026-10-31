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
- tiktoken's mapping of a model id to `o200k_base` or `o200k_harmony` is that model's tokenizer only on a route that serves OpenAI's own models (OpenAI's endpoint, Azure, Codex, OpenRouter `openai/` ids); a llama.cpp or other OpenAI-compatible server can serve any model under an OpenAI name.
- tiktoken downloads its vocabulary on first use with no timeout.

## Decisions

- Each route declares a `TokenCounter`; every count carries its kind: exact (the model's own tokenizer ran locally, or the provider counted the same text), calibrated (the route's estimate scaled by what its provider counted for other text), or estimated.
- A reply never waits for a provider count.
  The system prompt's messages, the tool definitions, and the `MEMORY.md` preload are counted in the background, a few at a time and each with a time limit; a later reply finds them cached.
  A failed count pauses that route's counting for a while, so one broken endpoint does not cost every reply a failed call.
- The family margins are upper bounds, because a cap or a compaction input that undercounts fails a request, while one that overcounts only wastes room.
  The compaction check before a reply is the exception: there a margin estimate would compact conversations that fit, so the provider's reported size of the latest request wins over an estimate that no provider count has calibrated yet.
- Estimates are o200k_base scaled by a margin per family: Claude 1.8, Gemini 1.35, OpenAI 1.0, any other model 1.25.
  The Claude and Gemini margins cover the largest ratios measured above (JSON 1.798, Markdown notes 1.309); the other margin covers a Qwen thread where o200k undercounted by about 10% (see [compaction](compaction.md)).
  Calibration replaces the margin once the provider has counted text.
- Calibration learns only from the provider's counts of text, never from the input tokens a request reported.
  Reported usage is a different quantity: it carries message framing, content the provider adds (Claude's structured-output tool, Gemini's cached content, earlier thinking), and per-route cache accounting, so a ratio learned from it would skew every text count.
  History sizing keeps the reported size of the latest request as its own anchor ([compaction](compaction.md)).
  Calibration is kept per route, not per agent, because a per-agent key would thread an agent name through every count for a second-order difference in content mix.
- Every local encode on a reply's path runs in a worker thread, and text is encoded in pieces of at most 8,192 characters cut at a newline or space: tiktoken's cost grows with the square of a run without whitespace (200,000 such characters took 18 s in one piece), while the pieces counted 85 repository files exactly as one encode did.
- While o200k_base loads, and for good when it cannot, estimates count a quarter token per character.
  The UTF-8 byte count is an upper bound but triples history estimates, which would compact conversations that fit on the first reply after a start.
- The `MEMORY.md` preload selects whole lines by their uncalibrated estimates, then counts the preload they make, which the provider counts exactly in the background, and rescales the line estimates until the preload fits.
  Per-line provider counts would send every withheld line and cost a call per line, and calibrated line counts would move the cut of an unchanged file whenever the provider counted other text, breaking the conversation's cached prompt.
- Counting may send text a reply withholds, such as the part of `MEMORY.md` past the preload cap, when it goes to the provider of the agent's own model: that model reads those files through its tools anyway.
- Exact counts stay in memory; a restart counts each stable input once more, in the background.
- Parts, not whole requests, are counted: each part caches on its own and changes on its own schedule.

## Limitations

- Ollama, OpenRouter models other than OpenAI's, and other OpenAI-compatible servers have no counting endpoint wired, so their counts stay estimated with fixed margins; vLLM's `/tokenize` is not used because no route identifies a vLLM server.
- OpenAI models tiktoken does not map, such as the GPT-6 family, count exactly only through the input-token endpoint of OpenAI's own Responses route; on Codex, Azure, and Chat Completions they stay estimated.
- `prompt_curation` measures with the calibration frozen when it plans, not with provider counts, because verify must compare the edited files on the basis the plan measured; its checks also run synchronously, with no event loop to await a count on.
