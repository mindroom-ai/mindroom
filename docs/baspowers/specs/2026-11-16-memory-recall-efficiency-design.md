# Efficient per-turn memory recall, chosen by a replay benchmark

## Why

Every turn, MindRoom searches the agent's file memory with the incoming message as query
and appends the top 3 hits to the prompt (`memory/functions.py`, `limit=3`). Each hit is a
whole 5,000-character chunk (`knowledge/file_memory_knowledge.py`, `_CHUNK_SIZE = 5000`).

An audit of Harry's real traffic (2,499 turns, 2026-07-23..11-16, every hit LLM-judged;
Sol cross-check kappa 0.77) found:

- 2,478 o200k tokens injected per turn on average (November: 3,625), re-sent on each of
  the ~9.5 requests of a turn.
- 21% of hits directly relevant, 45% unrelated; the reply used 17% of hits.
- ~5,000 tokens injected per hit the reply actually used; 20% of injected tokens were in used hits.
- Hit rank 1 is relevant 26% of the time, rank 3 only 15%.
- Messages under 40 characters get a relevant hit in 26% of turns (57% for >200 characters).
- The 20 most-repeated chunks are 26% of all injections; the worst are archived copies
  (`memory/archive/**`), February session dumps, and another agent's memory (`memory/agent_email/**`).

## Goal and success criteria

Cut injected recall tokens per turn by at least 3x without losing relevant recall, measured on
real past turns, not synthetic questions:

- Primary: relevant hits per 1K injected tokens (from ~0.25 today).
- Guard: share of turns with at least one directly relevant hit must not drop more than
  5 points below today's 48% on the same turns.
- Report: mean and p95 injected tokens, unrelated-only turn rate, abstention rate.

## Approach: offline replay of real turns

Build an eval that replays logged turns against candidate recall configurations and judges
the new hits, then ship the winning configuration as the default.

**Data.** Queries come from `debug.log_llm_requests` logs (first request of each turn with a
transient memory block). The corpus is the agent's `memory/` directory. Agent workspaces are
git repositories. Each arm builds one index of the current corpus and over-fetches 50 hits;
hits dated after the turn are dropped before selection. A hit's date is its `m_YYYYMMDD…` id,
else its `YYYY-MM-DD` file name, else the file's first commit in the workspace repository.
This keeps the auto-flush note written from that very turn from leaking the answer.

**Judging.** One LLM judge (DeepSeek V4.1 Flash, temperature 0) rates each (turn, hit) pair
0/1/2 for relevance, using the same rubric as the audit. Verdicts are cached by hash of
(model, prompt version, query, hit text), so each arm only pays for hits no other arm has
already retrieved. A second model re-judges a 30-turn sample per run to check agreement.

**Arms (cumulative where sensible):**

| Arm | Change |
|---|---|
| A0 | Current: 5,000-char chunks, k=3 |
| A1 | A0 plus excludes: `memory/archive/**`, session dumps, other agents' memory |
| A2 | A1 plus entry-level chunks: split daily notes on `- [id=` entries and topic files on headings, capped at ~1,200 chars |
| A3 | A2 with k chosen by score: keep hits within a relative gap of the top hit, at most 5, token budget ~800 |
| A4 | A3 plus no recall when the message is under 40 characters and the turn is a reply in an existing thread |
| A5 | A3 plus the existing knowledge reranker on the top 20 (only if A3 precision stays low) |

**Decision rule, fixed before running:** ship the cheapest arm that meets both success
criteria. If none does, ship A1 (pure win, no risk) and report.

## Shipping the result

Expected code changes (final shape depends on the winning arm):

- `MemorySearchConfig` gains `exclude` globs, passed through to the overlay's
  `KnowledgeBaseConfig.exclude_patterns` (already supported by the indexer).
- File-memory overlay uses an entry-aware Markdown chunker instead of fixed 5,000-char chunks;
  index rebuild happens through the existing index-settings fingerprint.
- `search_agent_memories` for the per-turn path uses score-gap selection and a token budget
  instead of a fixed `limit=3`.
- The eval lives in `evals/memory_recall/` with a tiny committed fixture corpus and query set so
  CI runs it; real data is passed by path and never committed.

## Out of scope

Memory writing (auto-flush) quality, MEMORY.md curation, mem0 backend, pointer-only
injection and turning recall off entirely (revisit only if no arm passes).

## Risks

- Judge bias: the judge model family is also used to write some memories; mitigated by the
  cross-model sample.
