# Read-Only Usage Statistics Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use baspowers:subagent-driven-development (recommended) or baspowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a local, read-only `usage_stats` tool that lets an agent inspect its own canonical requester's retained token usage and optionally lets an explicitly configured admin agent inspect retained usage across all configured agents and teams.

**Architecture:** Discover only approved Agno SQLite session databases, open every database through SQLite read-only mode, recursively normalize persisted run metrics into typed records, and aggregate those records after requester authorization and attribution.

**Tech Stack:** Python 3.13, standard-library `sqlite3`, `zoneinfo`, and `decimal`, Agno `Toolkit`, MindRoom runtime identity and tool registration, and pytest.

**Spec:** `docs/dev/2026-08-16-usage-stats-tool-design.md`

## Global Constraints

- Every database connection must use a `file:` URI with `mode=ro`, a private page cache, `PRAGMA query_only = ON`, and a bounded busy timeout.
- A WAL source must pass the read-only header and existing-sidecar preflight before SQLite opens it, and `immutable=1` must not be used for a live Agno database.
- The implementation must never instantiate Agno `SqliteDb`, call Agno `calculate_metrics()`, create a missing file, write a cache, or create an aggregate table.
- Self-service queries must derive the agent and requester from `ToolRuntimeContext` and must not accept either identity as a function argument.
- Self-service requester filtering must happen per persisted run, must canonicalize authorization aliases, and must fail closed when the requester cannot be established.
- Private-agent self-service discovery must use the current execution identity's exact private session state root and must never enumerate sibling private instances.
- Every source must be derived from the effective session storage root so `MINDROOM_SESSION_STORAGE_PATH` redirection is honored.
- Admin queries require both an authored `admin_scope: true` setting and a canonical requester listed in `authorization.global_users`.
- The admin authorization check must complete before any all-agent filesystem or database scan begins.
- Outputs must contain aggregates and diagnostics only, with no prompts, messages, room IDs, thread IDs, session IDs, event IDs, tool arguments, or raw run metadata.
- Public diagnostics must contain bounded category counts only and must never expose database paths, storage directory names, table names, or SQL errors.
- Version one reports retained Agno run usage only and must state that compaction can make retained history incomplete.
- Shared self-service output must not expose cumulative session metrics or a cumulative-minus-retained delta because those values can include other requesters.
- The tool must execute locally because worker sandboxes do not own or mount the host's complete Agno storage tree.
- Do not add a dependency for functionality available in the Python standard library.
- Keep production behavior out of `bot.py` and `orchestrator.py`.
- Process persisted usage one session row at a time, retain only field-selective usage records, and enforce explicit encoded-size, nesting-depth, and extracted-node limits.
- Keep Markdown at one sentence per line.
- Do not broaden this task to provider billing APIs, embeddings, speech-to-text accounting, or a new durable usage ledger.

## Baseline Verification

The main-based worktree passes the existing tool metadata, tool-system boundary, execution-identity payload, and team-run metadata test suites before this feature is implemented.

Keep those tests in the final focused regression set so any failure introduced during implementation remains attributable to the feature.

---

## Task 1: Build a Non-Mutating Agno SQLite Reader

**Files:**

- Create: `src/mindroom/usage_stats_storage.py`
- Create: `tests/test_usage_stats_storage.py`

- [ ] **Step 1: Write failing tests for the read-only connection contract.**

Create an Agno-compatible SQLite fixture with one `<entity>_sessions` table containing `session_id`, `session_type`, `agent_id`, `team_id`, `user_id`, `session_data`, `runs`, `created_at`, and `updated_at` columns.

Cover these behaviors:

- A valid row is decoded into an immutable, field-selective storage row.
- A missing database produces an `absent` diagnostic and does not create a file.
- WAL and rollback-journal fixtures preserve every durable database, WAL, rollback-journal, and other durable artifact's bytes and modification time across a read, create or remove no directory entries, and reject deliberate writes, while SQLite may change a pre-existing `-shm` file's bytes and modification time for live WAL reader coordination.
- A WAL database without the sidecars required for a non-mutating live read produces a bounded diagnostic and creates nothing.
- An `INSERT` attempted through `_open_read_only_database()` raises `sqlite3.OperationalError`.
- A corrupt database, missing session table, invalid JSON value, and locked database each produce bounded diagnostics instead of leaking content.
- Oversized `runs` or `session_data` JSON, excessive nested-response depth, and excessive extracted node count each produce a bounded resource-limit diagnostic.
- A large run containing messages, prompts, and tool payloads retains only the allowed usage fields and never accumulates more than one bounded session row at a time.
- Only `source.expected_session_table` is accepted, and another table in the same database is rejected.

