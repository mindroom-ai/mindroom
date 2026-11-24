# Memory Recall Efficiency Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development — six tasks with shared interfaces and a costly default change at the end; fresh per-task review is worth it.

**Goal:** Build a replay benchmark over real logged turns, use it to pick a cheaper per-turn memory recall, and ship the winner as the default.

**Architecture:** `evals/memory_recall/` extracts real turns from `debug.log_llm_requests` logs, replays each candidate arm (corpus filter, chunking, hit selection, skip rule) through MindRoom's own knowledge indexer and search, judges new hits with a cached LLM judge, and reports metrics. The chunking and selection logic lives in product code from the start, so the eval measures exactly what ships.

**Tech Stack:** Python 3, MindRoom knowledge stack (`KnowledgeManager`, `KnowledgeCollection`, hybrid search), tiktoken `o200k_base`, httpx to OpenRouter for the judge, pytest.

**Spec:** `docs/baspowers/specs/2026-11-16-memory-recall-efficiency-design.md`

## Global Constraints

- Never commit real logs, memory text, judged verdicts or results containing memory text; only the committed fixture corpus and aggregate numbers.
- Token counts use tiktoken `o200k_base`.
- Judge: `deepseek/deepseek-v4.1-flash-1017` via OpenRouter, temperature 0, `response_format: json_object`; API key from the env var named by `--key-env` (default `OPENROUTER_API_KEY`).
- Over-fetch 50 hits per query before date filtering and selection.
- Entry max size: 1,200 characters; selection token budget: 800 o200k tokens; max hits: 5.
- Short message: under 40 characters after stripping.
- Tests: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest <files> -x -n 0 --no-cov'`.
- Every commit ends with `--trailer "Assisted-by: MindRoom <agent display name>:<model id> <https://github.com/mindroom-ai/mindroom>"` (your own agent and model).

## Review Focus

- Topic files with no `[id=m_…]` entries and no date in the file name: date falls back to the file's first commit; without git it is "unknown" and kept (never silently dropped).
- One entry longer than 1,200 characters: split at whitespace into several entries, each keeping the parent's date.
- Logs in both formats: old top-level `messages`/`system_prompt`/`current_turn_prompt`, new `body.messages`; both must yield turns.
- Judge returns non-JSON or the wrong number of verdicts: retry 3 times, then record an error; errored pairs are excluded from metrics, never counted as irrelevant.
- A selection that keeps zero hits must inject nothing (no empty template block).

---

### Task 1: Turn extraction from request logs

**Files:**
- Create: `evals/memory_recall/__init__.py`, `evals/memory_recall/turns.py`
- Create: `tests/evals/test_memory_recall_turns.py`, `tests/evals/fixtures/memory_recall/requests.jsonl` (synthetic, both formats)

**Interfaces:**
- Produces: `@dataclass(frozen=True) class Turn: ts: datetime; agent: str; session_id: str; query: str; injected_hits: tuple[str, ...]; injected_tokens: int`; `def extract_turns(log_paths: Sequence[Path], agents: Collection[str]) -> list[Turn]`; `def write_turns(turns, path: Path) -> None`, `def read_turns(path: Path) -> list[Turn]` (JSONL).

- [ ] **Step 1: Write failing tests** — `test_extracts_new_format_turn`, `test_extracts_old_format_turn`, `test_dedupes_tool_loop_requests` (three requests of one turn sharing a transient block give one `Turn`), `test_query_is_current_message` (`query` is the text after `Current message:` when present, else `current_turn_prompt`), `test_hits_split_on_list_items` (a block with 3 `- ` hits gives `len(injected_hits) == 3`).
- [ ] **Step 2: Run; expect FAIL (module missing).**
- [ ] **Step 3: Implement `extract_turns`** — stream each log with `grep -F` prefilter on `"agent_id": "<agent>"` (logs are 1–3 GB/day), `json.loads` per line, find the last user message containing `Automatically extracted`, dedupe on `(session_id, block text)`. Logic matches the audit script `memrecall/extract.py` in DevAgent's workspace.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat(evals): extract memory-recall turns from request logs`.

### Task 2: Cached relevance judge

**Files:**
- Create: `evals/memory_recall/judge.py`, `tests/evals/test_memory_recall_judge.py`