- Relevance is not usefulness: the replay cannot re-run the reply. The audit showed relevance=2
  and "used" agree closely; an end-to-end check on ~40 turns can follow for the winning arm.
- Corpus differs per agent: run the eval on a second agent (DevAgent) before changing defaults.

## Results (2026-11-16)

Judge: DeepSeek V4.1 Flash (1017), rubric v1, query only (no reply). Embedder: EmbeddingGemma 2
(768 dims). Tokens are o200k counts of the formatted memory block. Turns whose search kept
failing (transient embedder HTTP 500s) are left out (0.2–0.8% per run). No judge errors.

**Harry, all turns (2,494, 2026-07-23 to 2026-11-16)**

| Arm | Mean tokens | p95 | Relevant per 1K | Turns with relevant | Unrelated-only |
|---|---:|---:|---:|---:|---:|
| A0 | 3,927 | 4,612 | 0.31 | 76.9% | 8.4% |
| A1 | 3,918 | 4,612 | 0.32 | 77.5% | 8.7% |
| A2 | 702 | 1,078 | 1.59 | 78.2% | 5.4% |
| **A3** | **636** | **809** | **1.74** | **77.4%** | **4.7%** |
| A4 | 557 | 807 | 1.64 | 65.1% | 3.8% |

**Harry, turns since 2026-10-27 (263; live embedder = replay embedder)**

| Arm | Mean tokens | p95 | Relevant per 1K | Turns with relevant | Unrelated-only |
|---|---:|---:|---:|---:|---:|
| A0 | 4,133 | 4,889 | 0.16 | 44.5% | 22.4% |
| A1 | 4,110 | 4,892 | 0.17 | 45.6% | 22.4% |
| A2 | 759 | 1,139 | 0.84 | 46.8% | 7.2% |
| **A3** | **699** | **815** | **0.87** | **46.4%** | **6.1%** |
| A4 | 699 | 815 | 0.87 | 46.4% | 6.1% |

**DevAgent (830 turns)**

| Arm | Mean tokens | p95 | Relevant per 1K | Turns with relevant | Unrelated-only |
|---|---:|---:|---:|---:|---:|
| A0 | 4,099 | 4,638 | 0.13 | 40.6% | 9.0% |
| A1 | 4,097 | 4,638 | 0.13 | 39.0% | 9.2% |
| A2 | 745 | 1,035 | 0.70 | 39.4% | 9.2% |
| **A3** | **663** | **813** | **0.75** | **37.8%** | **8.2%** |
| A4 | 660 | 813 | 0.74 | 37.5% | 8.0% |

**Baseline check.** A0 scored 76.9% turns-with-relevant, not the audit's 48%, so the plan's ±5 gate
failed. On the same 300 sampled turns with this judge, the hits Harry actually received score 63%:
the replay judge sees no reply and is more lenient than the audit judge (+15 points). The rest is
retrieval drift: live recall used gemini-embedding-2 until 2026-10-26, and only 9% of replayed
A0 hits match the logged ones (the logged hit text still exists in today's memory, 91–100%).
On the turns since the embedder switch, A0 scores 44.5%, close to the audit. Ruling: the guard
compares each arm to replayed A0 under the same judge, on the same turns (A0 − 5 points), and the
3x target compares to replayed A0 as well.

**Second judge.** Gemini 3.8 Flash re-judged 199 paired Harry turns (A0, A2 and A3 on the same
turns). It is stricter (hit-level kappa 0.45–0.47 vs DeepSeek on a 30-turn sample) but ranks the arms the
same way: turns with relevant A0 54.3%, A2 65.8% (+11.6 ± 7.1), A3 63.3% (+9.0 ± 7.4); relevant
per 1K tokens 0.18 / 1.14 / 1.26. Under DeepSeek on the same turns: A0 77.9%, A3 77.9% (+0.0 ± 6.2).

**Decision.** A3 passes both criteria on every run: 5.7x, 5.4x and 5.8x relevant hits per 1K tokens
(Harry all, Harry recent, DevAgent), with turns-with-relevant +0.5, +1.9 and −2.8 points. It is
the cheapest passing arm, because A4 fails the guard on Harry (−11.8 points). A5 is skipped. **Ship A3.**

**Caveat on A4.** Since about late October, logged queries carry the `<msg ...>` wrapper, so the
40-character test never fires on them. On the recent and DevAgent runs, A4 therefore ≈ A3, and its
measured loss comes from the older, unwrapped turns. The per-turn recall query live probably
includes the same wrapper. Searching with the bare message body is a possible follow-up.