Run:

```bash
uv run pytest -q tests/test_usage_stats_storage.py
```

Expected: failure because the storage module does not exist.

- [ ] **Step 2: Add typed storage boundaries.**

Define the core storage types with no dependency on Agno database classes:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

UsageStorageScope = Literal["shared_agent", "private_agent", "team"]


@dataclass(frozen=True, slots=True)
class UsageStorageSource:
    path: Path
    scope: UsageStorageScope
    expected_session_table: str
    source_agent_id: str | None
    allowed_agent_ids: frozenset[str]
    allowed_team_ids: frozenset[str]
    requester_isolated: bool


@dataclass(frozen=True, slots=True)
class UsageModelMetric:
    model_type: str
    provider: str
    model_id: str
    metrics: Mapping[str, int | str | None]


@dataclass(frozen=True, slots=True)
class UsageRunNode:
    agent_id: str | None
    team_id: str | None
    requester_id: str | None
    created_at: str | None
    model_provider: str | None
    model_id: str | None
    run_id: str | None
    status: str
    metrics: Mapping[str, int | str | None]
    model_metrics: tuple[UsageModelMetric, ...]
    member_responses: tuple["UsageRunNode", ...]


@dataclass(frozen=True, slots=True)
class UsageSessionRow:
    source: UsageStorageSource
    entity_id: str
    entity_kind: Literal["agent", "team"]
    row_key: str
    session_user_id: str | None
    session_metrics: Mapping[str, int | str | None] | None
    runs: tuple[UsageRunNode, ...]


@dataclass(frozen=True, slots=True)
class UsageStorageDiagnostic:
    path_label: str
    status: Literal[
        "absent",
        "busy",
        "corrupt",
        "unsupported_schema",
        "resource_limit",
        "partial",
    ]
    detail: str
```

Keep the diagnostic path label relative to the effective session storage root for internal logging and never include it in a public report.

Expose the reader as an iterator of `UsageSessionRow | UsageStorageDiagnostic` values so callers aggregate or discard each bounded session before advancing the SQLite cursor.

- [ ] **Step 3: Implement read-only connection and schema discovery.**

Use a context manager equivalent to:

```python
@contextmanager
def _open_read_only_database(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro&cache=private",
        uri=True,
        timeout=1.0,
    )
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 1000")
        connection.row_factory = sqlite3.Row
        yield connection
    finally:
        connection.close()
```

Inspect the database header through a binary read-only file handle before opening SQLite.

When the header identifies WAL mode, require existing readable non-symlink `-wal` and `-shm` siblings so SQLite never relies on directory write access to create them.

Do not use `immutable=1` because the live Agno database can change concurrently.

Read `sqlite_master` first, validate each table name in Python, quote accepted identifiers with doubled double quotes, and select only the columns needed for usage analysis.

Advance the SQLite cursor one row at a time and use SQL length guards so oversized JSON cells are not returned to Python for decoding.

Decode one bounded cell at a time with explicit type checks, copy only the fields declared by `UsageRunNode`, and discard the raw mapping before advancing the cursor.

Apply named encoded-byte, nested-response-depth, and extracted-node-count limits so hostile or accidentally huge history produces `resource_limit` diagnostics without unbounded memory use.

Malformed rows are skipped and recorded as `partial` without aborting other rows or databases.

- [ ] **Step 4: Verify the reader.**

Run:

```bash
uv run pytest -q tests/test_usage_stats_storage.py
```

Expected: all storage-reader tests pass.

---

## Task 2: Discover Only Authorized Storage Sources

**Files:**

- Modify: `src/mindroom/usage_stats_storage.py`
- Modify: `tests/test_usage_stats_storage.py`

- [ ] **Step 1: Write failing discovery tests.**

Build a temporary primary storage root containing these cases:

- One configured shared agent database at `agents/<agent>/sessions/<agent>.db`.
- Two requester-private instances of the same configured agent.
- One configured team database and one ad hoc team database containing nested member runs.
- Equivalent sources redirected beneath an absolute dedicated `MINDROOM_SESSION_STORAGE_PATH` root outside the primary storage root.
- One removed agent database that is no longer in the current config.
- One symlinked database and one symlinked private-instance directory.
- One expected but absent database.

Assert that self discovery returns the exact current agent database plus every structurally valid team database needed to find configured or ad hoc nested runs for that agent.

Assert that a private self query returns only the current requester's exact private database plus structurally valid team databases and never returns the sibling requester's private agent database.

Assert that admin discovery returns configured shared agents, configured private instances, and structurally valid configured or ad hoc team databases, while ignoring removed agent entities and all symlinks.

Assert that discovery uses the redirected session root when configured and finds nothing by scanning the corresponding canonical state-root session paths.

Build the private-instance fixture with `worker_dir_name(worker_key)`, assert that discovery finds it, and assert that a raw worker-key directory layout is ignored.

Run:

```bash
uv run pytest -q tests/test_usage_stats_storage.py -k discovery
```

Expected: failure because discovery functions do not exist.

- [ ] **Step 2: Implement self discovery through runtime resolution.**

Add:

```python
def discover_self_usage_sources(
    *,
    agent_name: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
) -> tuple[UsageStorageSource, ...]:
    ...
