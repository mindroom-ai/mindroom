# Replace Agno Design

This record explains why MindRoom replaces Agno's runtime with its own, which Agno parts it keeps, which shapes it preserves, and how the replacement lands.

## Problem

MindRoom pins `agno==3.0.9` and uses its agent and team loops, model adapters, tool containers, session storage, knowledge, skills, Learning, and about 96 vendor toolkits.
By October 2026 MindRoom carried 135 `AGNO_COMPAT` markers in 62 files and about 7,300 lines in `agno_compat_*.py` modules, most of them monkey patches of private Agno methods pinned to one exact version.
About 30 markers exist only to add hooks to Agno's two loop layers (`Agent._arun*` and `Model.aresponse_stream`): request caps, pre-request compaction, approval receipts, post-tool notices, tool-call rewriting, final-request capture, cancellation usage settlement, and running exactly the approved calls.
Another 32 repair provider wire behavior, 20 work around storage, and 41 sit in vendor toolkits.
MindRoom already owns almost all policy (history, compaction, approvals, tool jobs, worker routing, usage, knowledge lifecycle), so Agno supplies mostly mechanics that MindRoom then patches.
Every Agno upgrade must re-verify each private seam, and upstream fixes for the tracked gaps have stayed open for months.

## Decision

MindRoom owns its runtime and keeps Agno only as a tool library.
The owned code takes the extension points MindRoom needs as plain parameters instead of patches:

| Package | Owns |
| --- | --- |
| `mindroom/llm/` | Provider-neutral values: messages, media, usage metrics, run records, requirements, run events, provider errors, and the provider protocol |
| `mindroom/agent_loop/` | The request and tool loop shared by agents, team leaders, and skill reviews; agent runs and approval resumes; system-prompt rendering; one-shot helper calls |
| `mindroom/providers/` | One async, stream-only adapter per wire family over the official SDKs |
| `mindroom/tool_system/` | `ToolFunction`, the only tool shape the loop sees; argument parsing and tool-call execution; the adapter from Agno toolkits; the skills toolkit |
| `mindroom/session_storage/` | Conversation databases on stdlib `sqlite3` |
| `mindroom/teams/` | Team formation, member delegation, and team runs on the owned loop |
| `mindroom/knowledge/`, `mindroom/embeddings/` | Embedders, the chunk write pipeline, direct Chroma access, and knowledge search |

Agno stays for the parts that touch no loop, model, or storage, all called through public API:

- `Toolkit`, `Function`, and the `@tool` decorator remain the authoring API for vendor tools and for plugins written against them.
  They were MindRoom's authoring API at first too; once the replacement had shipped, MindRoom's tools moved to an owned `Toolkit`, `@tool`, and `current_call()`, which `mindroom.plugin_api` also offers plugins, with an owned schema generator that reproduces Agno's schemas, because Agno's containers made every MindRoom step edit a built toolkit's function maps (filters, output files, worker routes, a sync and an async copy of each function) and handed a tool its call only as an injected Agno `RunContext` or agent.
  `ToolResult` stayed in Agno's API at first; once the replacement had shipped, MindRoom's tools moved to returning text or MindRoom's `ToolValue`, and the adapter reads a plugin or vendor `ToolResult` as one, because keeping it left MindRoom's media tools and the worker media wire on Agno's pydantic media, converted again wherever they were read, and saved job results tagged with Agno's name.
  Agno's signature-and-docstring schema generation and pydantic argument coercion served those toolkits at first too; once the replacement had shipped, every Agno function whose signature gives its schema moved to MindRoom's generator and its coercion, because Agno rendered a value annotated `Any` or `object` as a closed empty object and a `**kwargs` catch-all as a required `kwargs` property, which refused real arguments, and one generator should own every schema.
  Agno still finishes a schema a function authored, such as one Agno's `@tool` derives, or a strict one, which MindRoom's generator does not derive.
- Its vendor toolkits stay as dependencies, with MindRoom's existing per-toolkit overrides.
- Its knowledge file readers and chunkers stay.

The adapter turns each Agno function into a `ToolFunction` once, when an agent is built.
At call time the owned executor builds the `run_context` some Agno toolkits declare from owned run state, and passes a stand-in as the Agno `agent` or `team`, because DaytonaTools reads `agent.session_state`; the stand-in carries only the toolkit's own state, which is never stored.
It is the only place the owned loop meets Agno; the `tests/test_agno_boundary.py` test restricts `agno` imports to the adapter, tool and toolkit modules, and the knowledge readers.
The remaining `AGNO_COMPAT` markers then all fix bugs inside individual toolkits, which break only when those toolkits change, not when MindRoom adds a loop stage.
Agno upgrades still re-verify those overrides and the injected-parameter contract.

