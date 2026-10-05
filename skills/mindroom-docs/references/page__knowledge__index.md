# Knowledge Bases

Knowledge bases give agents access to your own documents.
Point a knowledge base at a folder or Git repository and assign it to agents.
In `semantic` mode (the default), MindRoom indexes the files with an embedder into a vector database, and assigned agents get a `search_knowledge_base` tool.
In `files` mode, MindRoom skips embeddings and agents inspect the files directly with their file and shell tools.

## Quick Start

Add a knowledge base and assign it to an agent:

```yaml
knowledge_bases:
  docs:
    description: Product documentation, support notes, and internal operating procedures
    mode: semantic
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

Place files in `./knowledge_docs/`, then trigger a reindex from the dashboard or API, or set `watch: true` so MindRoom picks up file changes automatically.
Knowledge base IDs are the keys under `knowledge_bases`.
An ID must be a non-empty single path component such as `docs` or `company_docs`; `.`, `..`, and names containing `/`, `\`, or line breaks are rejected.

### Files-Only Knowledge Base

Use `mode: files` when agents should search, grep, list, and read the source files directly without paying for embeddings.

```yaml
knowledge_bases:
  source_docs:
    description: Source documents the agent should inspect directly
    mode: files
    path: ${MINDROOM_STORAGE_PATH}/agents/assistant/workspace/source_docs

agents:
  assistant:
    display_name: Assistant
    role: A helpful assistant with direct file access to source docs
    memory_backend: file
    tools: [file, shell]
    knowledge_bases: [source_docs]