```

Call `resolve_agent_runtime()` with the exact execution identity and derive the current agent database from the returned `session_state_root`.

Mark a private source `requester_isolated=True` and a shared source `requester_isolated=False`.

Add all structurally valid team databases because an agent's member run can be persisted inside a configured or ad hoc team session database.

Do not scan `private_instances` in this code path.

- [ ] **Step 3: Implement fixed-depth admin and team discovery.**

Add:

```python
def discover_admin_usage_sources(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
) -> tuple[UsageStorageSource, ...]:
    ...
```

Resolve the effective session storage root with `resolve_session_state_root(runtime_paths.storage_root, runtime_paths)` and discover only these fixed shapes beneath it:

```text
agents/<configured-agent>/sessions/<configured-agent>.db
private_instances/<worker-dir-name>/<configured-agent>/sessions/<configured-agent>.db
teams/<storage-name>/sessions/<storage-name>.db
```

Resolve every candidate and require it to remain under the resolved effective session storage root.

Reject a candidate when any traversed directory or the database itself is a symlink.

Require a team directory, database basename, and exact `<storage-name>_sessions` table name to agree before reading it.

Use `config.agents` and `config.teams` as the allowed attributed entity sets so removed agent or configured-team records are ignored, while valid nested runs for current agents remain visible inside ad hoc team databases.

Sort sources by stable labels relative to the effective session storage root to make output and tests deterministic.

- [ ] **Step 4: Verify discovery.**

Run:

```bash
uv run pytest -q tests/test_usage_stats_storage.py
```

Expected: all reader and discovery tests pass.

---

## Task 3: Normalize Direct Runs and Aggregate Token Usage

**Files:**

- Create: `src/mindroom/usage_stats.py`
- Create: `tests/test_usage_stats.py`

- [ ] **Step 1: Write failing tests for requester, time, model, token, and cost semantics.**

Use raw dictionaries shaped like persisted Agno `RunOutput` values rather than constructing provider models.

Cover:

- `metadata.requester_id` takes precedence over `user_id`.
- `user_id` is the requester fallback when metadata is absent.
- Authorization aliases canonicalize before comparison and grouping.
- A self query excludes missing or mismatched requesters.
- An admin query includes missing requesters in an `unknown` bucket and increments a diagnostic count.
- `start` is inclusive and `end` is exclusive.
- Date-only inputs use `config.timezone` and timestamp inputs require `Z` or an explicit offset.
- Serialized windows retain `config.timezone` for both date-only and explicit-offset inputs.
- A run at the exact end boundary is excluded.
- Raw input, output, total, cache-read, cache-write, reasoning, and audio token fields aggregate independently.
- Per-model metric details are preferred when present, with top-level metrics as the fallback.
- Cost is a decimal-string known subtotal with separate counts for runs with and without cost.
- Serialized reports contain no raw metadata, content, prompt, room, thread, event, session, or tool-argument fields.

Run:

```bash
uv run pytest -q tests/test_usage_stats.py
```

Expected: failure because the usage service does not exist.

- [ ] **Step 2: Define typed query and report models.**

Use immutable dataclasses with explicit JSON serialization:

```python
SelfGroupBy = Literal["day", "model"]
AdminGroupBy = Literal["entity", "requester", "model", "day"]


