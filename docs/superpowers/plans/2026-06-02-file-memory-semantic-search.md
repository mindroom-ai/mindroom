# File Memory Semantic Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace memory-as-knowledge-base configs with first-class semantic search for file-backed memory through the existing `search_memories` tool.

**Architecture:** Add global `memory.search` config with optional per-agent `memory_search` override. Keep actual memory indexes per agent or per requester-private root. Route file-backed `search_memories` through a memory-owned semantic index when enabled, and keep `knowledge_bases` only for real shared or private knowledge corpora.

**Tech Stack:** Python 3.13, Pydantic, Agno `Knowledge`, ChromaDB, existing MindRoom memory facade, Vite React dashboard, pytest, ruff.

**Operational Constraints:** Semantic file-memory indexes are scoped per effective memory root. Shared file-memory agents get one index per agent; requester-private agents get one lazy index per requester-private root. Private indexes are built only on identity-carrying `search_memories` calls, not by background prewarming or dashboard refresh. Index updates must be incremental by file signature so auto-flush appends do not force a full corpus rebuild on every search.

---

## File Structure

- Modify `src/mindroom/config/memory.py`: add memory search config models, defaults, validation, and merge helpers.
- Modify `src/mindroom/config/agent.py`: add optional per-agent `memory_search` override.
- Modify `src/mindroom/config/main.py`: expose `get_agent_memory_search(agent_name)`.
- Create `src/mindroom/path_globs.py`: shared root-anchored glob matching and safe relative path validation.
- Modify `src/mindroom/knowledge/manager.py`: import root-glob helpers from `path_globs.py` instead of carrying private copies.
- Create `src/mindroom/embedding_factory.py`: shared configured embedder construction for knowledge and memory semantic indexes.
- Modify `src/mindroom/knowledge/manager.py`: use `create_configured_embedder`.
- Create `src/mindroom/memory/_semantic_file_search.py`: lazy semantic index and vector search for file-backed memory roots.
- Modify `src/mindroom/memory/_file_backend.py`: call semantic search for agent/private scope when enabled; keep keyword search for disabled/fallback/team scope.
- Modify `src/mindroom/custom_tools/memory.py`: keep tool name and wording, no new public tool.
- Modify `src/mindroom/runtime_resolution.py`, `src/mindroom/knowledge/registry.py`, `src/mindroom/knowledge/status.py`, `src/mindroom/knowledge/refresh_scheduler.py`, `src/mindroom/knowledge/watch.py`, `src/mindroom/knowledge/refresh_runner.py`, and `src/mindroom/api/knowledge.py`: remove current PR's shared file-memory knowledge-base owner fan-out.
- Modify `src/mindroom/cli/config.py`: generated Mind/OpenClaw-style config uses `memory.search` and `tools: [memory]`, not `knowledge_bases: [mind_memory]`.
- Modify `config.yaml`, `docs/openclaw.md`, `docs/memory.md`, `docs/tools/memory-and-storage.md`, `docs/knowledge.md`, and `docs/configuration/agents.md`: document memory semantic search and remove memory-as-KB examples.
- Modify frontend types and editors: `frontend/src/types/config.ts`, `frontend/src/components/MemoryConfig/MemoryConfig.tsx`, `frontend/src/components/AgentEditor/AgentEditor.tsx`.
- Add or modify tests in `tests/test_memory_config.py`, `tests/test_memory_file_backend.py`, `tests/test_memory_tools.py`, `tests/test_knowledge_manager.py`, `tests/test_cli_config.py`, and frontend component tests.

## Task 1: Add Memory Search Config And Inheritance

**Files:**
- Modify: `src/mindroom/config/memory.py`
- Modify: `src/mindroom/config/agent.py`
- Modify: `src/mindroom/config/main.py`
- Test: `tests/test_memory_config.py`

- [ ] **Step 1: Write failing config tests**

Append these tests to `tests/test_memory_config.py`:

```python
def test_memory_search_defaults_to_keyword_daily_files() -> None:
    config = Config(router=RouterConfig(model="default"))

    search = config.get_agent_memory_search("missing_agent")

    assert search.mode == "keyword"
    assert search.include == ["memory/**/*.md"]
    assert search.include_entrypoint is False


def test_agent_memory_search_override_merges_per_field() -> None:
    config = Config(
        memory={
            "search": {
                "mode": "semantic",
                "include": ["memory/**/*.md"],
                "include_entrypoint": False,
            },
        },
        agents={
            "openclaw": AgentConfig(
                display_name="OpenClaw",
                memory_backend="file",
                memory_search={"include_entrypoint": True},
            ),
        },
        router=RouterConfig(model="default"),
    )

    search = config.get_agent_memory_search("openclaw")

    assert search.mode == "semantic"
    assert search.include == ["memory/**/*.md"]
    assert search.include_entrypoint is True


def test_agent_memory_search_can_override_include_patterns() -> None:
    config = Config(
        memory={
            "search": {
                "mode": "semantic",
                "include": ["memory/**/*.md"],
                "include_entrypoint": False,
            },
        },
        agents={
            "openclaw": AgentConfig(
                display_name="OpenClaw",
                memory_backend="file",
                memory_search={
                    "include": ["memory/**/*.md", "decisions/**/*.md"],
                    "include_entrypoint": True,
                },
            ),
        },
        router=RouterConfig(model="default"),
    )

    search = config.get_agent_memory_search("openclaw")

    assert search.include == ["memory/**/*.md", "decisions/**/*.md"]
    assert search.include_entrypoint is True
```

- [ ] **Step 2: Run failing config tests**

Run:

```bash
uv run pytest tests/test_memory_config.py::test_memory_search_defaults_to_keyword_daily_files tests/test_memory_config.py::test_agent_memory_search_override_merges_per_field tests/test_memory_config.py::test_agent_memory_search_can_override_include_patterns -x -n 0 --no-cov -v
```

Expected: fail because `get_agent_memory_search` and `memory_search` do not exist.

- [ ] **Step 3: Add config models**

In `src/mindroom/config/memory.py`, add these imports and models:

```python
from typing import Any, Literal

MemorySearchMode = Literal["keyword", "semantic"]


class MemorySearchConfig(BaseModel):
    """Search behavior for file-backed memory."""

    mode: MemorySearchMode = Field(
        default="keyword",
        description="Search mode for file-backed memory: keyword or semantic",
    )
    include: list[str] = Field(
        default_factory=lambda: ["memory/**/*.md"],
        description="Root-relative glob patterns included in file-memory semantic search",
    )
    include_entrypoint: bool = Field(
        default=False,
        description="When true, include MEMORY.md in file-memory semantic search",
    )


class AgentMemorySearchConfig(BaseModel):
    """Optional per-agent override for file-backed memory search."""

    mode: MemorySearchMode | None = Field(default=None, description="Per-agent memory search mode override")
    include: list[str] | None = Field(default=None, description="Per-agent included memory-search glob patterns")
    include_entrypoint: bool | None = Field(default=None, description="Per-agent MEMORY.md indexing override")
```

Then add this field to `MemoryConfig`:

```python
    search: MemorySearchConfig = Field(
        default_factory=MemorySearchConfig,
        description="Search behavior for file-backed memory",
    )
```

- [ ] **Step 4: Add per-agent field**

In `src/mindroom/config/agent.py`, import `AgentMemorySearchConfig` from `mindroom.config.memory` and add this field to `AgentConfig` after `memory_backend`:

```python
    memory_search: AgentMemorySearchConfig | None = Field(
        default=None,
        description="Optional per-agent file-memory search override; omitted fields inherit memory.search",
    )
```

- [ ] **Step 5: Add config merge method**

In `src/mindroom/config/main.py`, add this method near `get_agent_memory_backend`:

