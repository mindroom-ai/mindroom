# Memory recall benchmark

Offline replay of real logged turns, used to pick a cheaper per-turn memory recall.
The design is in `docs/baspowers/specs/2026-11-16-memory-recall-efficiency-design.md`.

## Arms

| Arm | Corpus | Chunks | Selection | Skip |
|---|---|---|---|---|
| A0 | all of `memory/**/*.md` | stock 5,000 characters | top 3 | no |
| A1 | without `memory/archive/**`, session dumps (`memory/YYYY-MM-DD-HHMM.md`), other agents' memory (`memory/agent_*/**`) | stock | top 3 | no |
| A2 | as A1 | one per memory entry (at most 1,200 characters) | top 3 | no |
| A3 | as A1 | per entry | `select_recall_hits`: score gap 0.05, at most 5, 800 tokens | no |
| A4 | as A1 | per entry | as A3 | under 40 characters in a thread |
| shipped | all of `memory/**/*.md`, which the shipped overlay filters with `memory.search.exclude: [memory/archive/**]`, as Bas's deployment sets it | the shipped overlay's entry chunks | as A3, over 20 hits | no |
| shipped_default | as `shipped`, with the default `memory.search`, which excludes nothing | as `shipped` | as `shipped` | no |

Every arm indexes its corpus with MindRoom's own knowledge indexer (`KnowledgeManager.reindex_all`) and the embedder in
`memory.embedder`, and searches it through the file-memory overlay with hybrid search.
A0 to A4 use the overlay as it was before A3 shipped (fixed 5,000-character chunks, no excludes); `shipped` and
`shipped_default` use the current one, so they measure the agent's own semantic hits in the shipped per-turn recall
(team keyword hits and the cold-index keyword fallback are not replayed).
A0 to A4 over-fetch 25 hits per query, the most that keep that recall's ranking: the store fuses `max(limit * 2, 50)`
candidates per ranking, and it asked for 3. `shipped` and `shipped_default` ask for the 20 the shipped recall does.
At most 4 searches run at once, since query embeddings share the local embedder with live agents.
A failed search (an embedder error such as HTTP 500 while it swaps models) is retried after 2, 5 and 15 seconds.
Hits that may have been written after the turn are dropped before selection:

- An entry with an `m_YYYYMMDDhhmmss_` id was written at that UTC time, and is dropped when that is after the turn.
- Otherwise its day comes from a `YYYY-MM-DD` file name, else the file's first commit in the memory directory's Git
  repository. A day-only entry is dropped from the turn's own local day on, since it may be from later that day.
- An entry with no known date is kept.
- A chunk holding several entries is dropped when any of them is.

Every section of a topic file without ids is dated by that file's first commit, even a section added much later, so
such a section can answer a turn from before it was written.
That is 23% of Harry's entry chunks and 2% of DevAgent's, which inflates absolute relevance for every arm alike;
compare arms with each other, not with live recall.

## Run

```bash
# 1. Extract the turns (see turns.py) to a JSONL file outside the repository.
# 2. Replay, judge and report:
uv run python -m evals.memory_recall.run \
  --turns /tmp/recall/turns.jsonl \
  --memory-dir ~/.mindroom-chat/mindroom_data/agents/<agent>/workspace/memory \
  --config ~/.mindroom-chat/config.yaml \
  --arms A0,A1,A2,A3,A4,shipped,shipped_default \
  --out /tmp/recall/out \
  --key-env OPENROUTER_API_KEY   # judge key; --limit N replays only the first N turns
```

Output in `--out`:

- `results.json` and `results.md`: per-arm metrics, numbers only.
- `index/<hash>/`: each arm's corpus and index, reused by reruns over the same corpus and embedder.
- `judge.sqlite`: cached verdicts, so each run only judges hits no earlier run judged.

The index and verdict cache hold memory text, so keep `--out` outside the repository and never commit it.

## Metrics

Per arm, over the turns whose search succeeded (tokens are o200k, counted over the rendered memory block):

- `turns`, `mean_tokens`, `p95_tokens`: turns searched and injected tokens per turn.
- `relevant_per_1k_tokens`: hits judged directly relevant (2) per 1,000 injected tokens.
- `turns_with_relevant`: share of turns with at least one directly relevant hit.
- `unrelated_only_turns`: share of turns that injected hits, all judged unrelated (0).
- `abstained_turns`: share of turns that injected nothing.
- `judge_errors`: hits the judge failed on. They count nowhere else: their tokens leave the per-token denominator, and
  a turn whose every hit failed leaves the two turn shares above.
- `search_errors`: turns whose search still failed after four attempts. They count nowhere else, not even in
  `turns`.