@dataclass(frozen=True, slots=True)
class TokenTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    audio_input_tokens: int = 0
    audio_output_tokens: int = 0
    audio_total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class CostCoverage:
    known_cost: str
    runs_with_cost: int
    runs_without_cost: int


@dataclass(frozen=True, slots=True)
class UsageBreakdownRow:
    dimension: Literal["day", "model", "entity", "requester"]
    key: str
    model_type: str | None
    provider: str | None
    model_id: str | None
    totals: TokenTotals
    cost: CostCoverage
    run_count: int


@dataclass(frozen=True, slots=True)
class UsageCoverage:
    status: Literal["complete_retained", "partial", "unknown"]
    scanned_sources: int
    partial_sources: int
    scanned_sessions: int
    retained_runs: int
    skipped_runs: int
    malformed_runs: int
    missing_requester_runs: int
    missing_timestamp_runs: int
    compacted_sessions: int
    note: str


@dataclass(frozen=True, slots=True)
class UsageReport:
    scope: Literal["self", "admin"]
    start: str | None
    end: str
    timezone: str
    as_of: str
    totals: TokenTotals
    cost: CostCoverage
    turn_count: int
    run_count: int
    session_count: int
    first_observed_at: str | None
    last_observed_at: str | None
    status_counts: Mapping[str, int]
    breakdown: tuple[UsageBreakdownRow, ...]
    breakdown_truncated: bool
    breakdown_omitted: int
    coverage: UsageCoverage

    def to_dict(self) -> dict[str, object]:
        ...
```

Use `Decimal` internally for cost and render the result without binary floating-point conversion.

Set the explicit model fields only for a model breakdown row and serialize them separately so distinct model type, provider, and model-ID tuples cannot merge behind an opaque key.

- [ ] **Step 3: Implement strict time-window parsing.**

Add:

```python
def parse_usage_window(
    *,
    start: str | None,
    end: str | None,
    timezone_name: str,
    as_of: datetime,
) -> tuple[datetime | None, datetime]:
    ...
```

Treat a date-only start as local midnight and a date-only end as the following local midnight so an end date remains naturally exclusive.

Reject naive timestamps, unknown timezones, invalid values, and windows where `start >= end` with concise user-facing errors.

Carry `timezone_name` into `UsageReport.timezone` independently of the normalized UTC boundaries so `to_dict()` can reproduce the configured IANA timezone under `window.timezone`.

- [ ] **Step 4: Implement direct-run normalization and public query functions.**

Add:

```python
def collect_self_usage(
    *,
    agent_name: str,
    requester_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity,
    start: str | None,
    end: str | None,
    group_by: SelfGroupBy,
    as_of: datetime | None = None,
) -> UsageReport:
    ...


def collect_admin_usage(
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    start: str | None,
    end: str | None,
    group_by: AdminGroupBy,
    entity_names: tuple[str, ...] | None,
    requester_ids: tuple[str, ...] | None,
    as_of: datetime | None = None,
) -> UsageReport:
    ...
