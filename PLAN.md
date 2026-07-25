## Verbatim user report (pinned — do not paraphrase, do not synthesize above this)

> "Oh, okay. I think that is, I would consider a bug or a missing feature. It should also be searchable."

(voice, 2026-07-25 15:26 PDT)

Follow-up, same thread:

> "How do you suggest to fix this?"

(voice, 2026-07-25 15:35 PDT)

### ⚠️ Framing warning — read before forming any position

Bas said the above **in response to a claim the orchestrator made and then retracted**: "file-memory is wired as an auto-injected retrieval source, not a manually searchable base ... I can't *query* memory on demand."

**That claim was FALSE.** Memory IS queryable today via `search_memories` — verified live against prod. The retracted framing ("memory is not searchable") MUST NOT leak into your reasoning, your plan, or the consensus. Any argument that rests on "memory is currently unsearchable" is invalid on its face.

The narrower, real gap: the `file_memory_*` runtime-overlay base never reaches `search_knowledge_base`'s `Available sources:` list.

**The post-correction ask is INFERRED, not restated by Bas.** The literal sentence "it should also be searchable" is arguably already satisfied. Bas has been asked directly to choose between a behavior change and a docs-only fix; his answer is not yet in. Your consensus MUST therefore remain valid under both outcomes — structure it so the confirmation gate sits between stages rather than assuming the behavior change is authorized.

# ISSUE-256 Authoritative Plan

## Phase status

This file is the single authoritative implementation plan.

This phase authorizes no implementation or source-code changes.

Stage 0 is the agreed description/docs correction that is safe to ship under either answer from Bas, but its implementation is not authorized yet.

Stage 1 remains gated on Bas explicitly confirming the inferred behavior change.

## Correction: isolation authority

`resolve_knowledge_owner_bindings()` does not exist and never landed on main.

It must not be resurrected, reintroduced, or replaced with a compatibility shim.

The existing isolation authority is `resolve_agent_runtime()` at `src/mindroom/runtime_resolution.py:170-245`.

It maps `(agent_name, execution_identity)` to the canonical state root, workspace, and `file_memory_root`.

The current file backend already reaches that authority through `resolve_file_memory_resolution()` at `src/mindroom/memory/_policy.py:215-252`.

## 1. H2 verdict

The current memory/knowledge split is deliberate at the capability and result-contract level, but the exclusion of a ready semantic agent-memory index from `search_knowledge_base` is not shown to be a deliberate discoverability policy.

`search_memories` owns capabilities the generic knowledge surface does not:

- It returns memory IDs, including `semantic:<source_file>:<rank>`, that feed memory read/update/delete operations (`src/mindroom/memory/_semantic_file_search.py:129-152`; `src/mindroom/custom_tools/memory.py:213-240` and the following update/delete methods).
- It falls back to direct keyword search when the semantic index is cold or degraded and preserves `SemanticFileMemoryIndexUnavailableError.degraded_reason` (`src/mindroom/memory/_file_backend.py:883-943`; `src/mindroom/memory/_semantic_file_search.py:42-52`).
- It merges team-visible file memory through keyword search (`src/mindroom/memory/_file_backend.py:755-814`).
- Its results and degradation notice are auto-injected during prompt assembly independently of Agno knowledge search (`src/mindroom/memory/functions.py:191-253`).

The semantic file-memory path already uses a real runtime `KnowledgeBaseConfig` overlay and the published knowledge-index machinery (`src/mindroom/memory/_semantic_file_search.py:75-92`; `src/mindroom/memory/_semantic_file_search.py:172-211`).

H2 therefore forbids removing or folding away `search_memories`, treating `search_knowledge_base` as canonical for memory, promising keyword fallback/team memory/CRUD IDs through the knowledge tool, or changing automatic prompt injection.

H2 permits only an additive, read-only, semantic, agent-scope exposure through `search_knowledge_base`, gated on Bas explicitly requesting it.