```python
    def get_agent_memory_search(self, agent_name: str) -> MemorySearchConfig:
        """Get effective file-memory search settings for one agent."""
        inherited = self.memory.search
        agent_config = self.agents.get(agent_name)
        override = agent_config.memory_search if agent_config is not None else None
        if override is None:
            return inherited
        return MemorySearchConfig(
            mode=override.mode if override.mode is not None else inherited.mode,
            include=override.include if override.include is not None else list(inherited.include),
            include_entrypoint=(
                override.include_entrypoint
                if override.include_entrypoint is not None
                else inherited.include_entrypoint
            ),
        )
```

- [ ] **Step 6: Run config tests**

Run:

```bash
uv run pytest tests/test_memory_config.py::test_memory_search_defaults_to_keyword_daily_files tests/test_memory_config.py::test_agent_memory_search_override_merges_per_field tests/test_memory_config.py::test_agent_memory_search_can_override_include_patterns -x -n 0 --no-cov -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/mindroom/config/memory.py src/mindroom/config/agent.py src/mindroom/config/main.py tests/test_memory_config.py
git commit -m "feat: add memory search config"
```

## Task 2: Add Safe Root-Anchored Glob Helpers

**Files:**
- Create: `src/mindroom/path_globs.py`
- Modify: `src/mindroom/knowledge/manager.py`
- Test: `tests/test_memory_config.py`

- [ ] **Step 1: Write failing glob tests**

Append these tests to `tests/test_memory_config.py`:

```python
def test_memory_search_include_pattern_matches_direct_and_nested_daily_files() -> None:
    from mindroom.path_globs import matches_root_glob

    assert matches_root_glob("memory/2026-06-02.md", "memory/**/*.md")
    assert matches_root_glob("memory/2026/06/02.md", "memory/**/*.md")
    assert not matches_root_glob("MEMORY.md", "memory/**/*.md")
    assert not matches_root_glob("docs/runbook.md", "memory/**/*.md")


def test_memory_search_rejects_unsafe_include_pattern() -> None:
    with pytest.raises(ValueError, match="memory.search.include"):
        Config(
            memory={"search": {"include": ["../secret.md"]}},
            router=RouterConfig(model="default"),
        )
```

- [ ] **Step 2: Run failing glob tests**

Run:

```bash
uv run pytest tests/test_memory_config.py::test_memory_search_include_pattern_matches_direct_and_nested_daily_files tests/test_memory_config.py::test_memory_search_rejects_unsafe_include_pattern -x -n 0 --no-cov -v
```

Expected: fail because `mindroom.path_globs` and validation do not exist.

- [ ] **Step 3: Create `path_globs.py`**

Create `src/mindroom/path_globs.py`:

```python
"""Root-anchored glob helpers for config-relative file sets."""

from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path


def split_posix_parts(value: str) -> tuple[str, ...]:
    """Split one slash-separated path or glob into normalized POSIX parts."""
    normalized = value.replace("\\", "/").strip()
    normalized = normalized.removeprefix("./")
    normalized = normalized.strip("/")
    if not normalized:
        return ()
    return tuple(part for part in normalized.split("/") if part and part != ".")


def validate_safe_relative_pattern(value: str, *, field_name: str) -> str:
    """Validate a root-relative glob pattern that cannot escape its root."""
    parts = split_posix_parts(value)
    if not parts or any(part == ".." for part in parts) or Path(value).is_absolute():
        msg = f"{field_name} must be a non-empty relative pattern inside the memory root"
        raise ValueError(msg)
    return "/".join(parts)


def matches_root_glob(relative_path: str, pattern: str) -> bool:
    """Return whether a root-relative POSIX path matches a root-anchored glob."""
    path_parts = split_posix_parts(relative_path)
    pattern_parts = split_posix_parts(pattern)
    if not path_parts or not pattern_parts:
        return False

    cache: dict[tuple[int, int], bool] = {}

    def _match(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in cache:
            return cache[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        else:
            pattern_part = pattern_parts[pattern_index]
            if pattern_part == "**":
                next_index = pattern_index
                while next_index < len(pattern_parts) and pattern_parts[next_index] == "**":
                    next_index += 1
                if next_index == len(pattern_parts):
                    result = True
                else:
                    result = any(_match(next_path, next_index) for next_path in range(path_index, len(path_parts) + 1))
            elif path_index < len(path_parts) and fnmatchcase(path_parts[path_index], pattern_part):
                result = _match(path_index + 1, pattern_index + 1)
            else:
                result = False
        cache[key] = result
        return result

    return _match(0, 0)
```

- [ ] **Step 4: Validate memory search include patterns**

In `src/mindroom/config/memory.py`, import `field_validator` and `validate_safe_relative_pattern`, then add validators to both search config classes:

```python
    @field_validator("include")
    @classmethod
    def validate_include_patterns(cls, value: list[str] | None) -> list[str] | None:
        """Validate include patterns stay inside the memory root."""
        if value is None:
            return None
        return [validate_safe_relative_pattern(pattern, field_name="memory.search.include") for pattern in value]
```

For `MemorySearchConfig`, use `list[str]` return type and reject an empty result:

```python
        normalized = [validate_safe_relative_pattern(pattern, field_name="memory.search.include") for pattern in value]
        if not normalized:
            msg = "memory.search.include must contain at least one pattern"
            raise ValueError(msg)
        return normalized
```

- [ ] **Step 5: Reuse helper in knowledge manager**

In `src/mindroom/knowledge/manager.py`, import:

```python
from mindroom.path_globs import matches_root_glob, split_posix_parts
```

Replace local `_split_posix_parts` and `_matches_root_glob` usage with the imported functions:

```python
if git_config.include_patterns and not any(
    matches_root_glob(relative_path, pattern) for pattern in git_config.include_patterns
):
    return False

return not any(matches_root_glob(relative_path, pattern) for pattern in git_config.exclude_patterns)
```

Remove the old private helper functions from `knowledge/manager.py`.

- [ ] **Step 6: Run glob and selected knowledge tests**

Run:

```bash
uv run pytest tests/test_memory_config.py::test_memory_search_include_pattern_matches_direct_and_nested_daily_files tests/test_memory_config.py::test_memory_search_rejects_unsafe_include_pattern tests/test_knowledge_manager.py::test_config_allows_exact_duplicate_git_roots_with_different_filters tests/test_knowledge_manager.py::test_indexing_settings_filter_keys_are_order_insensitive -x -n 0 --no-cov -v
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/mindroom/path_globs.py src/mindroom/config/memory.py src/mindroom/knowledge/manager.py tests/test_memory_config.py
git commit -m "refactor: share root glob matching"
```

## Task 3: Share Embedder Construction

**Files:**
- Create: `src/mindroom/embedding_factory.py`
- Modify: `src/mindroom/knowledge/manager.py`
- Test: `tests/test_knowledge_manager.py`

- [ ] **Step 1: Write a no-behavior-change test**

No new behavior is expected here. Use an existing focused knowledge embedder test after the refactor:

```bash
uv run pytest tests/test_knowledge_manager.py -k "embedder or embedding" -x -n 0 --no-cov -v
```

Expected before refactor: pass.

- [ ] **Step 2: Create shared embedder factory**

Create `src/mindroom/embedding_factory.py` with code moved from `knowledge.manager._create_embedder`:

```python
"""Embedder construction shared by knowledge and memory semantic indexes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.knowledge.embedder.base import Embedder
from agno.knowledge.embedder.ollama import OllamaEmbedder

from mindroom.constants import RuntimePaths
from mindroom.credentials_sync import get_api_key_for_provider, get_ollama_host
from mindroom.embeddings import MindRoomOpenAIEmbedder, create_sentence_transformers_embedder
from mindroom.model_defaults import OLLAMA_HOST_DEFAULT

if TYPE_CHECKING:
    from mindroom.config.main import Config


def create_configured_embedder(config: Config, runtime_paths: RuntimePaths) -> Embedder:
    """Create the configured embedding provider used for semantic indexes."""
    provider = config.memory.embedder.provider
    embedder_config = config.memory.embedder.config

    if provider == "openai":
        return MindRoomOpenAIEmbedder(
            id=embedder_config.model,
            api_key=get_api_key_for_provider("openai", runtime_paths=runtime_paths),
            base_url=embedder_config.host,
            dimensions=embedder_config.dimensions,
        )

    if provider == "ollama":
        host = get_ollama_host(runtime_paths=runtime_paths) or embedder_config.host or OLLAMA_HOST_DEFAULT
        return OllamaEmbedder(id=embedder_config.model, host=host)

    if provider == "sentence_transformers":
        return create_sentence_transformers_embedder(
            runtime_paths,
            embedder_config.model,
            dimensions=embedder_config.dimensions,
        )

    msg = (
        f"Unsupported semantic-search embedder provider: {provider}. "
        "Supported providers: openai, ollama, sentence_transformers"
    )
    raise ValueError(msg)
```

- [ ] **Step 3: Update knowledge manager**

In `src/mindroom/knowledge/manager.py`, import:

```python
from mindroom.embedding_factory import create_configured_embedder
```

Replace `_create_embedder(self.config, self.runtime_paths)` with:

```python
create_configured_embedder(self.config, self.runtime_paths)
```

Remove the old `_create_embedder` function and now-unused imports from `knowledge/manager.py`.

- [ ] **Step 4: Run embedder-focused tests and ruff**

Run:

```bash
uv run pytest tests/test_knowledge_manager.py -k "embedder or embedding" -x -n 0 --no-cov -v
uv run ruff check src/mindroom/embedding_factory.py src/mindroom/knowledge/manager.py
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add src/mindroom/embedding_factory.py src/mindroom/knowledge/manager.py
git commit -m "refactor: share semantic embedder factory"
```

## Task 4: Build File-Memory Semantic Index

**Files:**
- Create: `src/mindroom/memory/_semantic_file_search.py`
- Modify: `src/mindroom/memory/_file_backend.py`
- Test: `tests/test_memory_file_backend.py`

- [ ] **Step 1: Write failing semantic-search tests**

Append these tests to `tests/test_memory_file_backend.py`:

```python
@pytest.mark.asyncio
async def test_file_backend_semantic_search_reads_daily_memory_root(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"

    workspace = agent_workspace_root_path(storage_path, "general")
    (workspace / "memory").mkdir(parents=True)
    (workspace / "memory" / "2026-06-02.md").write_text("Bas prefers small precise plans.\n", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("Entrypoint should not be indexed by default.\n", encoding="utf-8")

    with patch("mindroom.memory._semantic_file_search.search_semantic_file_memories") as semantic_search:
        semantic_search.return_value = [
            {
                "id": "semantic:memory/2026-06-02.md:0",
                "memory": "Bas prefers small precise plans.",
                "user_id": "agent_general",
                "score": 1.0,
                "metadata": {"source_file": "memory/2026-06-02.md", "semantic": True},
            }
        ]

        results = await search_agent_memories("precise planning", "general", storage_path, config, limit=5)

    assert results[0]["memory"] == "Bas prefers small precise plans."
    semantic_search.assert_called_once()
    call = semantic_search.call_args
    assert call.kwargs["limit"] == 5


@pytest.mark.asyncio
async def test_file_backend_semantic_search_falls_back_to_keyword_on_index_error(
    storage_path: Path,
    config: Config,
) -> None:
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"

    await add_agent_memory("Keyword fallback memory", "general", storage_path, config)

    with patch(
        "mindroom.memory._semantic_file_search.search_semantic_file_memories",
        side_effect=RuntimeError("embedder offline"),
    ):
        results = await search_agent_memories("Keyword fallback", "general", storage_path, config, limit=5)

    assert any(result.get("memory") == "Keyword fallback memory" for result in results)


@pytest.mark.asyncio
async def test_file_backend_private_semantic_search_uses_requester_root(
    storage_path: Path,
    config: Config,
    build_private_template_dir: Callable[..., Path],
) -> None:
    template_dir = build_private_template_dir(
        files={"MEMORY.md": "# Memory\n", "memory/notes.md": "Alice private semantic note.\n"},
    )
    config.memory.backend = "file"
    config.memory.search.mode = "semantic"
    config.agents["general"].memory_backend = "file"
    config.agents["general"].private = AgentPrivateConfig(
        per="user",
        root="mind_data",
        template_dir=str(template_dir),
    )
    identity = ToolExecutionIdentity(
        channel="matrix",
        agent_name="general",
        requester_id="@alice:example.org",
        room_id="!room:example.org",
        thread_id=None,
        resolved_thread_id=None,
        session_id="session-alice",
    )

    with tool_execution_identity(identity), patch(
        "mindroom.memory._semantic_file_search.search_semantic_file_memories"
    ) as semantic_search:
        semantic_search.return_value = [
            {
                "id": "semantic:memory/notes.md:0",
                "memory": "Alice private semantic note.",
                "user_id": "agent_general",
                "score": 1.0,
                "metadata": {"source_file": "memory/notes.md", "semantic": True},
            }
        ]
        results = await search_agent_memories("private semantic", "general", storage_path, config, limit=5)

    assert results[0]["memory"] == "Alice private semantic note."
    resolution = semantic_search.call_args.kwargs["resolution"]
    assert "private_instances" in str(resolution.root)
    assert str(resolution.root).endswith("mind_data")


def test_semantic_memory_index_updates_changed_files_incrementally(tmp_path: Path) -> None:
    import json

    from mindroom.memory._semantic_file_search import _IndexedFile, _ensure_index_current

    class FakeVectorDb:
        collection_name = "memory_collection"

        def __init__(self) -> None:
            self.deleted = False
            self.created = False

        def exists(self) -> bool:
            return True

        def delete(self) -> None:
            self.deleted = True

        def create(self) -> None:
            self.created = True

    class FakeKnowledge:
        def __init__(self) -> None:
            self.vector_db = FakeVectorDb()
            self.removed: list[dict[str, str]] = []
            self.inserted: list[str] = []

        def remove_vectors_by_metadata(self, metadata: dict[str, str]) -> None:
            self.removed.append(metadata)

        def insert(self, *, path: str, metadata: dict[str, object], upsert: bool, reader: object) -> None:
            assert upsert is True
            self.inserted.append(path.rsplit("/", 1)[-1])

    memory_root = tmp_path / "memory-root"
    memory_root.mkdir()
    changed_file = memory_root / "memory" / "2026-06-02.md"
    changed_file.parent.mkdir()
    changed_file.write_text("changed", encoding="utf-8")
    current_file = _IndexedFile(
        path=changed_file,
        relative_path="memory/2026-06-02.md",
        mtime_ns=2,
        size=7,
        digest="new-digest",
    )
    index_path = tmp_path / "index"
    index_path.mkdir()
    (index_path / "index_state.json").write_text(
        json.dumps(
            {
                "settings_signature": "same-settings",
                "source_signature": "old-source",
                "collection": "memory_collection",
                "files": {
                    "memory/2026-06-02.md": {"mtime_ns": 1, "size": 3, "digest": "old-digest"},
                    "memory/deleted.md": {"mtime_ns": 1, "size": 3, "digest": "deleted-digest"},
                },
            }
        ),
        encoding="utf-8",
    )
    knowledge = FakeKnowledge()

    _ensure_index_current(
        knowledge,
        [current_file],
        index_path,
        "memory_collection",
        "same-settings",
        "new-source",
    )

    assert knowledge.vector_db.deleted is False
    assert knowledge.vector_db.created is False
    assert knowledge.removed == [
        {"source_path": "memory/2026-06-02.md"},
        {"source_path": "memory/deleted.md"},
    ]
    assert knowledge.inserted == ["2026-06-02.md"]
```