## Parity Contract

The replacement changes no persisted data and no model-visible bytes.
This keeps prompt caches warm across the upgrade, keeps paused approvals resumable, and avoids rewriting every stored run.

- Stored runs keep Agno 3's JSON keys, `RunStatus` strings, provider identity strings (`model_provider` and the metrics provider name that budgets price by), and `provider_data` keys.
  Owned codecs ignore keys they do not model.
  Once the replacement had shipped, each route records one usage name, its `model_provider`, instead of the metrics provider name that split the `openai`, `codex`, and `kimi` routes by API, because prices and usage reports then keyed one model under two names, so memory extraction and realtime calls matched no model entry's prices.
  Usage stored under the older names reads through a `LEGACY_COMPAT` map, and stored runs' `model_provider`, which native-compaction routes also hash, is unchanged.
- Conversation databases keep their paths and table names; no table is rebuilt.
  Agno merged the Agno 2 `runs` blob on every read, so the owned store moves those runs into the runs table once when a database opens.
- System prompts, team-leader prompts, helper prompts, tool schemas, and provider request bodies match what Agno 3.0.9 plus MindRoom's patches produce.
  Tool schemas still came from Agno's generator during the replacement, and the goldens proved that the adapter and MindRoom's tool wrappers preserved them; the move to MindRoom's generator came later, as recorded above.
- Approval-card ids, tool-job saved events, and the `io.mindroom.ai_run` metadata that MindRoom Chat reads keep their shapes.

Parity is proven by golden fixtures captured while Agno 3.0.9 still runs the loop: Agno-written databases, run and message dicts, rendered prompts, tool schemas for every registered function, tool-argument coercion results, provider request bodies for canonical conversations, and parsed provider responses.
Deliberate fixes land only after parity holds and update the golden in the same change.

## Phasing

Phase A lands the goldens, the core values, the tool adapter, and the owned knowledge internals while Agno still runs the loop.
Phase B swaps the loop, teams, providers, storage, and history assembly together and removes every Agno use outside the tool and reader boundary.
Storage, the loop, and teams must switch atomically, because Agno's `Team` requires Agno `Agent` members and Agno's loop writes through Agno's storage objects.
Provider adapters, storage, the loop, and teams are built and unit-tested during Phase A against the core values and goldens, then wired in Phase B.

## Retired Features

- Agno Learning (`learning`, `learning_mode`): a second memory system beside mem0 and file memory, with its own extraction calls and `learning/<agent>.db`, which the maintainer judged not useful.
  Configs that still set these fields fail validation with an error naming each field, because requiring a config edit was judged better than keeping tables that warn about and drop retired fields; existing learning databases are left on disk, since nothing reads them.
- `compress_tool_results`: Agno's extra model call that rewrites tool results; off by default.
- Agno's `{key}` and `$key` substitution in system prompts, which no MindRoom prompt uses.

## Rejected Alternatives

- **Keep patching and upstream the fixes.**
  Upstream PRs for the tracked gaps stayed open for weeks to months, and the missing loop hooks are design choices upstream has not taken.
  The cost of exact-version private patches grows with every MindRoom feature that needs a new loop stage.
- **Fork Agno.**
  A fork keeps the 435,000-line package and its abstractions, of which MindRoom uses a small part, and still needs every repair.
- **Port every vendor toolkit.**
  Copying the 96 toolkits adds about 14,000 lines whose 41 markers fix bugs inside single toolkits, not seams of the loop.
  It would also break every external plugin that subclasses `agno.tools.Toolkit` and drop Composio, whose `composio-agno` package returns Agno toolkits.
- **A new storage schema with a one-time run rewrite.**
  Rewriting every stored run risks replay and approval regressions for no user-visible gain; owned codecs over the existing keys need no migration.
  Once the replacement had shipped, [conversation storage rows](2026-10-25-conversation-storage-rows.md) reversed this for conversation databases in a release of its own.
- **Adapters at every layer.**
  An Agno `Model` adapter over owned providers or an Agno storage adapter over the owned store would force the owned designs to mimic Agno's internals.
  Only the tool boundary is adapted, because an Agno `Function` already exposes its finished schema, entrypoint, and approval flags as public fields.
