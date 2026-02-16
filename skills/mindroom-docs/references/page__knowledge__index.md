# Knowledge Bases

Knowledge bases give your agents access to your own documents through RAG (Retrieval-Augmented Generation). Drop files into a folder, point a knowledge base at it, and agents automatically search the indexed content before responding.

## How It Works

1. You configure a knowledge base pointing to a folder of documents
1. MindRoom indexes the files into a vector database (ChromaDB) using an embedder
1. Agents assigned to that knowledge base automatically search it before every response
1. Relevant document chunks are included in the agent's context

```
┌─────────────┐     ┌───────────┐     ┌──────────┐     ┌───────┐
│ Files/Folder │ ──▶ │  Embedder │ ──▶ │ ChromaDB │ ──▶ │ Agent │
└─────────────┘     └───────────┘     └──────────┘     └───────┘
       ▲                                                    │
       │  file watcher /                                    │
       │  git sync                              searches before
       │                                        each response
```

## Quick Start

Add a knowledge base and assign it to an agent:

```
knowledge_bases:
  docs:
    path: ./knowledge_docs
    watch: true

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
```

| Field   | Type   | Default            | Description                                             |
| ------- | ------ | ------------------ | ------------------------------------------------------- |
| `path`  | string | `./knowledge_docs` | Folder path (relative to working directory or absolute) |
| `watch` | bool   | `true`             | Watch for filesystem changes and reindex automatically  |
| `git`   | object | `null`             | Optional Git repository sync settings                   |

### Multiple Knowledge Bases

You can define multiple knowledge bases and assign them to different agents:

```
knowledge_bases:
  engineering:
    path: ./knowledge_docs/engineering
    watch: true
  product:
    path: ./knowledge_docs/product
    watch: true
  legal:
    path: ./knowledge_docs/legal
    watch: false

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
    provider: openai        # or "ollama"
    config:
      model: text-embedding-3-small
      host: null             # For self-hosted (Ollama)
```

| Provider | Model Example            | Notes                                    |
| -------- | ------------------------ | ---------------------------------------- |
| `openai` | `text-embedding-3-small` | Requires `OPENAI_API_KEY`                |
| `ollama` | `nomic-embed-text`       | Self-hosted, set `host` or `OLLAMA_HOST` |

## Storage

Knowledge data is stored under `<storage_path>/knowledge_db/<base_id_hash>/`. Each knowledge base gets its own ChromaDB collection named `mindroom_knowledge_<base_id_hash>`.

The storage path defaults to `mindroom_data/` next to your `config.yaml`, or can be set with `MINDROOM_STORAGE_PATH`.

## Dashboard Management

The web dashboard provides a Knowledge tab for managing knowledge bases without editing YAML:

- Create, edit, and delete knowledge bases
- Upload and remove files
- Trigger a full reindex on demand
- Monitor indexing status (file count vs. indexed count)
- Assign knowledge bases to agents from the Agents tab

Git settings are currently configured only in `config.yaml` — the dashboard preserves existing `git` settings when you edit `path` or `watch`.

## API Endpoints

| Method | Endpoint                                      | Description                          |
| ------ | --------------------------------------------- | ------------------------------------ |
| GET    | `/api/knowledge/bases`                        | List all knowledge bases with status |
| GET    | `/api/knowledge/bases/{base_id}/files`        | List files in a knowledge base       |
| POST   | `/api/knowledge/bases/{base_id}/upload`       | Upload files (1 GiB max per file)    |
| DELETE | `/api/knowledge/bases/{base_id}/files/{path}` | Delete a file from disk and index    |
| GET    | `/api/knowledge/bases/{base_id}/status`       | Get indexing status                  |
| POST   | `/api/knowledge/bases/{base_id}/reindex`      | Force full reindex                   |

## Hot Reload

Knowledge base configuration supports hot reload. When you change `config.yaml`:

- New knowledge bases are created and indexed
- Removed knowledge bases are stopped and cleaned up
- Changed settings (path, embedder, git config) trigger a re-initialization
- Unchanged knowledge bases continue running without interruption
- File watchers are preserved across reloads