```

Read the query's `as_of` once, use UTC internally, and compare every persisted run against the same start and end instants.

For self scope, accept only records whose canonical requester equals the canonical current requester and whose attributed agent equals `agent_name`.

For admin scope, validate entity filters against configured agent and team names before scanning, and canonicalize requester filters before matching.

Sort breakdowns by descending total tokens and then a stable tuple of dimension and explicit dimension values, retain at most 200 rows, and return both a truncation boolean and the number of omitted rows.

- [ ] **Step 5: Verify direct aggregation.**

Run:

```bash
uv run pytest -q tests/test_usage_stats.py
```

Expected: all direct-run aggregation tests pass.

---

## Task 4: Handle Team Trees, Deduplication, and Honest Coverage

**Files:**

- Modify: `src/mindroom/usage_stats.py`
- Modify: `tests/test_usage_stats.py`

- [ ] **Step 1: Write failing recursive-run tests.**

Cover these persisted structures:

- A top-level `TeamRunOutput.metrics` value attributed to the team leader entity.
- A nested member `RunOutput.metrics` value attributed to that member agent.
- A nested team whose own metrics are attributed to that nested team.
- A child run missing requester metadata that inherits the verified parent requester.
- A child run with an explicit different requester that does not inherit the parent requester.
- The same stable member `run_id` appearing twice in a team tree and counting only once.
- A run without a stable ID counting once per structural location.
- A self query finding the current agent's nested member metrics inside a team database while excluding leader and sibling metrics.

Run:

```bash
uv run pytest -q tests/test_usage_stats.py -k 'team or nested or dedup'
```

Expected: failure because the first implementation handles only direct runs.

- [ ] **Step 2: Implement recursive metric extraction.**

Introduce an internal normalized record:

```python
@dataclass(frozen=True, slots=True)
class _UsageMetricRecord:
    entity_kind: Literal["agent", "team"]
    entity_id: str
    requester_id: str | None
    created_at: datetime
    model_type: str | None
    provider: str | None
    model_id: str | None
    run_id: str | None
    structural_key: str
    status: str
    totals: TokenTotals
    cost: Decimal | None
```

Recursively walk the field-selective bounded `UsageRunNode.member_responses` tree without retaining prompts, message content, tool payloads, or other raw run mappings.

Use `source_agent_id` for a top-level direct agent run.

For a top-level team run, read the persisted row or run `team_id` and retain leader metrics only when that ID is present in `allowed_team_ids`.

Do not derive a team entity ID from the hashed storage name, and exclude top-level ad hoc team leader metrics in version one.

Use nested `agent_id` or `team_id` for child attribution and require each ID to be in the matching allowed agent or configured-team set.

Deduplicate with `(entity_kind, entity_id, run_id)` when a stable run ID exists and with the source row plus structural key when it does not.

- [ ] **Step 3: Write failing completeness tests.**

Cover:

- An uncompacted admin source whose cumulative session token metrics equal retained-run totals.
- A compacted admin source whose cumulative metrics exceed retained totals.
- A requester-isolated private self source where the same comparison is allowed.
- A shared self source where cumulative metrics and deltas are not exposed and coverage is `unknown`.
- A corrupt or busy source producing `partial` coverage while other sources still contribute.
- A query with more than 200 breakdown keys producing deterministic truncation.
- Exact public counts for scanned and partial sources, scanned sessions, retained and skipped runs, malformed runs, missing requester and timestamp attribution, and compacted sessions.
- A busy or corrupt sole existing self source producing a source-unavailable error instead of a successful partial or zero report.
- An all-absent self source producing a valid empty retained-history report.

- [ ] **Step 4: Implement privacy-safe coverage classification.**

Use `session_data.session_metrics` only as a coverage signal.

Permit cumulative-versus-retained comparison only when the query is admin scope or every compared source is physically requester-isolated for the self requester.

Never add cumulative metrics to the query totals because they cannot satisfy arbitrary date windows or per-run attribution.

Return a source-unavailable error when every existing expected self source is unreadable.

Return `partial` only when at least one source was read safely and another existing source is unreadable or cumulative metrics prove retained runs are lower.

Return `complete_retained` when all inspected eligible sources are readable and the retained comparison is equal, and return `unknown` when privacy or missing cumulative evidence prevents a conclusion.

- [ ] **Step 5: Verify recursion and coverage.**

Run:

```bash
uv run pytest -q tests/test_usage_stats.py
```

Expected: all recursive attribution, deduplication, and coverage tests pass.

---

## Task 5: Expose the Tool with Two Independent Admin Gates

**Files:**

- Create: `src/mindroom/custom_tools/usage_stats.py`
- Create: `src/mindroom/tools/usage_stats.py`
- Modify: `src/mindroom/tools/__init__.py`
- Modify: `src/mindroom/tool_system/worker_routing.py`
- Create: `tests/test_usage_stats_tool.py`
- Modify: `tests/test_sandbox_proxy.py`

- [ ] **Step 1: Write failing toolkit authorization tests.**

Patch `get_tool_runtime_context()` with typed `ToolRuntimeContext` instances and patch the collection functions so tests can prove call ordering without touching a database.

Cover:

- `get_my_usage` derives the current agent and requester from runtime context.
- `get_my_usage` has no agent-name or requester-ID argument.
- A missing requester fails before discovery.
- An alias requester is canonicalized.
- `get_all_usage` is not registered when `admin_scope` is false.
- `get_all_usage` is registered when `admin_scope` is true.
- An admin-scope toolkit still rejects a requester outside `authorization.global_users`.
- An unauthorized admin call does not call `collect_admin_usage`.
- An authorized canonical or aliased global user can call the admin collector.
- Collection runs through `asyncio.to_thread` so SQLite scanning does not block the event loop.
- Results use `custom_tool_payload` and expose no raw context or identity values beyond requested aggregate grouping keys.

Run:

```bash
uv run pytest -q tests/test_usage_stats_tool.py
```

Expected: failure because the toolkit does not exist.

- [ ] **Step 2: Implement the custom toolkit.**

Create `UsageStatsTools(Toolkit)` with this constructor and dynamic registration behavior:

```python
class UsageStatsTools(Toolkit):
    def __init__(self, *, admin_scope: bool = False) -> None:
        self._admin_scope = admin_scope
        functions = [self.get_my_usage]
        if admin_scope:
            functions.append(self.get_all_usage)
        super().__init__(name="usage_stats", tools=functions)
