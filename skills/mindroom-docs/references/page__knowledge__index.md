# Knowledge Bases

Knowledge bases give your agents access to your own documents through RAG (Retrieval-Augmented Generation). Drop files into a folder, point a knowledge base at it, and agents can search the indexed content when answering questions.

## How It Works

1. You configure a knowledge base pointing to a folder of documents
1. MindRoom indexes the files into a vector database (ChromaDB) using an embedder
1. Agents assigned to that knowledge base get a search tool that queries the indexed documents
1. When the agent uses the tool, relevant document chunks are included in its context

```
Indexing (startup + file changes):

  ┌──────────────┐      ┌──────────┐      ┌──────────┐
  │ Files/Folder │ ───▶ │ Embedder │ ───▶ │ ChromaDB │
  └──────────────┘      └──────────┘      └──────────┘
         ▲
         │ file watcher
         │ git sync

Querying (agentic RAG):

  ┌───────┐  search   ┌──────────┐
  │ Agent │ ────────▶ │ ChromaDB │
  │       │ ◀──────── │          │
  └───────┘  chunks   └──────────┘
```

## Quick Start

Add a knowledge base and assign it to an agent:

```
knowledge_bases:
  docs:
    path: ./knowledge_docs
    watch: true
    chunk_size: 5000
    chunk_overlap: 0

agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant with access to our docs
    knowledge_bases: [docs]
```

Place files in `./knowledge_docs/` and they'll be indexed automatically on startup. When `watch: true`, new or modified files are re-indexed in real time.

## Configuration

### Basic Knowledge Base

```
knowledge_bases:
  my_docs:
    path: ./knowledge_docs/my_docs   # Folder containing documents
    watch: true                       # Auto-reindex on file changes
    chunk_size: 5000                  # Max characters per chunk
    chunk_overlap: 0                  # Overlap between adjacent chunks
```

| Field           | Type   | Default            | Description                                                         |
| --------------- | ------ | ------------------ | ------------------------------------------------------------------- |
| `path`          | string | `./knowledge_docs` | Folder path (relative to the config file directory or absolute)     |
| `watch`         | bool   | `true`             | Watch for filesystem changes and reindex automatically              |
| `chunk_size`    | int    | `5000`             | Maximum characters per chunk for text-like files (minimum: `128`)   |
| `chunk_overlap` | int    | `0`                | Overlap characters between adjacent chunks (must be `< chunk_size`) |
| `git`           | object | `null`             | Optional Git repository sync settings                               |

Use smaller `chunk_size` values when your embedding server has lower token or batch limits. If chunking is too large, indexing retries will fail with embedder 500 errors.

### Private Agent Knowledge

Use `agents.<name>.private.knowledge` when one shared agent definition should index requester-local knowledge from that requester's private root.

```
knowledge_bases:
  company_docs:
    path: ./company_docs
    watch: true

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
        watch: true
    knowledge_bases: [company_docs]
```

With this configuration, each requester's private knowledge path becomes `<their private root>/memory`. The template source is explicit, so you can see and edit the files being copied into each requester's private root. `private.template_dir` only copies files. Requester-local knowledge is enabled only when you explicitly configure `private.knowledge.path`. `private.knowledge.path` must be relative to the private root and cannot be absolute or escape with `..`. `private.knowledge.path` can point to any folder inside the private root, including `.` for the private root itself. MindRoom keeps a separate index per effective private root, so one requester's indexed data is not shared with another requester's runtime. For isolating scopes such as `user` and `user_agent`, MindRoom refreshes the private index on access instead of keeping a background watcher alive for every requester root. Top-level `knowledge_bases` remain the shared/global mechanism, so the same agent can combine private local knowledge with shared company knowledge. This requester-local private knowledge flow applies to the normal agent runtime path, not the OpenAI-compatible `/v1` API.

| Field                             | Type   | Default | Description                                                                                                                                                               |
| --------------------------------- | ------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `private.knowledge.enabled`       | bool   | `true`  | Whether requester-local knowledge indexing is active for this agent                                                                                                       |
| `private.knowledge.path`          | string | `null`  | Private-root-relative folder to index. Required when `private.knowledge.enabled` is `true`                                                                                |
| `private.knowledge.watch`         | bool   | `true`  | Whether private knowledge should refresh when files change. For isolating scopes, MindRoom refreshes on access instead of keeping a background watcher per requester root |
| `private.knowledge.chunk_size`    | int    | `5000`  | Maximum characters per indexed chunk                                                                                                                                      |
| `private.knowledge.chunk_overlap` | int    | `0`     | Overlap characters between adjacent chunks. Must be smaller than `chunk_size`                                                                                             |
| `private.knowledge.git`           | object | `null`  | Optional Git sync configuration for requester-local knowledge                                                                                                             |

Use `private.knowledge` when the data itself should be private to that requester's private instance. Use top-level `knowledge_bases` when the same documents should stay shared across agents or users.

### Multiple Knowledge Bases

