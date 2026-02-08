# Knowledge/RAG Feature Report

## What I Implemented

### 1. Config updates (`src/mindroom/config.py`)
- Added `KnowledgeConfig` with:
  - `enabled: bool = False`
  - `path: str = "./knowledge_docs"`
  - `watch: bool = True`
- Added `knowledge: KnowledgeConfig` to root `Config`.
- Added `knowledge: bool = False` to `AgentConfig` so per-agent knowledge access can be toggled.

### 2. Knowledge manager (`src/mindroom/knowledge.py`)
- Added a new `KnowledgeManager` module using:
  - `agno.knowledge.knowledge.Knowledge`
  - `agno.vectordb.chroma.ChromaDb`
- Reused embedder settings from `config.memory.embedder`:
  - OpenAI provider via `OpenAIEmbedder`
  - Ollama provider via `OllamaEmbedder`
- Persisted vector DB at `{storage_path}/knowledge_db`.
- Implemented startup indexing and reindexing:
  - `initialize()`
  - `reindex_all()`
  - `index_file(path, upsert=True)`
- Implemented file deletion from index via metadata (`source_path`).
- Added async folder watching with `watchfiles.awatch()`:
  - add/modify -> upsert file
  - delete -> remove from index
- Added process-global lifecycle helpers:
  - `initialize_knowledge_manager(...)`
  - `get_knowledge_manager()`
  - `get_knowledge()`
  - `shutdown_knowledge_manager()`

### 3. Agent integration (`src/mindroom/agents.py`)
- Extended `create_agent(...)` signature to accept optional shared knowledge instance.
- When `agent_config.knowledge` is true and knowledge exists, Agent now receives:
  - `knowledge=...`
  - `search_knowledge=True`

### 4. Bot/orchestrator integration (`src/mindroom/bot.py`)
- Added orchestrator-managed knowledge lifecycle:
  - init during orchestrator initialization
  - watcher start once bots are running
  - reconfigure on config reload
  - full shutdown on orchestrator stop
- Added `knowledge_manager` state on `MultiAgentOrchestrator`.
- Updated agent creation path so `AgentBot` passes shared knowledge instance into `create_agent(...)`.

### 5. Knowledge API (`src/mindroom/api/knowledge.py` + router registration)
- Added new FastAPI router under `/api/knowledge` with endpoints:
  - `GET /api/knowledge/files`
  - `POST /api/knowledge/upload`
  - `DELETE /api/knowledge/files/{path:path}`
  - `GET /api/knowledge/status`
  - `POST /api/knowledge/reindex`
- Added safe path resolution to prevent traversal outside the configured knowledge folder.
- Added URL-decoding for delete path handling.
- Registered router in `src/mindroom/api/main.py`.

### 6. Frontend Knowledge tab
- Added Knowledge tab to main app shell (`frontend/src/App.tsx`).
- Added knowledge endpoints in `frontend/src/lib/api.ts`.
- Added `frontend/src/components/Knowledge/Knowledge.tsx` with:
  - status header (enabled/path/file/index stats)
  - table (Name, Size, Type, Modified, delete)
  - upload button + multi-file input
  - drag-and-drop upload zone
  - reindex button
- Updated frontend config typings/store to preserve and default `agent.knowledge` and root `config.knowledge`:
  - `frontend/src/types/config.ts`
  - `frontend/src/store/configStore.ts`

### 7. Dependency updates
- Added `watchfiles` in `pyproject.toml`.
- Updated lockfile via `uv sync --all-extras`.

## Key Decisions
- Used a single shared process-wide knowledge manager to avoid per-agent duplicate indexing and duplicate watchers.
- Reused existing memory embedder config for knowledge embeddings to keep setup simple and consistent.
- Used `source_path` metadata for file-level delete semantics in Chroma.
- Kept frontend implementation as one component file for Phase 1 simplicity.
- Wired watcher lifecycle to orchestrator runtime state (start only when running, stop on shutdown).

## Questions / Concerns For Review
- `indexed_count` is tracked in manager memory, so it resets when manager reinitializes (though files are reindexed on init).
- In API-only usage (without bot runtime), status/index operations can initialize/reindex the manager on first call when knowledge is enabled.
- Upload endpoint currently writes files by basename into the knowledge root (no nested directory upload behavior).

## Validation
- `pytest` (full suite): **880 passed, 34 skipped**
- `cd frontend && bun run build`: **passed**
- `pre-commit run --all-files`: **passed**
