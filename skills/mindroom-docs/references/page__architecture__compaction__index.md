# History compaction

History preparation chooses native provider checkpoints when the resolved policy permits them, or portable conversation summaries when configured.
The provider choice, configuration schema, durable state format, and retry attempt limit are independent of this module split.

## Ownership

All paths below are relative to `src/mindroom/`.

| Module | Owns |
| --- | --- |
| `history/policy.py` | Resolving configuration and deciding whether compaction is available or needed. |
| `history/runtime.py` | Preparing one response, model construction, compaction notices, failure handling, and applying the final replay decision. |
| `history/session_context.py` | Canonical agent/team scope identity, opening the correct session store, and closing owned handles. |
| `history/replay.py` | Materializing visible persisted history, estimating its cost, and fitting replay limits. |
| `history/compaction.py` | Selecting runs, executing summary chunks, switching fallback models, and committing chunk progress. |
| `history/summary_input.py` | Serializing conversation and prior summary into bounded inputs; preserving a progress-making run envelope when shrinking. |
| `history/message_content.py` | The shared text and media projection used by serialization and token estimation. |
| `history/summary_call.py` | One cancellable summary call, a typed wall deadline, complete-output validation, and bounded retry policy. |
| `history/storage.py` | Durable scope state, the replayed summary, archive reconciliation before each run, and generation-precise redaction. |
| `history/archive.py` | The `<session_table>_compactions` and `<session_table>_compacted_runs` tables: summary generations and archived runs, moved in one transaction with the live run rows. |
| `history/legacy_compaction_state.py` | Recognizing scope state written when compaction deleted runs, for one-time adoption as a content-free generation. |
| `history/native.py` | Selecting and configuring native compaction for the resolved history route. |
| `native_compaction.py` | Provider-neutral checkpoint and native replay data. |

`SummaryModel` binds the serving model, its configured name, and its input budget.
A fallback switch replaces that whole value, so later chunks cannot accidentally use the previous model's budget or identity.
Persisted replay fitting and the Vertex full-request guard remain separate because they measure different inputs at different boundaries.

## Compatibility code

Agno internals use named `agno_compat_<subject>.py` boundaries beside their owners.
Each boundary records the upstream issue or tracking gap, proposed PR, removal condition, and regression coverage in source comments.
The [Agno compatibility inventory](https://docs.mindroom.chat/architecture/agno-compatibility/) covers the full runtime; the table below describes the history-related boundaries and the shared tool-hook patch.

| Module | Workaround and installation point |
| --- | --- |
| `history/agno_compat_message_builder.py` | Preserves roleful Team inputs and strips historical inline media in Agent/Team message builders; installed explicitly by `agents._initialize_agent_instance` and `teams._create_team_instance`. |
| `agno_compat_session_persistence.py` | Guards and adapts Agno's asynchronous persistence against the owned session store; installed by `agent_storage._create_sqlite_state_storage` before filesystem work. |
| `agno_compat_prepared_tools.py` | Shares private Agent/Team tool preparation between RTC execution and prompt inspection; temporarily supplies Team tool instructions, restoring the original list even on failure. |
| `tool_system/agno_compat_tool_hooks.py` | Adapts Agno's private sync/async hook chains to deferred results and owner-controlled synchronous execution; installed by `tool_system/tool_hooks.py`, which retains dispatch, approval, and cancellation ownership. |
| `history/claude_replay_compat.py` | Removes stale signed reasoning from completed portable turns after rewriting; native checkpoint replay retains its separate provider policy. |
| `history/summary_provider_compat.py` | Normalizes effective Claude request overrides, preserves shorter HTTP limits and injected transports, disables nested Claude/OpenAI SDK retries, and classifies provider completion signals. |
| `agno_compat_provider_errors.py` | Interprets legacy provider error strings and SDK cause chains when typed errors are unavailable. |

Importing the history engine does not install either global Agno patch.
The explicit installers remain idempotent, and the persistence patch retains its pinned-version guard.
When upgrading Agno or a provider SDK, inspect these compatibility modules first and run their contract tests before removing a workaround.
Mantle currently needs its existing HTTP transport passed explicitly when copying SDK client options.

## Invariants

- Summary instructions occupy their own system message; serialized conversation and prior summary occupy the user message.
- System/developer prompt roles and bulky request metadata are excluded from the portable conversation input.
- Claude summaries stopped by an output or context limit, and incomplete OpenAI Chat/Responses summaries, cannot be committed as complete summaries.
- Summary overrides preserve effective output-cap precedence while removing thinking from typed fields, request parameters, and raw body parameters.
- The outer retry policy disables Agno-level retries; Claude and OpenAI summary adapters also disable SDK retries while preserving caller-owned clients and transports.
- If the saved summary alone exceeds the final history budget, replay preparation raises an explicit budget error before a reply model call; the durable summary and already committed compaction progress remain intact.
- A chunk moves its runs, member runs included, into the archive and records its summary generation in the same transaction that deletes their live rows; nothing compaction touches is lost.
- The session summary is written after that transaction; before each run, reconciliation restores the latest generation's summary and deletes live runs that are already archived.
- Earlier committed chunks survive later failure or cancellation; compacted runs cannot silently reappear.
- Redacting an event represented by an archived run removes that run and everything after it, returns the generation's earlier runs to replay, and replays the previous generation's summary.
- Content-free legacy generations cannot be split: while one still replays its summary, an event it may contain (its captured seen ids, retained source ownership, or any removed live run) clears it together with every later generation, their archived runs, and all live runs, as before the archive existed.
- Legacy provenance takes precedence over an archive hit, because every later generation was built on the legacy summary.
- After any redaction change, the scope's preserved seen ids are exactly those its remaining compacted history represents, so removed messages return as unseen thread context.
- The archive only grows; redaction and conversation deletion are its only removals.
- Downgrading to a release without the archive and upgrading again adopts the older release's compaction state once more; runs that release deleted stay lost.
- Only the affected scope is rewritten; current-turn media and current-turn reasoning retain their existing replay rules.

The history, provider transport, Agno patch, import-boundary, and native compaction tests exercise these contracts.

An oversized saved summary requires a model with a larger context window or a smaller current prompt.
This can happen after switching models or when system instructions and the current message leave too little room for history.
Preparation does not silently omit or truncate the saved summary.