```

A files-mode base has no vector index, does not use the embedder, and does not provide `search_knowledge_base`.
The agent needs a workspace, which `memory_backend: file` creates at `${MINDROOM_STORAGE_PATH}/agents/<agent>/workspace` (private agents use their private root).
When the base's path is inside that workspace and the folder exists, MindRoom exposes it as `knowledge/<base_id>` and tells the agent to use file tools on it.
Bases outside the workspace are not linked, and with the default [`file_access: workspace`](https://docs.mindroom.chat/configuration/agents/#configuration-options) the agent's file tools cannot reach them.

## Configuration

```yaml
knowledge_bases:
  my_docs:
    description: Product documentation, support notes, and internal operating procedures
    mode: semantic                    # "semantic" builds a vector search index; "files" skips embeddings
    path: ./knowledge_docs/my_docs   # Folder containing documents
    watch: false                      # Direct external edits require reindex; dashboard/API changes still refresh
    require_content_before_publish: false
    chunk_size: 5000
    chunk_overlap: 0
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `description` | string | `""` | What the base contains; shown to agents in the `search_knowledge_base` tool description and in files-mode instructions so they know when to use it |
| `mode` | `semantic` or `files` | `semantic` | `semantic` builds an embedding-backed search index; `files` skips embeddings and exposes the files to workspace-aware agents |
| `path` | string | `./knowledge_docs` | Folder path, relative to the config file directory or absolute |
| `watch` | bool | `true` | Watch the local folder and refresh in the background when files change. When `false`, direct external edits require an explicit reindex; dashboard/API uploads and deletes still trigger a refresh |
| `require_content_before_publish` | bool | `false` | Keep a new semantic index in the initializing state until at least one source file exists, instead of publishing an empty index |
| `chunk_size` | int | `5000` | Maximum characters per chunk for plain-text and Markdown files (minimum `128`); other formats such as JSON and PDF use their reader's own chunking; semantic mode only |
| `chunk_overlap` | int | `0` | Overlap characters between adjacent plain-text and Markdown chunks; must be smaller than `chunk_size`; semantic mode only |
| `include_patterns` | list | `[]` | Root-anchored glob patterns a file must match to be included |
| `exclude_patterns` | list | `[]` | Root-anchored glob patterns removed after include filtering |
| `include_extensions` | list | `null` | Exact set of extensions to index instead of the default text-like set (semantic mode only) |
| `extra_extensions` | list | `[]` | Extensions to index in addition to the default set (semantic mode only) |
| `exclude_extensions` | list | `[]` | Extensions to exclude, applied last (semantic mode only) |
| `skip_hidden` | bool | `true` | Skip files and folders whose names start with `.`, such as temp files from editors that write in place. Git-backed bases use `git.skip_hidden` instead |
| `git` | object | `null` | Sync the folder from a Git repository; see [Git-Backed Knowledge Bases](#git-backed-knowledge-bases) |

Set `skip_hidden: false` if the folder intentionally contains dot-prefixed files that should be indexed.
Use a smaller `chunk_size` when your embedding server has lower token or batch limits; chunks that are too large make indexing fail with embedder 500 errors.

### Keeping Indexes Fresh

Chat never waits for indexing or Git sync.
Agents search the last successfully published index, and a missing, stale, or failed base schedules a background refresh while the current request continues.
A successful refresh replaces the published index; a failed refresh keeps the previous one and shows the error in the base's status.

A refresh reuses the stored vectors of unchanged files and only embeds files that were added or edited.
Changing `chunk_size`, `chunk_overlap`, the embedder, or any file filter re-embeds the whole base on the next refresh.
An explicit reindex from the dashboard or API rebuilds every vector, which is how you replace an index you no longer trust.
An interrupted or failed semantic build continues where it stopped on the next refresh instead of starting over.
Temporary embedding failures (timeouts, connection errors, HTTP 408, 429, and 5xx) are retried, while permanent failures such as authentication errors stop the refresh until you fix the cause.

Knowledge base configuration hot-reloads.
After a `chunk_size` or `chunk_overlap` change, agents keep searching the previous index until the rebuild succeeds.
After a change to `path`, `mode`, the embedder, the Git source, or any file filter, the base is not searchable until its new index is built.

### Indexing Performance

| Environment variable | Default | Description |
|----------------------|---------|-------------|
| `MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY` | `4` | Files indexed concurrently within one semantic refresh, from `1` through `128`; invalid values are rejected at startup |
| `MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY` | `1` | Knowledge base refreshes that run at the same time; must be at least `1` |
| `MINDROOM_KNOWLEDGE_REFRESH_SUBPROCESS_TIMEOUT_SECONDS` | `3600` | Maximum seconds for one background refresh; must be a finite positive number. Raise it for very large corpora |

Local `sentence_transformers` embedding indexes one file at a time regardless of `MINDROOM_KNOWLEDGE_FILE_INDEX_CONCURRENCY`.

### File Type Filtering

In semantic mode, MindRoom indexes a default set of text-like extensions covering Markdown, plain text, source code, and structured text formats such as `.json`, `.yaml`, and `.csv`.

```yaml
knowledge_bases:
  reports:
    path: ./knowledge_docs/reports
    extra_extensions: [".pdf", ".docx"]  # Default text-like set plus PDF and Word
    exclude_extensions: [".csv"]         # Drop CSV files from the default set
```

- `extra_extensions` adds extensions on top of the default set.
- `include_extensions` replaces the default set entirely, and `extra_extensions` still adds on top of it.
- `exclude_extensions` removes extensions last and wins over both.

The allowed set depends only on this configuration, never on the embedder or installed packages.
Formats such as `.pdf`, `.docx`, or `.pptx` need their reader package (for example `pypdf`, `python-docx`, or `python-pptx`) installed in the MindRoom environment.
When a reader package is missing, the refresh indexes the other files, logs a warning naming the file and the missing package, and reports an indexing error instead of publishing an index that silently lacks those files.
Extension filtering does not apply in `files` mode.
Semantic indexing skips symlinked files and folders inside a base, so copy linked documents into the base instead.
Files larger than 64 MiB are left out of every knowledge base with a logged warning, and the next refresh removes any vectors they already had.

A `.json` file is split into one searchable document per top-level JSON value.
If the file is not valid JSON, its raw text is indexed like plain text and a warning names the file and the line and column of the parse error; fix that error to get per-value splitting back.

### Multiple Knowledge Bases

Define several knowledge bases and assign each agent the ones it needs:

```yaml
knowledge_bases:
  engineering:
    path: ./knowledge_docs/engineering
  product:
    path: ./knowledge_docs/product
  legal:
    path: ./knowledge_docs/legal
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
    knowledge_bases: [product, engineering]

  compliance:
    display_name: Compliance
    role: Legal and compliance reviewer
    knowledge_bases: [legal]
```

When an agent searches several semantic bases, results are interleaved so no single base dominates the top results.
Knowledge base paths must not be nested inside one another.
Several bases may use exactly the same path to expose different filtered views of one folder, as long as their Git settings (`repo_url`, `branch`, `credentials_service`, and `lfs`) match or none of them uses Git; see [File Filtering with Patterns](#file-filtering-with-patterns).

### Private Agent Knowledge

Use `agents.<name>.private.knowledge` when each requester of a [private agent](https://docs.mindroom.chat/configuration/agents/#private-instances) should have their own knowledge index built from their private root.

```yaml
knowledge_bases:
  company_docs:
    description: Shared company policies, project notes, and operating procedures
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
        description: Requester-private notes, preferences, and working memory for this agent
        path: memory
        watch: false
    knowledge_bases: [company_docs]
```

Each requester's private knowledge path becomes `<their private root>/memory`, and their index is never shared with another requester.
The fields are listed under [Private Fields](https://docs.mindroom.chat/configuration/agents/#private-fields).
`private.knowledge.path` is required unless `private.knowledge.enabled` is `false`; it must be relative to the private root (`.` allowed) and cannot be absolute or escape with `..`.
Private knowledge has no background file watcher: with `watch: true` or `git`, it refreshes when the agent uses it, and with `watch: false` and no `git`, external edits are not picked up automatically.
An agent can combine private knowledge with shared top-level `knowledge_bases`, which stay the mechanism for documents shared across agents or users.
Private knowledge is available in normal agent conversations, not through the OpenAI-compatible `/v1` API.
With `private.knowledge.git`, use a dedicated subtree such as `kb_repo`; MindRoom rejects `.` and any path that overlaps private file memory (`memory/`, `MEMORY.md`) or content copied from `template_dir`.

## Git-Backed Knowledge Bases

A knowledge base can sync its folder from a Git repository.
MindRoom refreshes a shared Git-backed base every `poll_interval_seconds`, starting one interval after startup, and agents keep using the last published index while a refresh runs.

```yaml
knowledge_bases:
  pipefunc_docs:
    path: ./knowledge_docs/pipefunc
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
| `repo_url` | string | *required* | Repository URL to clone and fetch; see the accepted forms below |
| `branch` | string | `main` | Branch to track |
| `poll_interval_seconds` | int | `300` | Interval between background Git refreshes (minimum `5`) |
| `credentials_service` | string | `null` | Credential service for private repositories; see [Private Repository Authentication](#private-repository-authentication) |
| `lfs` | bool | `false` | Download Git LFS files after each sync. Requires `git-lfs` on the machine running MindRoom; bundled container images include it |
| `sync_timeout_seconds` | int | `3600` | Abort a single Git command after this many seconds (minimum `5`) |
| `skip_hidden` | bool | `true` | Skip files and folders whose names start with `.` |
| `include_patterns` | list | `[]` | Root-anchored glob patterns to include |
| `exclude_patterns` | list | `[]` | Root-anchored glob patterns to exclude |

#### Accepted `repo_url` forms

| Form | Example |
|------|---------|
| URL with a host | `https://github.com/org/repo.git`, `ssh://git@host/org/repo.git` |
| scp-style SSH | `git@github.com:org/repo.git`, `github.com:org/repo.git` |
| Absolute local path | `/srv/repos/repo.git` |
| `file:` URL | `file:///srv/repos/repo.git` |

Network forms must have a resolvable host.
MindRoom refuses, with an error naming the reason, relative and home-relative paths (`./repo.git`, `../repo.git`, `~/repo.git`, `repo.git`), and URLs with an empty host (`https:///org/repo.git`).

**Do not embed credentials in `repo_url`.**
A password in a well-formed URL is kept out of the checkout's Git configuration, but it stays in your config file and is passed to Git on every command.
A credential MindRoom cannot reliably separate from the URL, such as `oauth2:TOKEN@gitlab.com:org/repo.git` or `x-access-token:TOKEN@github.com/org/repo.git`, makes the sync fail.
Use `credentials_service` instead; those credentials reach Git only for the duration of each command and are never written to disk.

### Sync Behavior

- An explicit reindex or sync from the dashboard or API syncs Git first, then rebuilds the semantic index or, in files mode, publishes the new file list.
- The checkout is owned by MindRoom: when the branch moves, sync discards local edits to tracked files and restores deleted ones, so edit the source repository and sync instead.
- Dashboard and API file uploads and deletes are rejected for Git-backed bases.
- When `lfs: true`, MindRoom downloads all LFS files after each sync, even ones the indexing filters exclude.
- LFS downloads use the endpoint derived from `repo_url`; an LFS URL set in the repository's `.lfsconfig` is ignored.

### File Filtering with Patterns

Patterns are matched from the repository or folder root.
`*` matches one path segment and `**` matches zero or more segments.

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

- If `include_patterns` is empty, all non-hidden files are eligible.
- If `include_patterns` is set, a file must match at least one pattern.
- `exclude_patterns` are applied last and remove matching files.
- On a Git-backed base, a file must pass both the top-level patterns and the `git` patterns.

Pointing several bases at the same path is the preferred way to expose separate views of a large repository without cloning it more than once:

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

### Checkout Layout

The knowledge folder holds only the checked-out files.
The Git data lives at `<storage>/knowledge_git/<folder>_<path digest>`, and bases that share a folder share it.
A `.git` written into the knowledge folder is ignored and never indexed.
The folder must be empty when MindRoom first clones into it.
Changing a base's `path` starts a new clone in the new folder; delete the old directory under `<storage>/knowledge_git/` to reclaim its space.
Deleting only the knowledge folder restores its files from the Git data on the next sync; delete both to clone afresh.

Knowledge Git commands ignore system and global Git configuration, repository hooks, and credential helpers, and they do not receive MindRoom's secrets.
Configure proxies, CA bundles, and SSH through environment variables or `~/.ssh/config`, and credentials through `credentials_service`.

### Private Repository Authentication

For private HTTPS repositories, store credentials and reference them in the config.

**Step 1:** Store credentials through the dashboard **Credentials** tab or the API:

```bash
curl -X POST http://localhost:8765/api/credentials/github_private \
  -H "Content-Type: application/json" \
  -d '{"credentials":{"username":"x-access-token","token":"ghp_your_token_here"}}'
```

**Step 2:** Reference the service name in the knowledge base config:

```yaml
knowledge_bases:
  private_docs:
    path: ./knowledge_docs/private
    git:
      repo_url: https://github.com/org/private-repo
      credentials_service: github_private
```

| Fields | Notes |
|--------|-------|
| `username` + `token` | Standard GitHub or GitLab access token |
| `username` + `password` | Basic HTTP auth; the password wins if a token is also set |
| `token` or `api_key` alone | Authenticates as `x-access-token` |

For unattended GitHub access, a GitHub App installation can replace a personal access token.
Store this object under the service named by `credentials_service`:

```json
{
  "auth_type": "github_app",
  "app_id": 12345,
  "installation_id": 67890,
  "private_key_file": "/var/run/secrets/github-app/private-key.pem"
}
```

`private_key_file` must be an absolute path, ideally a read-only secret mount; do not put the PEM contents in the credential object.
`repo_url` must use the canonical `https://github.com/<owner>/<repository>` form.
MindRoom requests short-lived installation tokens limited to that repository with read-only Contents permission, so the App needs Contents read access, and the tokens are never written to disk or logs.

## Open Knowledge Format Bundles

An [Open Knowledge Format](https://github.com/GoogleCloudPlatform/open-knowledge-format) (OKF) bundle is a folder of markdown files whose YAML frontmatter records who wrote each concept, who verified it, and whether it is current.
Use `mode: files` for OKF bundles, because semantic search cannot filter on frontmatter.
Agents with the bundled `open-knowledge-format` skill report trust and freshness when they answer, and when they edit, they record themselves as the author and add a `verified` entry only when a person confirms the content.

### Reading a Published Bundle

Mirror the bundle's repository inside the agent workspace and name the bundle root in the description:

```yaml
knowledge_bases:
  acme_okf:
    description: Acme Retail finance knowledge, a read-only Open Knowledge Format (OKF) bundle mirrored from Git. Start at knowledge/acme_okf/bundles/acme_retail/index.md.
    mode: files
    path: ${MINDROOM_STORAGE_PATH}/agents/analyst/workspace/okf/acme
    git:
      repo_url: https://github.com/GoogleCloudPlatform/open-knowledge-format

agents:
  analyst:
    display_name: Analyst
    role: Answers questions from the team's knowledge bundles
    memory_backend: file
    tools: [file]
    knowledge_bases: [acme_okf]
    skills: [open-knowledge-format]
```

The mirror is a [Git-backed base](#git-backed-knowledge-bases), so trigger a sync from the dashboard to clone it before the first poll, and keep it read-only because sync discards local edits when the branch moves.

### Maintaining a Bundle with Agents

For a bundle that agents edit, create a local folder with a root `index.md` inside the agent workspace, then add it to the agent's `knowledge_bases`:

```yaml
knowledge_bases:
  ops_wiki:
    description: The team's ops wiki, an Open Knowledge Format (OKF) bundle that the team and its agents maintain.
    mode: files
    path: ${MINDROOM_STORAGE_PATH}/agents/analyst/workspace/okf/ops_wiki
```

## Embedder Configuration

Semantic knowledge bases use the embedder configured under `memory.embedder`; files-mode bases use none.
Semantic knowledge and file-memory indexes support the `openai`, `ollama`, and `sentence_transformers` providers, while the Mem0 memory backend also supports `huggingface`.

| Provider | Model example | Notes |
|----------|---------------|-------|
| `openai` | `text-embedding-3-small` | Also works with keyless OpenAI-compatible local endpoints |
| `ollama` | `nomic-embed-text` | Self-hosted; set `host` or `OLLAMA_HOST` |
| `sentence_transformers` | `sentence-transformers/all-MiniLM-L6-v2` | Runs locally in MindRoom; the optional extra installs on first use |

See [Memory](https://docs.mindroom.chat/memory/#embedder) for the embedder fields, API key resolution, and how embedder failures are reported.

## Storage

Semantic indexes are stored under `<storage_path>/knowledge_db/<base_id>_<hash>/`, where the hash comes from the resolved knowledge path.
For private agent knowledge, the requester's private root is part of that path, so each requester gets a separate index.
Files-mode bases need no vector database.
The storage path defaults to `mindroom_data/` next to `config.yaml` and can be changed with `MINDROOM_STORAGE_PATH`.

## Search Limits

A search times out after 30 seconds, and at most four searches run at once; a search that finds no free slot can fail with `Knowledge reader is busy; try again shortly`.

## Dashboard and API

Manage knowledge bases, files, reindexing, and Git sync from the dashboard **Knowledge** tab or the API; see [Dashboard](https://docs.mindroom.chat/dashboard/#knowledge) for the tab and the [knowledge API endpoints](https://docs.mindroom.chat/dashboard/#knowledge-api).