- [ ] **Step 2: Run failing semantic-search tests**

Run:

```bash
uv run pytest tests/test_memory_file_backend.py::test_file_backend_semantic_search_reads_daily_memory_root tests/test_memory_file_backend.py::test_file_backend_semantic_search_falls_back_to_keyword_on_index_error tests/test_memory_file_backend.py::test_file_backend_private_semantic_search_uses_requester_root tests/test_memory_file_backend.py::test_semantic_memory_index_updates_changed_files_incrementally -x -n 0 --no-cov -v
```

Expected: fail because semantic module and wiring do not exist.

- [ ] **Step 3: Add semantic module skeleton**

Create `src/mindroom/memory/_semantic_file_search.py`:

```python
"""Semantic search for file-backed memory roots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from agno.knowledge.knowledge import Knowledge
from agno.knowledge.reader import ReaderFactory
from agno.knowledge.reader.markdown_reader import MarkdownReader
from agno.knowledge.reader.text_reader import TextReader
from agno.vectordb.chroma import ChromaDb

from mindroom.config.memory import MemorySearchConfig
from mindroom.embedding_factory import create_configured_embedder
from mindroom.knowledge.chunking import SafeFixedSizeChunking
from mindroom.logging_config import get_logger
from mindroom.path_globs import matches_root_glob

if TYPE_CHECKING:
    from agno.knowledge.reader.base import Reader

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.memory._shared import FileMemoryResolution, MemoryResult

logger = get_logger(__name__)
_COLLECTION_PREFIX = "mindroom_memory"
_SOURCE_PATH_KEY = "source_path"
_SOURCE_MTIME_NS_KEY = "source_mtime_ns"
_SOURCE_SIZE_KEY = "source_size"
_SOURCE_DIGEST_KEY = "source_digest"
_CHUNK_SIZE = 5000
_CHUNK_OVERLAP = 0


@dataclass(frozen=True)
class _IndexedFile:
    path: Path
    relative_path: str
    mtime_ns: int
    size: int
    digest: str


def _safe_identifier(value: str) -> str:
    sanitized = "".join(char if char.isalnum() or char in {"_", "-"} else "_" for char in value)
    return sanitized or "default"


def _file_digest(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
```

- [ ] **Step 4: Add file listing and signatures**

Add to `src/mindroom/memory/_semantic_file_search.py`:

```python
def _path_is_symlink_or_under_symlink(root: Path, path: Path) -> bool:
    try:
        relative_path = path.relative_to(root)
    except ValueError:
        return True
    current = root
    for part in relative_path.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _include_relative_path(relative_path: str, search_config: MemorySearchConfig) -> bool:
    if relative_path == "MEMORY.md":
        return search_config.include_entrypoint
    return any(matches_root_glob(relative_path, pattern) for pattern in search_config.include)


def _list_indexed_files(root: Path, search_config: MemorySearchConfig) -> list[_IndexedFile]:
    if not root.is_dir():
        return []
    resolved_root = root.resolve()
    files: list[_IndexedFile] = []
    for dirpath, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        current_dir = Path(dirpath)
        dirnames[:] = [dirname for dirname in dirnames if not (current_dir / dirname).is_symlink()]
        for filename in filenames:
            path = current_dir / filename
            if path.suffix.lower() != ".md":
                continue
            if _path_is_symlink_or_under_symlink(resolved_root, path):
                continue
            try:
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(resolved_root)
                relative_path = resolved_path.relative_to(resolved_root).as_posix()
                if not _include_relative_path(relative_path, search_config):
                    continue
                stat = resolved_path.stat()
                files.append(
                    _IndexedFile(
                        path=resolved_path,
                        relative_path=relative_path,
                        mtime_ns=stat.st_mtime_ns,
                        size=stat.st_size,
                        digest=_file_digest(resolved_path),
                    )
                )
            except (OSError, ValueError):
                continue
    return sorted(files, key=lambda item: item.relative_path)
```

- [ ] **Step 5: Add index storage, metadata, and incremental state**

Add to `src/mindroom/memory/_semantic_file_search.py`:

```python
def _source_signature(files: list[_IndexedFile]) -> str:
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(file.mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(file.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file.digest.encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _settings_signature(config: Config, search_config: MemorySearchConfig, root: Path) -> str:
    embedder_config = config.memory.embedder.config
    payload = repr(
        (
            str(root.resolve()),
            config.memory.embedder.provider,
            embedder_config.model,
            embedder_config.host,
            embedder_config.dimensions,
            tuple(search_config.include),
            search_config.include_entrypoint,
            _CHUNK_SIZE,
            _CHUNK_OVERLAP,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _index_storage_path(runtime_paths: RuntimePaths, root: Path, scope_user_id: str) -> Path:
    digest = hashlib.sha256(f"{scope_user_id}:{root.resolve()}".encode("utf-8")).hexdigest()[:16]
    return runtime_paths.storage_root / "memory_search_db" / f"{_safe_identifier(scope_user_id)}_{digest}"


def _collection_name(root: Path, scope_user_id: str) -> str:
    digest = hashlib.sha256(f"{scope_user_id}:{root.resolve()}".encode("utf-8")).hexdigest()[:16]
    return f"{_COLLECTION_PREFIX}_{_safe_identifier(scope_user_id)}_{digest}"


def _state_path(index_path: Path) -> Path:
    return index_path / "index_state.json"
```

The implementation step should use JSON state with these exact keys:

```python
{
    "settings_signature": settings_signature,
    "source_signature": source_signature,
    "collection": collection_name,
    "files": {
        "memory/2026-06-02.md": {
            "mtime_ns": 1780000000000000000,
            "size": 123,
            "digest": "sha256hex",
        },
    },
}
```

Add these helpers:

```python
def _file_state(files: list[_IndexedFile]) -> dict[str, dict[str, int | str]]:
    return {
        file.relative_path: {
            "mtime_ns": file.mtime_ns,
            "size": file.size,
            "digest": file.digest,
        }
        for file in files
    }


def _load_state(index_path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(_state_path(index_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_state(
    index_path: Path,
    *,
    settings_signature: str,
    source_signature: str,
    collection_name: str,
    files: list[_IndexedFile],
) -> None:
    _state_path(index_path).write_text(
        json.dumps(
            {
                "settings_signature": settings_signature,
                "source_signature": source_signature,
                "collection": collection_name,
                "files": _file_state(files),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
```

- [ ] **Step 6: Add search function**

Add the public function to `src/mindroom/memory/_semantic_file_search.py`:

```python
async def search_semantic_file_memories(
    query: str,
    *,
    scope_user_id: str,
    resolution: FileMemoryResolution,
    config: Config,
    runtime_paths: RuntimePaths,
    search_config: MemorySearchConfig,
    limit: int,
) -> list[MemoryResult]:
    """Search one file-memory scope with an embedding-backed index."""
    root = resolution.root
    index_path = _index_storage_path(runtime_paths, root, scope_user_id)
    index_path.mkdir(parents=True, exist_ok=True)
    collection_name = _collection_name(root, scope_user_id)
    files = await asyncio.to_thread(_list_indexed_files, root, search_config)
    if not files:
        return []

    knowledge = Knowledge(
        vector_db=ChromaDb(
            collection=collection_name,
            path=str(index_path),
            persistent_client=True,
            embedder=create_configured_embedder(config, runtime_paths),
        )
    )
    await asyncio.to_thread(
        _ensure_index_current,
        knowledge,
        files,
        index_path,
        collection_name,
        _settings_signature(config, search_config, root),
        _source_signature(files),
    )
    documents = await asyncio.to_thread(knowledge.search, query=query, max_results=limit)
    results: list[MemoryResult] = []
    for rank, document in enumerate(documents, start=1):
        metadata = dict(getattr(document, "meta_data", {}) or {})
        source_file = metadata.get(_SOURCE_PATH_KEY)
        if not isinstance(source_file, str):
            source_file = "memory"
        content = " ".join(getattr(document, "content", "").split())
        if not content:
            continue
        score = getattr(document, "reranking_score", None)
        results.append(
            cast(
                "MemoryResult",
                {
                    "id": f"semantic:{source_file}:{rank}",
                    "memory": content,
                    "user_id": scope_user_id,
                    "score": float(score) if isinstance(score, (int, float)) else 1.0 - (rank * 0.000001),
                    "metadata": {"source_file": source_file, "semantic": True, "search_mode": "semantic"},
                },
            )
        )
    return results
```

Implement `_ensure_index_current` below it. It must read `_state_path(index_path)` and behave this way:

```text
settings_signature changed or collection missing -> reset collection and index every included file
settings_signature same -> compute removed/new/changed files from state["files"] and current file signatures
removed file -> remove vectors with {_SOURCE_PATH_KEY: relative_path}
changed file -> remove vectors with {_SOURCE_PATH_KEY: relative_path}, then insert current file
new file -> insert current file
unchanged file -> do nothing
```

Use `knowledge.remove_vectors_by_metadata({_SOURCE_PATH_KEY: relative_path})` for removals. Only write JSON state after all removals and inserts finish. This is the auto-flush mitigation: appending to today's `memory/*.md` reindexes that file only, not the entire memory corpus.

- [ ] **Step 7: Use conservative markdown chunking**

Inside `_ensure_index_current`, call this helper before `knowledge.insert`:

```python
def _build_reader(file_path: Path) -> Reader:
    reader = ReaderFactory.get_reader_for_extension(file_path.suffix.lower())
    if not isinstance(reader, (TextReader, MarkdownReader)):
        return reader
    configured_reader = deepcopy(reader)
    configured_reader.chunk = True
    configured_reader.chunk_size = _CHUNK_SIZE
    configured_reader.chunking_strategy = SafeFixedSizeChunking(
        chunk_size=_CHUNK_SIZE,
        overlap=_CHUNK_OVERLAP,
    )
    return configured_reader
```

Use metadata for each insert:

```python
metadata = {
    _SOURCE_PATH_KEY: indexed_file.relative_path,
    _SOURCE_MTIME_NS_KEY: indexed_file.mtime_ns,
    _SOURCE_SIZE_KEY: indexed_file.size,
    _SOURCE_DIGEST_KEY: indexed_file.digest,
}
knowledge.insert(
    path=str(indexed_file.path),
    metadata=metadata,
    upsert=True,
    reader=_build_reader(indexed_file.path),
)
```

- [ ] **Step 8: Wire file backend to semantic search**

In `src/mindroom/memory/_file_backend.py`, import the semantic function:

```python
from mindroom.memory._semantic_file_search import search_semantic_file_memories
```

Change `search_file_agent_memories` so the agent scope search becomes:

```python
    search_config = config.get_agent_memory_search(agent_name)
    if search_config.mode == "semantic":
        try:
            results = await search_semantic_file_memories(
                query,
                scope_user_id=agent_scope_user_id(agent_name),
                resolution=agent_resolution,
                config=config,
                runtime_paths=runtime_paths,
                search_config=search_config,
                limit=limit,
            )
        except Exception:
            logger.exception("File-memory semantic search failed; falling back to keyword search", agent=agent_name)
            results = _search_agent_file_scope_memories(
                query,
                agent_name,
                agent_resolution,
                config,
                limit,
                timing_scope,
            )
    else:
        results = _search_agent_file_scope_memories(
            query,
            agent_name,
            agent_resolution,
            config,
            limit,
            timing_scope,
        )
```

Because `search_semantic_file_memories` is async, convert `search_file_agent_memories` to `async def` and update `_search_file_backend_memories` in `src/mindroom/memory/functions.py` to `await search_file_agent_memories(...)`.

Keep the existing team-memory keyword loop after this block. Team semantic indexing is not part of this plan, but team memory must remain visible through existing keyword search.

Private requester memory also reaches this path through `resolve_file_memory_resolution(...)` with an active `ToolExecutionIdentity`. Do not add private-memory background prewarming or dashboard refresh in this PR; first search by that requester builds or updates that requester's index lazily.

- [ ] **Step 9: Run semantic wiring tests**

Run:

```bash
uv run pytest tests/test_memory_file_backend.py::test_file_backend_semantic_search_reads_daily_memory_root tests/test_memory_file_backend.py::test_file_backend_semantic_search_falls_back_to_keyword_on_index_error tests/test_memory_file_backend.py::test_file_backend_private_semantic_search_uses_requester_root tests/test_memory_file_backend.py::test_semantic_memory_index_updates_changed_files_incrementally -x -n 0 --no-cov -v
uv run ruff check src/mindroom/memory/_semantic_file_search.py src/mindroom/memory/_file_backend.py src/mindroom/memory/functions.py
```

Expected: pass.

- [ ] **Step 10: Commit**

```bash
git add src/mindroom/memory/_semantic_file_search.py src/mindroom/memory/_file_backend.py src/mindroom/memory/functions.py tests/test_memory_file_backend.py
git commit -m "feat: add semantic search for file memory"
```

## Task 5: Keep Public Tool Surface As `search_memories`

**Files:**
- Modify: `src/mindroom/custom_tools/memory.py`
- Modify: `tests/test_memory_tools.py`

- [ ] **Step 1: Write tool wording test**

Add these methods inside `class TestMemoryTools` in `tests/test_memory_tools.py`:

```python
def test_search_memories_tool_description_is_backend_neutral(self, tools: MemoryTools) -> None:
    function = tools.async_functions["search_memories"]

    assert "Search your memories" in function.description
    assert "knowledge base" not in function.description.lower()


@pytest.mark.asyncio
async def test_search_memories_marks_result_modes(self, tools: MemoryTools) -> None:
    mock_results = [
        {
            "id": "semantic:memory/notes.md:1",
            "memory": "Semantic memory result",
            "score": 0.9,
            "metadata": {"search_mode": "semantic"},
        },
        {
            "id": "m_keyword",
            "memory": "Keyword team memory result",
            "score": 0.5,
            "metadata": {"search_mode": "keyword"},
        },
    ]

    with patch(
        "mindroom.custom_tools.memory.search_agent_memories",
        new_callable=AsyncMock,
        return_value=mock_results,
    ):
        result = await tools.search_memories("memory", limit=2)

    assert "[semantic]" in result
    assert "[keyword]" in result
```

- [ ] **Step 2: Run tool wording test**

Run:

```bash
uv run pytest tests/test_memory_tools.py::TestMemoryTools::test_search_memories tests/test_memory_tools.py::TestMemoryTools::test_search_memories_tool_description_is_backend_neutral tests/test_memory_tools.py::TestMemoryTools::test_search_memories_marks_result_modes -x -n 0 --no-cov -v
```

Expected: pass or fail only on wording.

- [ ] **Step 3: Tighten tool docstring if needed**

Keep the tool name `search_memories`. Do not add `semantic_search` or a second memory-search tool. Use this docstring:

```python
    async def search_memories(self, query: str, limit: int = 5) -> str:
        """Search your memories for information relevant to a query.

        Use this when you need to recall previously stored facts, decisions,
        preferences, or file-backed memory notes. The configured memory backend
        decides whether retrieval is vector-backed or keyword-based.
        """
```