**Interfaces:**
- Consumes: `Turn` (Task 1).
- Produces: `PROMPT_VERSION = "v1"`; `@dataclass(frozen=True) class Verdict: relevance: int; why: str`; `class JudgeCache` (SQLite file, key `sha256(model, PROMPT_VERSION, query, hit_text)`) with `get(key) -> Verdict | None` and `put(key, verdict)`; `async def judge_hits(query: str, hits: Sequence[str], *, cache: JudgeCache, client: httpx.AsyncClient, model: str, api_key: str) -> list[Verdict | None]` (None = errored).

- [ ] **Step 1: Write failing tests** with `httpx.MockTransport`: `test_cached_pair_skips_request` (second call makes zero requests), `test_wrong_count_retries_then_none` (3 bad responses give `[None, None]`), `test_parses_relevance_0_1_2`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** Rubric text is the audit's `PROMPT` in `memrecall/judge.py`, minus the reply and "used" parts (replay has no reply). Only uncached hits go into one request per turn.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat(evals): cached LLM relevance judge for memory recall`.

### Task 3: Memory entry splitting and recall hit selection (product code)

**Files:**
- Create: `src/mindroom/memory/entries.py`
- Modify: `src/mindroom/memory/_prompting.py` (add `select_recall_hits`)
- Test: `tests/test_memory_entries.py`, `tests/test_memory_recall_selection.py`

**Interfaces:**
- Produces: `@dataclass(frozen=True) class MemoryEntry: text: str; date: date | None`; `def split_memory_entries(text: str, *, file_name: str, max_chars: int = 1200) -> list[MemoryEntry]`; `def select_recall_hits(results: Sequence[MemoryResult], *, max_hits: int = 5, relative_gap: float = 0.05, token_budget: int = 800) -> list[MemoryResult]`; `def is_short_follow_up(message: str, *, in_thread: bool) -> bool`.

- [ ] **Step 1: Write failing tests:**
  - `test_splits_daily_note_on_id_entries`: three `- [id=m_20261109111659_x] …` lines give 3 entries dated 2026-11-09.
  - `test_splits_topic_file_on_headings`: `## A`/`## B` sections give 2 entries, date from a `2026-02-19.md` file name, else `None`.
  - `test_long_entry_split_at_whitespace`: a 3,000-char entry gives 3 entries ≤ 1,200 chars, same date.
  - `test_selection_drops_hits_beyond_gap`: scores `[0.80, 0.78, 0.60]` keep 2.
  - `test_selection_respects_token_budget`: two 600-token hits keep 1.
  - `test_selection_empty_input_returns_empty`.
  - `test_short_follow_up`: `("ok", in_thread=True)` is True; `("ok", in_thread=False)` is False; 40+ chars is False.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** Over-long entries reuse `SafeFixedSizeChunking` from `knowledge/chunking.py`. Token counts via the existing tiktoken helper if the repo has one, else `tiktoken.get_encoding("o200k_base")`. Nothing calls these yet.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat(memory): entry splitting and score-gap recall selection`.

### Task 4: Replay arms and report

**Files:**
- Create: `evals/memory_recall/arms.py`, `evals/memory_recall/run.py`, `evals/memory_recall/README.md`
- Create: `tests/evals/fixtures/memory_recall/memory/` (5–6 small synthetic notes: two daily, one topic, one `archive/` copy), `tests/evals/test_memory_recall_run.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces: `@dataclass(frozen=True) class Arm: name: str; exclude: tuple[str, ...]; entry_chunks: bool; selection: Literal["top3", "gap"]; skip_short: bool`; `ARMS: dict[str, Arm]` holding A0–A4 exactly as in the spec table; `async def replay(turns, arm, *, memory_dir: Path, config: Config) -> list[list[MemoryResult]]`; CLI `python -m evals.memory_recall.run --turns T.jsonl --memory-dir D --config ~/.mindroom-chat/config.yaml --arms A0,A1,A2,A3,A4 --out DIR [--key-env ENV] [--limit N]` writing `DIR/results.json` and `DIR/results.md`.

