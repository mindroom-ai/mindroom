---
icon: lucide/book-open
---

# Knowledge Bases

Knowledge bases give your agents access to your own documents through RAG (Retrieval-Augmented Generation).
Drop files into a folder, point a knowledge base at it, and agents can search the indexed content when answering questions.

## How It Works

1. You configure a knowledge base pointing to a folder of documents
2. MindRoom indexes the files into a vector database (ChromaDB) using an embedder
3. Agents assigned to that knowledge base get a search tool that queries the indexed documents
4. When the agent uses the tool, relevant document chunks are included in its context

```
Indexing (scheduled refresh):

  ┌──────────────┐      ┌──────────┐      ┌──────────┐
  │ Files/Folder │ ───▶ │ Embedder │ ───▶ │ ChromaDB │
  └──────────────┘      └──────────┘      └──────────┘
         ▲
         │ on-access/API refresh
         │ git sync during refresh

Querying (agentic RAG):

  ┌───────┐  search   ┌──────────┐
  │ Agent │ ────────▶ │ ChromaDB │
  │       │ ◀──────── │          │
  └───────┘  chunks   └──────────┘
```

## Quick Start

Add a knowledge base and assign it to an agent:

```yaml
knowledge_bases:
  docs:
    path: ./knowledge_docs
    watch: false
    chunk_size: 5000
    chunk_overlap: 0

agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant with access to our docs
    knowledge_bases: [docs]
```

Place files in `./knowledge_docs/`, then trigger a reindex from the dashboard/API or let MindRoom watch shared local bases with `watch: true`.
Chat uses the last successfully published snapshot and continues without blocking when a base is missing, stale, or failed.
When a watched file changes, MindRoom marks the snapshot stale, refreshes in the background, and atomically publishes the replacement when it succeeds.
When `watch: false`, direct external file edits require explicit reindex, while dashboard/API upload and delete actions still schedule refresh after a successful mutation.
Knowledge base IDs are the keys under `knowledge_bases`.
Use a non-empty single path component such as `docs` or `company_docs`, not `""`, `.`, `..`, or names containing `/` or `\`.

## Configuration

### Basic Knowledge Base

```yaml
knowledge_bases:
  my_docs:
    path: ./knowledge_docs/my_docs   # Folder containing documents
    watch: false                      # Direct external edits require reindex; API mutations still schedule refresh
    chunk_size: 5000                  # Max characters per chunk
    chunk_overlap: 0                  # Overlap between adjacent chunks
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | string | `./knowledge_docs` | Folder path (relative to the config file directory or absolute) |
| `watch` | bool | `true` | When true, shared local folders watch filesystem changes and schedule background snapshot refresh without blocking reads. When false, direct external edits require explicit reindex; dashboard/API upload and delete actions still schedule refresh |
| `chunk_size` | int | `5000` | Maximum characters per chunk for text-like files (minimum: `128`) |
| `chunk_overlap` | int | `0` | Overlap characters between adjacent chunks (must be `< chunk_size`) |
| `git` | object | `null` | Optional Git repository sync settings |

Use smaller `chunk_size` values when your embedding server has lower token or batch limits.
If chunking is too large, indexing retries will fail with embedder 500 errors.

### Private Agent Knowledge

Use `agents.<name>.private.knowledge` when one shared agent definition should index requester-local knowledge from that requester's private root.

```yaml
knowledge_bases:
  company_docs:
    path: ./company_docs
    watch: false

agents:
  mind:
    display_name: Mind
    role: A persistent personal AI companion
    model: sonnet
    private:
      per: user
      root: mind_data
      template_dir: ./mind_template
      knowledge:
        path: memory
        watch: false
    knowledge_bases: [company_docs]
```

