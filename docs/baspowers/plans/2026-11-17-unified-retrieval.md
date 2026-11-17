# Unified Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development — six PR-sized tasks with shared interfaces; PR 6 changes a per-turn default, so each task gets a fresh review.

**Goal:** One retrieval pipeline (candidates → optional rerank → selection) for `search_knowledge_base`, per-turn memory recall and `search_memories`, with OpenAI Decisions and TypeSafe System One as two more reranker providers.

**Architecture:** `AgentKnowledge.asearch` and `FileMemoryBackend.search` already share the candidate stage (hybrid search in `knowledge/vector_store.py`). They gain one shared second half, `retrieve()` in a new `knowledge/retrieval.py`, which replaces `_reranked` and `select_recall_hits`. The existing `Reranker` protocol stays the only scoring concept; its contract tightens to "scores in [0, 1]", and a judgment backend implements it with one predicate per candidate in ONE request.

**Tech Stack:** Python 3.12, pydantic v2, httpx, `mindroom.judgment`, `mindroom.knowledge`, tiktoken `o200k_base`, pytest, `evals/memory_recall`.

**Spec:** this document (design and plan in one, as Bas asked). Benchmark method and the shipped A3 baseline: `docs/baspowers/specs/2026-11-16-memory-recall-efficiency-design.md`.

## Global Constraints

- PR 1 changes no behaviour. No default changes before PR 6, and PR 6 only if an arm passes the gate below.
- Recall never blocks a reply: a reranker that fails or passes its deadline gives exactly today's hybrid selection.
- Never commit real memory text, queries, logs, verdicts or cached scores; only aggregate numbers.
- Existing single-question judgment users (`cold_cache`, `mid_turn`, `routing`, `participation`, `contacts`) keep their signatures and their request bytes.
- `memory.search.rerank` must not enter the knowledge index signature (no re-embedding).
- Token counts: `o200k_counter()`. Tests: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest <files> -x -n 0 --no-cov'`; run new test files once with default addopts.
- Every commit ends with `--trailer "Assisted-by: MindRoom <agent display name>:<model id> <https://github.com/mindroom-ai/mindroom>"`.

## Review Focus

- Reranker raises, times out, or returns a wrong count or a non-finite score during recall: result equals the hybrid selection (gap 0.05, ≤ 5 hits, ≤ 800 tokens), within the deadline.
- Every reranked score is below the threshold: nothing is injected (no empty memory block).
- Candidate text holds a credential-like value: it is sent redacted; a text redaction cannot read scores 0.0.
- More candidate text than one request holds: the trailing candidates score 0.0 and keep their hybrid order.
- Config sets `knowledge_reranker` only: loads unchanged. Sets both keys: error naming both.
- The provider refuses one question of a batch: that candidate scores 0.0, the others keep their scores.

---

## Design

### Before

```
search_knowledge_base ─ AgentKnowledge.asearch ─ sources ─ _interleave ─ _reranked(Reranker) ─ [:limit]
per-turn recall ─ FileMemoryBackend.search(recall=True) ─ search_semantic_file_memories(20)
                  ─ AgentKnowledge.asearch (no reranker) ─ _memory_results_from_hits (cosine scores)
                  ─ select_recall_hits (gap, hits, tokens) ─ add_within_recall_budget(team keyword hits)
search_memories ─ FileMemoryBackend.search(recall=False) ─ same search(limit) ─ merge team hits ─ sort ─ [:limit]
```

Two rerank/selection implementations, in two packages, and only the knowledge tool can rerank.

### After

```
search_knowledge_base ─ AgentKnowledge.asearch ─ sources ─ _interleave ─┐
recall / search_memories ─ FileMemoryBackend.search ─ candidates ───────┤
                                                                         ▼
                 retrieve(query, candidates, selection, reranker=None, deadline_seconds=None)
                   1. scores = reranker.ascore(query, texts)    else the candidates' hybrid scores
                   2. reranked: stable sort by score            failure or deadline: keep hybrid order
                   3. keep leading hits within Selection        (limit, token budget, score floor)
```

Team keyword hits stay outside (`add_within_recall_budget`, unchanged): keyword scores are not comparable.

### Types (all in `src/mindroom/knowledge/retrieval.py`, about 90 lines)