## 2. Root causes

### RC1 — the file-memory knowledge base exists only in a derived runtime overlay

The `file_memory_*` ID is derived from the memory scope and resolved root in `src/mindroom/memory/_semantic_file_search.py:55-65`.

The semantic `KnowledgeBaseConfig` is installed only on a derived `Config` in `src/mindroom/memory/_semantic_file_search.py:75-92`.

`Config.with_runtime_knowledge_base_overlay()` adds the base definition without assigning its ID to an entity in `src/mindroom/config/main.py:1083-1104`.

### RC2 — agent knowledge resolution enumerates only assigned entity IDs

`_semantic_agent_knowledge_base_ids()` reads `config.resolve_entity(agent_name).knowledge_base_ids` in `src/mindroom/knowledge/utils.py:369-374`.

`resolve_agent_knowledge_access()` returns no knowledge when that list is empty in `src/mindroom/knowledge/utils.py:412-422`.

The downstream merge and model-facing source rendering already work once a handle reaches them (`src/mindroom/knowledge/utils.py:695-719`; `src/mindroom/agent_knowledge_descriptions.py:27-56`).

### RC3 — `create_agent()` has a second authored-ID gate

`knowledge_enabled` requires both authored/resolved entity `knowledge_base_ids` and a non-`None` handle in `src/mindroom/agents.py:1807-1810`.

That value controls both the Agno `knowledge` handle and generated `search_knowledge_base` tool in `src/mindroom/agents.py:1844-1847`.

This gate is independently blocking for a file-memory-only agent and must be fixed in the behavior stage.

## 3. Minimum-scope staged plan

### Stage 0 — description/docs correction, safe to ship regardless

Stage 0 is the first implementation stage once implementation is authorized.

It is valid and safe under either eventual answer from Bas.

1. Rewrite the model-facing `search_memories` docstring at `src/mindroom/custom_tools/memory.py:148-160`.

   It must be backend-neutral because the same toolkit serves the active MindRoom memory backend (`src/mindroom/custom_tools/memory.py:90-117`).

   It should state that file memory searches configured Markdown paths, whose default is `memory/**/*.md`, with keyword fallback, and that returned IDs are used by get/update/delete.

2. Make `_knowledge_search_tool_description()` non-closed-world without adding agent state or a config branch (`src/mindroom/agent_knowledge_descriptions.py:43-56`).

   Stage 0 wording:

   ```text
   This list only describes sources available through search_knowledge_base.
   Other tools may search other corpora; use search_memories for MindRoom memory when that tool is available.
   ```

3. Update `docs/memory.md:153-164`, `docs/openclaw.md:104-109`, and `docs/tools/memory-and-storage.md:34-49`, then regenerate their checked-in documentation references.

   The Stage 0 docs must say that file memory is already searchable through `search_memories` even though it is not currently a `search_knowledge_base` source.

4. Delete the false `resolve_knowledge_owner_bindings` interface exposure from `tach.toml:3680-3691`.

   The symbol does not exist, so this is removal of a stale pointer, not a compatibility change.

   Do not add a shim.

5. Add focused tests for the generated `search_memories` description and the non-closed-world knowledge-tool description.

### Confirmation gate

After Stage 0, tell Bas that memory was already searchable on demand and that the descriptions are now corrected.

Ask whether he additionally wants a ready semantic agent-memory index listed inside `search_knowledge_base`.

State the limitations explicitly: semantic only, agent scope only, no keyword fallback, no team-visible memory, and no memory IDs.

Do not begin or ship Stage 1 without an affirmative answer.

### Stage 1 — additive behavior, gated on confirmation

