# Passive Trace Auditor, Friction-Triggered Skill Learning & Backtest Implementation Plan

> **Status (pre-merge / superseded):** The offline eval CLI (`evals/trace_audit/`) was removed prior to merge.
> Stage 1 was simplified to material friction signals only, and `review_audit_cluster` lives in `src/mindroom/skill_learning/audit_reviewer.py`.
> The original implementation plan below is retained for historical context.

> **For agentic workers:** REQUIRED SUB-SKILL: implement this plan task-by-task with the skill named in **Execution**. Steps use checkbox (`- [ ]`) syntax.

**Execution:** baspowers:subagent-driven-development — four well-bounded tasks with clean interfaces and independent test suites, implemented on `gemini4_neon@xhigh` (`minimal: true`) followed by real-data backtest and Opus+Astra whole-branch review.

**Goal:** Build MindRoom's passive trace auditor (`src/mindroom/trace_audit/`), wire it into `@automation("trace_audit")` and opt-in `skill_learning.trigger: friction`, and ship an offline read-only chronological backtest CLI (`evals/trace_audit/`).

**Architecture:** One pure core package (`src/mindroom/trace_audit/`) implements Stage 1 deterministic friction detection, read-only SQLite session loading, Stage 2 bounded evidence-card auditing, and stateful cross-thread clustering/promotion. A built-in-only `Run` automation step runs `@automation("trace_audit")` silently on the main loop under `AutomationRunner`'s existing lock and hands promoted clusters to a bounded evidence-only `skill_learning.reviewer.review_audit_cluster`. `evals/trace_audit/` drives the exact same core over a read-only snapshot DB and scratch skills copy.

**Tech Stack:** Python 3.13+, SQLite (`sqlite3` read-only URI), Pydantic v2, `mindroom.session_storage.schema`, `mindroom.automations`, `mindroom.skill_learning`, `mindroom.budgets.pricing`, `pytest`, `tach`, `ruff`, `mypy`.

**Spec:** `docs/baspowers/specs/2026-11-23-trace-audit-design.md`

## Global Constraints

- Default `skill_learning.trigger` stays `"interval"` (zero behavior change for existing configs).
- `skill_learning.trigger == "friction"` requires `trace_audit` in the agent's effective automations (validated in `config/main.py`).
- `Run` automation step is rejected for plugin automations (built-in only).
- `trace_audit/source.py` opens SQLite strictly read-only (`mode=ro`, `PRAGMA query_only = ON`) and never calls `create_session_storage`.
- Evidence cards are strictly bounded (`<= 4_000` chars, `<= 4` snippets × `<= 280` chars) and redacted via `redact_sensitive_text`; durable `trace_audit.json` stores zero raw transcripts or tool arguments.
- Bounded evidence-only learner review caps at `4` tool calls and `16_000` cumulative input tokens, using `SkillTools` (learner-only, read-before-write, `library_turn`, `.history/` backups, `kind="skill_learning"`).
- `tach check --dependencies --interfaces`, `ruff check`, `mypy`, and targeted `pytest` must pass after every task.
- All commits include `--trailer "Assisted-by: MindRoom <display name>:<model id> <https://github.com/mindroom-ai/mindroom>"`.

## Review Focus

1. **Benign shell exit-1 vs real failure:** `grep`/`rg`/`diff`/`test` returning `Exit code 1` with empty stderr must NOT flag friction, whereas `pytest`/`git push`/`grep` with non-empty stderr or `Exit code 2` MUST flag friction.
2. **Reader-tool false positives:** `read_file` or `grep` returning file contents starting with `Error:` or `Traceback` must NEVER be classified as `text_error`.
3. **Checkpointed/interrupted `CANCELLED` runs:** A `CANCELLED` run containing `INTERRUPTED_CALL_RESULT` or 0 tool calls & <=15s duration must NOT trigger `settled_cancel`.
4. **Scope isolation & anti-churn gating:** Two confirmed failures in different rooms must NOT merge into one cluster; two failures in the same session loop must NOT satisfy `min_cluster_sessions=2`; a promoted cluster within `cooldown_days` must NOT re-promote.
5. **Plugin `Run` rejection & offline read-only safety:** A plugin automation returning `Run` is rejected cleanly by `AutomationRunner`; `evals/trace_audit` never writes to the input SQLite file or original skills directory.

