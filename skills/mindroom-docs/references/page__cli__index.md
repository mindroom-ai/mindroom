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
│ local-stack-setup   Start local Synapse + MindRoom Cinny using Docker only.            │
│ config              Manage MindRoom configuration files.                               │
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
 - Starts the dashboard API server (disable with --no-api)

╭─ Options ──────────────────────────────────────────────────────────────────────────────╮
│ --log-level     -l              TEXT     Set the logging level (DEBUG, INFO, WARNING,  │
│                                          ERROR)                                        │
│                                          [env var: LOG_LEVEL]                          │
│                                          [default: INFO]                               │
│ --storage-path  -s              PATH     Base directory for persistent MindRoom data   │
│                                          (state, sessions, tracking)                   │
│                                          [default: mindroom_data]                      │
│ --api               --no-api             Start the dashboard API server alongside the  │
│                                          bot                                           │
│                                          [default: api]                                │
│ --api-port                      INTEGER  Port for the dashboard API server             │
│                                          [default: 8765]                               │
│ --api-host                      TEXT     Host for the dashboard API server             │
│                                          [default: 0.0.0.0]                            │
│ --help          -h                       Show this message and exit.                   │
╰────────────────────────────────────────────────────────────────────────────────────────╯
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