1. Add a focused cycle-free leaf such as `src/mindroom/file_memory_knowledge.py`.

   Move, rather than duplicate, the base-ID, include-pattern, and overlay-config construction currently at `src/mindroom/memory/_semantic_file_search.py:55-92`.

   The leaf's agent-specific resolver must:

   - Return `None` before runtime resolution when the name is not in `config.agents`, because the router also passes through knowledge resolution (`src/mindroom/bot.py:936-955`).
   - Gate on effective `memory_backend == "file"` and effective `memory_search.mode == "semantic"`.
   - Call `resolve_agent_runtime()` for the canonical `file_memory_root`.
   - Preserve the exact `agent_<name>` scope rule at `src/mindroom/memory/_policy.py:93-95`.
   - Return the exact base ID and effective overlaid config used by semantic memory search.
   - Describe the source as configured file memory, with `memory/**/*.md` identified as the default rather than an immutable path (`docs/memory.md:156-161`).

2. Replace the local builders in `src/mindroom/memory/_semantic_file_search.py:55-92` with the shared leaf.

   Leave semantic search, refresh scheduling, unavailable-error semantics, fallback triggers, and memory result IDs unchanged (`src/mindroom/memory/_semantic_file_search.py:95-240`).

3. Extend `resolve_agent_knowledge_access()` at `src/mindroom/knowledge/utils.py:412-455` with the one resolved agent-memory overlay.

   Use the effective overlaid config for lookup, append the memory base after authored semantic bases to preserve existing authored-source order, and route ready handles through `_merge_knowledge()` at `src/mindroom/knowledge/utils.py:695-719`.

   Appending last is deterministic and minimally disruptive, but it does not guarantee fairness because `_interleave_documents()` can starve later sources when `limit` is smaller than the number of sources (`src/mindroom/knowledge/utils.py:674-691`).

   Do not add the runtime ID to authored entity IDs or Docker projection.

4. Change the `create_agent()` gate at `src/mindroom/agents.py:1807-1847` to:

   ```python
   knowledge_enabled = not disable_runtime_capabilities and knowledge is not None
   ```

   All production non-`None` handles originate from `resolve_agent_knowledge_access()` directly or through `KnowledgeAccessSupport` (`src/mindroom/knowledge/utils.py:542-574`; `src/mindroom/teams.py:1556-1579`; `src/mindroom/custom_tools/delegate.py:108-122`).

5. Amend the knowledge-tool closing line to:

   ```text
   For resilient memory search, team-visible memory, and memory IDs, use search_memories.
   ```

   Give the `file_memory_*` source an accurate read-only, semantic-only description.

   Update the three canonical docs and generated references to describe the dual surface without weakening the Stage 0 correction.

6. Add only the Tach dependencies/interfaces needed for the new focused leaf and run `uv run tach check --dependencies --interfaces`.

   Do not resurrect `resolve_knowledge_owner_bindings()`.

7. Before shipping Stage 1, enumerate the effective file-memory + semantic agents in Bas's actual deployment config using the resolved entity fields at `src/mindroom/config/main.py:1728-1740`.

   Do not infer blast radius from defaults or a sample config.

## 4. Isolation authority

Reuse `resolve_agent_runtime()` at `src/mindroom/runtime_resolution.py:170-245`.

It is the existing authority mapping `(agent_name, execution_identity)` to canonical state, workspace, and `file_memory_root`.

The current file backend already reaches it through `resolve_file_memory_resolution()` at `src/mindroom/memory/_policy.py:215-252`.

Private agents fail closed without an execution identity or worker key in `src/mindroom/runtime_resolution.py:136-167`, and private-root resolution rejects symlink escapes in `src/mindroom/runtime_resolution.py:121-133`.

The new resolver must not catch or soften those failures.

`resolve_knowledge_binding()` at `src/mindroom/runtime_resolution.py:248-280` remains the downstream authority for an already-known overlay base, but it cannot originate the execution-scoped file-memory ID.

Keep semantic index configuration in the focused leaf rather than adding chunk/include policy to `runtime_resolution.py`.

Use the same builders from both the memory and knowledge paths so the base ID, root, and index settings cannot drift.

## 5. Test and validation plan