```

Expose these async functions:

```python
async def get_my_usage(
    self,
    start: str | None = None,
    end: str | None = None,
    group_by: Literal["day", "model"] = "day",
) -> str:
    ...


async def get_all_usage(
    self,
    start: str | None = None,
    end: str | None = None,
    group_by: Literal["entity", "requester", "model", "day"] = "entity",
    entity_names: list[str] | None = None,
    requester_ids: list[str] | None = None,
) -> str:
    ...
```

For `get_my_usage`, require `context.agent_name`, `context.requester_id`, `context.config`, and `context.runtime_paths`, then build the execution identity with `build_execution_identity_from_runtime_context()`.

For `get_all_usage`, check `self._admin_scope`, canonicalize `context.requester_id`, and require membership in canonicalized `context.config.authorization.global_users` before invoking `asyncio.to_thread` or any collector.

Return errors through the project's normal custom-tool payload shape without exposing filesystem or SQL details.

- [ ] **Step 3: Register metadata and make the tool local-only.**

Register the wrapper with:

```python
@register_tool_with_metadata(
    name="usage_stats",
    display_name="Usage Statistics",
    description="Inspect retained token usage without modifying session storage",
    category=ToolCategory.INFORMATION,
    status=ToolStatus.AVAILABLE,
    setup_type=SetupType.NONE,
    default_execution_target=ToolExecutionTarget.PRIMARY,
    icon="FaChartBar",
    icon_color="text-cyan-500",
    dependencies=["agno"],
    function_names=("get_my_usage", "get_all_usage"),
    config_fields=[
        ConfigField(
            name="admin_scope",
            label="Enable Admin Scope",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
    agent_override_fields=[
        ConfigField(
            name="admin_scope",
            label="Enable Admin Scope",
            type="boolean",
            required=False,
            default=False,
        ),
    ],
)
```

If metadata validation rejects the same field in global and agent override lists, keep it only in the supported authored-override location and add a regression test proving per-agent configuration reaches the toolkit constructor.

Import and export `usage_stats_tools` from `src/mindroom/tools/__init__.py`.

Add `usage_stats` to `_LOCAL_ONLY_TOOL_NAMES` in `worker_routing.py`.

Extend the existing local-only sandbox test to prove the tool never proxies to a worker target and assert `tool_stays_local("usage_stats")` directly.

- [ ] **Step 4: Verify toolkit and metadata behavior.**

Run:

```bash
uv run pytest -q tests/test_usage_stats_tool.py tests/test_sandbox_proxy.py -k 'usage_stats or local_only'
uv run pytest -q tests/test_tools_metadata.py tests/test_tool_system_facade_boundaries.py tests/test_tool_execution_identity_payloads.py
```

Expected: all selected tests pass.

---

## Task 6: Document Configuration and Regenerate Tool Metadata

**Files:**

- Modify: `docs/tools/agent-orchestration.md`
- Modify: `src/mindroom/tools_metadata.json`
- Modify: `tests/test_usage_stats_tool.py`

- [ ] **Step 1: Add user-facing documentation.**

Document a self-service agent configuration and an admin-agent override configuration.

Include these statements:

- Self-service scope is always the current agent plus the canonical current requester.
- Private-agent reads remain inside the current user's private instance.
- Admin access needs both `admin_scope: true` and a requester in `authorization.global_users`.
- All operations are local and read-only.
- Results cover retained Agno run history, exclude the in-flight call, and can be incomplete after compaction.
- Provider billing, embedding usage, and speech-to-text usage are outside version one.

Keep every sentence on one Markdown line.

- [ ] **Step 2: Regenerate the committed metadata snapshot.**

Use the same export path exercised by `tests/test_tools_metadata.py` rather than editing JSON by hand.

Run the repository's existing export command if one is documented by the test or task runner.

If no wrapper command exists, use `uv run python -c` with `mindroom.tool_system.metadata.export_tools_metadata()` and write only `src/mindroom/tools_metadata.json`.

- [ ] **Step 3: Add a public-shape regression test.**

Assert the exported tool metadata advertises both functions, defaults `admin_scope` to false, selects primary execution, and exposes the per-agent authored override needed to create an admin agent.

Assert serialized sample self and admin payloads contain only documented aggregate, grouping, time-window, cost-coverage, and completeness keys.

- [ ] **Step 4: Run focused documentation and metadata checks.**

Run:

```bash
uv run pytest -q tests/test_usage_stats_tool.py tests/test_tools_metadata.py
uv run pre-commit run --files docs/tools/agent-orchestration.md src/mindroom/tools_metadata.json
```

Expected: tests and file-specific hooks pass.

---

## Task 7: Run Integrated Verification and Review the Diff

**Files:**

- Verify all files changed in Tasks 1 through 6.

- [ ] **Step 1: Synchronize the full development environment.**

Run:

```bash
uv sync --all-extras
```

Expected: dependency synchronization succeeds on the main-based worktree.

- [ ] **Step 2: Run the complete focused test set.**

Run:

```bash
uv run pytest -q \
  tests/test_usage_stats_storage.py \
  tests/test_usage_stats.py \
  tests/test_usage_stats_tool.py \
  tests/test_tools_metadata.py \
  tests/test_tool_system_facade_boundaries.py \
  tests/test_tool_execution_identity_payloads.py \
  tests/test_team_run_metadata.py
```

Expected: all focused tests pass.

- [ ] **Step 3: Run import-boundary and configuration regressions.**

Run:

```bash
uv run pytest -q tests/test_import_graph.py tests/test_config_discovery.py tests/test_agents.py
```

Expected: all selected regressions pass without expanding the slim import allowlist.

- [ ] **Step 4: Run repository hooks.**

Run:

```bash
uv run pre-commit run --all-files
```

Expected: every hook passes.

- [ ] **Step 5: Perform an explicit privacy and read-only audit.**

Search the implementation for accidental write-capable connections, Agno aggregate writers, and leaked identifiers:

```bash
rg -n 'sqlite3\.connect|SqliteDb|calculate_metrics|CREATE TABLE|INSERT|UPDATE|DELETE' src/mindroom/usage_stats.py src/mindroom/usage_stats_storage.py src/mindroom/custom_tools/usage_stats.py
rg -n 'room_id|thread_id|session_id|event_id|messages|content|tool_args' src/mindroom/usage_stats.py src/mindroom/custom_tools/usage_stats.py
```

Expected: the only SQLite connection is the reviewed `mode=ro` helper, no mutating SQL exists, and disallowed identifiers are absent from public serialization.

- [ ] **Step 6: Review only intended changes before any commit.**

Run:

```bash
git status --short
git diff --check
git diff --stat
git diff
```

Expected: only the files named in this plan are changed, whitespace checks pass, and no `docs/baspowers/*` file is present.

If a commit is requested later, stage explicit paths rather than using `git add -A`, inspect `git diff --cached --stat` and `git diff --cached`, and create a new commit without amending an existing one.

---

## Deferred Follow-Up

If exact lifetime totals across compaction or provider surfaces become a requirement, design an append-only usage ledger at the response terminal boundary as a separate feature.

That follow-up must preserve the same canonical requester, private-instance, and admin authorization boundaries rather than weakening this tool's read-only contract.