- [ ] **Step 4: Make result modes legible**

Update result formatting so mode is legible when mixed semantic and keyword results are returned:

```python
            lines = [f"Found {len(results)} memory(ies):"]
            for i, mem in enumerate(results, 1):
                mid = mem.get("id", "?")
                metadata = mem.get("metadata")
                search_mode = metadata.get("search_mode") if isinstance(metadata, dict) else None
                mode_label = f" [{search_mode}]" if search_mode in {"semantic", "keyword"} else ""
                lines.append(f"{i}. [id={mid}]{mode_label} {mem.get('memory', '')}")
```

In `src/mindroom/memory/_file_backend.py`, add `metadata["search_mode"] = "keyword"` to keyword search results. In `src/mindroom/memory/_semantic_file_search.py`, add `metadata["search_mode"] = "semantic"` to semantic results.

- [ ] **Step 5: Run tool tests**

Run:

```bash
uv run pytest tests/test_memory_tools.py -x -n 0 --no-cov -v
uv run ruff check src/mindroom/custom_tools/memory.py tests/test_memory_tools.py
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/custom_tools/memory.py tests/test_memory_tools.py
git commit -m "docs: keep memory search tool backend neutral"
```

## Task 6: Remove Shared File-Memory Knowledge-Base Fan-Out From Current PR

**Files:**
- Modify: `src/mindroom/runtime_resolution.py`
- Modify: `src/mindroom/knowledge/registry.py`
- Modify: `src/mindroom/knowledge/status.py`
- Modify: `src/mindroom/knowledge/refresh_scheduler.py`
- Modify: `src/mindroom/knowledge/watch.py`
- Modify: `src/mindroom/knowledge/refresh_runner.py`
- Modify: `src/mindroom/api/knowledge.py`
- Modify: `tests/test_knowledge_manager.py`

- [ ] **Step 1: Identify tests that now represent the rejected abstraction**

In `tests/test_knowledge_manager.py`, remove tests whose assertion is that `KnowledgeBaseConfig(path="./memory")` binds to an agent workspace because an assigned agent has `memory_backend: file`.

Remove these current-PR tests:

```text
test_shared_relative_knowledge_base_uses_requesting_agent_workspace
_daily_memory_multi_owner_config
test_identityless_resolve_fails_closed_for_multi_owner_file_memory_base_without_stem
test_central_bindings_include_file_memory_and_shared_backends
test_dashboard_upload_fails_closed_for_mixed_owner_roots
test_dashboard_root_fails_closed_for_multi_owner_file_memory_bindings
test_dashboard_reindex_fails_closed_for_multi_owner_file_memory_bindings
test_dashboard_reindex_succeeds_for_single_owner_file_memory_binding
test_dashboard_root_uses_central_file_memory_binding
test_mode_transition_reconcile_fans_out_multi_owner_file_memory_base
test_identityless_refresh_resolves_single_file_memory_owner_workspace
test_identityless_background_refresh_fans_out_shared_file_memory_base
test_identityless_direct_refresh_paths_fan_out_nonstem_file_memory_base
test_identityless_refreshing_status_aggregates_multi_owner_file_memory_base
test_identityless_background_skips_private_file_memory_owner_without_config_fallback
test_shared_file_memory_path_accepts_literal_dollar_in_path
test_file_memory_agent_without_configured_file_path_keeps_shared_base_config_relative
test_file_memory_knowledge_search_uses_requesting_agent_workspace
```

Keep tests for ordinary shared knowledge paths and private knowledge paths.

- [ ] **Step 2: Restore shared knowledge resolution semantics**

In `src/mindroom/runtime_resolution.py`, remove these current-PR concepts:

```python
ResolvedKnowledgeOwnerBinding
_shared_knowledge_path_uses_agent_file_memory
_resolve_agent_file_memory_knowledge_path_from_runtime
_resolve_agent_file_memory_knowledge_binding
_background_refresh_identity
_file_memory_owner_binding
_dedupe_knowledge_owner_bindings
_shared_knowledge_base_agent_names
resolve_knowledge_owner_bindings
```

Keep `resolve_knowledge_binding` behavior:

```python
def resolve_knowledge_binding(
    base_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    execution_identity: ToolExecutionIdentity | None = None,
    *,
    start_watchers: bool = True,
    create: bool = False,
) -> ResolvedKnowledgeBinding:
    base_config = config.get_knowledge_base_config(base_id)
    effective_agent_name = resolve_private_knowledge_base_agent(...)
    if effective_agent_name is not None:
        return _private_knowledge_binding(...)
    knowledge_path = resolve_config_relative_path(base_config.path, runtime_paths).resolve()
    return ResolvedKnowledgeBinding(
        base_id=base_id,
        storage_root=runtime_paths.storage_root.expanduser().resolve(),
        knowledge_path=knowledge_path,
        incremental_sync_on_access=base_config.watch and base_config.git is None and not start_watchers,
    )
```

This means a shared knowledge base with `path: ./memory` resolves to `<config_dir>/memory` again. That is correct because memory recall no longer uses `knowledge_bases`.

- [ ] **Step 3: Simplify knowledge registry/status/scheduler/watch**

In `src/mindroom/knowledge/registry.py`, make `resolve_published_index_bindings` return one binding from `resolve_knowledge_binding`:

```python
def resolve_published_index_bindings(...) -> tuple[_ResolvedPublishedIndexBinding, ...]:
    key, binding = _resolve_published_index_key_and_binding(...)
    return (
        _ResolvedPublishedIndexBinding(
            key=key,
            binding=binding,
            owner_agent=None,
            execution_identity=execution_identity,
        ),
    )
```

Then remove multi-owner fan-out branches in:

```text
src/mindroom/knowledge/status.py
src/mindroom/knowledge/refresh_scheduler.py
src/mindroom/knowledge/watch.py
src/mindroom/knowledge/refresh_runner.py
src/mindroom/api/knowledge.py
```

The remaining code should schedule, refresh, status-check, and dashboard-manage one resolved knowledge root per base ID.

- [ ] **Step 4: Keep one regression test for the new boundary**

Add this test to `tests/test_knowledge_manager.py`:

```python
def test_shared_knowledge_path_named_memory_stays_config_relative(tmp_path: Path) -> None:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "mindroom_data",
        process_env={"MATRIX_HOMESERVER": "http://localhost:8008"},
    )
    config = bind_runtime_paths(
        Config(
            agents={
                "openclaw": AgentConfig(
                    display_name="OpenClaw",
                    memory_backend="file",
                    knowledge_bases=["daily_memory"],
                ),
            },
            knowledge_bases={"daily_memory": KnowledgeBaseConfig(path="./memory")},
        ),
        runtime_paths,
    )

    binding = resolve_knowledge_binding("daily_memory", config, runtime_paths)

    assert binding.knowledge_path == runtime_paths.config_dir / "memory"
```

- [ ] **Step 5: Run knowledge regression tests**

Run:

```bash
uv run pytest tests/test_knowledge_manager.py -k "shared_knowledge_path_named_memory_stays_config_relative or private_knowledge or refresh_scheduler" -x -n 0 --no-cov -v
uv run ruff check src/mindroom/runtime_resolution.py src/mindroom/knowledge/registry.py src/mindroom/knowledge/status.py src/mindroom/knowledge/refresh_scheduler.py src/mindroom/knowledge/watch.py src/mindroom/knowledge/refresh_runner.py src/mindroom/api/knowledge.py tests/test_knowledge_manager.py
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add src/mindroom/runtime_resolution.py src/mindroom/knowledge/registry.py src/mindroom/knowledge/status.py src/mindroom/knowledge/refresh_scheduler.py src/mindroom/knowledge/watch.py src/mindroom/knowledge/refresh_runner.py src/mindroom/api/knowledge.py tests/test_knowledge_manager.py
git commit -m "refactor: stop modeling file memory as knowledge"
```