With this configuration, each requester's private knowledge path becomes `<their private root>/memory`.
The template source is explicit, so you can see and edit the files being copied into each requester's private root.
`private.template_dir` only copies files.
Requester-local knowledge is enabled only when you explicitly configure `private.knowledge.path`.
`private.knowledge.path` must be relative to the private root and cannot be absolute or escape with `..`.
`private.knowledge.path` can point to any folder inside the private root, including `.` for the private root itself.
MindRoom keeps a separate index per effective private root, so one requester's indexed data is not shared with another requester's runtime.
For isolating scopes such as `user` and `user_agent`, MindRoom refreshes the private index on access instead of keeping a background watcher alive for every requester root.
Git-backed knowledge syncs during scheduled or explicit refreshes.
Top-level `knowledge_bases` remain the shared/global mechanism, so the same agent can combine private local knowledge with shared company knowledge.
This requester-local private knowledge flow applies to the normal agent runtime path, not the OpenAI-compatible `/v1` API.
If you enable `private.knowledge.git`, use a dedicated subtree such as `kb_repo`.
Do not point Git-backed private knowledge at `.` or `memory/`, and do not use a Git checkout path that your template or private file memory also writes into.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `private.knowledge.enabled` | bool | `true` | Whether requester-local knowledge indexing is active for this agent |
| `private.knowledge.path` | string | `null` | Private-root-relative folder to index. Required when `private.knowledge.enabled` is `true`; set `enabled: false` to disable private knowledge |
| `private.knowledge.watch` | bool | `true` | When true, requester-local private knowledge schedules background refresh on access. When false, direct external edits require explicit refresh |
| `private.knowledge.chunk_size` | int | `5000` | Maximum characters per indexed chunk |
| `private.knowledge.chunk_overlap` | int | `0` | Overlap characters between adjacent chunks. Must be smaller than `chunk_size` |
| `private.knowledge.git` | object | `null` | Optional Git sync configuration for requester-local knowledge. Git-backed private knowledge must use a dedicated subtree outside requester-writable memory/template content |

Use `private.knowledge` when the data itself should be private to that requester's private instance.
Use top-level `knowledge_bases` when the same documents should stay shared across agents or users.

### Multiple Knowledge Bases

You can define multiple knowledge bases and assign them to different agents:

```yaml
knowledge_bases:
  engineering:
    path: ./knowledge_docs/engineering
    watch: false
    chunk_size: 5000
    chunk_overlap: 0
  product:
    path: ./knowledge_docs/product
    watch: false
    chunk_size: 5000
    chunk_overlap: 0
  legal:
    path: ./knowledge_docs/legal
    watch: false
    chunk_size: 1000
    chunk_overlap: 100

agents:
  developer:
    display_name: Developer
    role: Engineering assistant
    knowledge_bases: [engineering]

  pm:
    display_name: Product Manager
    role: Product planning assistant
    knowledge_bases: [product, engineering]  # Can access multiple bases

  compliance:
    display_name: Compliance
    role: Legal and compliance reviewer
    knowledge_bases: [legal]
```

When an agent has multiple knowledge bases, results are interleaved fairly so no single base dominates the top results.

## Git-Backed Knowledge Bases

Knowledge bases can sync from a Git repository.
MindRoom clones or fetches the repo during scheduled or explicit refreshes, not during global startup.

```yaml
knowledge_bases:
  pipefunc_docs:
    path: ./knowledge_docs/pipefunc
    watch: false
    chunk_size: 1200
    chunk_overlap: 120
    git:
      repo_url: https://github.com/pipefunc/pipefunc
      branch: main
      poll_interval_seconds: 300
      lfs: false
      skip_hidden: true
      include_patterns:
        - "docs/**"
```

### Git Configuration Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `repo_url` | string | *required* | HTTPS repository URL to clone/fetch |
| `branch` | string | `main` | Branch to track |
| `poll_interval_seconds` | int | `300` | Minimum age before a READY Git snapshot schedules advisory on-access refresh. Git polling loops are not started |
| `credentials_service` | string | `null` | Service name in CredentialsManager for private repos |
| `lfs` | bool | `false` | Enable Git LFS support and run `git lfs pull` after sync. Requires `git-lfs` on the machine running MindRoom |
| `sync_timeout_seconds` | int | `3600` | Abort one Git command if it exceeds this timeout |
| `skip_hidden` | bool | `true` | Skip files/folders starting with `.` |
| `include_patterns` | list | `[]` | Root-anchored glob patterns to include |
| `exclude_patterns` | list | `[]` | Root-anchored glob patterns to exclude |

When `lfs: true`, install `git-lfs` on the runtime host for `uv run` or `uvx` flows.
Bundled container images already include it.

### Sync Behavior

- Chat and runtime requests never wait for Git sync or indexing.
- Missing, stale, or failed knowledge schedules a per-binding refresh and the current request continues with availability metadata.
- Explicit dashboard/API reindex runs Git sync first for Git-backed bases and then rebuilds a candidate index.
- When `lfs: true`, MindRoom runs `git lfs pull origin <branch>` when a checkout is first hydrated or when sync advances to a new Git head.
- Local edits to Git-tracked files are discarded during refresh sync, and tracked deletions are restored from the remote checkout.
- Git-backed bases reject dashboard/API file upload and delete mutations; update the repository and reindex instead.
- Successful refresh publishes a new last-good snapshot while failed refresh preserves the previous one and records the error in status metadata.