---

### Task 1: Core `src/mindroom/trace_audit/` Package (`models`, `signals`, `source`, `auditor`, `pipeline`)

**Files:**
- Create: `src/mindroom/trace_audit/__init__.py`
- Create: `src/mindroom/trace_audit/models.py`
- Create: `src/mindroom/trace_audit/signals.py`
- Create: `src/mindroom/trace_audit/source.py`
- Create: `src/mindroom/trace_audit/auditor.py`
- Create: `src/mindroom/trace_audit/pipeline.py`
- Modify: `src/mindroom/usage/storage.py` (add `"trace_audit"` to `IndependentUsageKind`)
- Modify: `tach.toml` (declare `mindroom.trace_audit.*` modules and visibility updates)
- Test: `tests/test_trace_audit_core.py`

**Interfaces:**
- Consumes: `mindroom.session_storage.schema.TableNames`, `mindroom.history.run_checkpoint.INTERRUPTED_CALL_RESULT`, `mindroom.llm.runs`, `mindroom.llm.messages`, `mindroom.redaction.redact_sensitive_text`, `mindroom.durable_write.write_json_file_durable`, `mindroom.providers.registry`, `mindroom.helper_usage.record_run_usage`.
- Produces:
  - `classify_tool_execution(tool: ToolExecutionRecord) -> ToolOutcome`
  - `detect_session_incidents(session: AuditedSession, *, now: float, min_idle_seconds: int = 600, median_tools: float | None = None, sample_rate: float = 0.02, sample_salt: str = "") -> list[Incident]`
  - `load_audited_sessions(db_path: Path, storage_name: str, *, since_ts: float | None = None, until_ts: float | None = None, excluded_sessions: AbstractSet[str] = frozenset(), allowed_room_id: str | None = None) -> list[AuditedSession]`
  - `load_usage_rows(db_path: Path, storage_name: str, *, kind: str | None = None, since_ts: float | None = None, until_ts: float | None = None) -> list[dict[str, Any]]`
  - `build_evidence_card(incident: Incident, session: AuditedSession) -> EvidenceCard`
  - `audit_evidence_card(card: EvidenceCard, *, judge_fn: AuditJudgeFn) -> AuditFinding`
  - `make_llm_audit_judge(config: Config, runtime_paths: RuntimePaths, model_name: str, *, usage_sink: Callable[..., None] | None = None) -> AuditJudgeFn`
  - `run_audit_pass(*, db_path: Path, storage_name: str, state_path: Path, scope_key: str, judge_fn: AuditJudgeFn | None, now: float | None = None, allowed_room_id: str | None = None, excluded_sessions: AbstractSet[str] = frozenset(), settings: TraceAuditSettings | None = None, nominate_fn: NominateClusterFn | None = None) -> AuditPassResult`

- [ ] **Step 1: Write failing unit tests in `tests/test_trace_audit_core.py`**
  Cover: tool outcome classifier (benign `grep` exit 1, `pytest` exit 1, `grep` with stderr, exit 2, shell timeout, JSON error, reader-tool `Error:` immunity, non-reader `Error:`, `approval_denied`), incident detector (material failures, weak-signal 2-channel rule, checkpointed `CANCELLED` exclusion, EN/NL `user_correction`, deterministic clean sampler), read-only synthetic v8 SQLite loader (`mode=ro`, exclusion of `:dynamic_workflow:` and `automation/` hook metadata, room scoping), evidence card redaction/size bounds, and `run_audit_pass` clustering (2 distinct sessions promote, 2 failures in 1 session do not, cross-room isolation, cooldown gating, rolling 30d state persistence without raw text).