## Task 7: Update Starter Configs, Docs, And Local Config

**Files:**
- Modify: `src/mindroom/cli/config.py`
- Modify: `tests/test_cli_config.py`
- Modify: `config.yaml`
- Modify: `docs/openclaw.md`
- Modify: `docs/memory.md`
- Modify: `docs/tools/memory-and-storage.md`
- Modify: `docs/knowledge.md`
- Modify: `docs/configuration/agents.md`

- [ ] **Step 1: Write failing CLI config test**

Update `tests/test_cli_config.py::test_init_adds_mindroom_style_mind` assertions:

```python
assert "knowledge_bases" not in mind
assert "mind_memory" not in config.get("knowledge_bases", {})
assert "memory" in mind["tools"]
assert config["memory"]["search"]["mode"] == "semantic"
assert config["memory"]["search"]["include"] == ["memory/**/*.md"]
assert config["memory"]["search"]["include_entrypoint"] is False
assert "search_memories" in "\n".join(mind["instructions"])
assert "search_knowledge_base" not in "\n".join(mind["instructions"])
```

- [ ] **Step 2: Run failing CLI config test**

Run:

```bash
uv run pytest tests/test_cli_config.py::TestConfigInit::test_init_adds_mindroom_style_mind -x -n 0 --no-cov -v
```

Expected: fail because generated config still uses `mind_memory`.

- [ ] **Step 3: Update generated config**

In `src/mindroom/cli/config.py`, remove this generated block from the `mind` agent:

```yaml
    knowledge_bases:
      - mind_memory
```

Add memory tool:

```yaml
      - memory
```

Change the prior-history instruction to:

```yaml
      - Before answering prior-history questions, use search_memories to search file-backed memory notes.
```

Add global memory search config:

```yaml
memory:
  backend: file
  search:
    mode: semantic
    include:
      - memory/**/*.md
    include_entrypoint: false
  file:
    max_entrypoint_lines: 200
  auto_flush:
    enabled: true
```

Remove the generated `knowledge_bases.mind_memory` block.

- [ ] **Step 4: Update `config.yaml` OpenClaw section**

In `config.yaml`, update OpenClaw instructions:

```yaml
    - >-
      Before answering questions about prior conversations, decisions, people,
      or preferences, use search_memories to search memory files. Do not guess
      from incomplete context; search first.
```

Remove:

```yaml
    knowledge_bases:
    - openclaw_memory
```

Add tool:

```yaml
    - memory
```

Remove:

```yaml
knowledge_bases:
  openclaw_memory:
    path: ./openclaw_data/memory
    watch: true
```

Add:

```yaml
memory:
  search:
    mode: semantic
    include:
    - memory/**/*.md
    include_entrypoint: false
```

Keep existing memory embedder, LLM, and file settings.

- [ ] **Step 5: Update docs**

In `docs/openclaw.md`, replace the drop-in config knowledge section with:

```yaml
memory:
  backend: file
  search:
    mode: semantic
    include:
      - memory/**/*.md
    include_entrypoint: false
  file:
    max_entrypoint_lines: 200
  auto_flush:
    enabled: true
```

Change all OpenClaw guidance from `search_knowledge_base` to `search_memories`.

In `docs/memory.md`, add a "Semantic File-Memory Search" section:

```markdown
## Semantic File-Memory Search

File-backed memory can search daily memory notes with embeddings through the existing `search_memories` tool.
Configure global defaults under `memory.search`.
Each agent still gets its own index rooted at that agent's effective file-memory root.
Requester-private agents get requester-scoped indexes rooted at the private file-memory root.
Private requester indexes are built lazily on the first `search_memories` call for that requester.
They are not prewarmed by background refresh or dashboard operations because those paths do not carry requester identity.
Indexes update incrementally by file signature, so editing or auto-flushing one daily memory file reindexes that file without rebuilding the whole memory corpus.

```yaml
memory:
  backend: file
  search:
    mode: semantic
    include:
      - memory/**/*.md
    include_entrypoint: false
```

`include` patterns are relative to the effective file-memory root.
`MEMORY.md` is not indexed by default because it is already preloaded into the prompt by `memory.file.max_entrypoint_lines`.
Set `include_entrypoint: true` when `MEMORY.md` should also be retrievable by semantic search.
```

In `docs/knowledge.md`, add one short warning:

```markdown
Do not configure agent memory folders as `knowledge_bases`.
Use `memory.search` and the `search_memories` tool for file-backed memory recall.
```

Add one migration note to `docs/memory.md`:

```markdown
If an older config used a memory folder as `knowledge_bases.mind_memory` or `knowledge_bases.openclaw_memory`, remove that knowledge base and add the `memory` tool.
Old Chroma collections under `mindroom_data/knowledge_db/` are no longer read by memory search after this migration.
They can be deleted manually after verifying the new memory search works.
```

- [ ] **Step 6: Run CLI/docs tests**

Run:

```bash
uv run pytest tests/test_cli_config.py::TestConfigInit::test_init_adds_mindroom_style_mind -x -n 0 --no-cov -v
uv run ruff check src/mindroom/cli/config.py tests/test_cli_config.py
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add src/mindroom/cli/config.py tests/test_cli_config.py config.yaml docs/openclaw.md docs/memory.md docs/tools/memory-and-storage.md docs/knowledge.md docs/configuration/agents.md
git commit -m "docs: configure memory semantic search"
```

## Task 8: Add Dashboard Fields

**Files:**
- Modify: `frontend/src/types/config.ts`
- Modify: `frontend/src/components/MemoryConfig/MemoryConfig.tsx`
- Modify: `frontend/src/components/AgentEditor/AgentEditor.tsx`
- Test: `frontend/src/components/MemoryConfig/MemoryConfig.test.tsx`
- Test: `frontend/src/components/AgentEditor/AgentEditor.test.tsx`

- [ ] **Step 1: Add frontend types**

In `frontend/src/types/config.ts`, add:

```ts
export type MemorySearchMode = "keyword" | "semantic";

export interface MemorySearchConfig {
  mode?: MemorySearchMode;
  include?: string[];
  include_entrypoint?: boolean;
}

export interface AgentMemorySearchConfig {
  mode?: MemorySearchMode;
  include?: string[];
  include_entrypoint?: boolean;
}
```

Add `search?: MemorySearchConfig;` to `MemoryConfig`.

Add `memory_search?: AgentMemorySearchConfig | null;` to `Agent`.

- [ ] **Step 2: Add Memory page controls**

In `frontend/src/components/MemoryConfig/MemoryConfig.tsx`, add controls under Memory Backend:

```tsx
<FieldGroup
  label="Memory Search"
  helperText="Choose keyword or semantic retrieval for file-backed memory."
  htmlFor="memory-search-mode"
>
  <Select
    value={localConfig.search?.mode ?? "keyword"}
    onValueChange={(value) =>
      applyMemoryConfig({
        ...localConfig,
        search: {
          mode: value as MemorySearchMode,
          include: localConfig.search?.include ?? ["memory/**/*.md"],
          include_entrypoint: localConfig.search?.include_entrypoint ?? false,
        },
      })
    }
  >
    <SelectTrigger id="memory-search-mode">
      <SelectValue />
    </SelectTrigger>
    <SelectContent>
      <SelectItem value="keyword">Keyword</SelectItem>
      <SelectItem value="semantic">Semantic</SelectItem>
    </SelectContent>
  </Select>
</FieldGroup>
```

Add include-entrypoint select or checkbox:

```tsx
<FieldGroup
  label="Index MEMORY.md"
  helperText="Include the prompt-preloaded MEMORY.md entrypoint in semantic memory search."
  htmlFor="memory-search-entrypoint"
>
  <Select
    value={String(localConfig.search?.include_entrypoint ?? false)}
    onValueChange={(value) =>
      applyMemoryConfig({
        ...localConfig,
        search: {
          mode: localConfig.search?.mode ?? "keyword",
          include: localConfig.search?.include ?? ["memory/**/*.md"],
          include_entrypoint: value === "true",
        },
      })
    }
  >
    <SelectTrigger id="memory-search-entrypoint">
      <SelectValue />
    </SelectTrigger>
    <SelectContent>
      <SelectItem value="false">Excluded</SelectItem>
      <SelectItem value="true">Included</SelectItem>
    </SelectContent>
  </Select>
</FieldGroup>
```

- [ ] **Step 3: Add per-agent advanced override**

In `frontend/src/components/AgentEditor/AgentEditor.tsx`, add a compact section below Memory Backend. Keep defaults inherited and show only mode and entrypoint first:

```tsx
<FieldGroup
  label="Memory Search"
  helperText="Inherit global memory search or override for this agent."
  htmlFor="memory_search_mode"
>
  <Controller
    name="memory_search.mode"
    control={control}
    render={({ field }) => (
      <Select
        value={field.value ?? "inherit"}
        onValueChange={(value) => {
          const resolved = value === "inherit" ? undefined : (value as MemorySearchMode);
          field.onChange(resolved);
          handleFieldChange("memory_search", {
            ...(agent.memory_search ?? {}),
            mode: resolved,
          });
        }}
      >
        <SelectTrigger id="memory_search_mode">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="inherit">Inherit global</SelectItem>
          <SelectItem value="keyword">Keyword</SelectItem>
          <SelectItem value="semantic">Semantic</SelectItem>
        </SelectContent>
      </Select>
    )}
  />
</FieldGroup>
```

- [ ] **Step 4: Add frontend tests**

In `frontend/src/components/MemoryConfig/MemoryConfig.test.tsx`, add:

```tsx
it("updates memory search mode", async () => {
  render(<MemoryConfig />);

  const modeSelect = screen.getByLabelText("Memory Search");
  await userEvent.click(modeSelect);
  await userEvent.click(screen.getByText("Semantic"));

  expect(useConfigStore.getState().config.memory.search?.mode).toBe("semantic");
});
```

In `frontend/src/components/AgentEditor/AgentEditor.test.tsx`, add:

```tsx
it("updates per-agent memory search mode override", async () => {
  renderAgentEditor({ agents: [{ ...mockAgent, memory_search: undefined }] });

  const modeSelect = screen.getByLabelText("Memory Search");
  await userEvent.click(modeSelect);
  await userEvent.click(screen.getByText("Semantic"));

  expect(handleUpdateAgent).toHaveBeenCalledWith(
    expect.objectContaining({
      memory_search: expect.objectContaining({ mode: "semantic" }),
    }),
  );
});
```

- [ ] **Step 5: Run frontend tests**

Run:

```bash
cd frontend
bun test src/components/MemoryConfig/MemoryConfig.test.tsx src/components/AgentEditor/AgentEditor.test.tsx
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/types/config.ts frontend/src/components/MemoryConfig/MemoryConfig.tsx frontend/src/components/AgentEditor/AgentEditor.tsx frontend/src/components/MemoryConfig/MemoryConfig.test.tsx frontend/src/components/AgentEditor/AgentEditor.test.tsx
git commit -m "feat: expose memory search config in dashboard"
```

## Task 9: Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
uv run pytest tests/test_memory_config.py tests/test_memory_file_backend.py tests/test_memory_tools.py tests/test_cli_config.py::TestConfigInit::test_init_adds_mindroom_style_mind tests/test_knowledge_manager.py -k "memory_search or semantic_search or shared_knowledge_path_named_memory_stays_config_relative or private_knowledge or refresh_scheduler" -x -n 0 --no-cov -v
```

Expected: pass.

- [ ] **Step 2: Run lint on touched backend files**

Run:

```bash
uv run ruff check src/mindroom/config/memory.py src/mindroom/config/agent.py src/mindroom/config/main.py src/mindroom/path_globs.py src/mindroom/embedding_factory.py src/mindroom/memory/_semantic_file_search.py src/mindroom/memory/_file_backend.py src/mindroom/memory/functions.py src/mindroom/runtime_resolution.py src/mindroom/knowledge/manager.py src/mindroom/knowledge/registry.py src/mindroom/knowledge/status.py src/mindroom/knowledge/refresh_scheduler.py src/mindroom/knowledge/watch.py src/mindroom/knowledge/refresh_runner.py src/mindroom/api/knowledge.py src/mindroom/cli/config.py tests/test_memory_config.py tests/test_memory_file_backend.py tests/test_memory_tools.py tests/test_knowledge_manager.py tests/test_cli_config.py
```

Expected: pass.

- [ ] **Step 3: Run frontend targeted tests**

Run:

```bash
cd frontend
bun test src/components/MemoryConfig/MemoryConfig.test.tsx src/components/AgentEditor/AgentEditor.test.tsx
```

Expected: pass.

- [ ] **Step 4: Inspect diff for deleted bad abstraction**

Run:

```bash
rg -n "openclaw_memory|mind_memory|search_knowledge_base.*memory|_shared_knowledge_path_uses_agent_file_memory|file-memory knowledge" config.yaml docs src tests
```

Expected: no `openclaw_memory`, no `mind_memory`, no memory instructions pointing at `search_knowledge_base`, no `_shared_knowledge_path_uses_agent_file_memory`. References to private knowledge safety docs may remain if they are not telling users to configure memory as a knowledge base.

- [ ] **Step 5: Run full pre-commit if local env is ready**

Run:

```bash
uv run pre-commit run --all-files
```

Expected: pass.

- [ ] **Step 6: Commit final cleanup if verification changed files**

```bash
git status --short
git add src/mindroom/config/memory.py src/mindroom/config/agent.py src/mindroom/config/main.py src/mindroom/path_globs.py src/mindroom/embedding_factory.py src/mindroom/memory/_semantic_file_search.py src/mindroom/memory/_file_backend.py src/mindroom/memory/functions.py src/mindroom/custom_tools/memory.py src/mindroom/runtime_resolution.py src/mindroom/knowledge/manager.py src/mindroom/knowledge/registry.py src/mindroom/knowledge/status.py src/mindroom/knowledge/refresh_scheduler.py src/mindroom/knowledge/watch.py src/mindroom/knowledge/refresh_runner.py src/mindroom/api/knowledge.py src/mindroom/cli/config.py frontend/src/types/config.ts frontend/src/components/MemoryConfig/MemoryConfig.tsx frontend/src/components/AgentEditor/AgentEditor.tsx tests/test_memory_config.py tests/test_memory_file_backend.py tests/test_memory_tools.py tests/test_knowledge_manager.py tests/test_cli_config.py frontend/src/components/MemoryConfig/MemoryConfig.test.tsx frontend/src/components/AgentEditor/AgentEditor.test.tsx config.yaml docs/openclaw.md docs/memory.md docs/tools/memory-and-storage.md docs/knowledge.md docs/configuration/agents.md
git commit -m "chore: verify memory semantic search"
```

Use exact file paths in `git add`. Do not use `git add .`.

## Self-Review

- Spec coverage: plan covers config, per-agent inheritance, semantic file-memory index, `search_memories` tool surface, removal of memory-as-KB fan-out, OpenClaw/Mind config migration, docs, dashboard, and tests.
- Marker scan: no unresolved implementation marker or undefined implementation-only task remains.
- Type consistency: public config names are `memory.search` and `agents.<name>.memory_search`; tool name remains `search_memories`; memory result shape matches existing `MemoryResult` dictionary usage.