You can define multiple knowledge bases and assign them to different agents:

```
knowledge_bases:
  engineering:
    path: ./knowledge_docs/engineering
    watch: true
    chunk_size: 5000
    chunk_overlap: 0
  product:
    path: ./knowledge_docs/product
    watch: true
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

Knowledge bases can sync from a Git repository. MindRoom clones the repo on first run and periodically pulls updates.

```
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
      skip_hidden: true
      include_patterns:
        - "docs/**"
```

### Git Configuration Fields

| Field                   | Type   | Default    | Description                                          |
| ----------------------- | ------ | ---------- | ---------------------------------------------------- |
| `repo_url`              | string | *required* | HTTPS repository URL to clone/fetch                  |
| `branch`                | string | `main`     | Branch to track                                      |
| `poll_interval_seconds` | int    | `300`      | How often to check for updates (minimum: 5)          |
| `credentials_service`   | string | `null`     | Service name in CredentialsManager for private repos |
| `skip_hidden`           | bool   | `true`     | Skip files/folders starting with `.`                 |
| `include_patterns`      | list   | `[]`       | Root-anchored glob patterns to include               |
| `exclude_patterns`      | list   | `[]`       | Root-anchored glob patterns to exclude               |

### Sync Behavior

- On startup, the repo is cloned (or fetched if it already exists)
- Every `poll_interval_seconds`, MindRoom runs `git fetch` + `git reset --hard origin/<branch>`
- Local uncommitted changes in the checkout folder are discarded on each sync
- Only changed files are re-indexed (not the entire repo each time)
- Deleted files are automatically removed from the index
- Git polling runs regardless of the `watch` setting — `watch` controls only local filesystem events

### File Filtering with Patterns

Patterns are matched from the repository root. `*` matches one path segment, `**` matches zero or more segments.

```
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

### Private Repository Authentication

For private HTTPS repositories, store credentials and reference them in the config.

**Step 1:** Store credentials via the API or Dashboard (Credentials tab):

```
curl -X POST http://localhost:8765/api/credentials/github_private \
  -H "Content-Type: application/json" \
  -d '{"credentials":{"username":"x-access-token","token":"ghp_your_token_here"}}'
```

**Step 2:** Reference the service name in your knowledge base config:

```
knowledge_bases:
  private_docs:
    path: ./knowledge_docs/private
    git:
      repo_url: https://github.com/org/private-repo
      credentials_service: github_private
```

Accepted credential fields:

| Fields                  | Notes                                           |
| ----------------------- | ----------------------------------------------- |
| `username` + `token`    | Standard GitHub/GitLab access token auth        |
| `username` + `password` | Basic HTTP auth                                 |
| `api_key`               | Uses `x-access-token` as username automatically |

## Embedder Configuration

Knowledge bases use the same embedder configured in the `memory` section:

```
memory:
  embedder:
    provider: openai        # or "ollama" or "sentence_transformers"
    config:
      model: text-embedding-3-small
      host: null             # For self-hosted (Ollama)
```

| Provider                | Model Example                            | Notes                                                                     |
| ----------------------- | ---------------------------------------- | ------------------------------------------------------------------------- |
| `openai`                | `text-embedding-3-small`                 | Requires `OPENAI_API_KEY`                                                 |
| `ollama`                | `nomic-embed-text`                       | Self-hosted, set `host` or `OLLAMA_HOST`                                  |
| `sentence_transformers` | `sentence-transformers/all-MiniLM-L6-v2` | Fully local Python runtime; auto-installs the optional extra on first use |

## Storage

Knowledge data is stored under `<storage_path>/knowledge_db/<base_id>_<hash>/`. Each knowledge base gets its own ChromaDB collection named `mindroom_knowledge_<base_id>_<hash>`. For requester-private agent knowledge, the effective private-root path is part of that storage key, so each requester-local root gets an isolated index.

The storage path defaults to `mindroom_data/` next to your `config.yaml`, or can be set with `MINDROOM_STORAGE_PATH`.

## Dashboard Management

The web dashboard provides a Knowledge tab for managing knowledge bases without editing YAML:

- Create, edit, and delete knowledge bases
- Configure chunk size and overlap per knowledge base
- Configure Git sync settings
- Upload and remove files
- Trigger a full reindex on demand
- Monitor indexing status (file count vs. indexed count)
- Assign knowledge bases to agents from the Agents tab

## API Endpoints

See the [Dashboard API reference](https://docs.mindroom.chat/dashboard/#knowledge) for the full list of knowledge base endpoints (list, upload, delete, reindex, status).

## Hot Reload

Knowledge base configuration supports hot reload. When you change `config.yaml`:

- New knowledge bases are created and indexed
- Removed knowledge bases are stopped and cleaned up
- Changed settings (path, chunking, embedder, git config) trigger a re-initialization
- Unchanged knowledge bases continue running without interruption
- Background watchers are preserved across reloads when that knowledge base actually runs a watcher