- [ ] **Step 2: Run tests to verify failure**
  Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_trace_audit_core.py -x -n 0 --no-cov'`
- [ ] **Step 3: Implement `src/mindroom/trace_audit/{__init__,models,signals,source,auditor,pipeline}.py`, update `usage/storage.py` and `tach.toml`**
- [ ] **Step 4: Run tests, `ruff`, `mypy`, and `tach`**
  Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_trace_audit_core.py -x -n 0 --no-cov && uv run ruff check src/mindroom/trace_audit src/mindroom/usage/storage.py tests/test_trace_audit_core.py && uv run mypy src/mindroom/trace_audit src/mindroom/usage/storage.py && uv run --with tach tach check --dependencies --interfaces'`
- [ ] **Step 5: Commit Task 1**

---

### Task 2: `@automation("trace_audit")`, Passive `Run` Step & Friction-Triggered `skill_learning`

**Files:**
- Modify: `src/mindroom/automations/steps.py` (add `Run` dataclass and extend `CheckStep`)
- Modify: `src/mindroom/automations/runner.py` (await `Run` for built-ins only; reject for plugins)
- Modify: `src/mindroom/automations/registry.py` (register built-in `trace_audit`)
- Create: `src/mindroom/automations/trace_audit.py`
- Modify: `src/mindroom/config/automations.py` (add `TraceAuditAutomation`)
- Modify: `src/mindroom/config/skill_learning.py` (add `trigger: Literal["interval", "friction"] = "interval"`)
- Modify: `src/mindroom/config/main.py` (validate `skill_learning.trigger == "friction"` requires `trace_audit` automation)
- Modify: `src/mindroom/skill_learning/reviewer.py` (add bounded `review_audit_cluster(...)` with 4-tool-call & 16k-input-token cap and conservative prompt)
- Modify: `src/mindroom/skill_learning/runner.py` (add `review_audit_cluster(...)` delegating through `library_turn`, archival, timeout, and notice)
- Modify: `src/mindroom/response_runner.py` (return early in `_response_skill_review` when `trigger == "friction"`)
- Modify: `src/mindroom/orchestrator.py` (wire `SkillReviewRunner` callback into `AutomationRunner` / `trace_audit`)
- Modify: `tach.toml`
- Test: `tests/test_trace_audit_integration.py`

**Interfaces:**
- Consumes: `mindroom.trace_audit.pipeline.run_audit_pass`, `mindroom.trace_audit.auditor.make_llm_audit_judge`, `mindroom.trace_audit.models.FailureCluster`.
- Produces:
  - `Run(execute: Callable[[], Awaitable[Done | None]])` in `mindroom.automations.steps`
  - `TraceAuditAutomation` in `mindroom.config.automations`
  - `SkillLearningConfig.trigger: Literal["interval", "friction"] = "interval"`
  - `review_audit_cluster(...) -> ReviewOutcome` in `mindroom.skill_learning.reviewer` and `SkillReviewRunner.review_audit_cluster(...)`

- [ ] **Step 1: Write failing integration tests in `tests/test_trace_audit_integration.py`**
  Cover: `TraceAuditAutomation` config validation & defaults, `skill_learning.trigger == "friction"` validation (fails without `trace_audit`, passes with it), `trigger == "interval"` unchanged default, `_response_skill_review` skips capture/counting in `"friction"` mode, `AutomationRunner` executes built-in `Run` and posts `Done.notice` while rejecting plugin `Run`, end-to-end `trace_audit` automation promoting a 2-session cluster into `review_audit_cluster` (capped at 4 tool calls / 16k tokens, learner-only skill write, report-only mode when `trigger == "interval"`).