1. **Description lists the base and RC3 is fixed.**

   With a ready file-memory index, assert the generated `search_knowledge_base` description contains the exact `file_memory_*` ID and accurate description.

   Use a memory-only agent with no authored knowledge bases so the test also proves the `src/mindroom/agents.py:1807-1847` gate is fixed.

   Build on the existing description seam at `tests/test_agents.py:3338-3398`.

2. **A real query returns a `memory/**/*.md` hit.**

   Write a distinctive token to `memory/notes.md`, publish through the normal refresh path, resolve agent knowledge, query the returned `Knowledge`, and assert the content plus `source_path` under `memory/`.

   Do not mock the final result.

3. **Cross-agent isolation uses genuinely distinct workspaces.**

   Create alpha and beta with separate real workspace roots and unique tokens in both.

   Publish both indexes, assert different roots and base IDs, query in both directions, and prove neither handle returns the other agent's token.

   Do not use symlinked or aliased workspaces.

4. **Cold/degraded keyword fallback and prompt auto-injection remain intact.**

   Retain and run `tests/test_memory_file_backend.py:824-885`, including the case whose `SemanticFileMemoryIndexUnavailableError` carries a classified `degraded_reason`.

   Retain and run the prompt-assembly guard at `tests/test_memory_file_backend.py:887-913`.

   If moving the builders changes the seam, extend one degraded-index test to resolve the shared overlay first and then assert keyword-tagged results plus unchanged `degraded_reason`.

5. **Base/index identity invariant.**

   Compare the public/shared memory and knowledge seam outputs and assert identical base IDs and compatible `indexing_settings_key` values.

   This test is justified because the published identity includes base ID, storage root, knowledge path, and settings (`src/mindroom/knowledge/registry.py:54-61`; `src/mindroom/knowledge/registry.py:157-175`; `src/mindroom/knowledge/indexing_config.py:288-340`).

   Include `include_entrypoint: true` and assert `MEMORY.md` remains in `include_patterns`, because include patterns participate in the signature (`src/mindroom/memory/_semantic_file_search.py:68-72`; `src/mindroom/knowledge/indexing_config.py:334`).

6. **Fail-closed and non-agent resolver behavior.**

   Assert a router/non-agent name returns `None` before runtime resolution.

   Assert a private agent without execution identity raises, while distinct requester identities produce distinct roots/base IDs (`src/mindroom/runtime_resolution.py:136-198`).

   These tests have direct value because Stage 1 introduces the first `resolve_agent_runtime()` call inside agent knowledge resolution.

7. **Pre-ship live checks.**

   Start one semantic file-memory base cold, observe scheduling through `src/mindroom/knowledge/utils.py:331-366`, and verify the raw `file_memory_*` availability notice clears after the index publishes.

   Smoke-test one known memory token through both tools and a negative query from a second, genuinely separate agent.

8. **Repository gates.**

   Run focused agent, knowledge, file-memory, memory-tool, and projection tests, then the complete `uv run pytest` suite.

   Run `uv run pre-commit run --all-files` and the Tach boundary check after `uv sync --all-extras`.

## 6. Explicit out of scope

- Removing, renaming, or folding away `search_memories`.
- Changing the memory ID or get/update/delete contracts.
- Keyword fallback, team-scope indexing, team-memory exposure, or writes through `search_knowledge_base`.
- Changing memory-search defaults, embedding configuration, chunking, publication format, or multi-source ranking/allocation.
- Dashboard listing or editing of runtime-only file-memory bases.
- Docker serialization of `file_memory_*` runtime IDs.
- Aliased-workspace bleed.
  Two agent names resolving to the same real workspace already share file content through the current memory path, so document this pre-existing ISSUE-253 vector and decide separately whether workspace aliases should imply shared memory.
- Correcting stale external investigation reports such as `ISSUE-228.md`.
- Cosmetic display names for raw `file_memory_*` availability notices.