```python
@dataclass(frozen=True)
class Candidate[T]:
    item: T        # SearchHit or MemoryResult
    text: str      # what a reranker reads and the token budget counts
    score: float   # hybrid score going in, final score coming out

@dataclass(frozen=True)
class Selection:
    limit: int
    token_budget: int | None = None        # o200k tokens of the kept texts
    hybrid_score_gap: float | None = None  # hybrid scores: keep hits within this of the best
    min_rerank_score: float | None = None  # reranker scores: keep hits at or above this

def hybrid_scores(hits: Sequence[SearchHit]) -> list[float]: ...   # monotone cosine, moved out of _memory_results_from_hits
def o200k_tokens(text: str) -> int: ...                             # moved from _prompting._recall_tokens
async def retrieve[T](query: str, candidates: Sequence[Candidate[T]], selection: Selection, *,
                      reranker: Reranker | None = None, deadline_seconds: float | None = None) -> list[Candidate[T]]: ...
```

**Score normalization.** `Reranker.ascore` returns one score in [0, 1] per document, higher is better, 0.0 for a document it could not judge. Cohere and Vertex document scores in [0, 1] (their missing-result filler changes from `-inf` to 0.0); the cross-encoder applies a sigmoid explicitly; judgment backends return probabilities. Ordering never depends on the scale, so a Cohere-compatible endpoint that returns raw logits still reranks knowledge search correctly; only recall's threshold needs the contract, and the docs say so. Being in [0, 1] does not make scales equal, so `Selection` has two floors, each tied to the scale it is valid on: a relative gap for hybrid scores (comparable only inside one search), an absolute threshold for reranker scores. The threshold comes from the reranker's config, because it belongs to the score scale.