- [ ] **Step 1: Write failing test** `test_run_smoke_on_fixture`: fake embedder (use `install_knowledge_embedder` from `tests/knowledge_test_support.py`) and a `MockTransport` judge; assert `results.json` has every arm with keys `turns`, `mean_tokens`, `p95_tokens`, `relevant_per_1k_tokens`, `turns_with_relevant`, `unrelated_only_turns`, `abstained_turns`, `judge_errors`, and that A1 never returns a hit from `archive/`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement.** Per arm: copy the memory dir into a temp corpus, dropping `exclude` globs; with `entry_chunks`, write each `MemoryEntry` as its own file named `<YYYY-MM-DD or undated>__<n>.md`, so the stock fixed chunker keeps each entry whole. Index the corpus through `KnowledgeManager.reindex_all()` with the embedder from `config.memory.embedder`, as `run_refresh_child_in_process` in `tests/knowledge_test_support.py` does. Search each query with limit 50, drop hits dated after `turn.ts` (date from the entry file name; for non-entry arms, from `split_memory_entries` of the hit), then select (`top3` or `select_recall_hits`). With `skip_short`, a short follow-up injects nothing. Cache the index per (arm corpus hash) under `DIR/index/`.
- [ ] **Step 4: Run; expect PASS.**
- [ ] **Step 5: Commit** `feat(evals): replay memory-recall arms over logged turns`.

### Task 5: Run the benchmark and decide

No code. Run on lab hardware (not pc; check `~/repos/MACHINES.md` first), the embedder at `http://llama.local/v1`.

- [ ] **Step 1:** Extract Harry's turns (`agents harry,openclaw`, all logs) and DevAgent's (`mindroom_dev`), into a path outside the repo.
- [ ] **Step 2:** Run A0–A4 on both. A0 must reproduce the audit within ±5 points on `turns_with_relevant` (48% for Harry) — if not, stop and fix the harness.
- [ ] **Step 3:** Re-judge 30 random turns of the winning arm with `--model` set to a second model; record agreement.
- [ ] **Step 3b:** If A3 meets the token target but not the relevance guard, add arm A5 (A3 plus the existing `knowledge/rerank.py` reranker over the top 20) and run it; otherwise skip A5.
- [ ] **Step 4:** Apply the spec's decision rule. Append a `## Results` table (aggregates only) and the chosen arm to the spec; commit `docs: memory recall benchmark results`.

### Task 6: Ship the winning arm as the default

Steps below assume the winner is A3 or A4; drop the parts of arms that lost.

**Files:**
- Modify: `src/mindroom/config/memory.py` (`MemorySearchConfig.exclude`, default `["memory/archive/**"]`; `AgentMemorySearchConfig.exclude`)
- Modify: `src/mindroom/config/knowledge.py` (`KnowledgeBaseConfig.chunk_strategy: Literal["fixed", "memory_entries"] = "fixed"`)
- Modify: `src/mindroom/knowledge/indexing_config.py` (include `chunk_strategy` in the settings fingerprint so the index rebuilds)
- Modify: `src/mindroom/knowledge/file_indexer.py` (use `split_memory_entries` when `chunk_strategy == "memory_entries"`)
- Modify: `src/mindroom/knowledge/file_memory_knowledge.py` (overlay passes `exclude_patterns` and `chunk_strategy="memory_entries"`)
- Modify: `src/mindroom/memory/functions.py:~279` (per-turn path searches with limit 20, applies `select_recall_hits`, and with A4 skips on `is_short_follow_up`)
- Test: `tests/test_file_memory_knowledge.py`, `tests/test_memory_functions.py` (or the nearest existing per-turn prompt test)

- [ ] **Step 1: Write failing tests:** `test_overlay_excludes_archive`, `test_overlay_uses_memory_entry_chunks`, `test_chunk_strategy_change_rebuilds_index`, `test_per_turn_recall_uses_gap_selection`, and, if A4 won, `test_short_follow_up_skips_recall`. `search_memories` (tool path) must still honour its own `limit` — `test_tool_search_limit_unchanged`.
- [ ] **Step 2: Run; expect FAIL.**
- [ ] **Step 3: Implement the wiring.**
- [ ] **Step 4: Run the touched test files plus `tests/test_knowledge_*.py tests/test_*memory*.py`; expect PASS.**
- [ ] **Step 5:** Re-run the eval arm against the shipped code path (`--arms shipped`, an arm that calls the real per-turn function) and confirm it matches Task 5's numbers.
- [ ] **Step 6: Commit** `feat(memory): entry-level, score-gap per-turn recall`.
