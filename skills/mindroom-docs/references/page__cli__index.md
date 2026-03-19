# CLI Reference

MindRoom provides a command-line interface for managing agents.

## Basic Usage

```
mindroom [OPTIONS] COMMAND [ARGS]...
```

## Commands

```
 Usage: root [OPTIONS] COMMAND [ARGS]...

 AI agents that live in Matrix and work everywhere via bridges.

 Quick start:
 mindroom config init   Create a starter config
 mindroom run           Start the system

╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --install-completion            Install completion for the current shell.              │
│ --show-completion               Show completion for the current shell, to copy it or   │
│                                 customize the installation.                            │
│ --help                -h        Show this message and exit.                            │
╰────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ─────────────────────────────────────────────────────────────────────────────╮
│ version             Show the current version of Mindroom.                              │
│ run                 Run the mindroom multi-agent system.                               │
│ doctor              Check your environment for common issues.                          │
│ connect             Pair this local MindRoom install with the hosted provisioning      │
│                     service.                                                           │
│ local-stack-setup   Start local Synapse + MindRoom Cinny using Docker only.            │
│ config              Manage MindRoom configuration files.                               │
│ avatars             Generate and sync managed avatar assets.                           │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## version

Show the current MindRoom version.

```
 Usage: root version [OPTIONS]

 Show the current version of Mindroom.


╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                          │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## run

Start MindRoom with your configuration.

```
 Usage: root run [OPTIONS]

 Run the mindroom multi-agent system.

 This command starts the multi-agent bot system which automatically:
 - Creates all necessary user and agent accounts
 - Creates all rooms defined in config.yaml
 - Manages agent room memberships
 - Starts the bundled dashboard/API server (disable with --no-api)

╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --log-level     -l              TEXT     Set the logging level (DEBUG, INFO, WARNING,  │
│                                          ERROR)                                        │
│                                          [env var: LOG_LEVEL]                          │
│                                          [default: INFO]                               │
│ --storage-path  -s              PATH     Base directory for persistent MindRoom data   │
│                                          (state, sessions, tracking)                   │
│                                          [default: None]                               │
│ --api               --no-api             Start the bundled dashboard/API server        │
│                                          alongside the bot                             │
│                                          [default: api]                                │
│ --api-port                      INTEGER  Port for the bundled dashboard/API server     │
│                                          [default: 8765]                               │
│ --api-host                      TEXT     Host for the bundled dashboard/API server     │
│                                          [default: 0.0.0.0]                            │
│ --help          -h                       Show this message and exit.                   │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## avatars

Generate and sync managed avatar assets.

```
 Usage: root avatars [OPTIONS] COMMAND [ARGS]...

 Generate and sync managed avatar assets.


╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                          │
╰────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ─────────────────────────────────────────────────────────────────────────────╮
│ generate   Generate missing managed avatar files in the workspace.                     │
│ sync       Sync configured room and root-space avatars to Matrix using the initialized │
│            router account.                                                             │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## avatars generate

Generate missing managed avatar files in the workspace. In a source checkout, generated files are written under `./avatars/`. In containerized deployments, generated overrides are written under the persistent MindRoom storage path.

```
 Usage: root avatars generate [OPTIONS]

 Generate missing managed avatar files in the workspace.


╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                          │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## avatars sync

Sync configured room and root-space avatars to Matrix using the initialized router account.

```
 Usage: root avatars sync [OPTIONS]

 Sync configured room and root-space avatars to Matrix using the initialized router
 account.


╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                          │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## doctor

Check your environment for common issues before running `mindroom run`.

Runs a series of checks in one pass:

- **Config file** exists and is valid YAML with correct Pydantic schema
- **Providers** — validates API keys for each configured provider (Anthropic, OpenAI, Ollama, Vertex AI Claude, etc.)
- **Memory config** — checks memory LLM and embedder reachability (Ollama, OpenAI embeddings, sentence-transformers)
- **Matrix homeserver** — verifies the homeserver is reachable via `/_matrix/client/versions`
- **Storage** — confirms the storage directory is writable

```
 Usage: root doctor [OPTIONS]

 Check your environment for common issues.
 Runs connectivity, configuration, and credential checks in a single pass so
 you can fix everything before running `mindroom run`.

╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                          │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## config

Manage MindRoom configuration files. The `config` subgroup contains commands for creating, viewing, editing, and validating your `config.yaml`.

```
 Usage: root config [OPTIONS] COMMAND [ARGS]...

 Manage MindRoom configuration files.

╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --help  -h        Show this message and exit.                                          │
╰────────────────────────────────────────────────────────────────────────────────────────╯
╭─ Commands ─────────────────────────────────────────────────────────────────────────────╮
│ init       Create a starter config.yaml with example agents and models.                │
│ show       Display the current config file with syntax highlighting.                   │
│ edit       Open config.yaml in your default editor.                                    │
│ validate   Validate config.yaml and check for common issues.                           │
│ path       Show the resolved config file path and search locations.                    │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

### config init

Create a starter `config.yaml` with example agents, models, and sensible defaults.

Profiles control the template style:

- `--profile full` (default) — rich example config with interactive provider selection
- `--profile minimal` — bare-minimum config
- `--profile public` — hosted Matrix (`mindroom.chat`) with prefilled homeserver settings
- `--profile public-vertexai-anthropic` — hosted Matrix with Vertex AI Claude defaults