**Selections.** Knowledge tool and `search_memories`: `Selection(limit)`. Recall: `Selection(limit=5, token_budget=800, hybrid_score_gap=0.05, min_rerank_score=<reranker threshold>)`. Selection walks in rank order and stops at the first hit that fails a rule (today's `select_recall_hits` semantics).

### Judgment backends as a reranker

- `judgment/rerank.py::JudgmentReranker` implements `Reranker`. Shared input: the query as one user message. One predicate per candidate, id `c<i>`, candidate text inside that question's `instructions`. One request.
- **Predicate only.** OpenAI's guide names passage relevance as a predicate use; TypeSafe's rerank cookbook sorts by one Noul per candidate, calibrated enough to threshold. `choice` splits its probability mass across several relevant items, wrong for "select all relevant". `score` needs level definitions per question (uncached tokens times 20) and a second normalization, for a value the predicate probability already gives. Neither is added.
- **Gate.** Not a pipeline concept. TypeSafe's companion Noul exists because a Choice cannot say "none"; per-candidate predicates with an absolute threshold already abstain. Arm A7 measures whether an extra gate question helps anyway. If it does, it lives inside the reranker (`JudgmentReranker(gate=True)` adds question `g` to the same request and returns `min(p_i, p_gate)`), so the pipeline and protocol do not change.
- **Batching without touching single-question users.** The canonical payload keeps `"question"` for one question (identical bytes, hashes and LLM-backend input) and gains `"questions"` for a batch. Each wire's `encode` maps `payload.get("questions") or [payload["question"]]`; `decode_probability(root, id)` becomes `decode_probabilities(root, ids) -> WireAnswer[tuple[float | None, ...]]`, and the single-question path asks for one id. New: `build_batch_request`, `JudgmentClient.judge_many`, `create_batch_evaluator`. `judge`, `judge_choice`, `build_judgment_request`, `fit_judgment_request` and both existing evaluator factories keep their signatures.
- **Limits.** `MAX_BATCH_REQUEST_BYTES = 128_000` (about 60 candidates clipped to 2,000 characters; under TypeSafe's 64k-token request context). Rerank requests use their own `JudgmentCapacity(max_concurrent=8, max_per_owner=8)`, owner `<storage_root>:rerank`, so concurrent turns of one agent do not starve each other. The `llm` judgment backend returns no probability and is not a reranker.

### Config

```yaml
reranker:                      # top level; was knowledge_reranker
  provider: openai_decisions   # or typesafe | cohere | sentence_transformers | vertex_ai
  threshold: 0.8               # minimum score for memories recalled without being asked for
  timeout_seconds: 1.5         # judgment providers only (existing field)
memory:
  search:
    rerank: true               # default false; per-agent override through the existing agent memory search block
```

- `RerankerConfig = KnowledgeRerankerConfig | TypeSafeJudgmentConfig | OpenAIDecisionsJudgmentConfig`, discriminated on `provider`. The two judgment classes are reused as they are; `KnowledgeRerankerConfig` gains `threshold: float = 0.5`.
- `knowledge_reranker` keeps loading: a `# LEGACY_COMPAT:` rule in `config/legacy_fields.py` renames it to `reranker` before validation (the "convert" handling `legacy_access.py` uses); both keys set is an error.
- Knowledge search uses the reranker whenever one is configured (today's behaviour). Memory uses it only with `memory.search.rerank`.
- Failure: knowledge search keeps today's warning and merged order. Recall passes `deadline_seconds=2.0`; past it, or on any error, the hybrid selection is used and one warning is logged.

### What is deleted or moved

`knowledge/search.py::_reranked` (deleted), `memory/_prompting.py::select_recall_hits`, `_recall_tokens`, `_SCORE_GAP_TOLERANCE`, `_RECALL_TOKEN_BUDGET` (moved into `retrieve`), the score math in `_memory_results_from_hits` (moved to `hybrid_scores`). Expected source growth: PR 1 about +40 lines; PRs 2 to 4 about +250 for the new capability.

---

## Benchmark gate (PR 5 measures, PR 6 acts)

Baseline: arm `shipped` (the shipped A3 recall) on the same turns, same judge (`deepseek/deepseek-v4.1-flash-1017`, unchanged `PROMPT_VERSION`), three runs as in the spec (Harry all, Harry recent, DevAgent).

| Arm | Recall |
|---|---|
| A5 | `shipped` candidates (20) → `sentence_transformers` `BAAI/bge-reranker-v2-m3` → threshold |
| A6o / A6t | `shipped` candidates → `openai_decisions` / `typesafe` (`jev-1.13.0`) → threshold |
| A7o / A7t | A6 plus the gate question in the same request |

Thresholds 0.3, 0.5, 0.7, 0.8 are swept offline from cached scores; the threshold is picked on a seeded half of the turns and every rule is checked on the other half.

An arm **passes** when all hold on every run:

1. Guard: `turns_with_relevant` ≥ `shipped` − 2.0 points.
2. Gain: `relevant_per_1k_tokens` ≥ 1.25 × `shipped`, or `turns_with_relevant` ≥ `shipped` + 3.0 points at no more mean tokens.
3. Latency, 200 turns run one at a time against the live provider: added p50 ≤ 400 ms, p95 ≤ 1,000 ms, and at most 2% of turns fall back (deadline 2 s or error).
4. Cost ≤ $0.002 per turn at list price.
5. A second judge (Gemini Flash, 200 paired turns) does not reverse the sign of the gain.

A7 replaces A6 only if its mean tokens are ≤ 0.9 × A6 with `turns_with_relevant` ≥ A6 − 1.0 point. If no arm passes, defaults stay, the opt-in reranker stays, and the numbers are reported.

Expected per turn: 20 candidates × (≤ 300 text + about 60 rubric tokens) + query ≈ 7–8K input tokens, so about $0.0008 (OpenAI, $0.10/M) or $0.0003 (Jev, $0.042/M), one request. Jev is reported at about 150 ms per call; OpenAI is unmeasured. The whole benchmark (about 2,500 turns) costs under $2 per provider. `docs/knowledge.md` reports about 30 s per search for bge on a 32-core CPU with 5,000-character chunks, so A5 is expected to fail rule 3 without a GPU; it is run for its quality numbers.

---

## Tasks (one PR each, in order; PR 3 can run in parallel with PRs 1 and 2)

### PR 1: Shared `retrieve()` (behaviour-preserving)

**Files:** create `src/mindroom/knowledge/retrieval.py`, `tests/test_knowledge_retrieval.py`; modify `knowledge/search.py`, `memory/_prompting.py`, `memory/_semantic_file_search.py`, `memory/_file_backend.py`, `evals/memory_recall/arms.py`; move the cases of `tests/test_memory_recall_selection.py` into the new test file with their assertions unchanged.

**Interfaces:** produces `Candidate`, `Selection`, `hybrid_scores`, `o200k_tokens`, `retrieve` as above; `RECALL_SELECTION = Selection(limit=5, token_budget=800, hybrid_score_gap=0.05)` in `memory/_file_backend.py`.

- [ ] Write failing tests: every moved selection case through `retrieve(..., RECALL_SELECTION)`; `test_reranker_orders_and_limits` (scores `[0.1, 0.9, 0.5]`, limit 2 keeps items 1 and 2); `test_failing_reranker_keeps_hybrid_selection`; `test_deadline_keeps_hybrid_selection` (reranker sleeps 1 s, `deadline_seconds=0.01`); `test_wrong_count_or_non_finite_score_is_a_failure`; `test_min_rerank_score_selects_nothing_below_threshold`.
- [ ] Implement `retrieval.py`; `AgentKnowledge.asearch` builds `Candidate(hit, hit.content, score)` and calls `retrieve(query, candidates, Selection(effective_limit), reranker=self.reranker)`; `FileMemoryBackend.search` calls it for both `recall` values; delete the replaced code.
- [ ] Run `tests/test_knowledge_retrieval.py tests/test_knowledge_rerank.py tests/evals/test_memory_recall_run.py` and the file-memory search tests; expect PASS.
- [ ] Replay `shipped` on the real turns before and after the change (outside the repository); the injected hits must be identical on every turn. Report only the count of differing turns (expected 0).

### PR 2: `reranker` config and opt-in reranked recall with today's providers

**Files:** modify `config/knowledge.py`, `config/main.py`, `config/legacy_fields.py`, `config/memory.py`, `memory/config.py` (`memory_search`), `knowledge/rerank.py`, `knowledge/utils.py` (`_configured_reranker` becomes public `configured_reranker`), `memory/_file_backend.py`, `docs/knowledge.md`, `docs/memory.md`, `docs/configuration/index.md`, the config schema fixture; tests in `tests/test_knowledge_rerank.py`, `tests/test_config_validation_helpers.py`, file-memory search tests.

**Interfaces:** consumes `retrieve`. Produces `Config.reranker`, `MemorySearchConfig.rerank: bool = False`, `AgentMemorySearchConfig.rerank: bool | None = None`, `KnowledgeRerankerConfig.threshold: float = 0.5`, `configured_reranker(config, runtime_paths) -> Reranker | None`.

- [ ] Write failing tests: `test_knowledge_reranker_key_still_loads`, `test_both_reranker_keys_rejected`, `test_scores_are_in_unit_interval` per provider (mocked transports; a result missing from the response scores 0.0), `test_recall_uses_reranker_when_enabled` (stub reranker, threshold 0.5, scores `[0.9, 0.4]` keep one hit), `test_recall_reranker_failure_matches_hybrid_recall`, `test_rerank_flag_keeps_index_signature`.
- [ ] Implement. With a reranker in use, `FileMemoryBackend.search` fetches 20 candidates for either `recall` value and passes `deadline_seconds=2.0` for recall, with `min_rerank_score` set to the reranker config's `threshold`.
- [ ] Run the touched test files; expect PASS.

### PR 3: Batched predicates in the judgment client

**Files:** modify `judgment/state.py`, `judgment/client.py`, `judgment/openai_decisions.py`, `judgment/typesafe.py`, `judgment/execution.py` (`run_judgment(..., max_request_bytes: int = MAX_REQUEST_BYTES)`), `judgment/evaluator.py`; tests in `tests/test_judgment_client.py`, `tests/test_judgment_openai_decisions.py`, `tests/test_judgment_state.py`.

**Interfaces:** produces `build_batch_request(questions: Sequence[JudgmentQuestion], message: str, *, instructions: str) -> JudgmentRequest`; `JudgmentClient.judge_many(request, *, owner, allow_network=False) -> JudgmentResult[tuple[float | None, ...]]` (probabilities in question order, `None` for a refusal); `create_batch_evaluator(settings: ProbabilityJudgmentConfig, runtime_paths, *, owner, question_id, capacity)`.

- [ ] Write failing tests: `test_single_question_request_bytes_unchanged` (golden hash of today's request), `test_batch_decodes_in_question_order` for both wires (answers returned shuffled), `test_batch_refusal_is_none` (OpenAI), `test_batch_missing_or_extra_answer_is_invalid_response`, `test_batch_over_128000_bytes_is_incomplete`, `test_batch_with_unredacted_secret_is_incomplete`.
- [ ] Implement as in "Batching" above. Run all `tests/test_judgment_*.py`; expect PASS with no edits to existing assertions.

### PR 4: `JudgmentReranker` and the config union

**Files:** create `src/mindroom/judgment/rerank.py`, `tests/test_judgment_rerank.py`; modify `config/knowledge.py` (`RerankerConfig` union), `config/main.py`, `knowledge/rerank.py::create_reranker`, `docs/knowledge.md`, `docs/memory.md`, the config schema fixture.

**Interfaces:** consumes PR 3. Produces `JudgmentReranker(evaluate, gate: bool = False)` with `ascore`.

- [ ] Write failing tests with `httpx.MockTransport`: `test_one_request_for_all_candidates`, `test_scores_follow_document_order`, `test_refused_candidate_scores_zero`, `test_secret_in_candidate_is_sent_redacted`, `test_candidates_past_the_byte_cap_score_zero`, `test_backend_failure_raises` (so `retrieve` falls back), `test_gate_caps_every_score` (`gate=True`, gate 0.2, candidates 0.9 and 0.1 give 0.2 and 0.1), `test_missing_credential_builds_no_reranker`.
- [ ] Implement: clip the query and each candidate with `clip_judgment_text`, redact with `redact_sensitive_text`, pack leading candidates while the request fits, raise `JudgmentError` on `result.failure`. Rubric — instructions: "Passage: <text>. Would this passage help reply to the user's message?"; true: "It states a fact, preference, decision or instruction the reply should use."; false: "It is unrelated, or only shares a topic or words with the message."
- [ ] Run the new file and `tests/test_knowledge_rerank.py`; expect PASS.

### PR 5: Benchmark arms A5 to A7

**Files:** modify `evals/memory_recall/arms.py` (`Arm.reranker: str | None = None`, new arms), `evals/memory_recall/run.py` (`--thresholds`, tuning/held-out split, latency and cost columns), `evals/memory_recall/README.md`; create `evals/memory_recall/rerank_cache.py` (`CachedReranker`: SQLite keyed by sha256 of provider, model, rubric version, query, text; records latency of uncached calls); tests in `tests/evals/`.

- [ ] Write failing tests on the synthetic fixture: `test_cached_reranker_skips_second_request`, `test_threshold_sweep_reuses_scores`, `test_split_is_seeded_and_disjoint`.
- [ ] Implement, run the three real runs outside the repository, append a "Results" section with aggregate numbers and the pass/fail verdict per arm to this plan.

### PR 6 (only if an arm passes): default switch

`MemorySearchConfig.rerank` becomes `bool | None = None`; unset means on when the configured reranker's provider is in the set that passed. Add the gate flag only if A7 beat A6. Update docs with the measured numbers. Tests: `test_recall_reranks_by_default_for_passing_provider`, `test_recall_does_not_rerank_by_default_for_other_providers`.

**Follow-ups, out of scope:** team keyword hits scored in the same request (one comparable list); conversation-aware rerank input for short follow-ups (the case where A4 lost 11.8 points); `jev-1.14.0`; Mem0 recall; running recall concurrently with the rest of prompt assembly.

## Risks and open questions for Bas

1. **Config name.** Rename to top-level `reranker` and keep `knowledge_reranker` loading through a LEGACY_COMPAT rename, or keep the old name for both uses? I recommend the rename (70%).
2. **Privacy.** Reranked recall sends about 20 memory entries per turn to OpenAI or TypeSafe, with credential-like values redacted. I recommend opt-in until PR 6, then on by default only for a configured provider that passed, with a release note (65%). The alternative is opt-in forever.
3. **Threshold default.** The judgment configs default to 0.8, tuned for allow/deny decisions; relevance may want 0.3 to 0.5. I recommend reusing the classes now and adding a reranker-specific default only if the sweep picks a value other than 0.8 (75%).
4. **Gate.** I recommend no gate in the pipeline, measured as A7 and shipped inside the reranker only if it beats A6 (80%).
5. **Local cross-encoder on the per-turn path.** Its scoring thread cannot be cancelled, so abandoned calls queue up. I recommend documenting `sentence_transformers` recall rerank as GPU-only and treating A5 as a measurement (80%).
6. **Latency.** A passing arm may still add up to 1 s at p95 before the first token. I recommend accepting that for PR 6 and moving recall off the serial path as a follow-up (60%).
7. **`jev-1.14.0`.** The pin is shared with routing and mid-turn judgments. I recommend a separate bump PR after this work (75%).
