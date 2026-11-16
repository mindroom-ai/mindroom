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