Provider presets (`--provider`) set the default model: `anthropic`, `openai`, `openrouter`, or `vertexai_claude`.

```
# Hosted Matrix quickstart (creates ~/.mindroom/config.yaml)
mindroom config init --profile public

# Minimal config with Anthropic
mindroom config init --minimal --provider anthropic

# Full config with Vertex AI Claude on hosted Matrix
mindroom config init --profile public-vertexai-anthropic

# Force overwrite existing config
mindroom config init --force
```

### config show

Display the current config file with syntax highlighting.

```
# Show config with syntax highlighting
mindroom config show

# Print raw YAML (useful for piping)
mindroom config show --raw

# Show config at a specific path
mindroom config show --path /custom/path/config.yaml
```

### config edit

Open `config.yaml` in your default editor. Editor preference: `$EDITOR` → `$VISUAL` → `nano` → `vim` → `vi`.

```
mindroom config edit
```

### config validate

Validate `config.yaml` and check for common issues. Parses the YAML config using Pydantic and reports errors in a friendly format. Also checks whether required API keys are set as environment variables.

```
mindroom config validate
```

### config path

Show the resolved config file path and all search locations.

```
mindroom config path
```

## connect

Pair this local MindRoom install with a provisioning service.

Default provisioning URL is `https://mindroom.chat` unless you override it with `--provisioning-url` or `MINDROOM_PROVISIONING_URL`.

```
mindroom connect --pair-code ABCD-EFGH
```

On success (default `--persist-env`), this writes to `.env` next to `config.yaml`:

- `MINDROOM_PROVISIONING_URL`
- `MINDROOM_LOCAL_CLIENT_ID`
- `MINDROOM_LOCAL_CLIENT_SECRET`

If your config still contains the owner placeholder token `__MINDROOM_OWNER_USER_ID_FROM_PAIRING__`, `connect` will auto-replace it when pairing returns a valid `owner_user_id`.

Use `--no-persist-env` if you want to export variables only for the current shell session.

```
mindroom connect --pair-code ABCD-EFGH --no-persist-env
```

Use `--provisioning-url` for non-default deployments:

```
mindroom connect \
  --pair-code ABCD-EFGH \
  --provisioning-url https://matrix.example.com
```

## local-stack-setup

Start local Synapse and the MindRoom Cinny client container for development.

By default this command also writes `MATRIX_HOMESERVER`, `MATRIX_SERVER_NAME`, and `MATRIX_SSL_VERIFY=false` into `.env` next to your active `config.yaml` so `mindroom run` works without inline env exports.

```
 Usage: root local-stack-setup [OPTIONS]

 Start local Synapse + MindRoom Cinny using Docker only.


╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --synapse-dir                                 PATH                 Directory           │
│                                                                    containing Synapse  │
│                                                                    docker-compose.yml  │
│                                                                    (from               │
│                                                                    mindroom-stack      │
│                                                                    settings).          │
│                                                                    [default:           │
│                                                                    local/matrix]       │
│ --homeserver-url                              TEXT                 Homeserver URL that │
│                                                                    Cinny and MindRoom  │
│                                                                    should use.         │
│                                                                    [default:           │
│                                                                    http://localhost:8… │
│ --server-name                                 TEXT                 Matrix server name  │
│                                                                    (default: inferred  │
│                                                                    from                │
│                                                                    --homeserver-url    │
│                                                                    hostname).          │
│                                                                    [default: None]     │
│ --cinny-port                                  INTEGER RANGE        Local host port for │
│                                               [1<=x<=65535]        the MindRoom Cinny  │
│                                                                    container.          │
│                                                                    [default: 8080]     │
│ --cinny-image                                 TEXT                 Docker image for    │
│                                                                    MindRoom Cinny.     │
│                                                                    [default:           │
│                                                                    ghcr.io/mindroom-a… │
│ --cinny-container-n…                          TEXT                 Container name for  │
│                                                                    MindRoom Cinny.     │
│                                                                    [default:           │
│                                                                    mindroom-cinny-loc… │
│ --skip-synapse                                                     Skip starting       │
│                                                                    Synapse (assume it  │
│                                                                    is already          │
│                                                                    running).           │
│ --persist-env             --no-persist-env                         Persist Matrix      │
│                                                                    local dev settings  │
│                                                                    to .env next to     │
│                                                                    config.yaml.        │
│                                                                    [default:           │
│                                                                    persist-env]        │
│ --help                -h                                           Show this message   │
│                                                                    and exit.           │
╰────────────────────────────────────────────────────────────────────────────────────────╯
```

## Examples

### Basic run

```
mindroom run
```

### Debug logging

```
mindroom run --log-level DEBUG
```

### Custom storage path

```
mindroom run --storage-path /data/mindroom
```

### Pair local install with hosted provisioning

```
mindroom connect --pair-code ABCD-EFGH
```

### Start local Synapse + Cinny (default local setup)

```
mindroom local-stack-setup --synapse-dir /path/to/mindroom-stack/local/matrix
```

### Start local stack without writing `.env`

```
mindroom local-stack-setup --no-persist-env
```

### Show version

```
mindroom version
```

### Preflight environment check

```
mindroom doctor
```

### Initialize a config

```
mindroom config init --profile public
```

### Validate your config

```
mindroom config validate
```