### File Filtering with Patterns

Patterns are matched from the repository root. `*` matches one path segment, `**` matches zero or more segments.

```yaml
knowledge_bases:
  project_docs:
    path: ./knowledge_docs/project
    git:
      repo_url: https://github.com/org/project
      include_patterns:
        - "docs/**"                    # All files under docs/
        - "README.md"                  # Root README only
        - "content/posts/*/index.md"   # Specific nested files
      exclude_patterns:
        - "docs/internal/**"           # Exclude internal docs
```

- If `include_patterns` is empty, all non-hidden files are eligible
- If `include_patterns` is set, a file must match at least one pattern
- `exclude_patterns` are applied last and remove matching files

Multiple knowledge bases may point at the same root when they use the same source ownership settings.
This is the preferred way to expose separate views of a large repository without cloning it more than once.

```yaml
knowledge_bases:
  project_docs:
    path: ./knowledge_docs/project
    git:
      repo_url: https://github.com/org/project
      branch: main
      include_patterns: ["docs/**"]
  project_source:
    path: ./knowledge_docs/project
    git:
      repo_url: https://github.com/org/project
      branch: main
      include_patterns: ["src/**"]
```

### Private Repository Authentication

For private HTTPS repositories, store credentials and reference them in the config.

**Step 1:** Store credentials via the API or Dashboard (Credentials tab):

```bash
curl -X POST http://localhost:8765/api/credentials/github_private \
  -H "Content-Type: application/json" \
  -d '{"credentials":{"username":"x-access-token","token":"ghp_your_token_here"}}'
```

**Step 2:** Reference the service name in your knowledge base config:

```yaml
knowledge_bases:
  private_docs:
    path: ./knowledge_docs/private
    git:
      repo_url: https://github.com/org/private-repo
      credentials_service: github_private
```

Accepted credential fields:

| Fields | Notes |
|--------|-------|
| `username` + `token` | Standard GitHub/GitLab access token auth |
| `username` + `password` | Basic HTTP auth |
| `api_key` | Uses `x-access-token` as username automatically |

## Embedder Configuration

Knowledge bases use the same embedder configured in the `memory` section:

```yaml
memory:
  embedder:
    provider: openai        # or "ollama", "huggingface", or "sentence_transformers"
    config:
      model: text-embedding-3-small
      host: null             # For self-hosted (Ollama)
      dimensions: null       # Optional: embedding dimension override (e.g., 256)
```

| Provider | Model Example | Notes |
|----------|---------------|-------|
| `openai` | `text-embedding-3-small` | Requires `OPENAI_API_KEY` |
| `ollama` | `nomic-embed-text` | Self-hosted, set `host` or `OLLAMA_HOST` |
| `sentence_transformers` | `sentence-transformers/all-MiniLM-L6-v2` | Fully local Python runtime; auto-installs the optional extra on first use |

## Storage

Knowledge data is stored under `<storage_path>/knowledge_db/<sanitized_base_id>_<hash>/`.
Each successful refresh publishes a generation-specific ChromaDB collection whose name begins with `mindroom_knowledge_<sanitized_base_id>_<hash>`.
The base ID is sanitized to alphanumerics, hyphens, and underscores only, and the hash is a digest of the resolved knowledge path.
For requester-private agent knowledge, the effective private-root path is part of that hash, so each requester-local root gets an isolated index.

The storage path defaults to `mindroom_data/` next to your `config.yaml`, or can be set with `MINDROOM_STORAGE_PATH`.

## Dashboard Management

The web dashboard provides a Knowledge tab for managing knowledge bases without editing YAML:

- Create, edit, and delete knowledge bases
- Configure chunk size and overlap per knowledge base
- Configure Git sync settings
- Upload and remove files for non-Git-backed bases
- Trigger a full reindex on demand
- Monitor indexing status (file count vs. indexed count)
- Assign knowledge bases to agents from the Agents tab

## API Endpoints

See the [Dashboard API reference](dashboard.md#knowledge) for the full list of knowledge base endpoints (list, upload, delete, reindex, status).

## Hot Reload

Knowledge base configuration supports hot reload.
Changing `config.yaml` does not initialize every configured knowledge base.
Agents keep using already-published last-good snapshots until a refresh for their resolved binding succeeds.
Changed settings make existing snapshots stale or unavailable depending on query compatibility, and scheduled refresh rebuilds the affected binding in the background.
