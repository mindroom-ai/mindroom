# Replace Agno Design

This record explains why MindRoom replaces the Agno framework with its own agent runtime, which shapes it keeps, and how the replacement lands.

## Problem

MindRoom pins `agno==3.0.9` and uses its agent and team loops, model adapters, tool containers, session storage, knowledge, skills, and about 96 vendor toolkits.
By October 2026 MindRoom carried 135 `AGNO_COMPAT` markers in 62 files and about 7,300 lines in `agno_compat_*.py` modules, most of them monkey patches of private Agno methods pinned to one exact version.
About 30 markers exist only to add hooks to Agno's two loop layers (`Agent._arun*` and `Model.aresponse_stream`): request caps, pre-request compaction, approval receipts, post-tool notices, tool-call rewriting, final-request capture, cancellation usage settlement, and running exactly the approved calls.
Another 32 repair provider wire behavior, 20 work around storage, and 41 sit in vendor toolkits.
MindRoom already owns almost all policy (history, compaction, approvals, tool jobs, worker routing, usage, knowledge lifecycle), so Agno supplies mostly mechanics that MindRoom then patches.
Every Agno upgrade must re-verify each private seam, and upstream fixes for the tracked gaps have stayed open for months.

## Decision

MindRoom owns its runtime and drops the `agno` dependency.
The owned code takes the extension points MindRoom needs as plain parameters instead of patches:

| Package | Owns |
| --- | --- |
| `mindroom/llm/` | Provider-neutral values: messages, media, usage metrics, run records, requirements, run events, provider errors, and the provider protocol |
| `mindroom/agent_loop/` | The request and tool loop shared by agents, team leaders, and skill reviews; agent runs and approval resumes; system-prompt rendering; one-shot helper calls |
| `mindroom/providers/` | One async, stream-only adapter per wire family over the official SDKs |
| `mindroom/tool_system/` | `Toolkit`, `ToolFunction`, signature-to-schema generation, argument parsing, and tool-call execution |
| `mindroom/session_storage/` | Conversation databases on stdlib `sqlite3` |
| `mindroom/teams/` | Team formation, member delegation, and team runs on the owned loop |
| `mindroom/knowledge/`, `mindroom/embeddings/` | Readers, chunkers, embedders, and direct Chroma access |

Vendor toolkits that MindRoom keeps are copied into `custom_tools/` with an Apache-2.0 attribution header, since Agno and MindRoom share that license.

## Parity Contract

The replacement changes no persisted data and no model-visible bytes.
This keeps prompt caches warm across the upgrade, keeps paused approvals resumable, and avoids rewriting every stored run.

- Stored runs keep Agno 3's JSON keys, `RunStatus` strings, provider identity strings (`model_provider` and the metrics provider name that budgets price by), and `provider_data` keys.
  Owned codecs ignore keys they do not model.
- Conversation databases keep their paths and table names; no table is rebuilt.
  Agno merged the Agno 2 `runs` blob on every read, so the owned store moves those runs into the runs table once when a database opens.
- System prompts, team-leader prompts, helper prompts, tool schemas, and provider request bodies match what Agno 3.0.9 plus MindRoom's patches produce.
- Approval-card ids, tool-job saved events, and the `io.mindroom.ai_run` metadata that MindRoom Chat reads keep their shapes.

Parity is proven by golden fixtures captured while Agno 3.0.9 is still installed: Agno-written databases, run and message dicts, rendered prompts, tool schemas for every registered function, provider request bodies for canonical conversations, and parsed provider responses.
Deliberate fixes, such as rendering `Any` parameters as `{}`, land only after parity holds and update the golden in the same change.

## Phasing

Phase A replaces the leaves while Agno still runs the loop: core values, the tool system, every toolkit, and knowledge internals.
One temporary adapter converts an owned `ToolFunction` into an Agno `Function` when an agent is built, so the whole existing suite exercises the ported tools; Phase B deletes it.
Phase B swaps the loop, teams, providers, storage, and history assembly together and removes Agno.
Storage, the loop, and teams must switch atomically, because Agno's `Team` requires Agno `Agent` members and Agno's loop writes through Agno's storage objects.
Provider adapters, storage, the loop, and teams are built and unit-tested during Phase A against the core values and goldens, then wired in Phase B.

## Retired Features

- Agno Learning (`learning`, `learning_mode`): a second memory system beside mem0 and file memory, with its own extraction calls and `learning/<agent>.db`.
  Configs that still set these fields load with a warning; existing learning databases are left on disk.
- `compress_tool_results`: Agno's extra model call that rewrites tool results; off by default.
- Agno's `{key}` and `$key` substitution in system prompts, which no MindRoom prompt uses.
- Vendor toolkits with no tests, defaults, or MindRoom customization, if the owner approves the list.

## Rejected Alternatives

- **Keep patching and upstream the fixes.**
  Upstream PRs for the tracked gaps stayed open for weeks to months, and the missing loop hooks are design choices upstream has not taken.
  The cost of exact-version private patches grows with every MindRoom feature that needs a new loop stage.
- **Fork Agno.**
  A fork keeps the 435,000-line package and its abstractions, of which MindRoom uses a small part, and still needs every repair.
- **A new storage schema with a one-time run rewrite.**
  Rewriting every stored run risks replay and approval regressions for no user-visible gain; owned codecs over the existing keys need no migration.
- **Temporary adapters at every layer.**
  An Agno `Model` adapter over owned providers or an Agno storage adapter over the owned store would force the owned designs to mimic Agno's internals.
  Only the tool adapter is cheap, because Agno accepts prebuilt `Function` objects with explicit schemas.

## Open Questions

- Which long-tail vendor toolkits to retire.
- Whether Composio is rewritten on its core SDK or retired, since `composio-agno` reinstalls Agno.
- External plugins that subclass `agno.tools.Toolkit` must switch to `mindroom.tool_system.toolkit.Toolkit` in the release that removes Agno.