- [ ] **Step 2: Run tests to verify failure**
- [ ] **Step 3: Implement wiring across `automations/`, `config/`, `skill_learning/`, `response_runner.py`, `orchestrator.py`, and `tach.toml`**
- [ ] **Step 4: Run targeted + existing automation/skill_learning/config tests, `ruff`, `mypy`, `tach`**
  Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/test_trace_audit_integration.py tests/test_trace_audit_core.py tests/test_skill_learning.py tests/test_automations.py -x -n 0 --no-cov && uv run ruff check src/mindroom tests/test_trace_audit_integration.py && uv run mypy src/mindroom/trace_audit src/mindroom/automations src/mindroom/skill_learning src/mindroom/config/automations.py src/mindroom/config/skill_learning.py && uv run --with tach tach check --dependencies --interfaces'`
- [ ] **Step 5: Commit Task 2**

---

### Task 3: Offline Chronological Backtest CLI (`evals/trace_audit/`)

**Files:**
- Create: `evals/trace_audit/__init__.py`
- Create: `evals/trace_audit/baseline.py` (Baseline A from `_usage`, Baseline B simulated `interval: 10`, USD pricing via `budgets.pricing.cost_usd`)
- Create: `evals/trace_audit/judge.py` (blinded independent judge + placebo control + JSON cache)
- Create: `evals/trace_audit/run.py` (CLI entrypoint: `--db`, `--skills-dir`, `--config`, `--window-days`, `--train-days`, `--audit-model`, `--judge-model`, `--out`, `--dry-run`, `--agent`)
- Test: `tests/evals/test_trace_audit_backtest.py`

**Interfaces:**
- Consumes: `mindroom.trace_audit.*`, `mindroom.skill_learning.reviewer.review_audit_cluster`, `mindroom.skill_learning.transcript.count_model_replies`, `mindroom.budgets.pricing`.
- Produces: runnable CLI `uv run python -m evals.trace_audit.run ...` generating aggregate-only `report.md` and `0600` `artifacts.json`.

- [ ] **Step 1: Write failing tests in `tests/evals/test_trace_audit_backtest.py`**
  Cover: read-only snapshot safety (input DB mtime/size & original skills dir unchanged), Baseline A vs Baseline B calculations, chronological train/holdout split with zero session leakage, blinded judge + placebo scoring with stubbed models, `report.md` generation without leaking raw user snippets.
- [ ] **Step 2: Run tests to verify failure**
- [ ] **Step 3: Implement `evals/trace_audit/{__init__,baseline,judge,run}.py`**
- [ ] **Step 4: Run tests + `ruff` + `mypy`**
  Run: `NIX_PATH=nixpkgs=flake:nixpkgs nix-shell --run 'uv run pytest tests/evals/test_trace_audit_backtest.py -x -n 0 --no-cov && uv run ruff check evals/trace_audit tests/evals/test_trace_audit_backtest.py && uv run mypy evals/trace_audit'`
- [ ] **Step 5: Commit Task 3**

---

### Task 4: Real-Data Backtest on Offline Snapshots (`agent-a.db`, `agent-b.db`) & Refinement

- [ ] **Step 1:** Run `--dry-run` backtest against `~/.cache/trace-auditor-backtest/agent-a.db` and `agent-b.db` (strictly offline copies for `<agent>`, output to `~/.cache/trace-auditor-backtest/out-*/`, never committed to git).
- [ ] **Step 2:** Run full chronological backtest (`20d` train → `10d` holdout) with cheap audit model (`gemini_flash` / `gpt5mini` / `deepseek_v4_1_flash_1017`) and separate-family judge model (`sonnet` or `gpt5mini`), inspecting precision, recall, cost vs historical `skill_learning`, candidate skill diffs in scratch, and held-out direct-hit rate vs placebo control.
- [ ] **Step 3:** Fix any threshold/signal bugs discovered by the real-data backtest (with synthetic unit tests — zero private data in repo!) and re-run to confirm.
- [ ] **Step 4:** Parallel Opus + Astra full-diff review, address real findings, run full verification gates, push PR, score with `scoring-pull-requests`, and present the concrete backtest numbers + PR to Bas.
