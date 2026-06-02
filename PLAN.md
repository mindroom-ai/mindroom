# ISSUE-228 Plan

## Diagnosis

PR #1112 fixed agent file-memory knowledge path resolution only when an active tool execution identity is present.
The background refresh path often resolves with `execution_identity=None`, so shared file-memory knowledge bases can still fall back to the config-relative `./memory` directory and publish an empty index.

The on-disk metadata at `/home/basnijholt/.mindroom-chat/mindroom_data/knowledge_db/openclaw_memory_1fffdb30/indexing_settings.json` confirms the failure mode.
It records `/home/basnijholt/.mindroom-chat/memory` as the indexed path and the empty-string SHA-256 source signature.

## Approach

Use option (b).
Teach `resolve_knowledge_binding` to derive the owning file-memory agent from config when there is no live execution identity.
For a shared semantic knowledge base whose path exactly matches `memory.file.path`, the identityless resolver should bind to the owning agent workspace instead of the config directory.

For multi-agent shared base IDs, preserve per-agent separation.
The refresh scheduler and source watcher should fan out identityless background work across each file-memory owner, using an agent-scoped refresh identity for each owner.
That keeps `openclaw_memory` for `openclaw` and `openclaw_memory` for another file-memory agent as separate published index keys because their workspace paths differ.

## Scope

- Update `src/mindroom/runtime_resolution.py` for identityless file-memory owner resolution and small helper APIs.
- Update the background refresh scheduling and watcher call sites only as needed to preserve per-agent separation.
- Add focused regressions in `tests/test_knowledge_manager.py`.
- Do not refactor unrelated knowledge indexing, agent creation, or Matrix runtime code.

## Verification

Run the focused knowledge-manager regression tests first.
Then run `uv run mypy` and `uv run ruff check`.
Do not mark the issue fixed after tests; live verification remains Bas's call.
