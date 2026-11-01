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

## Assumptions

- tiktoken's mapping of a model id to its encoding (`o200k_base`, `o200k_harmony`, or `cl100k_base` for older models such as GPT-4) is that model's tokenizer only on a route that serves OpenAI's own models (OpenAI's endpoint, Azure, Codex, OpenRouter `openai/` ids); a llama.cpp or other OpenAI-compatible server can serve any model under an OpenAI name.
- tiktoken downloads its vocabulary on first use with no timeout.

## Decisions

- Each route declares a `TokenCounter` whose estimate runs locally: the model's own tiktoken encoding on a route that serves OpenAI's models, and o200k_base scaled by a margin per family elsewhere.
  No count goes over the network, so no reply waits for one.
- Provider counting endpoints (Anthropic `count_tokens`, Gemini `countTokens`, OpenAI's input-token count, llama.cpp `/tokenize`) were tried and removed.
  Whenever a reported request size sizes history ([compaction](compaction.md)), the compaction check takes the smaller of the static estimate that request recorded and today's off that size, and the budgets take today's static estimate off the window, so an exact count of the system prompt and tools changes the decision only by how much they changed since that request.
  What exact counts still bought was a fuller `MEMORY.md` preload on Claude and Gemini and exactness in one-off cases without a reported size, at the cost of waits of 0.2–0.5 s whenever stable text changed, a timeout, a pause after failures, a count cache, framing subtraction, and endpoint failure modes.
  OpenClaw and Hermes Agent, checked on 2026-11-01, also size context from the provider's reported usage plus a local estimate of newer messages and call no counting endpoint; their reported estimate bugs (2–7 times off) came from character heuristics and from estimating text the request never sends, which a tokenizer-based estimate with margins avoids.
- History is sized from the request size the provider reported for the latest answer ([compaction](compaction.md)), because that is a provider count already paid for.
- Estimates are o200k_base scaled by a margin per family: Claude 1.8, Gemini 1.35, OpenAI 1.0, any other model 1.25.
  The margins are upper bounds, because a cap or a compaction input that undercounts fails a request, while one that overcounts only wastes room; the Claude and Gemini margins cover the largest ratios measured above (JSON 1.798, Markdown notes 1.309), and the other margin covers a Qwen thread where o200k undercounted by about 10% (see [compaction](compaction.md)).
- Reported sizes are not turned into a route-wide ratio for other text: the ratio depends on the content (Claude Opus 5.5 ranges from 1.225 to 1.798 above), so a ratio learned from one text misjudges another, and a moving ratio would move the `MEMORY.md` cut of an unchanged file and break the conversation's cached prompt.
- Every local encode on a reply's path runs in a worker thread, and text is encoded in pieces of at most 8,192 characters, cut just before a word: tiktoken's cost grows with the square of a run without whitespace (200,000 such characters took 18 s in one piece), while the pieces counted 829 repository files exactly as one encode did.
  A stretch of 8,192 characters with no word start is cut anyway, which can add a token per cut.
- While o200k_base loads, and for good when it cannot, estimates count a quarter token per character, and sizes that must not undercount (compaction input, the `MEMORY.md` preload) count UTF-8 bytes.
  The byte count is an upper bound but triples history estimates, which would compact conversations that fit on the first reply after a start.
- The `MEMORY.md` preload counts each line with its newline and keeps the curated head, then the newest entries, within the token cap.
  Counting line by line rounds each line up, so the preload can stop a little under the cap; that keeps the cut a single pass whose result never changes for an unchanged file.

## Limitations

- Caps and compaction on Claude and Gemini act early, because their estimates are upper bounds: on the samples above, a Claude Opus 5.5 estimate is 1.0 to 1.47 times the real count, a Claude Haiku 4.5 estimate up to 1.75 times, and a Gemini estimate 1.03 to 1.68 times, so a 50,000-token `MEMORY.md` cap preloads 34,000 to 50,000 real Claude Opus 5.5 tokens.
- The margins come from six samples; a future tokenizer that counts more than its family margin times o200k_base undercounts until the margin is measured again.
- OpenAI models tiktoken does not map, such as the GPT-6 family, estimate with o200k_base at margin 1.0, which GPT-6 Astra's input-token endpoint matched exactly in the live run above.
- Without a usable reported size, such as under a replay limit, after another model answered, or once a native checkpoint exists, history is sized by its estimate, so a Claude or Gemini conversation can compact early.
- An approval resume and a team member's run have no prepared history size, so the mid-turn hook sizes their first request by estimate, and a Claude or Gemini conversation can compact there early; anchoring on the paused run's own response would need to tell it apart from other models' and members' assistant messages in the request.
